import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(TIMEZONE_NAME)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

NAME, DESCRIPTION, DATE, USERS = range(4)

DATE_FORMAT = "%d.%m.%Y %H:%M"


def job_name(event_id: int, stage: str) -> str:
    return f"reminder_{event_id}_{stage}"


# ---------- helpers ----------

def parse_event_datetime(text: str) -> datetime:
    naive = datetime.strptime(text.strip(), DATE_FORMAT)
    return naive.replace(tzinfo=TZ)


def format_event_datetime(iso_string: str) -> str:
    dt = datetime.fromisoformat(iso_string)
    return dt.astimezone(TZ).strftime(DATE_FORMAT)


async def schedule_reminders(context: ContextTypes.DEFAULT_TYPE, event_row) -> None:
    event_id = event_row["id"]
    event_time = datetime.fromisoformat(event_row["event_time"])
    now = datetime.now(TZ)

    stages = [
        ("48h", event_time - timedelta(hours=48), bool(event_row["reminder_48_sent"])),
        ("24h", event_time - timedelta(hours=24), bool(event_row["reminder_24_sent"])),
    ]
    for stage, fire_at, already_sent in stages:
        if already_sent or event_time <= now:
            continue
        name = job_name(event_id, stage)
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
        if fire_at <= now:
            # We missed the exact moment (e.g. bot was offline) - send right away.
            context.job_queue.run_once(
                send_reminder, when=1, data={"event_id": event_id, "stage": stage}, name=name
            )
        else:
            context.job_queue.run_once(
                send_reminder, when=fire_at, data={"event_id": event_id, "stage": stage}, name=name
            )


def cancel_jobs(context: ContextTypes.DEFAULT_TYPE, event_id: int) -> None:
    for stage in ("48h", "24h"):
        for job in context.job_queue.get_jobs_by_name(job_name(event_id, stage)):
            job.schedule_removal()


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    event_id = data["event_id"]
    stage = data["stage"]

    event_row = db.get_event(event_id)
    if event_row is None:
        return

    label = "48 часов" if stage == "48h" else "24 часа"
    when_str = format_event_datetime(event_row["event_time"])
    text = (
        f"⏰ Напоминание: через {label} состоится событие!\n\n"
        f"📌 {event_row['name']}\n"
        f"🕒 {when_str}\n"
        f"📝 {event_row['description'] or '—'}"
    )

    chat_ids = set()
    chat_ids.add(event_row["creator_chat_id"])
    for username in db.get_subscribers(event_id):
        chat_id = db.get_chat_id_for_username(username)
        if chat_id:
            chat_ids.add(chat_id)

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("Failed to send reminder to chat_id=%s", chat_id)

    db.mark_reminder_sent(event_id, stage)


# ---------- basic commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user.username, update.effective_chat.id)
    await update.message.reply_text(
        "Привет! Я бот-календарь-напоминалка.\n\n"
        "Доступные команды:\n"
        "/add — добавить новое событие\n"
        "/list — список ваших ближайших событий\n"
        "/delete — удалить событие\n"
        "/cancel — отменить текущее действие\n\n"
        "Чтобы получать напоминания о событиях других людей, обязательно "
        "нажмите /start в этом боте хотя бы один раз — иначе бот не будет знать, "
        "куда вам писать."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


# ---------- /add conversation ----------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.upsert_user(update.effective_user.username, update.effective_chat.id)
    context.user_data.clear()
    await update.message.reply_text(
        "Создаём новое событие.\nВведите название события (или /cancel для отмены):"
    )
    return NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите описание события (или отправьте «-», если без описания):"
    )
    return DESCRIPTION


async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["description"] = "" if text == "-" else text
    await update.message.reply_text(
        f"Введите дату и время события в формате {DATE_FORMAT} (например, 25.12.2026 18:30):"
    )
    return DATE


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        event_dt = parse_event_datetime(text)
    except ValueError:
        await update.message.reply_text(
            f"Не получилось распознать дату. Используйте формат {DATE_FORMAT}, например 25.12.2026 18:30."
        )
        return DATE

    if event_dt <= datetime.now(TZ):
        await update.message.reply_text("Дата должна быть в будущем. Введите дату ещё раз:")
        return DATE

    context.user_data["event_dt"] = event_dt
    await update.message.reply_text(
        "Укажите username-ы пользователей, которым нужно отправлять напоминания "
        "(через запятую или пробел, например @ivan @maria).\n"
        "Эти люди должны хотя бы раз написать /start этому боту, иначе напоминания "
        "до них не дойдут.\n"
        "Если напоминания нужны только вам — отправьте «-»."
    )
    return USERS


async def add_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    usernames: list[str] = []
    if text != "-":
        for chunk in text.replace(",", " ").split():
            uname = chunk.strip().lstrip("@").lower()
            if uname:
                usernames.append(uname)

    user = update.effective_user
    event_dt = context.user_data["event_dt"]
    name = context.user_data["name"]
    description = context.user_data["description"]

    event_id = db.create_event(
        name=name,
        description=description,
        event_time_iso=event_dt.isoformat(),
        creator_chat_id=update.effective_chat.id,
        creator_username=user.username,
        subscriber_usernames=usernames,
    )
    event_row = db.get_event(event_id)
    await schedule_reminders(context, event_row)

    unknown = [u for u in usernames if db.get_chat_id_for_username(u) is None]
    summary = (
        f"✅ Событие создано (ID {event_id})\n\n"
        f"📌 {name}\n"
        f"🕒 {event_dt.strftime(DATE_FORMAT)}\n"
        f"📝 {description or '—'}\n"
        f"🔔 Напоминания придут за 48 и за 24 часа до события."
    )
    if usernames:
        summary += f"\n👥 Подписчики: {', '.join('@' + u for u in usernames)}"
    if unknown:
        summary += (
            "\n\n⚠️ Эти пользователи ещё не писали /start боту, поэтому напоминания "
            "им пока не придут, пока они не запустят бота: "
            + ", ".join("@" + u for u in unknown)
        )

    await update.message.reply_text(summary)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END


# ---------- /list ----------

async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user.username, update.effective_chat.id)
    rows = db.get_events_for_user(user.username, update.effective_chat.id)
    if not rows:
        await update.message.reply_text("У вас нет ближайших событий.")
        return

    lines = ["📅 Ваши ближайшие события:\n"]
    for row in rows:
        subs = db.get_subscribers(row["id"])
        subs_str = (", ".join("@" + s for s in subs)) if subs else "—"
        lines.append(
            f"ID {row['id']}: {row['name']}\n"
            f"  🕒 {format_event_datetime(row['event_time'])}\n"
            f"  📝 {row['description'] or '—'}\n"
            f"  👥 {subs_str}\n"
        )
    await update.message.reply_text("\n".join(lines))


# ---------- /delete ----------

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.get_events_created_by(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("У вас нет созданных событий для удаления.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"{row['name']} — {format_event_datetime(row['event_time'])}",
                callback_data=f"del:{row['id']}",
            )
        ]
        for row in rows
    ]
    await update.message.reply_text(
        "Выберите событие для удаления:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    event_id = int(query.data.split(":", 1)[1])
    row = db.get_event(event_id)
    if row is None or row["creator_chat_id"] != update.effective_chat.id:
        await query.edit_message_text("Событие не найдено или у вас нет прав на его удаление.")
        return

    cancel_jobs(context, event_id)
    db.delete_event(event_id)
    await query.edit_message_text(f"🗑 Событие «{row['name']}» удалено.")


# ---------- startup ----------

async def reschedule_all(application: Application) -> None:
    for row in db.get_future_events():
        await schedule_reminders(application, row)


async def post_init(application: Application) -> None:
    await reschedule_all(application)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Put it in a .env file or environment variable.")

    db.init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_date)],
            USERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_users)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(add_conv)
    application.add_handler(CommandHandler("list", list_events))
    application.add_handler(CommandHandler("delete", delete_start))
    application.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^del:\d+$"))

    application.run_polling()


if __name__ == "__main__":
    main()
