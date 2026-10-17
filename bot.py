import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import database
from config import get_settings
from workflow import FollowUpCooldown, follow_up_text, handle_incoming_reply


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        chat_id = (
            f" Your chat ID is {update.effective_user.id}; share it with your project manager."
            if update.effective_chat and update.effective_chat.type == "private"
            and update.effective_user
            else ""
        )
        await update.message.reply_text(
            "Welcome! Your chat is not registered automatically. Ask your project manager "
            f"to register this chat ID, then reply to task follow-ups here.{chat_id}"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Reply to a project follow-up with your progress or blocker. "
            "For several assigned tasks, include the task title."
        )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.message.reply_text("Please reply to this bot in a private chat.")
        return
    settings = get_settings()
    chat_id = str(update.effective_user.id) if update.effective_user else ""
    result = handle_incoming_reply(
        settings.database_path,
        update_id=str(update.update_id),
        telegram_message_id=str(update.message.message_id),
        sender_chat_id=chat_id,
        text=update.message.text,
        reply_to_telegram_message_id=(
            str(update.message.reply_to_message.message_id)
            if update.message.reply_to_message else None
        ),
    )
    await update.message.reply_text(result["reply_text"])


async def send_follow_up(
    db_path: str, task_id: str, token: str, message_text: str | None = None
) -> None:
    from telegram import Bot

    reservation = database.reserve_follow_up(
        db_path, task_id, get_settings().follow_up_cooldown_hours
    )
    message = message_text.strip() if message_text and message_text.strip() else follow_up_text(reservation)
    try:
        async with Bot(token=token) as bot:
            sent_message = await bot.send_message(
                chat_id=reservation["owner_telegram_chat_id"],
                text=message,
                connect_timeout=10,
                read_timeout=15,
                write_timeout=15,
            )
    except (TelegramError, OSError, RuntimeError):
        database.release_follow_up(
            db_path, task_id, reservation["reserved_at"], reservation["previous_at"]
        )
        raise
    database.record_follow_up_sent(
        db_path, task_id, reservation["reserved_at"], message,
        telegram_message_id=getattr(sent_message, "message_id", None),
    )


async def send_due_reminders(application: Application) -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    tasks = database.list_tasks(settings.database_path)
    for task in tasks:
        if task["status"] == "Complete":
            continue
        last_follow_up = task.get("last_follow_up_at")
        if last_follow_up:
            last_sent = datetime.fromisoformat(last_follow_up)
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            if now - last_sent < timedelta(hours=settings.follow_up_cooldown_hours):
                continue
        try:
            reservation = database.reserve_follow_up(
                settings.database_path, task["task_id"], settings.follow_up_cooldown_hours
            )
        except FollowUpCooldown:
            continue
        except database.ValidationError:
            continue
        message = follow_up_text(reservation)
        try:
            sent_message = await application.bot.send_message(
                chat_id=reservation["owner_telegram_chat_id"],
                text=message,
            )
        except (TelegramError, OSError, RuntimeError) as exc:
            database.release_follow_up(
                settings.database_path, task["task_id"],
                reservation["reserved_at"], reservation["previous_at"],
            )
            logging.error("Telegram reminder send failed (%s).", exc.__class__.__name__)
            continue
        database.record_follow_up_sent(
            settings.database_path, task["task_id"], reservation["reserved_at"], message,
            telegram_message_id=getattr(sent_message, "message_id", None),
        )


async def _reminder_loop(application: Application, interval_minutes: float) -> None:
    while True:
        await asyncio.sleep(interval_minutes * 60)
        await send_due_reminders(application)


async def run_polling() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before starting the Telegram bot.")
    database.connect(settings.database_path).close()
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    reminder_task = None
    await application.initialize()
    await application.start()
    try:
        await application.updater.start_polling()
        if settings.reminder_interval_minutes > 0:
            reminder_task = asyncio.create_task(
                _reminder_loop(application, settings.reminder_interval_minutes)
            )
        await asyncio.Event().wait()
    finally:
        if reminder_task:
            reminder_task.cancel()
            await asyncio.gather(reminder_task, return_exceptions=True)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Telegram polling for the project coordinator.")
    parser.add_argument("--check-config", action="store_true")
    options = parser.parse_args()
    settings = get_settings()
    if options.check_config:
        print(f"Database: {settings.database_path}")
        print(f"Telegram token configured: {'yes' if settings.telegram_bot_token else 'no'}")
        print(f"LLM provider: {settings.llm_provider}")
        if not settings.telegram_bot_token:
            raise SystemExit("Configuration incomplete: TELEGRAM_BOT_TOKEN is required to poll.")
        return
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
