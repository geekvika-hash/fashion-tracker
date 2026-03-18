"""
main.py — Fashion Size Tracker Telegram Bot

Flow:
  1. User sends a product URL
  2. Bot scrapes page, shows all sizes as buttons
  3. User picks a size (or types it manually)
  4. Bot starts hourly tracking
  5. When the size is found → notification with "Continue / Stop" buttons
  6. /list or "Мои отслеживания" — shows all active trackings

Environment variables required:
  BOT_TOKEN   — Telegram bot token from @BotFather
"""

import asyncio
import logging
import os
import re
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db
import scrapers

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ──────────────────────────────────────────────────────
WAITING_SIZE = 1

# ── Callback data patterns ───────────────────────────────────────────────────
CB_SIZE_PREFIX    = "size:"       # size:<tracking_candidate_id>:<size>
CB_CONTINUE       = "continue:"   # continue:<tracking_id>
CB_STOP           = "stop:"       # stop:<tracking_id>
CB_MANUAL_SIZE    = "manual_size" # user wants to type a size manually

# Temporary in-memory store while conversation is in progress
# { chat_id: {"url": ..., "product_name": ..., "all_sizes": [...]} }
pending: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def is_url(text: str) -> bool:
    return bool(re.match(r'https?://', text.strip()))


def size_buttons(sizes: list, max_per_row: int = 4) -> list:
    """Build InlineKeyboardButton rows for size selection."""
    rows = []
    row = []
    for i, size in enumerate(sizes):
        row.append(InlineKeyboardButton(size, callback_data=f"{CB_SIZE_PREFIX}{size}"))
        if len(row) == max_per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Ввести другой размер", callback_data=CB_MANUAL_SIZE)])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  BOT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👗 *Fashion Tracker*\n\n"
        "Отправь мне ссылку на товар — я спрошу нужный размер и буду проверять наличие каждый час.\n\n"
        "Как только размер появится — пришлю уведомление 🔔\n\n"
        "Команды:\n"
        "/list — посмотреть все активные отслеживания\n"
        "/help — помощь",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *Как пользоваться:*\n\n"
        "1. Скопируй ссылку на товар (Zara, Massimo, Loewe и др.)\n"
        "2. Отправь её мне\n"
        "3. Выбери или введи нужный размер\n"
        "4. Готово — я буду проверять каждый час!\n\n"
        "*/list* — все активные отслеживания",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items = db.get_user_trackings(chat_id)

    if not items:
        await update.message.reply_text("У тебя пока нет активных отслеживаний. Отправь ссылку на товар!")
        return

    text = "📋 *Твои активные отслеживания:*\n\n"
    for i, item in enumerate(items, 1):
        name = item["product_name"] or "Товар"
        text += f"{i}. [{name}]({item['url']}) — размер *{item['size']}*\n"

    # Add stop buttons for each
    keyboard = []
    for item in items:
        name = item["product_name"] or "Товар"
        keyboard.append([
            InlineKeyboardButton(
                f"🛑 Остановить: {name[:25]} ({item['size']})",
                callback_data=f"{CB_STOP}{item['id']}",
            )
        ])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


# ── Conversation: URL → size selection ───────────────────────────────────────

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    chat_id = update.effective_chat.id

    msg = await update.message.reply_text("🔍 Загружаю страницу товара...")

    result = await asyncio.get_event_loop().run_in_executor(None, scrapers.check_product, url)

    if result.error and not result.all_sizes:
        await msg.edit_text(
            f"⚠️ {result.error}\n\n"
            "Напиши нужный размер вручную (например: S, M, L, 38, 40):"
        )
        pending[chat_id] = {"url": url, "product_name": result.product_name, "all_sizes": []}
        return WAITING_SIZE

    pending[chat_id] = {
        "url": url,
        "product_name": result.product_name,
        "all_sizes": result.all_sizes,
    }

    keyboard = size_buttons(result.all_sizes) if result.all_sizes else [
        [InlineKeyboardButton("✏️ Ввести размер", callback_data=CB_MANUAL_SIZE)]
    ]

    caption = f"✅ Нашла товар: *{result.product_name}*\n\n"
    if result.available_sizes:
        caption += f"Сейчас в наличии: {', '.join(result.available_sizes)}\n\n"
    caption += "Какой размер ждёшь?"

    await msg.edit_text(
        caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_SIZE


async def handle_size_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data == CB_MANUAL_SIZE:
        await query.edit_message_text("Напиши нужный размер (например: S, M, 38, 40):")
        return WAITING_SIZE

    size = query.data.replace(CB_SIZE_PREFIX, "", 1)
    return await _save_tracking(update, ctx, chat_id, size, via_button=True)


async def handle_size_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    size = update.message.text.strip()
    chat_id = update.effective_chat.id
    return await _save_tracking(update, ctx, chat_id, size, via_button=False)


async def _save_tracking(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    size: str,
    via_button: bool,
) -> int:
    info = pending.pop(chat_id, None)
    if not info:
        msg_fn = update.callback_query.edit_message_text if via_button else update.message.reply_text
        await msg_fn("Что-то пошло не так. Попробуй снова — отправь ссылку на товар.")
        return ConversationHandler.END

    tracking_id = db.add_tracking(
        chat_id=chat_id,
        url=info["url"],
        product_name=info["product_name"],
        size=size,
    )

    text = (
        f"🔔 Отслеживаю!\n\n"
        f"Товар: *{info['product_name']}*\n"
        f"Размер: *{size}*\n\n"
        f"Буду проверять каждый час и пришлю уведомление, как только размер появится в наличии."
    )

    if via_button:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    pending.pop(update.effective_chat.id, None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ── Inline button callbacks (continue / stop) ────────────────────────────────

async def handle_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Продолжаю отслеживать! 🔔")
    tracking_id = int(query.data.replace(CB_CONTINUE, "", 1))
    db.unmark_notified(tracking_id)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("✅ Продолжаю отслеживать этот товар. Пришлю, когда размер снова появится!")


async def handle_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Отслеживание остановлено.")
    tracking_id = int(query.data.replace(CB_STOP, "", 1))
    db.stop_tracking(tracking_id)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("🛑 Отслеживание остановлено.")


# ─────────────────────────────────────────────────────────────────────────────
#  HOURLY CHECKER (runs inside the same async event loop)
# ─────────────────────────────────────────────────────────────────────────────

async def check_all_trackings(app: Application):
    """Called by APScheduler every hour."""
    items = db.get_active_trackings()
    logger.info(f"Hourly check: {len(items)} active tracking(s).")

    for item in items:
        if item["notified"]:
            # Already notified — check if size went out of stock to reset flag
            result = await asyncio.get_event_loop().run_in_executor(
                None, scrapers.check_product, item["url"]
            )
            if not result.is_size_available(item["size"]):
                db.unmark_notified(item["id"])
                logger.info(f"[{item['id']}] Size {item['size']} out of stock again → reset notified flag.")
            continue

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, scrapers.check_product, item["url"]
            )
        except Exception as e:
            logger.error(f"Error checking {item['url']}: {e}")
            continue

        if result.is_size_available(item["size"]):
            logger.info(f"[{item['id']}] Size {item['size']} AVAILABLE for {item['url']}")
            db.mark_notified(item["id"])

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔔 Продолжить отслеживать", callback_data=f"{CB_CONTINUE}{item['id']}"),
                    InlineKeyboardButton("🛑 Прекратить", callback_data=f"{CB_STOP}{item['id']}"),
                ]
            ])

            name = item["product_name"] or "Товар"
            text = (
                f"✅ *Появился размер {item['size']}!*\n\n"
                f"Товар: [{name}]({item['url']})\n\n"
                f"Скорее бери — размеры разлетаются быстро! 🛍"
            )

            await app.bot.send_message(
                chat_id=item["chat_id"],
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
        else:
            logger.info(f"[{item['id']}] Size {item['size']} not available yet.")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана!")

    db.init_db()

    app = Application.builder().token(token).build()

    # Conversation handler: URL → size
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex(r'https?://'), handle_url)
        ],
        states={
            WAITING_SIZE: [
                CallbackQueryHandler(handle_size_button, pattern=f"^({CB_SIZE_PREFIX}|{CB_MANUAL_SIZE})"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_size_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(handle_continue, pattern=f"^{CB_CONTINUE}"))
    app.add_handler(CallbackQueryHandler(handle_stop, pattern=f"^{CB_STOP}"))

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_all_trackings,
        trigger="interval",
        hours=1,
        args=[app],
        id="hourly_check",
        replace_existing=True,
    )

    async def post_init(application: Application):
        scheduler.start()
        logger.info("Scheduler started — checking every hour.")

    app.post_init = post_init

    logger.info("Bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
