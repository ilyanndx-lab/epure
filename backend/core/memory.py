import concurrent.futures
import json
import logging
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.jsonstore import read_json, transaction, write_json

logger = logging.getLogger(__name__)

_MEMORY_DIR = Path(__file__).parent.parent / "memory"

_PROFILE_DEFAULT = {
    "identité": {"niveau": "PTSI2", "établissement": "", "objectif": ""},
    "préférences_interaction": {
        "style": "direct, sans reformulation inutile",
        "ne_pas_faire": ["répéter la question", "sur-expliquer les bases"],
    },
    "forces": [],
    "lacunes_confirmées": [],
}
_SESSIONS_DEFAULT = {"sessions": []}
_CONTEXT_DEFAULT = {
    "fichiers_actifs": [],
    "résumé_contexte": "",
    "modèle_actif": "qwen2.5:7b",
    "strict_mode": False,
    "session_instruction": "",
}

_VALID_SECTIONS = {"lacunes", "forces", "style", "sessions_récentes", "aucune"}
_CACHE_MAXSIZE = 20
_context_cache: OrderedDict = OrderedDict()


def _cache_get(key: str):
    if key not in _context_cache:
        return None
    _context_cache.move_to_end(key)
    return _context_cache[key]


def _cache_set(key: str, value: list) -> None:
    if key in _context_cache:
        _context_cache.move_to_end(key)
    else:
        if len(_context_cache) >= _CACHE_MAXSIZE:
            _context_cache.popitem(last=False)
    _context_cache[key] = value


class MemoryEngine:
    def __init__(self, llm=None):
        self._llm = llm
        _MEMORY_DIR.mkdir(exist_ok=True)
        self._profile_path = _MEMORY_DIR / "profile.json"
        self._sessions_path = _MEMORY_DIR / "memory_sessions.json"
        self._context_path = _MEMORY_DIR / "context_session.json"

        if not self._profile_path.exists():
            self._write(self._profile_path, _PROFILE_DEFAULT)
        if not self._sessions_path.exists():
            self._write(self._sessions_path, _SESSIONS_DEFAULT)
        # Always reset context on startup
        self._write(self._context_path, _CONTEXT_DEFAULT)

    # ── I/O helpers ────────────────────────────────────────────────────────

    def _read(self, path: Path) -> dict:
        return read_json(path, {})

    def _write(self, path: Path, data: dict) -> None:
        try:
            write_json(path, data)
        except Exception:
            logger.exception("Erreur écriture %s", path)

    # ── Profile ────────────────────────────────────────────────────────────

    def load_profile(self) -> dict:
        return self._read(self._profile_path)

    def save_profile(self, data: dict) -> None:
        self._write(self._profile_path, data)

    def profile_transaction(self):
        """RMW verrouillé du profil, pour les appelants qui chargent, modifient
        et réécrivent.

        Nécessaire parce que ce couple load/save est effectué DEPUIS L'EXTÉRIEUR
        (ConsolidationEngine.apply_consolidation), lancé dans des Thread explicites
        par les modules kholle et chat : deux consolidations simultanées
        chargeaient le même profil et la seconde écrasait les lacunes ajoutées par
        la première. Un verrou dans `save_profile` n'y changerait rien — la course
        est entre le load et le save.
        """
        return transaction(self._profile_path, {})

    # ── Sessions ───────────────────────────────────────────────────────────

    def get_all_sessions(self) -> list:
        return self._read(self._sessions_path).get("sessions", [])

    def get_sessions(self, days: int = 7) -> list:
        if days <= 0:
            return self.get_all_sessions()
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        return [
            s for s in self.get_all_sessions()
            if not s.get("archivée") and s.get("date", "") >= cutoff
        ]

    def add_session(
        self,
        matière: str,
        fichier: str,
        erreurs: list,
        réussies: int,
        ratées: int,
    ) -> None:
        with transaction(self._sessions_path, {"sessions": []}) as data:
            data.setdefault("sessions", []).append({
                "date": datetime.now().date().isoformat(),
                "matière": matière,
                "fichier": fichier,
                "erreurs": erreurs,
                "réussies": réussies,
                "ratées": ratées,
                "archivée": False,
            })

    def archive_sessions(self, dates: list) -> None:
        with transaction(self._sessions_path, {"sessions": []}) as data:
            for s in data.get("sessions", []):
                if s.get("date") in dates:
                    s["archivée"] = True

    def archive_old_sessions(self, days: int = 30) -> None:
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        with transaction(self._sessions_path, {"sessions": []}) as data:
            for s in data.get("sessions", []):
                if s.get("date", "") < cutoff:
                    s["archivée"] = True

    def promote_lacunes(self) -> None:
        recent = self.get_sessions(days=30)
        counts: dict[str, int] = {}
        for s in recent:
            for err in s.get("erreurs", []):
                counts[err] = counts.get(err, 0) + 1

        with self.profile_transaction() as profile:
            lacunes = set(profile.get("lacunes_confirmées", []))
            changed = False
            for err, n in counts.items():
                if n >= 3 and err not in lacunes:
                    lacunes.add(err)
                    changed = True
            if changed:
                profile["lacunes_confirmées"] = sorted(lacunes)

    # ── Context session ────────────────────────────────────────────────────

    def get_context(self) -> dict:
        return self._read(self._context_path)

    def update_context(self, **kwargs) -> None:
        # Le site le plus chaud du dépôt : appelé à chaque message (modèle actif,
        # fichiers actifs, résumé de contexte) depuis le pool de threads.
        with transaction(self._context_path, {}) as data:
            data.update(kwargs)

    # ── Selective memory retrieval ─────────────────────────────────────────

    def _available_sections(self) -> list[str]:
        """Return which sections actually have data (avoids LLM hallucinating missing sections)."""
        available: list[str] = []
        profile = self.load_profile()
        if profile.get("lacunes_confirmées"):
            available.append("lacunes")
        if profile.get("forces"):
            available.append("forces")
        prefs = profile.get("préférences_interaction", {})
        if prefs.get("style") or prefs.get("ne_pas_faire"):
            available.append("style")
        recent = self.get_sessions(days=7)
        if any(s.get("erreurs") for s in recent):
            available.append("sessions_récentes")
        return available

    def retrieve_relevant_context(self, message: str) -> list[str]:
        """
        Ask the local LLM (timeout 2s) which profile sections are relevant.
        Returns section names to inject. Falls back to ['style'] on any failure.
        """
        # Short-circuit: very short messages never need profile context
        if len(message.strip()) < 20:
            logger.debug("Memory retrieve: message court (%d chars) → aucune section", len(message.strip()))
            return []

        cache_key = message[:100]
        cached = _cache_get(cache_key)
        if cached is not None:
            logger.debug("Memory retrieve: cache hit → %s", cached)
            return cached

        available = self._available_sections()
        if not available:
            _cache_set(cache_key, [])
            return []

        if not self._llm:
            # No LLM available — inject everything (legacy)
            _cache_set(cache_key, available)
            return available

        sections_str = ", ".join(available)
        prompt = (
            f"Message : {message[:200]}\n"
            f"Sections de profil disponibles : {sections_str}\n"
            "Quelles sections sont pertinentes pour adapter la réponse à ce message ?\n"
            'Réponds UNIQUEMENT avec une liste JSON, par exemple : ["lacunes", "style"] ou ["aucune"]'
        )

        fallback = ["style"] if "style" in available else []

        def _call():
            return self._llm.generate([{"role": "user", "content": prompt}])

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_call)
        try:
            raw = future.result(timeout=2.0)
            match = re.search(r'\[.*?\]', raw, re.DOTALL)
            if not match:
                raise ValueError(f"Pas de JSON array dans la réponse: {raw[:80]!r}")
            sections = json.loads(match.group())
            if not isinstance(sections, list):
                raise ValueError("La réponse n'est pas une liste")
            result = [s for s in sections if s in _VALID_SECTIONS and s != "aucune"]
            # Only keep sections that actually have data
            result = [s for s in result if s in available]
            _cache_set(cache_key, result)
            estimated = len(" ".join(result))
            logger.info("Memory retrieve: %r → %s (~%d chars clés)", message[:40], result, estimated)
            return result
        except concurrent.futures.TimeoutError:
            logger.info("Memory retrieve: timeout (>2s) → fallback %s", fallback)
            _cache_set(cache_key, fallback)
            return fallback
        except Exception:
            logger.exception("Memory retrieve: erreur → fallback %s", fallback)
            _cache_set(cache_key, fallback)
            return fallback
        finally:
            executor.shutdown(wait=False)

    # ── System prompt builder ──────────────────────────────────────────────

    def build_system_context(self, message: Optional[str] = None) -> str:
        """
        Build the system context string.
        If message is provided: selectively inject only relevant profile sections.
        If message is None: inject all (legacy behaviour, used by @mémoire skill).
        Active context (résumé, instruction, strict_mode) is always included.
        """
        profile = self.load_profile()
        prefs = profile.get("préférences_interaction", {})
        style = prefs.get("style", "")
        ne_pas = prefs.get("ne_pas_faire", [])
        forces = profile.get("forces", [])
        lacunes = profile.get("lacunes_confirmées", [])
        recent = self.get_sessions(days=7)
        ctx = self.get_context()

        # Decide which profile sections to include
        if message is not None:
            sections = self.retrieve_relevant_context(message)
            include_style = "style" in sections
            include_lacunes = "lacunes" in sections
            include_forces = "forces" in sections
            include_sessions = "sessions_récentes" in sections
        else:
            # Legacy: inject everything
            include_style = bool(style or ne_pas)
            include_lacunes = bool(lacunes)
            include_forces = bool(forces)
            include_sessions = True

        parts: list[str] = []

        # Profile block (selective)
        profile_lines: list[str] = []
        if include_style and (style or ne_pas):
            if style:
                profile_lines.append(f"Style attendu : {style}")
            if ne_pas:
                profile_lines.append("À éviter : " + ", ".join(ne_pas))
        if include_forces and forces:
            profile_lines.append("Points forts : " + " ; ".join(forces[:5]))
        if include_lacunes and lacunes:
            profile_lines.append("Lacunes confirmées : " + " ; ".join(lacunes))
        if profile_lines:
            parts.append("[PROFIL ÉLÈVE]\n" + "\n".join(profile_lines))

        # Recent errors (selective)
        if include_sessions and recent:
            errors: list[str] = []
            for s in recent[-5:]:
                for e in s.get("erreurs", []):
                    errors.append(f"- {e} ({s.get('date', '')})")
            if errors:
                parts.append("[ERREURS RÉCENTES]\n" + "\n".join(errors))

        # Active file context — always injected
        résumé = ctx.get("résumé_contexte", "")
        if résumé:
            parts.append(f"[CONTEXTE ACTIF]\n{résumé}")

        # Session instruction — always injected
        instruction = ctx.get("session_instruction", "")
        if instruction:
            parts.append(f"[INSTRUCTION DE SESSION]\n{instruction}")

        # Strict mode — always injected
        if ctx.get("strict_mode"):
            parts.append(
                "[MODE STRICT]\n"
                "Réponds de façon concise et directe. "
                "Pas d'introduction ni de reformulation inutile."
            )

        result = "\n\n".join(parts)
        est_tokens = len(result.split())
        if message is not None:
            used = []
            if include_style: used.append("style")
            if include_lacunes: used.append("lacunes")
            if include_forces: used.append("forces")
            if include_sessions: used.append("sessions_récentes")
            logger.info("Memory context: %r → sections=%s ~%d tokens", message[:40], used, est_tokens)
        else:
            logger.info("Memory context: legacy → ~%d tokens", est_tokens)
        return result
