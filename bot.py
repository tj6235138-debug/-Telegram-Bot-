import os
import threading
import requests

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

# 继续使用你 Railway 现在已经设置好的变量名
ODDIWIRE_API_KEY = os.environ.get("FIELDFUNDED_API_KEY", "")

# 你现在的 GitHub Pages 小程序
MINI_APP_URL = os.environ.get(
    "MINI_APP_URL",
    "https://tj6235138-debug.github.io/jiangtian-esports/",
)

# 后面可以在 Railway Variables 里配置
SUPPORT_URL = os.environ.get("SUPPORT_URL", "")
RECHARGE_URL = os.environ.get("RECHARGE_URL", "")

ODDIWIRE_BASE_URL = "https://oddiwire.com"

# Railway 会自动提供 PORT
PORT = int(os.environ.get("PORT", "8080"))


# =========================================================
# Flask Web API
# =========================================================

web_app = Flask(__name__)

# 允许 GitHub Pages 小程序访问 API
CORS(web_app)


# =========================================================
# Oddiwire 请求
# =========================================================

def oddiwire_request(path, params=None):
    if not ODDIWIRE_API_KEY:
        return None, "Oddiwire API Key 未配置", 500

    url = f"{ODDIWIRE_BASE_URL}{path}"

    headers = {
        "x-api-key": ODDIWIRE_API_KEY,
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20,
        )

        print("=" * 60)
        print("ODDIWIRE REQUEST:", path)
        print("ODDIWIRE STATUS:", response.status_code)
        print("=" * 60)

        if response.status_code == 401:
            return None, "Oddiwire API Key 无效", 401

        if response.status_code == 403:
            return None, "Oddiwire API 没有访问权限", 403

        if response.status_code == 429:
            return None, "Oddiwire API 请求过于频繁", 429

        if response.status_code >= 500:
            return (
                None,
                f"Oddiwire 服务异常 HTTP {response.status_code}",
                502,
            )

        response.raise_for_status()

        return response.json(), None, 200

    except requests.exceptions.Timeout:
        return None, "Oddiwire API 请求超时", 504

    except requests.exceptions.ConnectionError:
        return None, "无法连接 Oddiwire API", 502

    except Exception as e:
        print("ODDIWIRE ERROR:", repr(e))
        return None, str(e), 500


# =========================================================
# API 首页
# =========================================================

@web_app.route("/", methods=["GET"])
def api_home():
    return jsonify(
        {
            "ok": True,
            "name": "姜天电竞 API",
            "status": "online",
        }
    )


# =========================================================
# 健康检查
# =========================================================

@web_app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify(
        {
            "ok": True,
            "service": "jiangtian-esports",
            "oddiwire_configured": bool(ODDIWIRE_API_KEY),
        }
    )


# =========================================================
# 获取比赛
#
# 使用方法：
#
# /api/fixtures?event_type=live
# /api/fixtures?event_type=prematch
#
# 可选：
# &sport=CS2
# &tier=1
# &limit=100
# =========================================================

@web_app.route("/api/fixtures", methods=["GET"])
def api_fixtures():

    event_type = request.args.get(
        "event_type",
        "live",
    ).lower()

    if event_type not in ("live", "prematch"):
        return jsonify(
            {
                "ok": False,
                "error": "event_type 必须是 live 或 prematch",
            }
        ), 400

    sport = request.args.get(
        "sport",
        "",
    ).strip()

    try:
        tier = int(
            request.args.get(
                "tier",
                "1",
            )
        )
    except ValueError:
        tier = 1

    try:
        limit = int(
            request.args.get(
                "limit",
                "100",
            )
        )
    except ValueError:
        limit = 100

    # 防止前端一次请求过多
    limit = max(
        1,
        min(limit, 1000),
    )

    params = {
        "event_type": event_type,
        "tier": tier,
        "limit": limit,
    }

    if sport:
        params["sport"] = sport

    data, error, status = oddiwire_request(
        "/v1/fixtures",
        params=params,
    )

    if error:
        return jsonify(
            {
                "ok": False,
                "error": error,
            }
        ), status

    # Oddiwire 当前实际结构：
    #
    # {
    #   "count": ...,
    #   "events": [...]
    # }

    events = []

    if isinstance(data, list):

        events = data

    elif isinstance(data, dict):

        possible_events = data.get(
            "events",
            [],
        )

        if isinstance(possible_events, list):
            events = possible_events

        # 兼容未来可能出现的其它结构
        if not events:

            for key in (
                "fixtures",
                "results",
                "items",
            ):

                value = data.get(key)

                if isinstance(value, list):
                    events = value
                    break

    return jsonify(
        {
            "ok": True,
            "event_type": event_type,
            "sport": sport or "ALL",
            "count": len(events),
            "events": events,
        }
    )


# =========================================================
# 单场比赛详情
# =========================================================

@web_app.route(
    "/api/fixtures/<fixture_id>",
    methods=["GET"],
)
def api_fixture_detail(fixture_id):

    data, error, status = oddiwire_request(
        f"/v1/fixtures/{fixture_id}"
    )

    if error:
        return jsonify(
            {
                "ok": False,
                "error": error,
            }
        ), status

    return jsonify(
        {
            "ok": True,
            "event": data,
        }
    )


# =========================================================
# API Key 状态
# =========================================================

@web_app.route("/api/status", methods=["GET"])
def api_status():

    data, error, status = oddiwire_request(
        "/v1/me"
    )

    if error:
        return jsonify(
            {
                "ok": False,
                "error": error,
            }
        ), status

    # 不把敏感 Key 返回给前端
    return jsonify(
        {
            "ok": True,
            "message": "Oddiwire API 正常",
        }
    )


# =========================================================
# Telegram 主菜单
# =========================================================

def telegram_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🎮 进入姜天电竞",
                web_app=WebAppInfo(
                    url=MINI_APP_URL
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "💰 充值",
                callback_data="recharge",
            ),
            InlineKeyboardButton(
                "👤 联系客服",
                callback_data="support",
            ),
        ],
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    name = (
        user.first_name
        or user.username
        or "玩家"
    )

    text = (
        f"👋 欢迎 {name}\n\n"
        "🎮 姜天电竞\n"
        "━━━━━━━━━━━━━━\n"
        "⚡ 电竞赛事\n"
        "📊 实时赔率\n"
        "🎯 赛事中心\n"
        "━━━━━━━━━━━━━━\n\n"
        "点击下方按钮进入姜天电竞。"
    )

    await update.message.reply_text(
        text,
        reply_markup=telegram_keyboard(),
    )


# =========================================================
# Telegram 按钮
# =========================================================

async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    # ---------------------
    # 充值
    # ---------------------

    if query.data == "recharge":

        if RECHARGE_URL:

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "💰 打开充值入口",
                            url=RECHARGE_URL,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🎮 进入姜天电竞",
                            web_app=WebAppInfo(
                                url=MINI_APP_URL
                            ),
                        )
                    ],
                ]
            )

            await query.message.reply_text(
                "💰 请选择充值入口：",
                reply_markup=keyboard,
            )

        else:

            await query.message.reply_text(
                "💰 充值入口暂未配置。\n\n"
                "如需帮助，请联系在线客服。"
            )

    # ---------------------
    # 客服
    # ---------------------

    elif query.data == "support":

        if SUPPORT_URL:

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👤 联系在线客服",
                            url=SUPPORT_URL,
                        )
                    ]
                ]
            )

            await query.message.reply_text(
                "👤 点击下方按钮联系在线客服：",
                reply_markup=keyboard,
            )

        else:

            await query.message.reply_text(
                "👤 客服入口暂未配置。"
            )


# =========================================================
# 启动 Flask
# =========================================================

def run_web_server():

    print(
        f"姜天电竞 Web API 启动，PORT={PORT}"
    )

    web_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# =========================================================
# 启动 Telegram Bot
# =========================================================

def run_telegram_bot():

    print(
        "姜天电竞 Telegram Bot 启动"
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback,
        )
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print("=" * 60)
    print("姜天电竞系统启动")
    print("=" * 60)

    if ODDIWIRE_API_KEY:
        print("Oddiwire API Key：已配置")
    else:
        print("WARNING：Oddiwire API Key 未配置")

    print(
        "Mini App:",
        MINI_APP_URL,
    )

    # Flask API 放到后台线程
    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True,
    )

    web_thread.start()

    # Telegram Bot 保持主线程运行
    run_telegram_bot()


if __name__ == "__main__":
    main()
