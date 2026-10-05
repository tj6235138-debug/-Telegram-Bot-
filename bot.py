import os
import threading
import hashlib
import hmac
import json
from datetime import datetime
from urllib.parse import parse_qsl

import requests
import psycopg

from flask import Flask, jsonify, request
from flask_cors import CORS

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


# =========================================================
# 环境变量
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

ODDIWIRE_API_KEY = os.environ.get(
    "FIELDFUNDED_API_KEY",
    ""
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    ""
)

MINI_APP_URL = os.environ.get(
    "MINI_APP_URL",
    "https://tj6235138-debug.github.io/jiangtian-esports/"
)

SUPPORT_URL = os.environ.get("SUPPORT_URL", "")
RECHARGE_URL = os.environ.get("RECHARGE_URL", "")

ODDIWIRE_BASE_URL = "https://oddiwire.com"


# =========================================================
# Flask
# =========================================================

web = Flask(__name__)

CORS(
    web,
    resources={
        r"/api/*": {
            "origins": [
                "https://tj6235138-debug.github.io",
                "https://web.telegram.org",
            ]
        }
    }
)


# =========================================================
# 数据库
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 未配置")

    return psycopg.connect(
        DATABASE_URL,
        autocommit=False
    )


def init_database():
    """
    Railway启动时自动创建需要的数据表。
    """

    with get_db() as conn:
        with conn.cursor() as cur:

            # 用户表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    photo_url TEXT,

                    balance NUMERIC(18,2)
                        NOT NULL DEFAULT 10000.00,

                    created_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW(),

                    updated_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW()
                )
            """)

            # 投注记录
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id BIGSERIAL PRIMARY KEY,

                    telegram_id BIGINT NOT NULL
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE,

                    event_id TEXT,
                    sport TEXT,
                    league TEXT,

                    home_team TEXT,
                    away_team TEXT,

                    market TEXT,
                    selection TEXT,

                    odds NUMERIC(12,4) NOT NULL,
                    stake NUMERIC(18,2) NOT NULL,
                    potential_return NUMERIC(18,2) NOT NULL,

                    status TEXT
                        NOT NULL DEFAULT 'pending',

                    result TEXT,

                    created_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW(),

                    settled_at TIMESTAMPTZ
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_bets_telegram_id
                ON bets(telegram_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_bets_event_id
                ON bets(event_id)
            """)

        conn.commit()

    print("数据库初始化完成")


# =========================================================
# Telegram Mini App 身份验证
# =========================================================

def verify_telegram_init_data(init_data):
    """
    验证 Telegram WebApp initData。

    不能相信浏览器直接传来的 telegram_id，
    必须通过 Telegram 签名验证。
    """

    if not init_data:
        return None

    try:
        values = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True
            )
        )

        received_hash = values.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={values[key]}"
            for key in sorted(values.keys())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        user_json = values.get("user")

        if not user_json:
            return None

        user = json.loads(user_json)

        if not user.get("id"):
            return None

        return user

    except Exception as exc:
        print("Telegram身份验证失败:", exc)
        return None


def get_request_user():
    """
    从请求中读取 Telegram initData。
    """

    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    if not init_data and request.is_json:
        body = request.get_json(silent=True) or {}
        init_data = body.get("init_data", "")

    return verify_telegram_init_data(init_data)


# =========================================================
# 用户数据库
# =========================================================

def upsert_user(user):
    telegram_id = int(user["id"])

    username = user.get("username")
    first_name = user.get("first_name")
    last_name = user.get("last_name")
    photo_url = user.get("photo_url")

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO users (
                    telegram_id,
                    username,
                    first_name,
                    last_name,
                    photo_url
                )
                VALUES (%s, %s, %s, %s, %s)

                ON CONFLICT (telegram_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    photo_url = EXCLUDED.photo_url,
                    updated_at = NOW()

                RETURNING
                    telegram_id,
                    username,
                    first_name,
                    last_name,
                    photo_url,
                    balance,
                    created_at
            """, (
                telegram_id,
                username,
                first_name,
                last_name,
                photo_url,
            ))

            row = cur.fetchone()

        conn.commit()

    return {
        "telegram_id": row[0],
        "username": row[1],
        "first_name": row[2],
        "last_name": row[3],
        "photo_url": row[4],
        "balance": float(row[5]),
        "created_at": row[6].isoformat()
        if row[6] else None,
    }


def ensure_bot_user(tg_user):
    """
    用户在Telegram机器人发送/start时也创建账户。
    """

    user = {
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
        "photo_url": None,
    }

    try:
        return upsert_user(user)
    except Exception as exc:
        print("创建Telegram用户失败:", exc)
        return None


# =========================================================
# Oddiwire
# =========================================================

def oddiwire_get(path, params=None):

    if not ODDIWIRE_API_KEY:
        return None, "Oddiwire API Key 未配置", 500

    try:
        response = requests.get(
            f"{ODDIWIRE_BASE_URL}{path}",
            headers={
                "x-api-key": ODDIWIRE_API_KEY,
                "Accept": "application/json",
            },
            params=params or {},
            timeout=20,
        )

        if response.status_code == 401:
            return None, "Oddiwire API Key 无效", 502

        if response.status_code == 429:
            return None, "Oddiwire请求过于频繁", 429

        response.raise_for_status()

        return response.json(), None, 200

    except requests.RequestException as exc:
        return (
            None,
            f"Oddiwire连接失败: {exc}",
            502
        )

    except ValueError:
        return (
            None,
            "Oddiwire返回格式异常",
            502
        )


# =========================================================
# 健康检查
# =========================================================

@web.get("/")
def health():

    return jsonify({
        "ok": True,
        "name": "姜天电竞 API",
        "status": "online",
        "database": bool(DATABASE_URL),
    })


@web.get("/api/status")
def api_status():

    database_ok = False

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                database_ok = cur.fetchone()[0] == 1
    except Exception as exc:
        print("数据库状态检查失败:", exc)

    return jsonify({
        "ok": True,
        "service": "Jiangtian Esports",
        "database": database_ok,
        "oddiwire": bool(ODDIWIRE_API_KEY),
    })


# =========================================================
# 当前用户
# =========================================================

@web.route(
    "/api/user",
    methods=["GET", "POST"]
)
def api_user():

    user = get_request_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Telegram身份验证失败，请从Telegram机器人重新进入小程序。"
        }), 401

    try:
        account = upsert_user(user)

        return jsonify({
            "ok": True,
            "user": account,
        })

    except Exception as exc:
        print("读取用户失败:", exc)

        return jsonify({
            "ok": False,
            "error": "读取用户账户失败"
        }), 500


# =========================================================
# 我的投注
# =========================================================

@web.get("/api/bets")
def api_bets():

    user = get_request_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Telegram身份验证失败"
        }), 401

    telegram_id = int(user["id"])

    try:
        # 确保用户存在
        upsert_user(user)

        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT
                        id,
                        event_id,
                        sport,
                        league,
                        home_team,
                        away_team,
                        market,
                        selection,
                        odds,
                        stake,
                        potential_return,
                        status,
                        result,
                        created_at,
                        settled_at
                    FROM bets
                    WHERE telegram_id = %s
                    ORDER BY id DESC
                    LIMIT 100
                """, (
                    telegram_id,
                ))

                rows = cur.fetchall()

        bets = []

        for row in rows:
            bets.append({
                "id": row[0],
                "event_id": row[1],
                "sport": row[2],
                "league": row[3],
                "home_team": row[4],
                "away_team": row[5],
                "market": row[6],
                "selection": row[7],
                "odds": float(row[8]),
                "stake": float(row[9]),
                "potential_return": float(row[10]),
                "status": row[11],
                "result": row[12],
                "created_at":
                    row[13].isoformat()
                    if row[13] else None,
                "settled_at":
                    row[14].isoformat()
                    if row[14] else None,
            })

        return jsonify({
            "ok": True,
            "count": len(bets),
            "bets": bets,
        })

    except Exception as exc:
        print("读取投注失败:", exc)

        return jsonify({
            "ok": False,
            "error": "读取投注记录失败"
        }), 500


# =========================================================
# 提交模拟投注
# =========================================================

@web.post("/api/bet")
def api_place_bet():

    user = get_request_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Telegram身份验证失败"
        }), 401

    body = request.get_json(
        silent=True
    ) or {}

    telegram_id = int(user["id"])

    try:
        stake = round(
            float(body.get("stake", 0)),
            2
        )

        odds = round(
            float(body.get("odds", 0)),
            4
        )

    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "投注金额或赔率格式错误"
        }), 400

    if stake <= 0:
        return jsonify({
            "ok": False,
            "error": "投注金额必须大于0"
        }), 400

    if stake > 1000000:
        return jsonify({
            "ok": False,
            "error": "单笔模拟投注金额过大"
        }), 400

    if odds <= 1:
        return jsonify({
            "ok": False,
            "error": "赔率无效"
        }), 400

    event_id = str(
        body.get("event_id", "")
    )[:200]

    sport = str(
        body.get("sport", "")
    )[:100]

    league = str(
        body.get("league", "")
    )[:300]

    home_team = str(
        body.get("home_team", "")
    )[:300]

    away_team = str(
        body.get("away_team", "")
    )[:300]

    market = str(
        body.get("market", "")
    )[:300]

    selection = str(
        body.get("selection", "")
    )[:300]

    if not event_id:
        return jsonify({
            "ok": False,
            "error": "缺少赛事ID"
        }), 400

    if not market or not selection:
        return jsonify({
            "ok": False,
            "error": "请选择投注盘口"
        }), 400

    potential_return = round(
        stake * odds,
        2
    )

    try:
        # 先确保用户存在
        upsert_user(user)

        with get_db() as conn:
            with conn.cursor() as cur:

                # 锁定当前用户余额
                cur.execute("""
                    SELECT balance
                    FROM users
                    WHERE telegram_id = %s
                    FOR UPDATE
                """, (
                    telegram_id,
                ))

                row = cur.fetchone()

                if not row:
                    conn.rollback()

                    return jsonify({
                        "ok": False,
                        "error": "用户不存在"
                    }), 404

                current_balance = float(
                    row[0]
                )

                if current_balance < stake:
                    conn.rollback()

                    return jsonify({
                        "ok": False,
                        "error": "模拟余额不足",
                        "balance": current_balance,
                    }), 400

                # 扣除余额
                cur.execute("""
                    UPDATE users
                    SET
                        balance = balance - %s,
                        updated_at = NOW()
                    WHERE telegram_id = %s
                    RETURNING balance
                """, (
                    stake,
                    telegram_id,
                ))

                new_balance = float(
                    cur.fetchone()[0]
                )

                # 创建投注记录
                cur.execute("""
                    INSERT INTO bets (
                        telegram_id,
                        event_id,
                        sport,
                        league,
                        home_team,
                        away_team,
                        market,
                        selection,
                        odds,
                        stake,
                        potential_return,
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        'pending'
                    )
                    RETURNING id, created_at
                """, (
                    telegram_id,
                    event_id,
                    sport,
                    league,
                    home_team,
                    away_team,
                    market,
                    selection,
                    odds,
                    stake,
                    potential_return,
                ))

                bet_row = cur.fetchone()

            conn.commit()

        return jsonify({
            "ok": True,
            "message": "模拟投注成功",
            "bet": {
                "id": bet_row[0],
                "event_id": event_id,
                "market": market,
                "selection": selection,
                "odds": odds,
                "stake": stake,
                "potential_return":
                    potential_return,
                "status": "pending",
                "created_at":
                    bet_row[1].isoformat(),
            },
            "balance": new_balance,
        })

    except Exception as exc:
        print("提交投注失败:", exc)

        return jsonify({
            "ok": False,
            "error": "提交模拟投注失败"
        }), 500


# =========================================================
# Oddiwire赛事接口
# =========================================================

@web.get("/api/fixtures")
def api_fixtures():

    event_type = request.args.get(
        "event_type",
        "live"
    ).lower()

    if event_type not in (
        "live",
        "prematch"
    ):
        return jsonify({
            "ok": False,
            "error":
                "event_type必须是live或prematch"
        }), 400

    try:
        limit = int(
            request.args.get(
                "limit",
                "50"
            )
        )

    except (TypeError, ValueError):
        limit = 50

    limit = max(
        1,
        min(limit, 100)
    )

    params = {
        "event_type": event_type,
        "limit": limit,
    }

    tier = request.args.get("tier")

    if tier:
        params["tier"] = tier

    sport = request.args.get("sport")

    if sport:
        params["sport"] = sport

    status = request.args.get("status")

    if status:
        params["status"] = status

    data, error, status_code = (
        oddiwire_get(
            "/v1/fixtures",
            params
        )
    )

    if error:
        return jsonify({
            "ok": False,
            "error": error
        }), status_code

    if isinstance(data, dict):

        events = data.get(
            "events",
            []
        )

    elif isinstance(data, list):

        events = data

    else:

        events = []

    return jsonify({
        "ok": True,
        "event_type": event_type,
        "count": len(events),
        "events": events,
    })


# =========================================================
# Telegram机器人按钮
# =========================================================

def home_keyboard():

    rows = [
        [
            InlineKeyboardButton(
                "🎮 进入姜天电竞",
                web_app=WebAppInfo(
                    url=MINI_APP_URL
                ),
            )
        ]
    ]

    if RECHARGE_URL:
        recharge_button = (
            InlineKeyboardButton(
                "💰 充值",
                url=RECHARGE_URL
            )
        )
    else:
        recharge_button = (
            InlineKeyboardButton(
                "💰 充值",
                callback_data="recharge"
            )
        )

    if SUPPORT_URL:
        support_button = (
            InlineKeyboardButton(
                "👤 联系客服",
                url=SUPPORT_URL
            )
        )
    else:
        support_button = (
            InlineKeyboardButton(
                "👤 联系客服",
                callback_data="support"
            )
        )

    rows.append([
        recharge_button,
        support_button
    ])

    return InlineKeyboardMarkup(rows)


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    tg_user = update.effective_user

    account = None

    if tg_user:
        account = ensure_bot_user(
            tg_user
        )

    name = (
        tg_user.first_name
        if tg_user
        else "玩家"
    )

    if account:
        balance_text = (
            f"{account['balance']:,.2f}"
        )
    else:
        balance_text = "--"

    text = (
        f"🎮 <b>姜天电竞</b>\n\n"
        f"欢迎你，{name}\n\n"
        f"💰 模拟余额："
        f"<b>{balance_text} USDT</b>\n\n"
        f"点击下方按钮进入赛事中心。"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=home_keyboard(),
    )


# =========================================================
# Telegram按钮回调
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if query.data == "recharge":

        await query.message.reply_text(
            "💰 充值入口暂未配置。\n\n"
            "当前系统使用模拟余额。"
        )

    elif query.data == "support":

        await query.message.reply_text(
            "👤 客服入口暂未配置。"
        )


# =========================================================
# Flask启动
# =========================================================

def run_web():

    port = int(
        os.environ.get(
            "PORT",
            "8080"
        )
    )

    web.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


# =========================================================
# 主程序
# =========================================================

def main():

    print("正在初始化姜天电竞...")

    # 初始化数据库
    init_database()

    # Flask放后台线程
    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    # Telegram Bot保持主线程运行
    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    print("姜天电竞 Telegram Bot 已启动")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
