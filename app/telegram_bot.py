"""Bot Telegram : chaque message reçu est analysé puis rangé dans Airtable.

Lancement (depuis la racine du projet) :
    python -m app.telegram_bot
"""
import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.airtable_store import get_due_items, mark_done, mark_reminded, save_item
from app.brain import analyze
from app.gmail_client import fetch_new_emails, mark_processed
from app.gmail_client import is_authorized as gmail_is_authorized

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID")  # ton identifiant Telegram
REMINDER_LEAD_MINUTES = int(os.getenv("REMINDER_LEAD_MINUTES", "30"))  # prévenir X min avant
REMINDER_CHECK_SECONDS = 60
EMAIL_CHECK_SECONDS = int(os.getenv("EMAIL_CHECK_SECONDS", "600"))  # toutes les 10 min
MAX_EMAILS_PER_RUN = 5  # limite pour ménager le quota de Gemini
MAX_EMAIL_FAILURES = 3  # après 3 échecs, on abandonne cet email
_email_failures: dict[str, int] = {}
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Paris"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s : %(message)s", level=logging.INFO
)
# Les logs HTTP de la bibliothèque affichent l'URL (donc le token du bot) : on les coupe.
logging.getLogger("httpx").setLevel(logging.WARNING)

ICONS = {"tache": "✅ Tâche", "rappel": "⏰ Rappel", "info": "🗂️ Info"}


def _is_allowed(update: Update) -> bool:
    return bool(ALLOWED_USER_ID) and str(update.effective_user.id) == ALLOWED_USER_ID


def _format_deadline(iso: str | None) -> str:
    if not iso:
        return "aucune"
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y à %H:%M")
    except ValueError:
        return iso


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Bonjour ! Envoie-moi un message (une tâche, un rappel, une info) et je le range.\n"
        f"Ton identifiant Telegram : {update.effective_user.id}"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ton identifiant Telegram : {update.effective_user.id}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text(
            "Ce bot est privé. Si c'est ton bot, ajoute ton identifiant "
            "(commande /id) dans TELEGRAM_ALLOWED_USER_ID du fichier .env."
        )
        return

    text = update.message.text
    status = await update.message.reply_text("⏳ Reçu, j'analyse…")

    async def answer(content: str) -> None:
        """Remplace le message d'attente ; sinon envoie un nouveau message."""
        try:
            await status.edit_text(content)
        except Exception:
            logging.exception("Modification impossible, envoi d'un nouveau message")
            await update.message.reply_text(content)

    try:
        # analyze() et save_item() sont bloquants : on les exécute dans un thread
        item = await asyncio.to_thread(analyze, text, "telegram")
        await asyncio.to_thread(save_item, item)
    except Exception:
        logging.exception("Échec du traitement du message")
        await answer(
            "Désolé, je n'ai pas réussi à enregistrer ce message. Réessaie dans un instant."
        )
        return

    logging.info("Enregistré dans Airtable : %s", item["titre"])
    try:
        await answer(
            f"{ICONS[item['type']]} enregistré\n"
            f"• {item['titre']}\n"
            f"• Échéance : {_format_deadline(item.get('echeance'))}\n"
            f"• Priorité : {item['priorite']}"
        )
        logging.info("Réponse envoyée sur Telegram")
    except Exception:
        logging.exception("La ligne est dans Airtable, mais la réponse Telegram a échoué")


def _format_airtable_date(iso: str | None) -> str:
    """Airtable renvoie les dates en UTC (« ...Z ») : on les convertit en heure locale."""
    if not iso:
        return "aucune"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TIMEZONE)
        return dt.strftime("%d/%m/%Y à %H:%M")
    except ValueError:
        return iso


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tâche planifiée : envoie un rappel pour chaque échéance proche."""
    try:
        records = await asyncio.to_thread(get_due_items, REMINDER_LEAD_MINUTES)
    except Exception:
        logging.exception("Lecture des rappels impossible")
        return

    for record in records:
        fields = record["fields"]
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Terminé", callback_data=f"done:{record['id']}")]]
        )
        try:
            await context.bot.send_message(
                chat_id=int(ALLOWED_USER_ID),
                text=(
                    f"⏰ Rappel : {fields.get('Titre', '(sans titre)')}\n"
                    f"• Échéance : {_format_airtable_date(fields.get('Échéance'))}\n"
                    f"• Priorité : {fields.get('Priorité', '—')}"
                ),
                reply_markup=keyboard,
            )
            await asyncio.to_thread(mark_reminded, record["id"])
            logging.info("Rappel envoyé : %s", fields.get("Titre"))
        except Exception:
            logging.exception("Envoi du rappel impossible pour %s", record["id"])


def _short_sender(sender: str) -> str:
    """« Jean Dupont <jean@x.fr> » -> « Jean Dupont »."""
    return sender.split("<")[0].strip().strip('"') or sender


async def check_emails(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tâche planifiée : analyse les nouveaux emails et signale ceux qui demandent une action."""
    try:
        emails = await asyncio.to_thread(fetch_new_emails, MAX_EMAILS_PER_RUN)
    except Exception:
        logging.exception("Lecture Gmail impossible")
        return

    for mail in emails:
        text = f"De : {mail['from']}\nObjet : {mail['subject']}\n\n{mail['body']}"
        try:
            item = await asyncio.to_thread(analyze, text, "gmail")
            if item["type"] != "ignorer":
                await asyncio.to_thread(save_item, item)
        except Exception:
            _email_failures[mail["id"]] = _email_failures.get(mail["id"], 0) + 1
            logging.exception("Échec d'analyse de l'email %s", mail["id"])
            if _email_failures[mail["id"]] >= MAX_EMAIL_FAILURES:
                await asyncio.to_thread(mark_processed, [mail["id"]])  # on abandonne
            continue  # sinon : nouvel essai au prochain passage

        await asyncio.to_thread(mark_processed, [mail["id"]])
        if item["type"] == "ignorer":
            logging.info("Email ignoré : %s", mail["subject"])
            continue
        try:
            await context.bot.send_message(
                chat_id=int(ALLOWED_USER_ID),
                text=(
                    f"📧 Email de {_short_sender(mail['from'])}\n"
                    f"{ICONS[item['type']]} enregistré\n"
                    f"• {item['titre']}\n"
                    f"• Échéance : {_format_deadline(item.get('echeance'))}\n"
                    f"• Priorité : {item['priorite']}"
                ),
            )
        except Exception:
            logging.exception("Notification Telegram impossible pour l'email %s", mail["id"])


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bouton « ✅ Terminé » sous un rappel."""
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    action, _, record_id = query.data.partition(":")
    if action != "done":
        await query.answer()
        return
    try:
        await asyncio.to_thread(mark_done, record_id)
    except Exception:
        logging.exception("Impossible de marquer comme terminé")
        await query.answer("Échec, réessaie dans un instant", show_alert=True)
        return
    await query.answer("Marqué comme terminé")
    await query.edit_message_text(f"{query.message.text}\n\n✅ Terminé")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Erreur non gérée dans le bot", exc_info=context.error)


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN est absent du fichier .env")
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CallbackQueryHandler(on_button))
    if not ALLOWED_USER_ID:
        print("ATTENTION : TELEGRAM_ALLOWED_USER_ID est vide, les rappels sont désactivés.")
    elif app.job_queue is None:
        raise SystemExit(
            'Les rappels demandent : pip install "python-telegram-bot[job-queue]" tzdata'
        )
    else:
        app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_SECONDS, first=10)
        if gmail_is_authorized():
            app.job_queue.run_repeating(check_emails, interval=EMAIL_CHECK_SECONDS, first=30)
        else:
            print("Gmail non activé : lance `python -m app.gmail_client` pour l'autoriser.")
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot démarré. Ctrl+C pour arrêter.")
    app.run_polling()


if __name__ == "__main__":
    main()