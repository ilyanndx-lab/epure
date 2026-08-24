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
import secrets
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from core.jsonstore import read_json, transaction, write_json
from core.paths import BACKEND_DIR, FICHES_DIR, resolve_data_dir

logger = logging.getLogger(__name__)

def _config_file() -> Path:
    """Chemin d'``instance_config.json``. Fonction, PAS constante.

    Cf. core.paths.resolve_data_dir : le calculer à l'import figerait le dossier
    de données avant qu'un test ait pu poser ``EPURE_DATA_DIR``.
    """
    return resolve_data_dir() / "instance_config.json"

#: Module dont la désactivation est refusée : sans lui, plus d'écran pour
#: réactiver quoi que ce soit. Réinjecté à l'écriture si un patch client
#: l'omet (cf. :meth:`InstanceConfig.update`).
_MODULE_INDESACTIVABLE = "settings"

_API_KEY_NAMES = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"]
_KEY_TO_PROVIDER = {
    "GEMINI_API_KEY": "gemini",
    "GROQ_API_KEY": "groq",
    "CEREBRAS_API_KEY": "cerebras",
    "MISTRAL_API_KEY": "mistral",
    "NVIDIA_API_KEY": "nvidia",
    "DEEPSEEK_API_KEY": "deepseek",
}

#: Préfixes de fournisseurs DISTANTS. Dérivé de la table ci-dessus plutôt que
#: réécrit : une clé ajoutée là-haut serait sinon absente ici, et un modèle de ce
#: fournisseur passerait pour local dans les tâches de fond — exactement la fuite
#: que ce garde-fou existe pour empêcher.
#:
#: `flm` en est ABSENT, et ce n'est pas un oubli : c'est le NPU de la machine, du
#: local au même titre qu'Ollama.
_FOURNISSEURS_CLOUD = frozenset(_KEY_TO_PROVIDER.values())

_DEFAULT_LOCAL_MODEL = "qwen2.5:7b"


def _default_config() -> dict:
    return {
        "instance_id": str(uuid.uuid4()),
        "nom_affiché": "Épure",
        # Vide et NON une liste de modules en dur. L'ancien défaut était
        # ["chat","kholle","flashcards","code","docs","admin","history"] : tout
        # module ajouté au dépôt après l'écriture de cette constante n'y entrait
        # jamais — c'est ainsi que `reviseur` était installé, monté, et pourtant
        # absent de la barre. Vide signifie « jamais initialisée » et déclenche
        # le défaut dynamique « tous les modules installés, ordre
        # discover_manifests() » (core.module_registry.active_ids).
        "modules_activés": [],
        "providers": {
            "actif": _DEFAULT_LOCAL_MODEL,
            "local": _DEFAULT_LOCAL_MODEL,
            "clés_présentes": {},  # dérivé de l'environnement, jamais persisté
        },
        "fiches": {
            "racine": str(FICHES_DIR),
            # Vide, et non les trois matières de l'auteur. Une installation
            # neuve ne surveille rien tant que l'utilisateur n'a pas déclaré ses
            # propres dossiers dans Réglages › Fiches. `fiches_watch_paths()`
            # gère la liste vide sans erreur, et `core.admin._categories()` en
            # déduit qu'il n'y a pas encore de classement possible.
            "watch_folders": [],
        },
        "thème": "dark",
        "preset_défaut": None,
        "atelier": {
            # Binaire `claude` (nom sur le PATH ou chemin complet).
            "claude_path": "claude",
            # Binaire `aider` (nom sur le PATH ou chemin complet).
            "aider_path": "aider",
            # Timeout (minutes) d'une génération aider headless.
            "aider_timeout_min": 15,
            # Passerelle Anthropic-compatible (LiteLLM / claude-code-router) pour
            # le moteur claude_gateway. start_command : commande de démarrage
            # optionnelle, lancée par le bouton « Démarrer » des Réglages.
            "gateway": {
                "base_url": "http://localhost:4000",
                "model": "",
                "api_key": "",
                "start_command": "",
            },
            "moteur_defaut": "ollama",
            "mode_defaut": "headless",
        },
    }


def _key_status() -> dict:
    """État (présence) des clés API, dérivé de l'environnement courant."""
    return {_KEY_TO_PROVIDER[k]: bool(os.environ.get(k, "").strip()) for k in _API_KEY_NAMES}


class InstanceConfig:
    """Configuration d'instance persistée, thread-safe, interface DB-ready."""

    def __init__(self, path: Optional[Path] = None):
        # `path=_CONFIG_FILE` en défaut d'argument était le piège le plus
        # discret des neuf : un défaut est évalué à la DÉFINITION de la
        # fonction, donc à l'import du module. Le sentinelle None reporte la
        # résolution à la construction de l'objet.
        self._path = Path(path) if path is not None else _config_file()
        self._lock = RLock()
        self._cache: Optional[dict] = None
        self._ensure()

    # ── Persistance (unique point de bascule vers une vraie DB) ──────────────

    def _load(self) -> dict:
        data = read_json(self._path, None)
        if isinstance(data, dict):
            return data
        return _default_config()

    def _save(self, cfg: dict) -> None:
        write_json(self._path, cfg)

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
    def _garder_settings(modules: Any) -> list:
        """Réinjecte ``settings`` dans une liste de modules non vide.

        Depuis que ``modules_activés`` est l'unique source de vérité, elle ne
        pilote plus seulement l'affichage : elle conditionne le **montage du
        routeur**. Une liste qui perd ``settings`` ne cache donc plus un écran,
        elle le débranche — et c'est précisément l'écran par lequel on
        réactiverait quoi que ce soit. Le garde-fou est ici, au point d'écriture,
        et pas seulement dans ``set_status`` : ``update()`` accepte une liste
        entière venant du client (réordonnancement, retrait), qui ne passe pas
        par ``set_status``.

        La liste VIDE est laissée telle quelle : elle signifie « jamais
        initialisée » et déclenche le défaut dynamique de
        ``module_registry.active_ids()``, qui remonte tout — settings compris.
        """
        if not isinstance(modules, list):
            return modules
        if modules and _MODULE_INDESACTIVABLE not in modules:
            logger.info(
                "modules_activés : %r réinjecté (indésactivable)", _MODULE_INDESACTIVABLE
            )
            return [*modules, _MODULE_INDESACTIVABLE]
        return modules

    def _mutate(self, muter) -> dict:
        """Read-modify-write de instance_config.json, sous verrou de FICHIER.

        Passe par ``jsonstore.transaction`` et non par ``read_json`` puis
        ``write_json`` : entre les deux, une autre écriture du même fichier
        s'intercale et se fait écraser (mesuré ailleurs dans le dépôt : 240
        écritures concurrentes attendues, 2 conservées — cf. la docstring de
        core/jsonstore.py). Le ``RLock`` de l'instance ne suffit pas : il ne
        sérialise que les appelants qui passent par CET objet, alors que
        ``instance_config.json`` porte désormais la liste qui pilote le montage
        des routeurs au démarrage. La corrompre coûte un démarrage sans modules.

        Ordre de verrouillage : ``self._lock`` PUIS le verrou de fichier. Jamais
        l'inverse, sous peine d'interblocage.

        Si le fichier est illisible, on repart du cache mémoire plutôt que des
        défauts : régénérer ``_default_config()`` par-dessus écraserait
        ``instance_id`` et le token d'API.
        """
        with self._lock:
            with transaction(self._path, {}) as doc:
                source = doc if (isinstance(doc, dict) and doc) else (self._cache or {})
                cfg = self._merge_defaults(source)
                muter(cfg)
                cfg.get("providers", {}).pop("clés_présentes", None)  # dérivé
                cfg["modules_activés"] = self._garder_settings(cfg.get("modules_activés"))
                doc.clear()
                doc.update(cfg)  # mutation EN PLACE : c'est doc qui est réécrit
            self._cache = cfg
        return cfg

    @staticmethod
    def _deep_merge(base: dict, patch: dict) -> dict:
        """Fusion récursive : les sous-dictionnaires sont fusionnés, pas remplacés."""
        out = dict(base)
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = InstanceConfig._deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    @staticmethod
    def _merge_defaults(cfg: dict) -> dict:
        base = _default_config()
        merged = {**base, **cfg}
        # instance_id stable s'il existe déjà
        merged["instance_id"] = cfg.get("instance_id") or base["instance_id"]
        merged["providers"] = {**base["providers"], **(cfg.get("providers") or {})}
        merged["fiches"] = {**base["fiches"], **(cfg.get("fiches") or {})}
        # atelier : fusion profonde (gateway imbriqué) + on ne garde que les clés
        # connues (migration depuis l'ancien schéma plat).
        atelier = InstanceConfig._deep_merge(base["atelier"], cfg.get("atelier") or {})
        merged["atelier"] = {k: atelier.get(k, base["atelier"][k]) for k in base["atelier"]}
        return merged

    @staticmethod
    def _clean_gateway_patch(atelier_patch: dict) -> dict:
        """Nettoie le sous-bloc ``gateway`` d'un patch client.

        Deux champs y sont ignorés :

        - ``api_key_présente`` : dérivé, jamais persisté (comme
          ``providers.clés_présentes``) ;
        - ``api_key`` **vide** : depuis que :meth:`get` n'expose plus la clé, le
          champ du formulaire des Réglages part vide quand l'utilisateur n'y a
          pas touché. La prendre au mot effacerait la clé enregistrée à chaque
          passage dans le formulaire. Conséquence assumée : on remplace une clé
          en en saisissant une nouvelle, on ne la vide pas depuis l'UI.
        """
        gw = atelier_patch.get("gateway")
        if not isinstance(gw, dict):
            return atelier_patch
        gw = {k: v for k, v in gw.items() if k != "api_key_présente"}
        if not str(gw.get("api_key") or "").strip():
            gw.pop("api_key", None)
        return {**atelier_patch, "gateway": gw}

    @staticmethod
    def _apply_partial(cfg: dict, partial: dict) -> None:
        """Merge partiel en place. Sous-dictionnaires fusionnés en profondeur
        (providers, fiches, atelier.gateway)."""
        for k, v in partial.items():
            if k in ("instance_id", "auth"):
                continue  # immuables via l'API publique
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                if k == "atelier":
                    v = InstanceConfig._clean_gateway_patch(v)
                merged = InstanceConfig._deep_merge(cfg[k], v)
                merged.pop("clés_présentes", None)  # dérivé : jamais persisté
                cfg[k] = merged
            else:
                cfg[k] = v

    # ── API publique ─────────────────────────────────────────────────────────

    def raw(self) -> dict:
        """Config complète, secrets INCLUS — usage serveur uniquement.

        Ne jamais renvoyer ce dictionnaire dans une réponse HTTP : c'est le rôle
        de :meth:`get`, qui en expurge les secrets. Seuls les appelants qui ont
        besoin de la vraie valeur d'un secret (démarrage de la passerelle,
        moteur claude_gateway) passent par ici.
        """
        with self._lock:
            return json.loads(json.dumps(self._cache or self._load()))  # copie profonde

    def get(self) -> dict:
        """Retourne la config courante, expurgée de ses secrets.

        Deux valeurs ne sortent JAMAIS par ``GET /instance/config`` :

        - le bloc ``auth`` (token d'API) — il ne sort que par la route
          d'appairage locale ``/pair`` (cf. core.auth) ;
        - ``atelier.gateway.api_key`` — la clé du fournisseur cloud placé
          derrière la passerelle. Elle était renvoyée en clair : toute page
          ayant obtenu le token (cf. DNS rebinding, CLAUDE.md §6) la lisait
          d'une seule requête. Elle est remplacée par le booléen dérivé
          ``api_key_présente``, sur le modèle de ``providers.clés_présentes``.
          Le code serveur qui a besoin de la vraie valeur passe par :meth:`raw`.
        """
        cfg = self.raw()
        cfg.pop("auth", None)
        cfg.setdefault("providers", {})["clés_présentes"] = _key_status()
        gw = (cfg.get("atelier") or {}).get("gateway")
        if isinstance(gw, dict):
            gw["api_key_présente"] = bool(str(gw.pop("api_key", "") or "").strip())
        return cfg

    def update(self, partial: dict) -> dict:
        """Applique un merge partiel, persiste, et retourne la config à jour."""
        self._mutate(lambda cfg: self._apply_partial(cfg, partial))
        return self.get()

    def enabled_modules(self) -> list[str]:
        """Liste ORDONNÉE des modules actifs, telle qu'elle est stockée.

        Peut être vide : c'est le cas « jamais initialisée » d'une installation
        neuve. Le défaut « tous les modules installés » n'est délibérément PAS
        appliqué ici — seul :func:`core.module_registry.active_ids` sait quels
        modules sont réellement présents sur le disque. Le sens de la dépendance
        est imposé : ``module_registry`` importe ``instance``, l'inverse
        créerait un cycle d'import.
        """
        with self._lock:
            cfg = self._cache or self._load()
        return list(cfg.get("modules_activés") or [])

    def auth_token(self) -> str:
        """Token d'API de l'instance — généré au premier appel puis persistant.

        Stocké sous ``auth.token`` dans instance_config.json ; jamais renvoyé
        par :meth:`get` ni modifiable par :meth:`update` (cf. _apply_partial).
        """
        with self._lock:
            cfg = self._merge_defaults(self._cache or self._load())
            token = (cfg.get("auth") or {}).get("token") or ""
            if token:
                return token
            # Génération : read-modify-write, donc sous verrou de fichier. Deux
            # threads qui arrivent ici en même temps ne doivent pas produire deux
            # tokens dont un seul survit — le client appairé avec le perdant
            # serait rejeté par le middleware jusqu'au prochain /pair.
            nouveau = secrets.token_urlsafe(32)

            def _poser(c: dict) -> None:
                if not (c.get("auth") or {}).get("token"):
                    c.setdefault("auth", {})["token"] = nouveau

            cfg = self._mutate(_poser)
            logger.info("Token d'API généré (premier démarrage)")
            return (cfg.get("auth") or {}).get("token") or nouveau


# Singleton applicatif (créé une fois au démarrage).
instance_config = InstanceConfig()


# ── Modèle des tâches de fond ────────────────────────────────────────────────

def est_modele_cloud(model_id: str) -> bool:
    """``provider:modèle`` d'un fournisseur distant ?

    Un nom Ollama contient un « : » lui aussi (``qwen2.5:7b``) : le préfixe doit
    donc être comparé à la liste des fournisseurs, jamais déduit de la présence
    du séparateur. ``flm`` compte comme LOCAL — c'est le NPU de la machine, pas
    un service distant, et le confondre avec du cloud interdirait le seul moteur
    local rapide de ce poste.

    Jumeau de `core.orchestrator._is_cloud_model`, délibérément non partagé :
    l'orchestrateur importe `core.llm`, et `core/instance.py` est importé par
    presque tout le backend — y compris des scripts qui doivent rester légers.
    Payer cet import pour six lignes recréerait le cycle qu'on évite.
    """
    if ":" not in model_id:
        return False
    return model_id.split(":", 1)[0] in _FOURNISSEURS_CLOUD


def modele_local_defaut() -> str:
    """Modèle des tâches qui ne sont PAS le tour de chat de l'utilisateur.

    **La règle que cette fonction sert** : résumé, titrage, classification,
    réflexion de l'agent de code, fiches — tout ce que l'utilisateur n'a pas
    demandé nommément — tourne en local, et ne part vers le cloud que sur un
    choix explicite pour cette tâche précise. Jamais en héritant du modèle actif
    du chat, qui est un choix fait pour *répondre à un message*.

    Pourquoi une fonction et pas cinq lectures : ``self._llm._model`` était lu en
    dur à cinq endroits (`orchestrator.build_steps` ×2, `run_pipeline`,
    `docanalysis` par ``model=None``, `history._generate_title`). Chacun retombait
    donc sur ``config.yaml``, un fichier que l'utilisateur n'édite pas depuis
    l'interface. Remplacer un défaut en dur par cinq lectures d'un réglage aurait
    juste déplacé le problème.

    Trois niveaux, dans cet ordre :

    1. ``providers.local`` de la config d'instance — **le réglage**. Ce champ
       existait déjà et n'était lu par personne ; il est utilisé plutôt que
       créé, parce que deux champs pour une notion divergent mécaniquement
       (CLAUDE.md §3.3, mesuré sur `modules_state.json`).
    2. ``model.name`` de ``config.yaml`` — le comportement d'avant, donc aucune
       instance ne change de modèle en installant cette version.
    3. :data:`_DEFAULT_LOCAL_MODEL`, si le fichier est illisible.

    **Un identifiant cloud dans ce réglage est REFUSÉ ici**, pas seulement à
    l'écriture : un `providers.local` valant ``groq:…`` viderait la règle de son
    sens tout en ayant l'air d'un réglage valide, et le vérifier au seul point
    d'écriture laisserait passer un fichier édité à la main.
    """
    brut = ((instance_config.get().get("providers") or {}).get("local") or "").strip()
    if brut and not est_modele_cloud(brut):
        return brut
    if brut:
        logger.warning(
            "providers.local vaut %r, un modèle cloud — ignoré pour les tâches de "
            "fond, qui restent locales. Corrigez le réglage dans Réglages.", brut,
        )
    return _modele_config_yaml()


def _modele_config_yaml() -> str:
    """``model.name`` de ``config.yaml`` — le défaut d'avant ce réglage.

    Lu ici et non pris sur ``LLMEngine._model`` : ce module ne doit pas importer
    ``core.llm`` (cycle, et poids inutile pour les scripts légers). Le fichier est
    relu à chaque appel, jamais mémorisé — même règle que `core.paths` : figer un
    chemin ou une valeur de config à l'import est le piège que ce dépôt a déjà
    payé plusieurs fois.
    """
    try:
        import yaml
        cfg = yaml.safe_load((BACKEND_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        nom = ((cfg.get("model") or {}).get("name") or "").strip()
        return nom or _DEFAULT_LOCAL_MODEL
    except Exception:
        logger.warning("config.yaml illisible — modèle local par défaut : %s",
                       _DEFAULT_LOCAL_MODEL, exc_info=True)
        return _DEFAULT_LOCAL_MODEL


def modele_pour_tache(use_cloud: bool, modele_cloud: str, cle_env: str) -> Optional[str]:
    """Le modèle d'une tâche de fond, selon un choix EXPLICITE de l'utilisateur.

    Contrat unique de toutes les tâches de fond, copié de
    ``consolidation._pick_model`` qui était le seul endroit à le tenir :

    * ``use_cloud=False`` (le défaut partout) → :func:`modele_local_defaut`.
      Jamais ``None``, et c'est une différence avec l'original : rendre ``None``
      laissait ``LLMEngine`` retomber sur ``config.yaml``, donc court-circuitait
      le réglage sans que le site d'appel le sache.
    * ``use_cloud=True`` → ``modele_cloud``, **nommé pour la tâche**, et
      seulement si sa clé est présente. Sinon repli local avec un avertissement :
      une clé absente est un état prévu, pas une panne, et échouer serait pire
      que répondre plus lentement.

    Ce qui n'arrive JAMAIS ici : lire ``modèle_actif``. C'est tout le sujet — le
    modèle du chat est un choix fait pour répondre à un message, pas un mandat
    pour toutes les tâches de fond de la session.
    """
    if not use_cloud:
        return modele_local_defaut()
    if not os.environ.get(cle_env, "").strip():
        logger.warning("%s absente — tâche de fond gardée en local (%s demandé)",
                       cle_env, modele_cloud)
        return modele_local_defaut()
    return modele_cloud


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
