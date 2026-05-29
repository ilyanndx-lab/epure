import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FLASHCARDS_PATH = Path(__file__).parent.parent / "memory" / "flashcards.json"

# Spaced-repetition intervals in days, indexed by level (0-5)
_INTERVALS = [1, 3, 7, 14, 30, 60]
_MAX_LEVEL = 5


class FlashcardsEngine:
    def __init__(self):
        _FLASHCARDS_PATH.parent.mkdir(exist_ok=True)
        if not _FLASHCARDS_PATH.exists():
            self._write({"decks": []})

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _read(self) -> dict:
        try:
            return json.loads(_FLASHCARDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Erreur lecture flashcards.json")
            return {"decks": []}

    def _write(self, data: dict) -> None:
        try:
            _FLASHCARDS_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.exception("Erreur écriture flashcards.json")

    def _today(self) -> str:
        return datetime.now().date().isoformat()

    def _n_dues(self, deck: dict) -> int:
        today = self._today()
        return sum(
            1 for c in deck.get("cartes", [])
            if (c.get("prochaine_révision") or "0000-00-00") <= today
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_decks(self) -> list:
        result = []
        for d in self._read().get("decks", []):
            entry = {k: v for k, v in d.items() if k != "cartes"}
            entry["n_cartes"] = len(d.get("cartes", []))
            entry["n_dues"] = self._n_dues(d)
            result.append(entry)
        return result

    def get_deck(self, deck_id: str) -> Optional[dict]:
        for d in self._read().get("decks", []):
            if d["id"] == deck_id:
                return d
        return None

    def create_deck(self, nom: str, source: str, cartes: list) -> str:
        data = self._read()
        today = self._today()
        deck_id = str(uuid.uuid4())
        data.setdefault("decks", []).append({
            "id": deck_id,
            "nom": nom,
            "source": source,
            "créé_le": today,
            "cartes": [
                {
                    "id": str(uuid.uuid4()),
                    "question": c["question"],
                    "réponse": c["réponse"],
                    "niveau": 0,
                    "dernière_révision": None,
                    "prochaine_révision": today,
                    "historique": [],
                }
                for c in cartes
            ],
        })
        self._write(data)
        return deck_id

    def delete_deck(self, deck_id: str) -> bool:
        data = self._read()
        before = len(data.get("decks", []))
        data["decks"] = [d for d in data.get("decks", []) if d["id"] != deck_id]
        if len(data["decks"]) < before:
            self._write(data)
            return True
        return False

    def update_carte(self, deck_id: str, carte_id: str, resultat: str) -> Optional[dict]:
        data = self._read()
        today = self._today()
        for deck in data.get("decks", []):
            if deck["id"] != deck_id:
                continue
            for carte in deck.get("cartes", []):
                if carte["id"] != carte_id:
                    continue
                if resultat == "su":
                    carte["niveau"] = min(carte.get("niveau", 0) + 1, _MAX_LEVEL)
                else:
                    carte["niveau"] = 0
                interval = _INTERVALS[carte["niveau"]]
                next_date = (datetime.now().date() + timedelta(days=interval)).isoformat()
                carte["dernière_révision"] = today
                carte["prochaine_révision"] = next_date
                carte.setdefault("historique", []).append({
                    "date": today,
                    "resultat": resultat,
                    "niveau": carte["niveau"],
                })
                self._write(data)
                return {"niveau": carte["niveau"], "prochaine_révision": next_date}
        return None

    def get_due(self) -> list:
        today = self._today()
        due = []
        for deck in self._read().get("decks", []):
            for carte in deck.get("cartes", []):
                if (carte.get("prochaine_révision") or "0000-00-00") <= today:
                    due.append({
                        **carte,
                        "deck_id": deck["id"],
                        "deck_nom": deck["nom"],
                    })
        return due
