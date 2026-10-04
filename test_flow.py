"""Test local du flux complet : message -> Claude -> Airtable.

Usage :
    python test_flow.py            # analyse seulement (n'écrit rien)
    python test_flow.py --save     # analyse + écriture dans Airtable
"""
import json
import sys

from app.brain import analyze

SAMPLES = [
    ("Rappelle-moi d'envoyer le rapport BSC à Mme Laurent vendredi avant 17h", "telegram"),
    ("Le code wifi de la salle B204 c'est EPSI-2026", "telegram"),
    (
        "Bonjour, pouvez-vous nous confirmer votre présence à la soutenance "
        "du 12 octobre à 9h30 ? Merci de répondre avant lundi.",
        "gmail",
    ),
]

if __name__ == "__main__":
    save = "--save" in sys.argv
    if save:
        from app.airtable_store import save_item

    for text, source in SAMPLES:
        print(f"\n> [{source}] {text}")
        item = analyze(text, source)
        print(json.dumps(item, ensure_ascii=False, indent=2))
        if save:
            print("  -> enregistré :", save_item(item))
