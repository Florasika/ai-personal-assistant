"""Écriture des éléments structurés dans Airtable."""
import os
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv("AIRTABLE_TOKEN")
BASE_ID = os.getenv("AIRTABLE_BASE_ID")
TABLE_NAME = os.getenv("AIRTABLE_TABLE", "Tâches")

URL = f"https://api.airtable.com/v0/{BASE_ID}/{quote(TABLE_NAME)}"
HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

TYPE_LABELS = {"tache": "Tâche", "rappel": "Rappel", "info": "Info"}


def _check(response: requests.Response) -> None:
    """Lève une erreur qui inclut le détail renvoyé par Airtable."""
    if not response.ok:
        raise RuntimeError(
            f"Airtable {response.status_code} : {response.text}"
        )


def save_item(item: dict) -> str:
    """Crée une ligne dans Airtable et renvoie son identifiant."""
    fields = {
        "Titre": item["titre"],
        "Type": TYPE_LABELS[item["type"]],
        "Priorité": item["priorite"].capitalize(),
        "Source": item["source"].capitalize(),
        "Statut": "À faire",
    }
    if item.get("echeance"):
        fields["Échéance"] = item["echeance"]
    if item.get("resume"):
        fields["Notes"] = item["resume"]

    # typecast=True : Airtable crée automatiquement les options des champs
    # à sélection unique si elles n'existent pas encore.
    payload = {"records": [{"fields": fields}], "typecast": True}
    response = requests.post(URL, headers=HEADERS, json=payload, timeout=15)
    _check(response)
    return response.json()["records"][0]["id"]


def list_open_items() -> list[dict]:
    """Renvoie les éléments non terminés (utile pour le futur scheduler)."""
    params = {"filterByFormula": "{Statut} != 'Terminé'"}
    response = requests.get(URL, headers=HEADERS, params=params, timeout=15)
    _check(response)
    return [r["fields"] | {"id": r["id"]} for r in response.json()["records"]]


def get_due_items(lead_minutes: int = 30) -> list[dict]:
    """Éléments (hors Info) à rappeler : échéance dans moins de `lead_minutes` minutes
    (ou dépassée depuis moins de 24 h), pas encore terminés, rappel pas encore envoyé."""
    formula = (
        "AND("
        "{Statut}!='Terminé', "
        "{Type}!='Info', "
        "{Échéance}, "
        "NOT({Rappel envoyé}), "
        f"IS_BEFORE({{Échéance}}, DATEADD(NOW(), {int(lead_minutes)}, 'minutes')), "
        "IS_AFTER({Échéance}, DATEADD(NOW(), -1, 'days'))"
        ")"
    )
    response = requests.get(
        URL, headers=HEADERS, params={"filterByFormula": formula}, timeout=15
    )
    _check(response)
    return response.json()["records"]


def _update(record_id: str, fields: dict) -> None:
    response = requests.patch(
        f"{URL}/{record_id}", headers=HEADERS, json={"fields": fields}, timeout=15
    )
    _check(response)


def mark_reminded(record_id: str) -> None:
    """Coche « Rappel envoyé » pour ne pas prévenir deux fois."""
    _update(record_id, {"Rappel envoyé": True})


def mark_done(record_id: str) -> None:
    """Passe le statut à « Terminé »."""
    _update(record_id, {"Statut": "Terminé"})