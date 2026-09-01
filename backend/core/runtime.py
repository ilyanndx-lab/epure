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
# WhisperEngine (faster-whisper) valide son cache auprès de huggingface.co À
# CHAQUE démarrage. Si HF est
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
    # Le modèle d'EMBEDDING n'est plus dans cette liste depuis le 2026-08-26 : il
    # ne passe plus par `huggingface_hub` du tout. `core/embedding_install.py` le
    # télécharge par `urllib` et le vérifie par sha256 (idiome de
    # `core/voice.py`), donc ni `HF_HUB_OFFLINE` ni ce cache ne le concernent.
    # Le laisser ici aurait un effet précis et faux : sur une instance qui a le
    # cache Whisper mais pas celui de MiniLM, on resterait EN LIGNE — donc on
    # réintroduirait le démarrage bloqué que cette fonction existe pour éviter,
    # au nom d'un modèle qui n'y est plus.
    needed = (f"models--Systran--faster-whisper-{whisper_size}",)
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
from core.embedding_install import (
    fichiers_manquants as fichiers_embedding_manquants,
    pile_presente as pile_embedding_presente,
)
from core.flashcards import FlashcardsEngine
from core.history import HistoryEngine
from core.instance import est_modele_cloud, fiches_watch_paths, modele_local_defaut
from core.llm import LLMEngine
from core.memory import MemoryEngine
from core.models import ModelsRegistry
from core.orchestrator import OrchestratorEngine
from core.quota_tracker import QuotaTracker
from core.paths import resolve_vector_dir
from core.rag import RAGEngine
from core.vector_store import VectorStore
from core.voice import PiperEngine, WhisperEngine

logger = logging.getLogger(__name__)

# ── Configuration technique (config.yaml) ────────────────────────────────────
with open(Path(__file__).parent.parent / "config.yaml") as _f:
    cfg = yaml.safe_load(_f)

class _LazyEngine:
    """Proxy de chargement paresseux : l'engine réel (et son modèle, lourd à
    charger en RAM) n'est construit qu'au PREMIER accès à une de ses méthodes.
    Évite de bloquer le démarrage sur des moteurs coûteux (RAG → modèle
    d'embedding, dont 90 Mo à télécharger au premier usage ; voix optionnelle).
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

# RAG = le moteur le plus lourd au démarrage, et il reste paresseux même après le
# remplacement de la pile d'embedding (2026-08-26). Le coût a chuté d'un ordre de
# grandeur — import 15,1 s → 0,37 s, chargement du modèle 4,7 s → 0,38 s, mesuré
# — mais la paresse ne tenait pas qu'à la durée : construire ce moteur peut lever
# `EmbeddingIndisponible` et lancer le téléchargement des 90 Mo du modèle. Le
# faire à l'import de ce module, c'est-à-dire au démarrage d'uvicorn, tirerait ces
# 90 Mo sur la connexion du destinataire avant qu'il ait ouvert quoi que ce soit.
# Un thread de préchauffage le construit en tâche de fond pendant qu'uvicorn sert
# déjà — et seulement si le modèle est DÉJÀ là (cf. `_warmup`).
# Store vectoriel partagé par les TROIS collections (`fiches`, `doc_analysis`,
# `history`). C'est lui qui porte le modèle d'embedding, donc lui qui coûte les
# 30 s à 2 min de chargement — d'où le `_LazyEngine` : le construire à l'import
# rendrait la paresse de `rag` sans objet, puisque le poids a simplement changé
# de propriétaire en quittant chromadb.
#
# Ce qui change vraiment ici : jusqu'à présent `docanalysis` et `history_engine`
# récupéraient le client et la fonction d'embedding dans `rag._client`/`rag._ef`,
# deux attributs PRIVÉS de `RAGEngine`. Le partage existait donc déjà, mais sous
# une forme que rien ne signalait — et brancher un nouveau stockage sur
# `core/rag.py` seul aurait laissé les deux autres sur chromadb sans que rien ne
# proteste. Le store est désormais un objet nommé, passé explicitement aux trois.
vector_store = _LazyEngine(
    lambda: VectorStore(resolve_vector_dir()),
    "Store vectoriel (embeddings, ONNX Runtime)",
)
rag = _LazyEngine(
    lambda: RAGEngine(store=vector_store, llm=llm),
    "RAG (embeddings, ONNX Runtime)",
)
# docanalysis/history partagent le même store → paresseux eux aussi (sinon ils
# forceraient son chargement, donc celui du modèle, à l'import).
docanalysis = _LazyEngine(
    lambda: DocAnalysisEngine(store=vector_store, llm=llm),
    "Analyse de documents",
)
admin_engine = AdminEngine(llm, rag)  # ne stocke que la référence (proxy)
history_engine = _LazyEngine(
    lambda: HistoryEngine(llm, vector_store),
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
    """Modèle de l'étape de réflexion de l'agent de code. **Local par défaut.**

    Elle rendait un modèle CLOUD dès qu'une clé traînait dans l'environnement —
    Gemini, sinon Groq — pour une étape qui part **automatiquement** avant chaque
    demande de code jugée telle par `_is_code_request`. Personne ne l'avait
    choisie : c'était le cas le plus net du lot avec `classify_task`.

    Ce qui est conservé, et c'est le seul cas de cloud restant ici :
    `preferred`, quand l'appelant a nommé un modèle distant. C'est un choix
    explicite pour cette tâche — exactement ce que la règle autorise. En pratique
    il vient du `pipeline.reflection.model` de l'écran Code, donc d'un réglage
    visible.

    Le repli n'est plus `None` : `None` désactivait l'étape entière côté
    `CodeAgent.run_turn` (`if ref_enabled and eff_ref_model`). Sur une machine
    sans aucune clé, la réflexion ne tournait donc **jamais** — une capacité
    silencieusement absente, alors qu'un modèle local en est parfaitement capable.

    À noter, mesuré le 2026-08-24 : `groq:llama-3.3-70b-versatile`, l'ancien repli
    Groq, répond **404** (retiré du catalogue). Cette branche était donc morte en
    plus d'être non choisie.
    """
    if preferred and est_modele_cloud(preferred):
        return preferred
    return modele_local_defaut()


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
    """Préchauffage en tâche de fond : construit le RAG (embeddings) puis
    met les dossiers de fiches sous surveillance. Lancé dès l'import mais SANS
    bloquer : uvicorn peut binder et répondre (/health, etc.) pendant ce temps.
    La 1re requête RAG ne paie alors pas le coût de chargement si le warmup a fini.

    **Ne préchauffe PAS quand la pile d'embedding est absente**, et cette
    condition n'est pas une optimisation. Depuis que `VectorStore.__init__`
    déclenche la mise à disposition du modèle au lieu de lever
    (`core/embedding_install.py`), résoudre le RAG ici lancerait le
    téléchargement du modèle au DÉMARRAGE, sur la connexion du destinataire d'un
    paquet, avant qu'il ait ouvert quoi que ce soit — y compris s'il ne se sert
    jamais de la recherche documentaire. Le volume a changé avec la pile (90 Mo
    de poids ONNX au lieu de ~2 Go de wheels torch), la décision non : on ne
    télécharge rien au boot. L'installation part au premier appel qui a
    réellement besoin du moteur (`GET /rag/files` à l'ouverture d'un module qui
    offre le contexte documentaire), jamais au boot.

    `apply_fiches_watch()` reste appelé dans les deux cas : la surveillance des
    dossiers de fiches passe par `rag.watch`, qui résoudra le proxy et déclenchera
    l'installation — mais seulement si l'instance a réellement des dossiers
    configurés, ce qui est déjà un usage revendiqué de la recherche documentaire.
    """
    try:
        if pile_embedding_presente():
            rag._resolve()
        else:
            logger.info(
                "Modèle d'embedding absent (%s) — pas de préchauffage RAG : le "
                "téléchargement partira au premier usage réel de la recherche "
                "documentaire, pas au démarrage.",
                ", ".join(fichiers_embedding_manquants()) or "runtime ONNX absent",
            )
    except Exception:
        logger.exception("Préchauffage RAG échoué")
    apply_fiches_watch()


threading.Thread(target=_warmup, daemon=True, name="epure-warmup").start()


# ── Utilitaires partagés ─────────────────────────────────────────────────────
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
API_KEY_NAMES = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"]
