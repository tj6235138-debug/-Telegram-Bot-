import os
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://tj6235138-debug.github.io/jiangtian-esports/")
SUPPORT_URL = os.getenv("SUPPORT_URL", "")
GROUP_URL = os.getenv("GROUP_URL", "")

def home_keyboard():
    rows = [[InlineKeyboardButton("🎮 开始游戏 🎮", web_app=WebAppInfo(url=MINI_APP_URL))]]
    rows.append([
        InlineKeyboardButton("👤 联系客服", url=SUPPORT_URL) if SUPPORT_URL else InlineKeyboardButton("👤 联系客服", callback_data="support"),
        InlineKeyboardButton("👥 官方群组", url=GROUP_URL) if GROUP_URL else InlineKeyboardButton("👥 官方群组", callback_data="group")
    ])
    rows.append([InlineKeyboardButton("💰 模拟余额", callback_data="balance")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = html.escape(user.first_name or user.username or "玩家")
    text = (
        f"👋 欢迎 {name} 来到麦天电竞！\n"
        f"🆔 Telegram ID：<code>{user.id}</code>\n"
        "━━━━━━━━━━━━━━\n"
        "💰 模拟余额：10,000.00 USDT\n"
        "🎮 请选择下方功能开始体验。"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=home_keyboard())

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    messages = {
        "balance": "💰 当前模拟余额：10,000.00 USDT\n仅用于演示，不代表真实资金。",
        "support": "👤 客服入口尚未配置。部署时设置 SUPPORT_URL 即可。",
        "group": "👥 官方群组尚未配置。部署时设置 GROUP_URL 即可。"
    }
    if q.data in messages:
        await q.message.reply_text(messages[q.data])

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
