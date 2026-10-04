"""Lecture des nouveaux emails Gmail (accès en lecture seule).

Première utilisation (ouvre le navigateur pour autoriser l'accès) :
    python -m app.gmail_client
"""
import base64
import html
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# Lecture seule : l'assistant ne peut ni envoyer, ni supprimer, ni modifier un email.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
# Par défaut : boîte de réception principale, non lus, de moins d'un jour (pas de promotions)
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox is:unread category:primary newer_than:1d")
PROCESSED_FILE = Path(os.getenv("PROCESSED_EMAILS_FILE", "data/processed_emails.json"))

MAX_BODY_CHARS = 3000  # on n'envoie à Gemini que le début du message
MAX_KEPT_IDS = 2000


def is_authorized() -> bool:
    """Vrai si l'autorisation Gmail a déjà été donnée (fichier token présent)."""
    return os.path.exists(TOKEN_FILE)


def get_service(interactive: bool = False):
    """Renvoie un client Gmail. `interactive=True` ouvre le navigateur si nécessaire."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            Path(TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")
        except RefreshError:
            creds = None  # autorisation expirée ou révoquée

    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError(
                "Gmail non autorisé ou expiré : relance `python -m app.gmail_client`."
            )
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------- emails déjà traités (stockage local) ----------

def _load_processed() -> list[str]:
    try:
        return json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def mark_processed(email_ids: list[str]) -> None:
    ids = _load_processed()
    ids.extend(i for i in email_ids if i not in ids)
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.write_text(json.dumps(ids[-MAX_KEPT_IDS:]), encoding="utf-8")


# ---------- extraction du texte d'un email ----------

def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", errors="replace"
    )


def _find_part(payload: dict, mime_type: str) -> str:
    """Cherche récursivement le premier corps du type demandé."""
    if payload.get("mimeType") == mime_type and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts") or []:
        found = _find_part(part, mime_type)
        if found:
            return found
    return ""


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return html.unescape(raw)


def extract_text(payload: dict) -> str:
    """Texte brut de l'email (version texte si présente, sinon HTML nettoyé)."""
    text = _find_part(payload, "text/plain") or _html_to_text(_find_part(payload, "text/html"))
    return re.sub(r"\s+", " ", text).strip()[:MAX_BODY_CHARS]


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ---------- récupération ----------

def fetch_new_emails(max_results: int = 5) -> list[dict]:
    """Renvoie jusqu'à `max_results` emails correspondant à la requête et pas encore traités."""
    service = get_service()
    listing = service.users().messages().list(
        userId="me", q=GMAIL_QUERY, maxResults=20
    ).execute()

    done = set(_load_processed())
    new_ids = [m["id"] for m in listing.get("messages", []) if m["id"] not in done]

    emails = []
    for message_id in new_ids[:max_results]:
        message = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        payload = message.get("payload", {})
        emails.append(
            {
                "id": message_id,
                "from": _header(payload, "From"),
                "subject": _header(payload, "Subject") or "(sans objet)",
                "date": _header(payload, "Date"),
                "body": extract_text(payload),
            }
        )
    return emails


if __name__ == "__main__":
    # Autorisation + test : affiche les 3 derniers emails de la boîte (sans rien marquer)
    service = get_service(interactive=True)
    result = service.users().messages().list(userId="me", q="in:inbox", maxResults=3).execute()
    print("Autorisation Gmail OK. Derniers emails :")
    for m in result.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject"]
        ).execute()
        headers = msg["payload"]["headers"]
        print(" -", _header({"headers": headers}, "Subject"), "|", _header({"headers": headers}, "From"))
