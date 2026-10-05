import os
import html
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

# 保留你 Railway 现在已经设置好的变量名
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
            InlineKeyboardButton("ℹ️ 关于姜天电竞", callback_data="about"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================
# Oddiwire API
# =========================
def get_fixtures(event_type="live", limit=10):
    if not ODDIWIRE_API_KEY:
        return None, "未配置 Oddiwire API Key"

    url = f"{ODDIWIRE_BASE_URL}/v1/fixtures"

    headers = {
        "x-api-key": ODDIWIRE_API_KEY,
        "Accept": "application/json",
    }

    params = {
        "event_type": event_type,
        "tier": 1,
        "limit": limit,
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=15,
        )

        if response.status_code == 401:
            return None, "Oddiwire API 密钥无效或未授权"

        if response.status_code == 429:
            return None, "Oddiwire API 请求过于频繁，请稍后再试"

        response.raise_for_status()

        return response.json(), None

    except Exception as e:
        return None, f"API连接失败：{str(e)}"


# =========================
# 数据处理
# =========================
def extract_fixtures(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("fixtures", "data", "results", "items"):
            value = data.get(key)

            if isinstance(value, list):
                return value

    return []


def format_fixture(fixture):
    sport = fixture.get("sport", "ESPORTS")

    competition = fixture.get("competition", {})
    if isinstance(competition, dict):
        league = competition.get("name", "未知赛事")
    else:
        league = str(competition or "未知赛事")

    competitors = fixture.get("competitors", [])

    team1 = "队伍A"
    team2 = "队伍B"

    if len(competitors) >= 1:
        team1 = competitors[0].get("name", team1)

    if len(competitors) >= 2:
        team2 = competitors[1].get("name", team2)

    phase = fixture.get("phase", "")
    status = fixture.get("status", "")

    text = (
        f"🎮 <b>{html.escape(str(sport))}</b>\n"
        f"🏆 {html.escape(str(league))}\n"
        f"⚔️ <b>{html.escape(str(team1))}</b>\n"
        f"🆚 <b>{html.escape(str(team2))}</b>\n"
    )

    if phase:
        text += f"📡 状态：{html.escape(str(phase))}\n"

    if status:
        text += f"📊 {html.escape(str(status))}\n"

    markets = fixture.get("markets", [])

    if markets:
        text += "\n💹 <b>实时赔率</b>\n"

        shown = 0

        for market in markets:
            if shown >= 3:
                break

            market_name = (
                market.get("market")
                or market.get("name")
                or market.get("canonical")
                or "盘口"
            )

            selections = market.get("selections", [])

            if not selections:
                continue

            text += f"\n• {html.escape(str(market_name))}\n"

            for selection in selections[:3]:
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
                        f"  └ {html.escape(str(side))}"
                        f"{html.escape(str(line_text))}"
                        f"  @ {html.escape(str(price))}\n"
                    )

            shown += 1

    return text


# =========================
# /start
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
# 显示赛事
# =========================
async def show_fixtures(query, event_type):
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
            f"⚠️ {html.escape(error)}",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    fixtures = extract_fixtures(data)

    if not fixtures:
        await query.edit_message_text(
            "📭 当前暂时没有找到对应赛事。\n\n"
            "可以稍后点击刷新重新查询。",
            reply_markup=main_keyboard(),
        )
        return

    title = (
        "🔴 <b>实时比赛</b>"
        if event_type == "live"
        else "📅 <b>赛前比赛</b>"
    )

    text = title + "\n━━━━━━━━━━━━━━\n\n"

    for fixture in fixtures[:5]:
        text += format_fixture(fixture)
        text += "\n━━━━━━━━━━━━━━\n"

    # Telegram 单条消息长度限制
    if len(text) > 3900:
        text = text[:3900] + "\n\n……"

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# =========================
# 按钮回调
# =========================
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.data == "live":
        await show_fixtures(query, "live")

    elif query.data == "prematch":
        await show_fixtures(query, "prematch")

    elif query.data == "hot":
        await show_fixtures(query, "live")

    elif query.data == "about":
        await query.answer()

        text = (
            "🎮 <b>姜天电竞</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "提供电竞赛事与赔率信息查询。\n\n"
            "📊 数据接口：Oddiwire\n"
            "⚡ 支持实时与赛前赛事\n\n"
            "赔率信息仅作数据展示。"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )


# =========================
# 启动机器人
# =========================
def main():
    if not ODDIWIRE_API_KEY:
        print("WARNING: FIELDFUNDED_API_KEY 未配置")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CallbackQueryHandler(callback)
    )

    print("姜天电竞 Telegram Bot 已启动")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
