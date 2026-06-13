"""Configuration personnalisable d'une instance Épure.

Permet à chaque utilisateur de personnaliser son Épure (modules visibles,
provider/modèle actif, dossiers de fiches, thème, nom) sans toucher au code.

Interface « DB-ready » : tout passe par :class:`InstanceConfig` via ``get()`` /
``update(partial)`` / ``enabled_modules()``. La persistance JSON
(``backend/memory/instance_config.json``) est entièrement encapsulée dans
``_load`` / ``_save`` — la remplacer par une vraie base ne touchera que ces deux
méthodes. Le pattern (load/save/ensure + défauts) est calqué sur
``core.orchestrator`` (presets).

Ce module remplace l'ancien ``_FICHES_DIR`` codé en dur et la clé
``rag.watch_folders`` de ``config.yaml`` (qui ne garde que les réglages
techniques : whisper, chunk_size...).
"""

import json
import logging
import os
import uuid
from pathlib import Path
from threading import RLock
from typing import Optional

from core.paths import FICHES_DIR

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path(__file__).parent.parent / "memory" / "instance_config.json"

# Modules « core » livrés avec Épure (cf. core.module_registry / manifests).
# settings est volontairement exclu de la liste activable : il reste toujours
# accessible (sinon impossible de réactiver un module).
_CORE_MODULES = ["chat", "kholle", "flashcards", "code", "docs", "admin", "history"]

_API_KEY_NAMES = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY"]
_KEY_TO_PROVIDER = {
    "GEMINI_API_KEY": "gemini",
    "GROQ_API_KEY": "groq",
    "CEREBRAS_API_KEY": "cerebras",
    "MISTRAL_API_KEY": "mistral",
    "NVIDIA_API_KEY": "nvidia",
}

_DEFAULT_LOCAL_MODEL = "qwen2.5:7b"


def _default_config() -> dict:
    return {
        "instance_id": str(uuid.uuid4()),
        "nom_affiché": "Épure",
        "modules_activés": list(_CORE_MODULES),
        "providers": {
            "actif": _DEFAULT_LOCAL_MODEL,
            "local": _DEFAULT_LOCAL_MODEL,
            "clés_présentes": {},  # dérivé de l'environnement, jamais persisté
        },
        "fiches": {
            "racine": str(FICHES_DIR),
            "watch_folders": ["Maths", "Physique-Chimie", "SI"],
        },
        "thème": "dark",
        "preset_défaut": None,
        "atelier": {
            # Passerelle Anthropic-compatible (LiteLLM / claude-code-router) pour
            # le moteur claude_gateway. URL + modèle configurables.
            "gateway_url": "http://localhost:4000",
            "gateway_model": "claude-sonnet-4-5",
        },
    }


def _key_status() -> dict:
    """État (présence) des clés API, dérivé de l'environnement courant."""
    return {_KEY_TO_PROVIDER[k]: bool(os.environ.get(k, "").strip()) for k in _API_KEY_NAMES}


class InstanceConfig:
    """Configuration d'instance persistée, thread-safe, interface DB-ready."""

    def __init__(self, path: Path = _CONFIG_FILE):
        self._path = path
        self._lock = RLock()
        self._cache: Optional[dict] = None
        self._ensure()

    # ── Persistance (unique point de bascule vers une vraie DB) ──────────────

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Erreur lecture instance_config, retour aux défauts")
            return _default_config()

    def _save(self, cfg: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _ensure(self) -> None:
        with self._lock:
            if not self._path.exists():
                cfg = _default_config()
                self._save(cfg)
                self._cache = cfg
            else:
                # Fusion avec les défauts : tolère les champs absents (migrations).
                self._cache = self._merge_defaults(self._load())

    @staticmethod
    def _merge_defaults(cfg: dict) -> dict:
        base = _default_config()
        merged = {**base, **cfg}
        # instance_id stable s'il existe déjà
        merged["instance_id"] = cfg.get("instance_id") or base["instance_id"]
        merged["providers"] = {**base["providers"], **(cfg.get("providers") or {})}
        merged["fiches"] = {**base["fiches"], **(cfg.get("fiches") or {})}
        merged["atelier"] = {**base["atelier"], **(cfg.get("atelier") or {})}
        return merged

    @staticmethod
    def _apply_partial(cfg: dict, partial: dict) -> None:
        """Merge partiel en place. Champs imbriqués (providers, fiches) fusionnés."""
        for k, v in partial.items():
            if k == "instance_id":
                continue  # immuable
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                merged = {**cfg[k], **v}
                merged.pop("clés_présentes", None)  # dérivé : jamais persisté
                cfg[k] = merged
            else:
                cfg[k] = v

    # ── API publique ─────────────────────────────────────────────────────────

    def get(self) -> dict:
        """Retourne la config courante (clés_présentes recalculées à la volée)."""
        with self._lock:
            cfg = json.loads(json.dumps(self._cache or self._load()))  # copie profonde
        cfg.setdefault("providers", {})["clés_présentes"] = _key_status()
        return cfg

    def update(self, partial: dict) -> dict:
        """Applique un merge partiel, persiste, et retourne la config à jour."""
        with self._lock:
            cfg = self._merge_defaults(self._cache or self._load())
            self._apply_partial(cfg, partial)
            cfg.get("providers", {}).pop("clés_présentes", None)
            self._save(cfg)
            self._cache = cfg
        return self.get()

    def enabled_modules(self) -> list[str]:
        """Identifiants des modules activés par l'utilisateur."""
        with self._lock:
            cfg = self._cache or self._load()
        return list(cfg.get("modules_activés", _CORE_MODULES))


# Singleton applicatif (créé une fois au démarrage).
instance_config = InstanceConfig()


# ── Helpers fiches (remplacent _FICHES_DIR / config.yaml watch_folders) ───────

def fiches_root() -> Path:
    """Dossier racine des fiches, piloté par la config d'instance."""
    cfg = instance_config.get()
    racine = (cfg.get("fiches") or {}).get("racine") or str(FICHES_DIR)
    return Path(racine).expanduser()


def fiches_watch_paths() -> list[Path]:
    """Chemins absolus des dossiers surveillés (relatifs résolus sous la racine)."""
    root = fiches_root()
    folders = (instance_config.get().get("fiches") or {}).get("watch_folders") or []
    paths: list[Path] = []
    for f in folders:
        p = Path(f).expanduser()
        paths.append(p if p.is_absolute() else root / f)
    return paths
