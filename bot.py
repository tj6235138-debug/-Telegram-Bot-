import os
import html
import json
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# 环境变量
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Railway 目前保存 Oddiwire Key 的变量名称
ODDIWIRE_API_KEY = os.environ.get("FIELDFUNDED_API_KEY", "")

ODDIWIRE_BASE_URL = "https://oddiwire.com"


# =========================
# 主菜单
# =========================
def main_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🎮 实时比赛", callback_data="live"),
            InlineKeyboardButton("📅 赛前比赛", callback_data="prematch"),
        ],
        [
            InlineKeyboardButton("🔥 热门赛事", callback_data="hot"),
            InlineKeyboardButton("🔄 刷新数据", callback_data="live"),
        ],
        [
            InlineKeyboardButton("🔌 API状态", callback_data="api_status"),
            InlineKeyboardButton("ℹ️ 关于姜天电竞", callback_data="about"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================
# Oddiwire 通用请求
# =========================
def oddiwire_request(path, params=None):
    if not ODDIWIRE_API_KEY:
        return None, "Railway 中没有找到 Oddiwire API Key"

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

        # 只打印状态和响应内容
        # 不打印 API Key
        print("=" * 60)
        print("ODDIWIRE REQUEST:", path)
        print("ODDIWIRE STATUS:", response.status_code)
        print("ODDIWIRE RESPONSE:", response.text[:3000])
        print("=" * 60)

        if response.status_code == 401:
            return None, "Oddiwire API Key 无效或没有权限"

        if response.status_code == 403:
            return None, "Oddiwire 拒绝访问，请检查套餐/API权限"

        if response.status_code == 404:
            return None, f"Oddiwire 接口不存在：{path}"

        if response.status_code == 429:
            return None, "Oddiwire 请求额度已达到限制，请稍后再试"

        if response.status_code >= 500:
            return None, f"Oddiwire服务器异常：HTTP {response.status_code}"

        response.raise_for_status()

        try:
            return response.json(), None
        except Exception:
            return None, "Oddiwire 返回的不是有效 JSON 数据"

    except requests.exceptions.Timeout:
        return None, "连接 Oddiwire 超时"

    except requests.exceptions.ConnectionError:
        return None, "无法连接 Oddiwire"

    except Exception as e:
        print("ODDIWIRE ERROR:", repr(e))
        return None, f"API连接失败：{str(e)}"


# =========================
# API Key 状态
# =========================
def get_api_status():
    return oddiwire_request("/v1/me")


# =========================
# 获取赛事
# =========================
def get_fixtures(event_type="live", limit=10):
    params = {
        "event_type": event_type,
        "tier": 1,
        "limit": limit,
    }

    return oddiwire_request(
        "/v1/fixtures",
        params=params,
    )


# =========================
# 提取赛事
# =========================
def extract_fixtures(data):
    print(
        "FIXTURE DATA TYPE:",
        type(data).__name__,
    )

    if isinstance(data, list):
        print("FIXTURE LIST COUNT:", len(data))
        return data

    if isinstance(data, dict):

        print(
            "FIXTURE TOP LEVEL KEYS:",
            list(data.keys()),
        )

        for key in (
            "fixtures",
            "data",
            "results",
            "items",
        ):
            value = data.get(key)

            if isinstance(value, list):
                print(
                    f"FIXTURE LIST FOUND: {key}",
                    len(value),
                )
                return value

            # 有些 API 会 data -> fixtures
            if isinstance(value, dict):

                for subkey in (
                    "fixtures",
                    "results",
                    "items",
                    "events",
                ):
                    subvalue = value.get(subkey)

                    if isinstance(subvalue, list):
                        print(
                            f"FIXTURE LIST FOUND: "
                            f"{key}.{subkey}",
                            len(subvalue),
                        )
                        return subvalue

    print("NO FIXTURE LIST FOUND")
    return []


# =========================
# 格式化单场比赛
# =========================
def format_fixture(fixture):
    sport = fixture.get(
        "sport",
        fixture.get("game", "ESPORTS"),
    )

    competition = fixture.get(
        "competition",
        {},
    )

    if isinstance(competition, dict):
        league = (
            competition.get("name")
            or competition.get("title")
            or "未知赛事"
        )
    else:
        league = str(
            competition or "未知赛事"
        )

    competitors = fixture.get(
        "competitors",
        [],
    )

    team1 = "队伍A"
    team2 = "队伍B"

    if isinstance(competitors, list):

        if len(competitors) >= 1:
            first = competitors[0]

            if isinstance(first, dict):
                team1 = (
                    first.get("name")
                    or first.get("title")
                    or team1
                )

        if len(competitors) >= 2:
            second = competitors[1]

            if isinstance(second, dict):
                team2 = (
                    second.get("name")
                    or second.get("title")
                    or team2
                )

    phase = fixture.get("phase", "")
    status = fixture.get("status", "")

    text = (
        f"🎮 <b>{html.escape(str(sport))}</b>\n"
        f"🏆 {html.escape(str(league))}\n"
        f"⚔️ <b>{html.escape(str(team1))}</b>\n"
        f"🆚 <b>{html.escape(str(team2))}</b>\n"
    )

    if phase:
        text += (
            f"📡 状态："
            f"{html.escape(str(phase))}\n"
        )

    if status:
        text += (
            f"📊 "
            f"{html.escape(str(status))}\n"
        )

    markets = fixture.get(
        "markets",
        [],
    )

    if isinstance(markets, list) and markets:

        text += "\n💹 <b>赔率盘口</b>\n"

        shown = 0

        for market in markets:

            if shown >= 3:
                break

            if not isinstance(market, dict):
                continue

            market_name = (
                market.get("market")
                or market.get("name")
                or market.get("canonical")
                or "盘口"
            )

            selections = market.get(
                "selections",
                [],
            )

            if not isinstance(
                selections,
                list,
            ):
                continue

            if not selections:
                continue

            text += (
                f"\n• "
                f"{html.escape(str(market_name))}"
                f"\n"
            )

            for selection in selections[:4]:

                if not isinstance(
                    selection,
                    dict,
                ):
                    continue

                side = (
                    selection.get("name")
                    or selection.get("side")
                    or selection.get("selection")
                    or "-"
                )

                line = selection.get("line")
                price = selection.get("price")

                line_text = ""

                if line is not None:
                    line_text = f" {line}"

                if price is not None:
                    text += (
                        f"  └ "
                        f"{html.escape(str(side))}"
                        f"{html.escape(str(line_text))}"
                        f"  @ "
                        f"{html.escape(str(price))}"
                        f"\n"
                    )

            shown += 1

    return text


# =========================
# /start
# =========================
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    name = html.escape(
        user.first_name
        or user.username
        or "玩家"
    )

    text = (
        f"👋 欢迎 <b>{name}</b> 来到\n\n"
        f"🎮 <b>姜天电竞</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚡ 实时电竞赛事\n"
        f"📊 实时赔率数据\n"
        f"🔥 热门比赛查询\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"请选择下方功能："
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# =========================
# API 状态
# =========================
async def show_api_status(query):
    await query.answer()

    await query.edit_message_text(
        "🔌 正在检查 Oddiwire API..."
    )

    data, error = get_api_status()

    if error:
        await query.edit_message_text(
            f"❌ <b>API连接失败</b>\n\n"
            f"{html.escape(error)}",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    print(
        "API STATUS DATA:",
        json.dumps(
            data,
            ensure_ascii=False,
        )[:3000],
    )

    text = (
        "✅ <b>Oddiwire API连接成功</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🔑 API Key：已识别\n"
        "🌐 API：正常响应\n\n"
        "现在可以测试实时比赛和赛前比赛。"
    )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# =========================
# 显示赛事
# =========================
async def show_fixtures(
    query,
    event_type,
):
    await query.answer()

    await query.edit_message_text(
        "⏳ 正在从 Oddiwire 获取最新数据..."
    )

    data, error = get_fixtures(
        event_type=event_type,
        limit=10,
    )

    if error:
        await query.edit_message_text(
            f"⚠️ <b>数据获取失败</b>\n\n"
            f"{html.escape(error)}",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    fixtures = extract_fixtures(data)

    if not fixtures:
        await query.edit_message_text(
            "📭 <b>接口连接成功，但没有解析到赛事</b>\n\n"
            "这不一定代表没有比赛。\n"
            "Railway 日志已经记录 Oddiwire "
            "实际返回的数据结构，"
            "我们可以据此继续调整。",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if event_type == "live":
        title = "🔴 <b>实时比赛</b>"
    else:
        title = "📅 <b>赛前比赛</b>"

    text = (
        title
        + "\n━━━━━━━━━━━━━━\n\n"
    )

    for fixture in fixtures[:5]:
        text += format_fixture(fixture)
        text += "\n━━━━━━━━━━━━━━\n"

    if len(text) > 3900:
        text = (
            text[:3900]
            + "\n\n……"
        )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# =========================
# 按钮
# =========================
async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.data == "live":

        await show_fixtures(
            query,
            "live",
        )

    elif query.data == "prematch":

        await show_fixtures(
            query,
            "prematch",
        )

    elif query.data == "hot":

        await show_fixtures(
            query,
            "live",
        )

    elif query.data == "api_status":

        await show_api_status(query)

    elif query.data == "about":

        await query.answer()

        text = (
            "🎮 <b>姜天电竞</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "⚡ 电竞赛事数据\n"
            "📊 实时赔率盘口\n"
            "🎯 赛前与滚球赛事\n\n"
            "数据接口：Oddiwire\n\n"
            "赔率信息仅作数据展示。"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )


# =========================
# 启动
# =========================
def main():

    if not ODDIWIRE_API_KEY:
        print(
            "WARNING: "
            "FIELDFUNDED_API_KEY 未配置"
        )
    else:
        print(
            "Oddiwire API Key 已从 Railway 读取"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback,
        )
    )

    print(
        "姜天电竞 Telegram Bot 已启动"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
