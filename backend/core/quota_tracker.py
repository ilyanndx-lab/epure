"""
Persistent per-provider usage tracking (tokens + requests).

Counters live in backend/memory/quota_usage.json, auto-reset when the
provider's period (day/month) rolls over, and are saved from a background
thread so tracking never blocks a streaming response.
"""
import copy
import logging
import threading
import time
from datetime import date
from pathlib import Path
from typing import Optional

from core.jsonstore import read_json, write_json

logger = logging.getLogger(__name__)

_USAGE_FILE = Path(__file__).parent.parent / "memory" / "quota_usage.json"

# Estimated free-tier limits. limit=None → unlimited (rate-limited only).
PROVIDER_LIMITS: dict[str, dict] = {
    "gemini":   {"limit": 1_500,         "type": "requests", "period": "day",   "label": "1500 req/jour"},
    "groq":     {"limit": 14_400,        "type": "requests", "period": "day",   "label": "14400 req/jour"},
    "cerebras": {"limit": 1_000_000,     "type": "tokens",   "period": "day",   "label": "1M tokens/jour"},
    "nvidia":   {"limit": None,          "type": "requests", "period": "day",   "label": "illimité (40 RPM)"},
    "mistral":  {"limit": 1_000_000_000, "type": "tokens",   "period": "month", "label": "~1B tokens/mois"},
}

# Providers that run locally — no quota to track
_LOCAL_PROVIDERS = {"local", "ollama", "flm"}


class QuotaTracker:
    def __init__(self, path: Path = _USAGE_FILE):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()
        # Single background writer: concurrent per-call save threads corrupt
        # the file on Windows (os.replace fails while another thread holds the
        # tmp file open). The event coalesces bursts into one write.
        self._dirty = threading.Event()
        threading.Thread(target=self._writer_loop, daemon=True).start()

    def _load(self) -> dict:
        data = read_json(self._path, {})
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _period_key(provider: str) -> str:
        period = PROVIDER_LIMITS.get(provider, {}).get("period", "month")
        today = date.today()
        return str(today) if period == "day" else f"{today.year}-{today.month:02d}"

    def _entry(self, provider: str) -> dict:
        """Counters for a provider, reset if the period has rolled over. Lock held."""
        entry = self._data.setdefault(provider, {
            "tokens_input": 0, "tokens_output": 0, "requests": 0,
            "reset_date": self._period_key(provider),
        })
        if entry.get("reset_date") != self._period_key(provider):
            entry.update(
                tokens_input=0, tokens_output=0, requests=0,
                reset_date=self._period_key(provider),
            )
        return entry

    def _save_async(self) -> None:
        self._dirty.set()

    def _writer_loop(self) -> None:
        while True:
            self._dirty.wait()
            time.sleep(0.1)  # debounce: coalesce rapid successive tracks
            self._dirty.clear()
            # Copie sous verrou, sérialisation dehors : write_json parcourt la
            # structure, et le faire sur self._data laisserait un track()
            # concurrent muter un dict en cours d'itération.
            with self._lock:
                snapshot = copy.deepcopy(self._data)
            try:
                # Le tmp+replace local a été retiré : write_json est désormais
                # atomique (jsonstore), donc un seul chemin d'écriture pour tous
                # les JSON de runtime.
                write_json(self._path, snapshot)
            except Exception:
                logger.exception("Erreur sauvegarde %s", self._path)

    # ── Public API ───────────────────────────────────────────────────────────

    def track(self, provider: str, prompt_tokens: int = 0, output_tokens: int = 0) -> None:
        if not provider or provider in _LOCAL_PROVIDERS:
            return
        try:
            with self._lock:
                entry = self._entry(provider)
                entry["tokens_input"] += int(prompt_tokens or 0)
                entry["tokens_output"] += int(output_tokens or 0)
                entry["requests"] += 1
                self._save_async()
        except Exception:
            logger.exception("Erreur track quota %s", provider)

    def get_usage(self) -> dict:
        """Per-provider counters with limits and computed percentages."""
        with self._lock:
            out: dict[str, dict] = {}
            providers = set(PROVIDER_LIMITS) | (set(self._data) - _LOCAL_PROVIDERS)
            for provider in sorted(providers):
                lim = PROVIDER_LIMITS.get(
                    provider,
                    {"limit": None, "type": "tokens", "period": "month", "label": "inconnu"},
                )
                entry = self._entry(provider)
                used = (
                    entry["requests"]
                    if lim["type"] == "requests"
                    else entry["tokens_input"] + entry["tokens_output"]
                )
                pct: Optional[float] = (
                    None if not lim["limit"]
                    else round(min(100.0, used / lim["limit"] * 100), 1)
                )
                out[provider] = {
                    **entry,
                    "limite": lim["limit"],
                    "type_limite": lim["type"],
                    "période": lim["period"],
                    "label_limite": lim["label"],
                    "utilisé": used,
                    "pourcentage": pct,
                }
            return out

    def reset(self, provider: str) -> bool:
        with self._lock:
            if provider not in self._data and provider not in PROVIDER_LIMITS:
                return False
            entry = self._entry(provider)
            entry.update(
                tokens_input=0, tokens_output=0, requests=0,
                reset_date=self._period_key(provider),
            )
            self._save_async()
            return True
