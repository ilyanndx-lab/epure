"""État partagé d'Épure : moteurs (singletons) et helpers transverses.

Centralise les instances créées au démarrage (LLM, RAG, mémoire, etc.) pour
qu'elles soient injectables dans les routeurs de modules
(``modules/<id>/router.py``) sans dépendance circulaire vers ``main``. Les
*classes* des moteurs restent dans ``core/*.py`` ; ce module ne fait que les
instancier une fois et exposer des utilitaires communs.

L'import de ce module a des effets de bord volontaires (chargement des modèles,
reset du contexte de session, démarrage de la surveillance des fiches),
identiques à l'ancien bloc d'init de ``main.py``.
"""

import logging
import os
from datetime import date as _date
from pathlib import Path
from typing import Optional

import yaml

from core.admin import AdminEngine
from core.codeagent import CodeAgent, WORKSPACE as _CODE_WORKSPACE
from core.consolidation import ConsolidationEngine
from core.docanalysis import DocAnalysisEngine
from core.flashcards import FlashcardsEngine
from core.history import HistoryEngine
from core.instance import fiches_watch_paths
from core.llm import LLMEngine
from core.memory import MemoryEngine
from core.models import ModelsRegistry
from core.orchestrator import OrchestratorEngine
from core.quota_tracker import QuotaTracker
from core.rag import RAGEngine
from core.voice import PiperEngine, WhisperEngine

logger = logging.getLogger(__name__)

# ── Configuration technique (config.yaml) ────────────────────────────────────
with open(Path(__file__).parent.parent / "config.yaml") as _f:
    cfg = yaml.safe_load(_f)

# ── Moteurs partagés (ordre significatif : dépendances entre moteurs) ────────
llm = LLMEngine()
rag = RAGEngine()
memory = MemoryEngine(llm=llm)  # resets context_session on startup
docanalysis = DocAnalysisEngine(chroma_client=rag._client, embedding_function=rag._ef, llm=llm)
code_agent = CodeAgent(llm=llm)
_CODE_WORKSPACE.mkdir(parents=True, exist_ok=True)

flashcards_engine = FlashcardsEngine()
admin_engine = AdminEngine(llm, rag)
models_registry = ModelsRegistry()
history_engine = HistoryEngine(llm, rag._client, rag._ef)
consolidation_engine = ConsolidationEngine(llm, memory, history_engine)
orchestrator = OrchestratorEngine(llm)

_voice_cfg = cfg.get("voice", {})
whisper = WhisperEngine(
    model_size=_voice_cfg.get("whisper_model", "small"),
    language=_voice_cfg.get("language", "fr"),
)
piper = PiperEngine(
    voice=_voice_cfg.get("piper_voice", "fr_FR-upmc-medium"),
)


# ── QuotaTracker session (reset quotidien) ───────────────────────────────────

class SessionQuotaTracker:
    _STEPS = ("reflection", "generation", "verification", "tests", "other")

    def __init__(self):
        self._day = str(_date.today())
        self._reset()

    def _check_day(self):
        today = str(_date.today())
        if today != self._day:
            self._day = today
            self._reset()

    def _reset(self):
        self.total = 0
        self.by_provider: dict[str, int] = {}
        self.by_step: dict[str, int] = {s: 0 for s in self._STEPS}

    def track(self, step: str, provider: str, tokens: int):
        self._check_day()
        self.total += tokens
        self.by_provider[provider] = self.by_provider.get(provider, 0) + tokens
        key = step if step in self.by_step else "other"
        self.by_step[key] += tokens

    def get_usage(self) -> dict:
        self._check_day()
        return {
            "session": {"total_tokens": self.total, "providers": dict(self.by_provider)},
            "session_tokens_by_step": dict(self.by_step),
        }

    def reset(self):
        self._reset()


quota_tracker = SessionQuotaTracker()

# Usage persistant par provider (tokens + requêtes) — backend/memory/quota_usage.json
usage_tracker = QuotaTracker()


def provider_of(model: Optional[str]) -> str:
    if not model:
        return "local"
    if ":" in model:
        prefix = model.split(":", 1)[0]
        return prefix if prefix not in ("ollama",) else "local"
    return "local"


def pick_reflection_model(preferred: Optional[str]) -> Optional[str]:
    """Retourne un modèle cloud pour la réflexion, ou None si aucun disponible."""
    if preferred and ":" in preferred and not preferred.startswith("ollama:"):
        return preferred
    if os.environ.get("GEMINI_API_KEY", "").strip():
        return "gemini:gemini-2.0-flash"
    if os.environ.get("GROQ_API_KEY", "").strip():
        return "groq:llama-3.3-70b-versatile"
    return None


# ── Surveillance des dossiers de fiches (config d'instance) ──────────────────
_watched_folders: set[str] = set()


def apply_fiches_watch() -> None:
    for _p in fiches_watch_paths():
        _sp = str(_p)
        if _sp in _watched_folders:
            continue
        try:
            _p.mkdir(parents=True, exist_ok=True)
            rag.watch(_sp)
            _watched_folders.add(_sp)
        except Exception:
            logger.exception("Erreur surveillance dossier fiches %s", _sp)


apply_fiches_watch()


# ── Utilitaires partagés ─────────────────────────────────────────────────────
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
API_KEY_NAMES = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"]
