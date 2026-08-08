import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.jsonstore import read_json, transaction
from core.paths import resolve_data_dir

logger = logging.getLogger(__name__)


# Fonction et non constante : cf. core.paths.resolve_data_dir.
def _log_path() -> Path:
    return resolve_data_dir() / "consolidation_log.json"


_CLOUD_MODEL = "groq:llama-3.3-70b-versatile"
_CTX_LIMIT = 12000  # chars fed to LLM for conversation consolidation


class ConsolidationEngine:
    def __init__(self, llm, memory, history_engine):
        self._llm = llm
        self._memory = memory
        self._history = history_engine

    # ── Log ──────────────────────────────────────────────────────────────────

    def _load_log(self) -> list:
        return read_json(_log_path(), {}).get("log", [])

    def _append_log(self, entry: dict) -> None:
        with transaction(_log_path(), {"log": []}) as doc:
            log = doc.setdefault("log", [])
            log.insert(0, entry)
            del log[50:]   # en place : `log = log[:50]` ne persisterait rien

    def get_log(self, n: int = 20) -> list:
        return self._load_log()[:n]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pick_model(self, use_cloud: bool) -> Optional[str]:
        if not use_cloud:
            return None
        import os
        if not os.environ.get("GROQ_API_KEY", "").strip():
            logger.warning("GROQ_API_KEY absent — fallback local pour consolidation")
            return None
        return _CLOUD_MODEL

    def _parse_json(self, raw: str) -> dict:
        cleaned = re.sub(r'```(?:json)?\s*|\s*```', '', raw).strip()
        match = re.search(r'\{[\s\S]*\}', cleaned)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _dedup_add(existing: list, new_items: list) -> tuple[list, list]:
        lower_set = {x.lower() for x in existing}
        added = []
        for item in new_items:
            item = (item or "").strip()
            if item and item.lower() not in lower_set:
                existing.append(item)
                lower_set.add(item.lower())
                added.append(item)
        return existing, added

    # ── Apply ─────────────────────────────────────────────────────────────────

    def apply_consolidation(
        self, observations: dict, source_type: str = "auto", source: str = ""
    ) -> dict:
        if not observations:
            return {}

        changes: dict = {"lacunes_ajoutées": [], "forces_ajoutées": [], "style_màj": ""}

        # Lecture ET écriture du profil sous le même verrou : ce couple
        # load/modifie/save tourne dans des Thread lancés par kholle et chat, donc
        # deux consolidations concurrentes partaient du même profil et la seconde
        # écrasait les lacunes ajoutées par la première.
        try:
            with self._memory.profile_transaction() as profile:
                lacunes = profile.get("lacunes_confirmées", [])
                lacunes, added = self._dedup_add(
                    lacunes, observations.get("lacunes_confirmées", [])
                )
                profile["lacunes_confirmées"] = lacunes
                changes["lacunes_ajoutées"] = added

                forces = profile.get("forces", [])
                new_forces = (
                    observations.get("forces_détectées", [])
                    or observations.get("points_forts", [])
                )
                forces, added_f = self._dedup_add(forces, new_forces)
                profile["forces"] = forces
                changes["forces_ajoutées"] = added_f

                style = (observations.get("style_préféré") or "").strip()
                if style:
                    prefs = profile.get("préférences_interaction", {})
                    if prefs.get("style", "").lower() != style.lower():
                        prefs["style"] = style
                        profile["préférences_interaction"] = prefs
                        changes["style_màj"] = style
        except Exception:
            logger.exception("Erreur mise à jour du profil pour consolidation")
            return {}

        if any(v for v in changes.values()):
            try:
                self._append_log({
                    "date": datetime.now().isoformat(),
                    "type": source_type,
                    "source": source,
                    **changes,
                })
            except Exception:
                logger.exception("Erreur log consolidation")

        return changes

    # ── Session consolidation ─────────────────────────────────────────────────

    def consolidate_session(self, session_data: dict, use_cloud: bool = False) -> dict:
        model = self._pick_model(use_cloud)
        try:
            profile = self._memory.load_profile()
        except Exception:
            profile = {}

        prompt = (
            "Tu es un expert pédagogique. Analyse cette session de révision et "
            "extrais des observations précises sur l'apprenant.\n\n"
            f"Session : {json.dumps(session_data, ensure_ascii=False)}\n"
            f"Profil actuel — forces : {profile.get('forces', [])}, "
            f"lacunes : {profile.get('lacunes_confirmées', [])}\n\n"
            "Génère UNIQUEMENT ce JSON valide (max 3 items par liste) :\n"
            '{"observations": ["..."], "lacunes_confirmées": ["..."], '
            '"forces_détectées": ["..."], "priorités_révision": ["..."]}'
        )

        try:
            raw = self._llm.generate([{"role": "user", "content": prompt}], model=model)
            obs = self._parse_json(raw)
        except Exception:
            logger.exception("Erreur LLM consolidate_session")
            return {}

        changes = self.apply_consolidation(obs, "session", session_data.get("matière", "kholle"))
        logger.info("Consolidation session : %s", changes)
        return changes

    # ── History consolidation ─────────────────────────────────────────────────

    def consolidate_history(self, conv_id: str, use_cloud: bool = False) -> dict:
        conv = self._history.get_conversation(conv_id)
        if not conv:
            return {}

        model = self._pick_model(use_cloud)
        text = "\n".join(
            f"[{m.get('role', 'user')}]: {m.get('content', '')}"
            for m in conv.get("messages", [])
        )[:_CTX_LIMIT]

        prompt = (
            "Analyse cette conversation entre un étudiant en prépa et son assistant IA. "
            "Extrais des observations utiles pour personnaliser les futures interactions.\n\n"
            f"Conversation :\n{text}\n\n"
            "Génère UNIQUEMENT ce JSON valide :\n"
            '{"sujets_abordés": ["..."], "style_préféré": "...", '
            '"difficultés_détectées": ["..."], "points_forts": ["..."]}'
        )

        try:
            raw = self._llm.generate([{"role": "user", "content": prompt}], model=model)
            obs = self._parse_json(raw)
        except Exception:
            logger.exception("Erreur LLM consolidate_history %s", conv_id)
            return {}

        mapped = {
            "lacunes_confirmées": obs.get("difficultés_détectées", []),
            "forces_détectées": obs.get("points_forts", []),
            "style_préféré": obs.get("style_préféré", ""),
        }
        changes = self.apply_consolidation(mapped, "conversation", conv.get("titre", conv_id))
        logger.info("Consolidation conversation %s : %s", conv_id, changes)
        return changes

    # ── Global consolidation ──────────────────────────────────────────────────

    def consolidate_all(self, use_cloud: bool = False) -> dict:
        model = self._pick_model(use_cloud)

        try:
            sessions = self._memory.get_all_sessions()[-30:]
        except Exception:
            sessions = []
        conversations = self._history.list_conversations(30)[:10]
        profile = self._memory.load_profile()

        prompt = (
            "Tu es un expert pédagogique. Génère une synthèse du profil d'un étudiant en prépa "
            "à partir de ses sessions de révision et de l'historique de ses conversations.\n\n"
            f"Sessions récentes : {json.dumps(sessions, ensure_ascii=False)[:3000]}\n"
            f"Conversations (titres) : {[c.get('titre', '') for c in conversations]}\n"
            f"Profil actuel — forces : {profile.get('forces', [])}, "
            f"lacunes : {profile.get('lacunes_confirmées', [])}\n\n"
            "Génère UNIQUEMENT ce JSON valide (max 5 items par liste) :\n"
            '{"lacunes_confirmées": ["..."], "forces_détectées": ["..."], '
            '"style_préféré": "...", "observations": ["..."]}'
        )

        try:
            raw = self._llm.generate([{"role": "user", "content": prompt}], model=model)
            obs = self._parse_json(raw)
        except Exception:
            logger.exception("Erreur LLM consolidate_all")
            return {"erreur": "Échec de la consolidation LLM"}

        changes = self.apply_consolidation(obs, "manuel", "consolidate_all")
        logger.info("Consolidation globale : %s", changes)
        return {
            "changements": changes,
            "modèle_utilisé": model or "local",
            "sessions_analysées": len(sessions),
            "conversations_analysées": len(conversations),
        }
