import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

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


class MemoryEngine:
    def __init__(self):
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
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Erreur lecture %s", path)
            return {}

    def _write(self, path: Path, data: dict) -> None:
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.exception("Erreur écriture %s", path)

    # ── Profile ────────────────────────────────────────────────────────────

    def load_profile(self) -> dict:
        return self._read(self._profile_path)

    def save_profile(self, data: dict) -> None:
        self._write(self._profile_path, data)

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
        data = self._read(self._sessions_path)
        data.setdefault("sessions", []).append({
            "date": datetime.now().date().isoformat(),
            "matière": matière,
            "fichier": fichier,
            "erreurs": erreurs,
            "réussies": réussies,
            "ratées": ratées,
            "archivée": False,
        })
        self._write(self._sessions_path, data)

    def archive_sessions(self, dates: list) -> None:
        data = self._read(self._sessions_path)
        for s in data.get("sessions", []):
            if s.get("date") in dates:
                s["archivée"] = True
        self._write(self._sessions_path, data)

    def archive_old_sessions(self, days: int = 30) -> None:
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        data = self._read(self._sessions_path)
        for s in data.get("sessions", []):
            if s.get("date", "") < cutoff:
                s["archivée"] = True
        self._write(self._sessions_path, data)

    def promote_lacunes(self) -> None:
        """Promote errors appearing in 3+ recent sessions to lacunes_confirmées."""
        recent = self.get_sessions(days=30)
        counts: dict[str, int] = {}
        for s in recent:
            for err in s.get("erreurs", []):
                counts[err] = counts.get(err, 0) + 1

        profile = self.load_profile()
        lacunes = set(profile.get("lacunes_confirmées", []))
        changed = False
        for err, n in counts.items():
            if n >= 3 and err not in lacunes:
                lacunes.add(err)
                changed = True
        if changed:
            profile["lacunes_confirmées"] = sorted(lacunes)
            self.save_profile(profile)

    # ── Context session ────────────────────────────────────────────────────

    def get_context(self) -> dict:
        return self._read(self._context_path)

    def update_context(self, **kwargs) -> None:
        data = self._read(self._context_path)
        data.update(kwargs)
        self._write(self._context_path, data)

    # ── System prompt builder ──────────────────────────────────────────────

    def build_system_context(self) -> str:
        parts: list[str] = []

        profile = self.load_profile()
        prefs = profile.get("préférences_interaction", {})
        style = prefs.get("style", "")
        ne_pas = prefs.get("ne_pas_faire", [])
        lacunes = profile.get("lacunes_confirmées", [])

        if style or ne_pas or lacunes:
            lines = ["[PROFIL ÉLÈVE]"]
            if style:
                lines.append(f"Style attendu : {style}")
            if ne_pas:
                lines.append("À éviter : " + ", ".join(ne_pas))
            if lacunes:
                lines.append("Lacunes confirmées : " + " ; ".join(lacunes))
            parts.append("\n".join(lines))

        recent = self.get_sessions(days=7)
        if recent:
            errors: list[str] = []
            for s in recent[-5:]:
                for e in s.get("erreurs", []):
                    errors.append(f"- {e} ({s.get('date', '')})")
            if errors:
                parts.append("[ERREURS RÉCENTES]\n" + "\n".join(errors))

        ctx = self.get_context()
        résumé = ctx.get("résumé_contexte", "")
        if résumé:
            parts.append(f"[CONTEXTE ACTIF]\n{résumé}")

        instruction = ctx.get("session_instruction", "")
        if instruction:
            parts.append(f"[INSTRUCTION DE SESSION]\n{instruction}")

        if ctx.get("strict_mode"):
            parts.append(
                "[MODE STRICT]\n"
                "Réponds de façon concise et directe. "
                "Pas d'introduction ni de reformulation inutile."
            )

        return "\n\n".join(parts)
