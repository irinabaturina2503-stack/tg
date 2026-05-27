import os
import logging
import threading
import psycopg2
import psycopg2.extras
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])

CHOOSE_DATE, CHOOSE_TIME, CONFIRM = range(3)

AVAILABLE_TIMES = [f"{h:02d}:00" for h in range(10, 24)] + ["00:00"]
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def run_health_server() -> None:
    port = int(os.environ.get("PORT", 8080))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
        logger.info(f"Health check server listening on port {port}")
        server.serve_forever()
    except OSError as e:
        logger.warning(f"Health check server could not bind to port {port}: {e} — bot continues without it")


def get_db():
    return psycopg2.connect(DATABASE_URL)


def save_booking(
    telegram_id: int,
    username: str | None,
    full_name: str | None,
    chosen_date: str,
    chosen_time: str,
) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bookings (telegram_id, username, full_name, chosen_date, chosen_time)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (telegram_id, username, full_name, chosen_date, chosen_time),
            )
            booking_id = cur.fetchone()[0]
        conn.commit()
    return booking_id


def get_user_bookings(telegram_id: int) -> list[dict]:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, chosen_date, chosen_time, created_at
                FROM bookings
                WHERE telegram_id = %s
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (telegram_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def get_all_bookings(limit: int = 30) -> list[dict]:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, full_name, username, chosen_date, chosen_time, created_at
                FROM bookings
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]


def get_booking_stats() -> dict:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS total FROM bookings")
            total = cur.fetchone()["total"]
            cur.execute(
                "SELECT COUNT(*) AS today FROM bookings WHERE created_at::date = CURRENT_DATE"
            )
            today = cur.fetchone()["today"]
    return {"total": total, "today": today}


def get_available_dates(days_ahead: int = 7) -> list[str]:
    dates = []
    today = date.today()
    for i in range(1, days_ahead + 1):
        d = today + timedelta(days=i)
        if d.weekday() < 6:
            weekday = WEEKDAYS_RU[d.weekday()]
            dates.append(f"{d.strftime('%d.%m')} ({weekday})")
    return dates


def build_keyboard(options: list[str], cols: int = 3) -> ReplyKeyboardMarkup:
    rows = [options[i : i + cols] for i in range(0, len(options), cols)]
    rows.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [["Записаться"], ["Мои записи"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Приветствую! Выберите действие:",
        reply_markup=reply_markup,
    )


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    bookings = get_user_bookings(user.id)

    if not bookings:
        await update.message.reply_text("У вас пока нет записей.")
        return

    lines = ["📋 *Ваши последние записи:*\n"]
    for b in bookings:
        lines.append(f"📅 {b['chosen_date']} в {b['chosen_time']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dates = get_available_dates()
    context.user_data["available_dates"] = dates
    await update.message.reply_text(
        "📅 Выберите удобную дату:",
        reply_markup=build_keyboard(dates),
    )
    return CHOOSE_DATE


async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "❌ Отмена":
        return await cancel(update, context)

    available_dates = context.user_data.get("available_dates", [])
    if text not in available_dates:
        await update.message.reply_text(
            "Пожалуйста, выберите дату из предложенных вариантов.",
            reply_markup=build_keyboard(available_dates),
        )
        return CHOOSE_DATE

    context.user_data["chosen_date"] = text
    await update.message.reply_text(
        f"✅ Дата: *{text}*\n\n🕐 Теперь выберите время:",
        parse_mode="Markdown",
        reply_markup=build_keyboard(AVAILABLE_TIMES, cols=4),
    )
    return CHOOSE_TIME


async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "❌ Отмена":
        return await cancel(update, context)

    if text not in AVAILABLE_TIMES:
        await update.message.reply_text(
            "Пожалуйста, выберите время из предложенных вариантов.",
            reply_markup=build_keyboard(AVAILABLE_TIMES, cols=4),
        )
        return CHOOSE_TIME

    context.user_data["chosen_time"] = text
    chosen_date = context.user_data["chosen_date"]

    confirm_keyboard = ReplyKeyboardMarkup(
        [["✅ Подтвердить", "❌ Отмена"]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        f"📋 *Ваша запись:*\n\n"
        f"📅 Дата: *{chosen_date}*\n"
        f"🕐 Время: *{text}*\n\n"
        f"Подтвердите запись:",
        parse_mode="Markdown",
        reply_markup=confirm_keyboard,
    )
    return CONFIRM


async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "❌ Отмена":
        return await cancel(update, context)

    if text != "✅ Подтвердить":
        await update.message.reply_text(
            "Пожалуйста, нажмите «Подтвердить» или «Отмена»."
        )
        return CONFIRM

    user = update.effective_user
    chosen_date = context.user_data["chosen_date"]
    chosen_time = context.user_data["chosen_time"]

    try:
        booking_id = save_booking(
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
            chosen_date=chosen_date,
            chosen_time=chosen_time,
        )
        logger.info(
            f"Booking saved: id={booking_id}, user={user.id}, {chosen_date} {chosen_time}"
        )
    except Exception:
        logger.exception("Failed to save booking")
        await update.message.reply_text(
            "⚠️ Произошла ошибка при сохранении записи. Попробуйте ещё раз."
        )
        return CONFIRM

    try:
        name = user.full_name or "—"
        username = f"@{user.username}" if user.username else "без username"
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🔔 *Новая запись!*\n\n"
                f"👤 {name} ({username})\n"
                f"📅 {chosen_date} в {chosen_time}"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        logger.warning("Failed to send admin notification", exc_info=True)

    main_keyboard = ReplyKeyboardMarkup(
        [["Записаться"], ["Мои записи"]], resize_keyboard=True
    )
    await update.message.reply_text(
        f"🎉 Вы успешно записаны!\n\n"
        f"📅 *{chosen_date}* в *{chosen_time}*\n\n"
        f"Ждём вас! Чтобы посмотреть свои записи, нажмите «Мои записи».",
        parse_mode="Markdown",
        reply_markup=main_keyboard,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return

    try:
        stats = get_booking_stats()
        bookings = get_all_bookings(limit=30)
    except Exception:
        logger.exception("Failed to fetch admin data")
        await update.message.reply_text("⚠️ Ошибка при получении данных.")
        return

    lines = [
        f"📊 *Статистика записей*",
        f"Всего: {stats['total']} | Сегодня: {stats['today']}\n",
    ]

    if not bookings:
        lines.append("Записей пока нет.")
    else:
        lines.append("📋 *Последние 30 записей:*\n")
        for b in bookings:
            name = b["full_name"] or "—"
            username = f"@{b['username']}" if b["username"] else "без username"
            lines.append(
                f"• {b['chosen_date']} {b['chosen_time']} — {name} ({username})"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    main_keyboard = ReplyKeyboardMarkup(
        [["Записаться"], ["Мои записи"]], resize_keyboard=True
    )
    await update.message.reply_text(
        "❌ Запись отменена. Выберите действие:",
        reply_markup=main_keyboard,
    )
    return ConversationHandler.END


def main() -> None:
    app = ApplicationBuilder().token(TOKEN).build()

    booking_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Записаться$"), booking_start)],
        states={
            CHOOSE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_date)],
            CHOOSE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_time)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_booking)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.Regex("^Мои записи$"), my_bookings))
    app.add_handler(booking_handler)

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
