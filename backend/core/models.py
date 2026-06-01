"""
Dynamic model registry — fetches available LLM IDs from cloud providers,
enriches with qualitative metadata, caches 5 minutes.
"""
import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# ── Qualitative metadata ─────────────────────────────────────────────────────

QUALITATIVE_METADATA: dict[str, dict] = {
    "llama-3.1-8b-instant": {
        "categorie": "rapide",
        "description": "Chat temps réel · 560 tok/s",
        "usages": ["Chat rapide", "Discussion libre"],
    },
    "llama-3.3-70b-versatile": {
        "categorie": "puissant",
        "description": "Qualité générale · 280 tok/s",
        "usages": ["Kholle", "Explication cours"],
    },
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
    "deepseek-v4-flash": {
        "categorie": "puissant",
        "description": "Chat + raisonnement · DeepSeek",
        "usages": ["Kholle maths", "Discussion libre"],
    },
    "deepseek-r1": {
        "categorie": "puissant",
        "description": "Raisonnement avancé · chain-of-thought",
        "usages": ["Kholle maths", "Kholle physique"],
    },
    # Groq reasoning
    "deepseek-r1-distill-llama-70b": {
        "categorie": "puissant",
        "description": "Raisonnement · chain-of-thought · Groq",
        "usages": ["Kholle maths", "Kholle physique"],
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
    "llama-3.1-8b-instant":               "Llama 3.1 8B",
    "llama-3.3-70b-versatile":            "Llama 3.3 70B",
    "openai/gpt-oss-120b":                "GPT OSS 120B",
    "openai/gpt-oss-20b":                 "GPT OSS 20B",
    "nvidia/nemotron-3-super-120b-a12b":   "Nemotron 120B",
    "nvidia/llama-3.1-nemotron-nano-8b-v1":"Nemotron Nano 8B",
    "gemini-2.5-flash":                   "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite":              "Gemini 2.5 Flash-Lite",
    "gemini-2.5-pro":                     "Gemini 2.5 Pro",
    "gemini-3.1-flash-lite":              "Gemini 3.1 Flash-Lite",
    "deepseek-r1-distill-llama-70b": "DeepSeek R1 70B",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek-r1":       "DeepSeek R1",
    "llama3.1-8b":  "Llama 3.1 8B",
    "llama-4-scout":   "Llama 4 Scout",
    "llama-4-maverick":"Llama 4 Maverick",
}

# Recommendations that must override the dynamic first-match logic
RECOMMENDATION_OVERRIDES: dict[str, str] = {
    "Kholle maths":    "groq:deepseek-r1-distill-llama-70b",
    "Kholle physique": "groq:deepseek-r1-distill-llama-70b",
}

_GROQ_EXCLUDE = {"whisper", "guard", "orpheus", "compound"}
_NVIDIA_STATIC    = ["nvidia/nemotron-3-super-120b-a12b", "nvidia/llama-3.1-nemotron-nano-8b-v1"]
_DEEPSEEK_STATIC = ["deepseek-v4-flash", "deepseek-r1"]
_GEMINI_STATIC    = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3.1-flash-lite"]

# FastFlowLM (local NPU) — static list, availability checked at request time
FLM_MODELS_STATIC: list[dict] = [
    {
        "id": "flm:qwen3:4b",
        "nom": "Qwen3 4B (NPU)",
        "provider": "flm",
        "gratuit": True,
        "description": "Léger · NPU AMD · 17 tok/s",
        "_categorie": "rapide",
        "_usages": ["Chat rapide", "Flashcards", "Classification"],
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
]


def check_flm() -> bool:
    """Return True if the FastFlowLM server responds on localhost:11435."""
    try:
        req = urllib.request.Request("http://localhost:11435/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


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
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
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

    def _fetch_groq(self) -> list[str]:
        token = os.environ.get("GROQ_API_KEY", "").strip()
        if not token:
            return []
        try:
            data = _http_json("https://api.groq.com/openai/v1/models", token)
            ids = []
            for m in data.get("data", []):
                mid = m.get("id", "")
                if any(ex in mid.lower() for ex in _GROQ_EXCLUDE):
                    continue
                ids.append(mid)
            logger.info("Groq: %d modèles récupérés", len(ids))
            return ids
        except Exception:
            logger.exception("Erreur fetch Groq models")
            return []

    def _fetch_cerebras(self) -> list[str]:
        token = os.environ.get("CEREBRAS_API_KEY", "").strip()
        if not token:
            return []
        try:
            data = _http_json("https://api.cerebras.ai/v1/models", token)
            items = data.get("data") or data.get("models") or []
            ids = [m.get("id", "") for m in items if m.get("id")]
            logger.info("Cerebras: %d modèles récupérés", len(ids))
            return ids
        except Exception:
            logger.exception("Erreur fetch Cerebras models")
            return []

    # ── Async orchestration ──────────────────────────────────────────────────

    async def _guarded(self, func) -> list:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, func), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Timeout fetch provider: %s", func.__name__)
            return []
        except Exception:
            logger.exception("Erreur fetch provider: %s", func.__name__)
            return []

    async def _build(self) -> dict:
        groq_ids, cerebras_ids = await asyncio.gather(
            self._guarded(self._fetch_groq),
            self._guarded(self._fetch_cerebras),
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

        for mid in groq_ids:
            _place(_make_entry("groq", mid))
        for mid in cerebras_ids:
            _place(_make_entry("cerebras", mid))
        for mid in _NVIDIA_STATIC:
            _place(_make_entry("nvidia", mid))
        for mid in _DEEPSEEK_STATIC:
            _place(_make_entry("deepseek", mid))
        for mid in _GEMINI_STATIC:
            _place(_make_entry("gemini", mid))

        return {"rapide": rapide, "puissant": puissant, "long_contexte": long_contexte}

    async def get_catalog(self) -> dict:
        """Return cached cloud catalog (entries include _usages, no 'disponible')."""
        now = time.time()
        if self._catalog is not None and now - self._catalog_at < self._CACHE_TTL:
            return self._catalog
        self._catalog = await self._build()
        self._catalog_at = time.time()
        return self._catalog
