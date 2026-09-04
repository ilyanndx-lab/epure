"""
Dynamic model registry — fetches available LLM IDs from cloud providers,
enriches with qualitative metadata, caches 5 minutes.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import yaml

from core.llm import lmstudio_host as _lmstudio_host
from core.llm import ollama_host as _ollama_host

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"

# ── Qualitative metadata ─────────────────────────────────────────────────────

QUALITATIVE_METADATA: dict[str, dict] = {
    "openai/gpt-oss-120b": {
        "categorie": "puissant",
        "description": "Raisonnement fort · 500 tok/s",
        "usages": ["Kholle maths", "Kholle physique"],
    },
    "openai/gpt-oss-20b": {
        "categorie": "rapide",
        "description": "Rapide + raisonnement · 1000 tok/s",
        "usages": ["Chat rapide", "Flashcards"],
    },
    "nvidia/nemotron-3-super-120b-a12b": {
        "categorie": "puissant",
        "description": "Sciences & maths · NVIDIA",
        "usages": ["Kholle physique", "Kholle maths"],
    },
    "nvidia/llama-3.1-nemotron-nano-8b-v1": {
        "categorie": "rapide",
        "description": "Léger sciences · 48 tok/s",
        "usages": ["Chat rapide"],
    },
    "gemini-2.5-flash": {
        "categorie": "puissant",
        "description": "Usage général · Google",
        "usages": ["Flashcards", "Résumé"],
    },
    "gemini-2.5-flash-lite": {
        "categorie": "rapide",
        "description": "Rapide · Google",
        "usages": ["Chat rapide"],
    },
    "gemini-2.5-pro": {
        "categorie": "puissant",
        "description": "Raisonnement avancé · Google",
        "usages": ["Kholle maths", "Kholle physique"],
    },
    "gemini-3.1-flash-lite": {
        "categorie": "rapide",
        "description": "Nouveau · Google",
        "usages": ["Chat rapide"],
    },
    # NVIDIA NIM DeepSeek
    "deepseek-ai/deepseek-r1": {
        "categorie": "puissant",
        "description": "Raisonnement · NVIDIA NIM",
        "usages": ["Kholle maths", "Kholle physique"],
    },
    "deepseek-ai/deepseek-v4-flash": {
        "categorie": "puissant",
        "description": "Chat général · NVIDIA NIM",
        "usages": ["Discussion libre", "Flashcards"],
    },
    # Mistral
    "codestral-latest": {
        "categorie": "puissant",
        "description": "Code SOTA · Mistral",
        "usages": ["Code"],
    },
    "mistral-small-latest": {
        "categorie": "puissant",
        "description": "Français + sciences · RGPD",
        "usages": ["Discussion libre", "Kholle physique"],
    },
    # DeepSeek (API officielle)
    "deepseek-v4-pro": {
        "categorie": "puissant",
        "description": "Raisonnement avancé · DeepSeek",
        "usages": ["Code", "Kholle maths", "Kholle physique"],
    },
    "deepseek-v4-flash": {
        "categorie": "rapide",
        "description": "Rapide · DeepSeek",
        "usages": ["Chat rapide", "Code"],
    },
    # Ollama qualitative metadata
    "qwen2.5-coder:7b": {
        "categorie": "puissant",
        "description": "Code spécialisé · local CPU",
        "usages": ["Code"],
    },
    "moondream": {
        "categorie": "rapide",
        "description": "Vision + OCR léger · local CPU",
        "usages": ["Vision"],
    },
    "mistral-small:24b": {
        "categorie": "puissant",
        "description": "Français + sciences · local CPU",
        "usages": ["Kholle physique", "Discussion libre"],
    },
    # Cerebras
    "llama3.1-8b": {
        "categorie": "rapide",
        "description": "Ultra-rapide · 2600 tok/s",
        "usages": ["Chat rapide", "Discussion libre"],
    },
    "llama-4-scout": {
        "categorie": "rapide",
        "description": "Scout · 2600 tok/s",
        "usages": ["Chat rapide"],
    },
    "llama-4-maverick": {
        "categorie": "puissant",
        "description": "Maverick · Cerebras",
        "usages": ["Kholle", "Discussion libre"],
    },
}

_NOM_MAP: dict[str, str] = {
    "openai/gpt-oss-120b":                "GPT OSS 120B",
    "openai/gpt-oss-20b":                 "GPT OSS 20B",
    "nvidia/nemotron-3-super-120b-a12b":   "Nemotron 120B",
    "nvidia/llama-3.1-nemotron-nano-8b-v1":"Nemotron Nano 8B",
    "gemini-2.5-flash":                   "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite":              "Gemini 2.5 Flash-Lite",
    "gemini-2.5-pro":                     "Gemini 2.5 Pro",
    "gemini-3.1-flash-lite":              "Gemini 3.1 Flash-Lite",
    "deepseek-ai/deepseek-r1":        "DeepSeek R1 (NIM)",
    "deepseek-ai/deepseek-v4-flash":  "DeepSeek V4 Flash (NIM)",
    "codestral-latest":               "Codestral",
    "mistral-small-latest":           "Mistral Small",
    "deepseek-v4-pro":                "DeepSeek V4 Pro",
    "deepseek-v4-flash":              "DeepSeek V4 Flash",
    "llama3.1-8b":  "Llama 3.1 8B",
    "llama-4-scout":   "Llama 4 Scout",
    "llama-4-maverick":"Llama 4 Maverick",
}

# Recommendations that must override the dynamic first-match logic
RECOMMENDATION_OVERRIDES: dict[str, str] = {
    # `groq:deepseek-r1-distill-llama-70b` ici jusqu'au 2026-08-24 : 404 mesuré.
    # Recommander un modèle mort est pire que ne rien recommander — l'utilisateur
    # suit le conseil et tombe sur une erreur qu'il n'a aucun moyen de relier au
    # conseil.
    "Kholle maths":    "groq:openai/gpt-oss-120b",
    "Kholle physique": "groq:openai/gpt-oss-120b",
}

_GROQ_EXCLUDE = {"whisper", "guard", "orpheus", "compound"}

# Static fallbacks when the live /v1/models fetch is unavailable.
# For NVIDIA/Mistral these are also the curated surface (the live list is only
# used to validate availability — NIM exposes 100+ models we don't want to show).
#: Catalogue Groq de secours, quand `/v1/models` ne répond pas. **Chaque
#: identifiant doit exister**, et trois n'existaient plus :
#: `llama-3.1-8b-instant`, `llama-3.3-70b-versatile` et
#: `deepseek-r1-distill-llama-70b` répondaient tous 404 — mesuré le 2026-08-24,
#: où Groq n'avait plus AUCUN modèle Llama de chat à son catalogue.
#:
#: Retirés et non remplacés : les deux `gpt-oss` restants couvrent les mêmes
#: catégories (`rapide` et `puissant`) et répondent — vérifié par un appel réel,
#: 1,1 s et 2,5 s. Ajouter `qwen/qwen3.6-27b`, seul autre modèle de chat du
#: catalogue, aurait demandé de le mesurer d'abord : il rend son raisonnement en
#: balises `<think>` DANS le contenu, ce qui s'afficherait tel quel dans le chat.
#:
#: Ce que coûtait un identifiant mort ici : cette liste ne sert que de REPLI, donc
#: le bug restait invisible tant que l'API répondait. Le jour où elle ne répond
#: pas, l'utilisateur se voyait proposer trois modèles dont chaque appel échouait
#: en 404 — sur le chemin où tout va déjà mal.
_GROQ_STATIC = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]
_CEREBRAS_STATIC = ["llama3.1-8b", "llama-4-scout", "llama-4-maverick"]
_NVIDIA_STATIC = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "deepseek-ai/deepseek-r1",
    "deepseek-ai/deepseek-v4-flash",
]
_MISTRAL_STATIC = ["codestral-latest", "mistral-small-latest"]
_GEMINI_STATIC  = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3.1-flash-lite"]
# DeepSeek API officielle (api.deepseek.com). deepseek-chat / deepseek-reasoner
# ont été retirés le 2026-07-24 — on n'expose que les v4.
_DEEPSEEK_STATIC = ["deepseek-v4-pro", "deepseek-v4-flash"]

# FastFlowLM (local NPU) — static list, availability checked at request time
FLM_MODELS_STATIC: list[dict] = [
    {
        "id": "flm:qwen3:4b",
        "nom": "Qwen3 4B (NPU)",
        "provider": "flm",
        "gratuit": True,
        "description": "Léger · NPU AMD · 17 tok/s",
        "_categorie": "rapide",
        "_usages": ["Chat rapide", "Flashcards", "Classification", "Conversation instantanée"],
    },
    {
        "id": "flm:qwen3:8b",
        "nom": "Qwen3 8B (NPU)",
        "provider": "flm",
        "gratuit": True,
        "description": "Qualité · NPU AMD · économe",
        "_categorie": "puissant",
        "_usages": ["Kholle maths", "Chat rapide"],
    },
    {
        "id": "flm:gemma3:4b",
        "nom": "Gemma 3 4B (NPU)",
        "provider": "flm",
        "gratuit": True,
        "description": "Français + vision · 20 tok/s",
        "_categorie": "rapide",
        "_usages": ["Chat rapide", "Discussion libre"],
    },
    {
        "id": "flm:phi4-mini-it:4b",
        "nom": "Phi-4 Mini (NPU)",
        "provider": "flm",
        "gratuit": True,
        "description": "Raisonnement compact · NPU",
        "_categorie": "rapide",
        "_usages": ["Kholle maths", "Chat rapide"],
    },
    {
        "id": "flm:qwen3vl-it:4b",
        "nom": "Qwen3-VL 4B (NPU)",
        "provider": "flm",
        "gratuit": True,
        "description": "Vision + OCR · NPU",
        "_categorie": "rapide",
        "_usages": ["Vision", "Chat rapide"],
        # Flag consommé par `premier_modele_vision_disponible()` — la SEULE
        # lecture qui doit connaître ce nom, pour que `core/rag.py` n'ait jamais
        # à écrire "qwen3vl-it:4b" en dur.
        "vision": True,
    },
    {
        "id": "flm:gpt-oss:20b",
        "nom": "GPT-OSS 20B (NPU)",
        "provider": "flm",
        "gratuit": True,
        "description": "Raisonnement fort · NPU AMD",
        "_categorie": "puissant",
        "_usages": ["Kholle maths", "Kholle physique"],
    },
]

# ── FLM local model detection ────────────────────────────────────────────────

_FLM_MODELS_DIR = Path.home() / ".flm" / "models"

# Installed folder name → FLM model ID. Folders not listed here go through
# the generic parser below (Name-Size-NPUx → name:size).
_FLM_FOLDER_MAP: dict[str, str] = {
    "GPT-OSS-20B-NPU2":           "gpt-oss:20b",
    "Gemma3-4B-NPU2":             "gemma3:4b",
    "Phi4-mini-Instruct-NPU2":    "phi4-mini-it:4b",
    "Qwen3-4B-NPU2":              "qwen3:4b",
    "Qwen3-8B-NPU2":              "qwen3:8b",
    "Qwen3-VL-4B-Instruct-NPU2":  "qwen3vl-it:4b",
}


def _flm_folder_to_id(folder: str) -> str:
    if folder in _FLM_FOLDER_MAP:
        return _FLM_FOLDER_MAP[folder]
    parts = re.sub(r"-NPU\d*$", "", folder, flags=re.IGNORECASE).split("-")
    size = next(
        (p.lower() for p in reversed(parts) if re.fullmatch(r"\d+(?:\.\d+)?[bB]|e\d+[bB]", p)),
        None,
    )
    if size:
        name = "-".join(p for p in parts if p.lower() != size).lower()
        return f"{name}:{size}"
    return folder.lower()


def get_flm_installed() -> set[str]:
    """FLM model IDs physically present in ~/.flm/models (empty set if none)."""
    try:
        if not _FLM_MODELS_DIR.is_dir():
            return set()
        return {
            _flm_folder_to_id(d.name) for d in _FLM_MODELS_DIR.iterdir() if d.is_dir()
        }
    except Exception:
        logger.exception("Erreur scan dossier FLM %s", _FLM_MODELS_DIR)
        return set()


def get_ollama_installed() -> Optional[list[str]]:
    """Modèles Ollama installés, par HTTP direct. None si le serveur ne répond pas.

    Host repris de ``core.llm.ollama_host`` (donc normalisé : OLLAMA_HOST=0.0.0.0
    est une adresse d'écoute, pas une adresse de connexion, et faisait échouer
    l'appel sous Windows) — mais on garde volontairement urllib avec son
    ``timeout=3`` au lieu de ``ollama_client`` :

    c'est la SONDE de /health. Elle doit répondre en quelques secondes même quand
    Ollama est figé, alors que le client partagé attend `model.timeout_s` (~5 min)
    entre deux paquets pour ne pas avorter le chargement à froid d'un gros modèle.
    Passer cette fonction sur le client partagé rendrait /health muet aussi
    longtemps — soit exactement ce que le healthcheck doit détecter.
    """
    try:
        req = urllib.request.Request(f"{_ollama_host}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [
            m.get("model") or m.get("name", "")
            for m in data.get("models", [])
            if m.get("model") or m.get("name")
        ]
    except Exception:
        logger.warning("Ollama non joignable sur %s", _ollama_host)
        return None


def check_flm() -> bool:
    """Return True if the FastFlowLM server responds on localhost:11435."""
    try:
        req = urllib.request.Request("http://localhost:11435/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def flm_model_ids() -> Optional[set[str]]:
    """Model IDs known by the FLM server catalog, or None if unreachable."""
    try:
        req = urllib.request.Request("http://localhost:11435/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {m.get("id", "") for m in data.get("data", []) if m.get("id")}
    except Exception:
        return None


def check_lmstudio() -> bool:
    """Return True if the LM Studio server responds on `_lmstudio_host`.

    L'hôte est importé de `core.llm` (qui l'utilise aussi pour router les
    appels de génération via `_OPENAI_COMPAT["lmstudio"]`) plutôt que reparsé
    ici : une même variable d'environnement (`LMSTUDIO_HOST`) lue deux fois
    indépendamment pourrait diverger si l'une des deux normalisations change
    sans l'autre.
    """
    try:
        req = urllib.request.Request(f"{_lmstudio_host}/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_lmstudio_installed() -> Optional[list[str]]:
    """Modèles que LM Studio annonce sur `/v1/models`, par HTTP direct. None si
    le serveur ne répond pas — même contrat que `get_ollama_installed()` :
    l'appelant doit pouvoir distinguer "rien annoncé" de "serveur injoignable".

    Format OpenAI (`{"data": [{"id": ...}, ...]}`), pas le format Ollama
    (`{"models": [{"name"/"model": ...}]}`) : LM Studio suit `/v1/models`
    tel quel, comme FLM.

    **Ce que la liste contient dépend d'un réglage qu'on ne contrôle pas ici**
    (le JIT loading de LM Studio, Réglages > Developer) : chargement à la
    demande ACTIVÉ (comportement courant sur les versions récentes) → la liste
    porte TOUT le catalogue téléchargé sur disque, chargé ou non — LM Studio
    charge lui-même le modèle au premier appel, sans action de notre côté ;
    DÉSACTIVÉ → seuls les modèles physiquement en mémoire y figurent. Les deux
    cas restent utilisables tels quels, sans branche à ajouter ici : un modèle
    annoncé mais pas encore chargé se comporte comme un modèle Ollama installé
    mais pas encore lancé — le premier appel le charge (JIT) ou échoue avec une
    erreur claire (`_provider_error_message`, JIT désactivé), jamais un
    silence. Filtrer selon ce réglage demanderait de le lire depuis LM Studio,
    que `/v1/models` n'expose pas.
    """
    try:
        req = urllib.request.Request(f"{_lmstudio_host}/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except Exception:
        logger.warning("LM Studio non joignable sur %s", _lmstudio_host)
        return None


def _ollama_vision_model() -> str:
    """``vision.ollama_model`` de config.yaml, même registre que ``model.name``.

    Défaut ``moondream`` — vérifié empiriquement le 2026-09-01 : existe dans la
    bibliothèque Ollama, se pull sans erreur, et répond en quelques secondes sur
    ce poste. Pas un nom en dur au sens où le compte `core/rag.py` : il ne le lit
    jamais, il n'appelle que `premier_modele_vision_disponible()`.
    """
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return (cfg.get("vision") or {}).get("ollama_model") or "moondream"
    except Exception:
        logger.warning("vision.ollama_model illisible dans config.yaml — défaut 'moondream'")
        return "moondream"


def _match_ollama_model(configured: str, installed: list[str]) -> Optional[str]:
    """Fait correspondre un nom configuré (``config.yaml``) à un nom RÉELLEMENT
    installé, en tolérant un tag absent.

    Bug confirmé en production : ``config.yaml`` porte ``moondream`` sans tag,
    mais ``get_ollama_installed()`` restitue les noms TELS QU'OLLAMA LES
    EXPOSE via ``/api/tags`` — avec tag, ``moondream:latest``. L'égalité
    stricte d'avant (``configured in installed``) ne matchait donc jamais,
    et ``premier_modele_vision_disponible()`` rendait ``None`` même Ollama
    joignable et le modèle installé — silencieusement, `_texte_image` (core/
    rag.py) retombant sur le placeholder sans qu'aucun log ne le distingue
    d'une vraie absence de modèle.

    Rend le nom RÉELLEMENT installé (avec son tag), pas la valeur brute de
    config : reste correct même si le tag installé change un jour
    (``moondream:1.8b`` plutôt que ``:latest``), et c'est ce nom qui doit
    partir dans l'appel à `describe_image`, pas celui de `config.yaml`.
    """
    if configured in installed:
        return configured
    base = configured.split(":", 1)[0]
    for name in installed:
        if name.split(":", 1)[0] == base:
            return name
    return None


def premier_modele_vision_disponible() -> Optional[str]:
    """Premier modèle capable de décrire une image, RÉELLEMENT disponible.

    **Source unique de ce choix** — `core/rag.py` et `core/llm.py` n'ont aucun
    nom de modèle vision en dur, ils appellent cette fonction. FLM est essayé
    d'abord : c'est le chemin NPU dédié de ce poste, déjà répertorié dans
    `FLM_MODELS_STATIC` (flag `vision`), sans téléchargement à froid tant que
    le modèle est installé. L'Ollama configuré (`vision.ollama_model`,
    `moondream` par défaut) est le repli — c'est ce qui couvre une machine sans
    FLM (ARM64, pas de NPU AMD). Matché via `_match_ollama_model` : `/api/tags`
    rend les noms AVEC tag (`moondream:latest`), jamais nus comme
    `config.yaml` — une égalité stricte ne les faisait donc jamais coïncider.

    Rend ``None`` si aucun des deux n'est disponible : `core/rag.py` retombe
    alors sur le placeholder existant, comme un paquet d'extraction absent
    (§3.3 bis de CLAUDE.md) — dégradation, jamais d'exception.
    """
    if check_flm():
        installed = get_flm_installed()
        live = flm_model_ids()
        for m in FLM_MODELS_STATIC:
            if not m.get("vision"):
                continue
            model_name = m["id"].split("flm:", 1)[1]
            if model_name in installed and (live is None or model_name in live):
                return m["id"]

    ollama_model = _ollama_vision_model()
    ollama_installed = get_ollama_installed()
    if ollama_installed:
        return _match_ollama_model(ollama_model, ollama_installed)

    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _derive_nom(model_id: str) -> str:
    base = model_id.split("/")[-1].split(":")[0]
    return " ".join(w.capitalize() for w in base.replace("-", " ").replace("_", " ").split())


def _make_entry(provider: str, model_id: str) -> dict:
    """Build a model entry dict. Internal keys _categorie/_usages stripped by caller."""
    meta = QUALITATIVE_METADATA.get(model_id, {})
    return {
        "id": f"{provider}:{model_id}",
        "nom": _NOM_MAP.get(model_id, _derive_nom(model_id)),
        "provider": provider,
        "gratuit": True,
        "description": meta.get("description", ""),
        "_categorie": meta.get("categorie", "puissant"),
        "_usages": meta.get("usages", []),
    }


def _http_json(url: str, token: str) -> dict:
    # User-Agent explicite : Cloudflare (Groq/Cerebras) renvoie 403 au UA Python-urllib
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) epure/1.0",
    })
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Registry ─────────────────────────────────────────────────────────────────

class ModelsRegistry:
    _CACHE_TTL = 300  # 5 minutes

    def __init__(self) -> None:
        self._catalog: Optional[dict] = None
        self._catalog_at: float = 0.0

    def invalidate(self) -> None:
        self._catalog = None
        self._catalog_at = 0.0

    # ── Sync fetches (run in executor) ───────────────────────────────────────
    # Each fetch returns: list of live IDs, [] when the API key is rejected
    # (401/403 → mark everything unavailable), or None when no key is set or
    # the request failed (→ fall back to key-presence availability).

    @staticmethod
    def _fetch_models(name: str, url: str, env_key: str) -> Optional[list[str]]:
        token = os.environ.get(env_key, "").strip()
        if not token:
            return None
        try:
            data = _http_json(url, token)
            items = data.get("data") or data.get("models") or []
            ids = [m.get("id", "") for m in items if m.get("id")]
            logger.info("%s: %d modèles récupérés via /v1/models", name, len(ids))
            return ids
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                logger.warning(
                    "%s: clé API refusée (HTTP %d) — modèles marqués indisponibles",
                    name, exc.code,
                )
                return []
            logger.exception("Erreur fetch %s models", name)
            return None
        except Exception:
            logger.exception("Erreur fetch %s models", name)
            return None

    def _fetch_groq(self) -> Optional[list[str]]:
        ids = self._fetch_models("Groq", "https://api.groq.com/openai/v1/models", "GROQ_API_KEY")
        if ids is None:
            return None
        return [mid for mid in ids if not any(ex in mid.lower() for ex in _GROQ_EXCLUDE)]

    def _fetch_cerebras(self) -> Optional[list[str]]:
        return self._fetch_models("Cerebras", "https://api.cerebras.ai/v1/models", "CEREBRAS_API_KEY")

    def _fetch_mistral(self) -> Optional[list[str]]:
        return self._fetch_models("Mistral", "https://api.mistral.ai/v1/models", "MISTRAL_API_KEY")

    def _fetch_nvidia(self) -> Optional[list[str]]:
        return self._fetch_models("NVIDIA", "https://integrate.api.nvidia.com/v1/models", "NVIDIA_API_KEY")

    # ── Async orchestration ──────────────────────────────────────────────────

    async def _guarded(self, func) -> Optional[list]:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, func), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Timeout fetch provider: %s", func.__name__)
            return None
        except Exception:
            logger.exception("Erreur fetch provider: %s", func.__name__)
            return None

    async def _build(self) -> dict:
        groq_ids, cerebras_ids, mistral_ids, nvidia_ids = await asyncio.gather(
            self._guarded(self._fetch_groq),
            self._guarded(self._fetch_cerebras),
            self._guarded(self._fetch_mistral),
            self._guarded(self._fetch_nvidia),
        )

        rapide: list[dict] = []
        puissant: list[dict] = []
        long_contexte: list[dict] = []

        def _place(entry: dict) -> None:
            cat = entry.pop("_categorie", "puissant")
            if cat == "rapide":
                rapide.append(entry)
            elif cat == "long_contexte":
                long_contexte.append(entry)
            else:
                puissant.append(entry)

        def _add(provider: str, surface: list[str], live: Optional[list[str]]) -> None:
            live_set = set(live) if live is not None else None
            for mid in surface:
                entry = _make_entry(provider, mid)
                entry["_disponible"] = None if live_set is None else (mid in live_set)
                _place(entry)

        # Groq/Cerebras: the live list is the surface; static fallback on failure
        _add("groq", groq_ids if groq_ids else _GROQ_STATIC, groq_ids)
        _add("cerebras", cerebras_ids if cerebras_ids else _CEREBRAS_STATIC, cerebras_ids)
        # NVIDIA/Mistral: curated surface, live list only validates availability
        _add("nvidia", _NVIDIA_STATIC, nvidia_ids)
        _add("mistral", _MISTRAL_STATIC, mistral_ids)
        # DeepSeek : le /v1/models live ne liste PAS les noms curatés (v4-pro/flash),
        # donc la validation live les marquerait à tort indisponibles. Comme Gemini :
        # disponibilité = présence de la clé (live=None).
        _add("deepseek", _DEEPSEEK_STATIC, None)
        # Gemini: pas de /v1/models OpenAI-compatible — availability = key presence
        _add("gemini", _GEMINI_STATIC, None)

        return {"rapide": rapide, "puissant": puissant, "long_contexte": long_contexte}

    async def get_catalog(self) -> dict:
        """Return cached cloud catalog (entries include _usages, no 'disponible')."""
        now = time.time()
        if self._catalog is not None and now - self._catalog_at < self._CACHE_TTL:
            return self._catalog
        self._catalog = await self._build()
        self._catalog_at = time.time()
        return self._catalog


# ── Disponibilité, pour VALIDATION (pas seulement affichage) ────────────────

async def ids_disponibles(registry: "ModelsRegistry") -> set[str]:
    """Ensemble des IDs de modèles RÉELLEMENT disponibles, tous fournisseurs.

    Même détection que `GET /models` (`main.py:list_models`), reprise ici pour
    que tout site qui doit VALIDER un ID de modèle côté serveur (au lieu de
    simplement l'afficher) la réutilise plutôt que de la redériver —
    CLAUDE.md §3.7 : « ne jamais accepter un ID de modèle sans le confronter
    à ce qui est réellement disponible ». Un modèle cloud n'est disponible que
    si SA CLÉ est présente ET que son entrée de catalogue n'est pas
    explicitement marquée indisponible (`_disponible is False`, clé refusée
    par le fournisseur) — ni l'un ni l'autre seul ne suffit.
    """
    loop = asyncio.get_running_loop()
    ids: set[str] = set()

    ollama_models = await loop.run_in_executor(None, get_ollama_installed)
    if ollama_models:
        ids.update(ollama_models)

    lmstudio_models = await loop.run_in_executor(None, get_lmstudio_installed)
    if lmstudio_models:
        ids.update(f"lmstudio:{name}" for name in lmstudio_models)

    try:
        flm_ok = await asyncio.wait_for(loop.run_in_executor(None, check_flm), timeout=2.5)
    except Exception:
        flm_ok = False
    if flm_ok:
        try:
            flm_installed = await loop.run_in_executor(None, get_flm_installed)
            flm_live = await loop.run_in_executor(None, flm_model_ids)
        except Exception:
            flm_installed, flm_live = set(), None
        for m in FLM_MODELS_STATIC:
            mid = m["id"].split("flm:", 1)[1]
            if mid in flm_installed and (flm_live is None or mid in flm_live):
                ids.add(m["id"])

    key_ok = {
        "gemini":   bool(os.environ.get("GEMINI_API_KEY", "").strip()),
        "groq":     bool(os.environ.get("GROQ_API_KEY", "").strip()),
        "cerebras": bool(os.environ.get("CEREBRAS_API_KEY", "").strip()),
        "mistral":  bool(os.environ.get("MISTRAL_API_KEY", "").strip()),
        "nvidia":   bool(os.environ.get("NVIDIA_API_KEY", "").strip()),
        "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
    }
    catalog = await registry.get_catalog()
    for modeles in catalog.values():
        for m in modeles:
            if not key_ok.get(m["provider"], False):
                continue
            if m.get("_disponible") is False:
                continue
            ids.add(m["id"])

    return ids
