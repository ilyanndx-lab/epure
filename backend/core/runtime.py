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
import threading
from datetime import date as _date
from pathlib import Path
from typing import Optional

import yaml

# ── Démarrage hors-ligne quand les modèles HF sont déjà en cache ──────────────
# RAGEngine (embedding SentenceTransformer) et WhisperEngine (faster-whisper)
# valident leur cache auprès de huggingface.co À CHAQUE démarrage. Si HF est
# injoignable (getaddrinfo failed → 5 retries avec backoff PAR fichier), l'import
# de ce module se bloque plusieurs minutes et uvicorn ne répond à rien (écran
# « Chargement… » figé). Les poids étant déjà téléchargés, on bascule en mode
# hors-ligne : chargement direct depuis le cache, zéro réseau. Sur une machine
# vierge (cache absent) on reste en ligne pour le 1er téléchargement.
# IMPORTANT : doit être posé AVANT le 1er import de huggingface_hub (les imports
# core.* ci-dessous le tirent), car la constante HF_HUB_OFFLINE est figée à
# l'import. Surchargeable : EPURE_HF_OFFLINE=0 force le comportement en ligne.
def _hf_offline_if_cached() -> None:
    if os.environ.get("EPURE_HF_OFFLINE", "").strip() in ("0", "false", "no"):
        return
    hub = (os.environ.get("HF_HUB_CACHE")
           or (os.path.join(os.environ["HF_HOME"], "hub") if os.environ.get("HF_HOME")
               else os.path.join(Path.home(), ".cache", "huggingface", "hub")))
    try:
        _voice = (yaml.safe_load(open(Path(__file__).parent.parent / "config.yaml")) or {}).get("voice", {})
    except Exception:
        _voice = {}
    whisper_size = _voice.get("whisper_model", "small")
    needed = (
        "models--sentence-transformers--all-MiniLM-L6-v2",
        f"models--Systran--faster-whisper-{whisper_size}",
    )
    if all(os.path.isdir(os.path.join(hub, n)) for n in needed):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        logging.getLogger(__name__).info(
            "Modèles HF déjà en cache → démarrage hors-ligne (HF_HUB_OFFLINE=1, "
            "EPURE_HF_OFFLINE=0 pour revenir en ligne)."
        )

_hf_offline_if_cached()

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

class _LazyEngine:
    """Proxy de chargement paresseux : l'engine réel (et son modèle, lourd à
    charger en RAM) n'est construit qu'au PREMIER accès à une de ses méthodes.
    Évite de bloquer le démarrage sur des moteurs coûteux (RAG → torch +
    sentence-transformers ≈ 30 s à chaud, 2 min à froid ; voix optionnelle).
    Thread-safe : construit une fois. Un thread de préchauffage (cf. plus bas)
    les résout en tâche de fond pour que la 1re requête ne paie pas le coût.
    """

    def __init__(self, factory, label):
        self._factory = factory
        self._label = label
        self._engine = None
        self._lock = threading.Lock()

    def _resolve(self):
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    logger.info("Chargement paresseux : %s", self._label)
                    self._engine = self._factory()
        return self._engine

    def __getattr__(self, name):
        # Appelé uniquement quand l'attribut n'existe pas sur le proxy (donc pour
        # transcribe/query/… et les attributs internes _client/_ef/_model) →
        # construit l'engine réel et délègue.
        return getattr(self._resolve(), name)


# ── Moteurs partagés (ordre significatif : dépendances entre moteurs) ────────
# Légers (construction quasi instantanée) : créés tout de suite.
llm = LLMEngine()
memory = MemoryEngine(llm=llm)  # resets context_session on startup
code_agent = CodeAgent(llm=llm)
_CODE_WORKSPACE.mkdir(parents=True, exist_ok=True)
flashcards_engine = FlashcardsEngine()
models_registry = ModelsRegistry()
orchestrator = OrchestratorEngine(llm)

# RAG = seul moteur lourd au démarrage : sa construction importe torch +
# sentence-transformers et charge le modèle d'embedding (≈ 30 s à chaud, 2 min à
# froid). Le faire à l'import de ce module bloquait uvicorn (il ne répondait à
# RIEN, /health compris) tant que ce n'était pas fini → l'app restait figée sur
# « Chargement… ». On le rend paresseux ; un thread de préchauffage le construit
# en tâche de fond pendant qu'uvicorn sert déjà.
rag = _LazyEngine(RAGEngine, "RAG (embeddings, torch + sentence-transformers)")
# docanalysis/history accèdent à rag._client / rag._ef → eux aussi paresseux
# (sinon ils forceraient la construction de RAG à l'import).
docanalysis = _LazyEngine(
    lambda: DocAnalysisEngine(chroma_client=rag._client, embedding_function=rag._ef, llm=llm),
    "Analyse de documents",
)
admin_engine = AdminEngine(llm, rag)  # ne stocke que la référence (proxy)
history_engine = _LazyEngine(
    lambda: HistoryEngine(llm, rag._client, rag._ef),
    "Historique des conversations",
)
consolidation_engine = ConsolidationEngine(llm, memory, history_engine)

_voice_cfg = cfg.get("voice", {})
# Voix chargée à la 1re utilisation (transcribe/synthesize), pas au démarrage :
# faster-whisper + Piper coûtent plusieurs secondes de chargement modèle sinon
# payées à chaque boot alors que la voix est optionnelle.
whisper = _LazyEngine(
    lambda: WhisperEngine(
        model_size=_voice_cfg.get("whisper_model", "small"),
        language=_voice_cfg.get("language", "fr"),
    ),
    "Whisper (transcription vocale)",
)
#: Voix Piper active. Exposée parce que le modèle est téléchargé à la demande :
#: `GET /voice/model` doit pouvoir dire s'il est déjà là SANS construire le
#: moteur — le construire, c'est déclencher le téléchargement. Un routeur qui
#: relirait config.yaml de son côté ferait diverger les deux.
PIPER_VOICE = _voice_cfg.get("piper_voice", "fr_FR-upmc-medium")
piper = _LazyEngine(
    lambda: PiperEngine(voice=PIPER_VOICE),
    "Piper (synthèse vocale)",
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


def _warmup() -> None:
    """Préchauffage en tâche de fond : construit le RAG (torch + embeddings) puis
    met les dossiers de fiches sous surveillance. Lancé dès l'import mais SANS
    bloquer : uvicorn peut binder et répondre (/health, etc.) pendant ce temps.
    La 1re requête RAG ne paie alors pas le coût de chargement si le warmup a fini.
    """
    try:
        rag._resolve()
    except Exception:
        logger.exception("Préchauffage RAG échoué")
    apply_fiches_watch()


threading.Thread(target=_warmup, daemon=True, name="epure-warmup").start()


# ── Utilitaires partagés ─────────────────────────────────────────────────────
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
API_KEY_NAMES = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"]
