"""Registre des modules d'Épure, piloté par des manifestes.

Chaque module core déclare un ``backend/modules/<id>/manifest.json`` SANS que le
code du module y soit déplacé (les composants/endpoints restent où ils sont).
Le manifeste décrit le module (id, version, nom, icône lucide-react, description,
frontend.component, backend.prefix, core_module, origin, status, removable).

DEUX ÉTATS, UNE SEULE SOURCE DE VÉRITÉ (cf. CLAUDE.md §3.3) :

- **installé** = ``modules/<id>/manifest.json`` existe. Dérivé du disque, jamais
  stocké.
- **actif** = ``id`` ∈ ``instance_config.modules_activés``, liste ORDONNÉE.
  Actif signifie routeur monté ET visible dans la barre, à la position donnée
  par la liste. Il n'existe pas d'état « monté mais invisible ».

``memory/modules_state.json`` a été supprimé et NE DOIT PAS ÊTRE RECRÉÉ. Il
portait un second ``status`` par module, en doublon de ``modules_activés``. Deux
fichiers pour une notion divergent mécaniquement, et c'est ce qui a été mesuré
avant migration : 9 des 11 entrées de ``modules_state.json`` pointaient des
modules supprimés, 4 des 12 entrées de ``modules_activés`` aussi, et ``reviseur``
était installé et monté tout en étant absent de ``modules_activés`` (le défaut
``_CORE_MODULES`` de core/instance.py était une liste en dur qui ne le
mentionnait pas). Le champ ``status: "active"|"disabled"`` reste exposé par
``GET /modules`` — il est désormais DÉRIVÉ de l'appartenance à la liste, et le
frontend n'a pas à savoir que le stockage a changé.
"""

import importlib
import json
import logging
from pathlib import Path
from threading import RLock
from typing import Optional

from core.instance import instance_config
from core.jsonstore import read_json
from core.paths import resolve_data_dir, resolve_modules_dir

logger = logging.getLogger(__name__)


def _modules_dir() -> Path:
    """Dossier des modules. Fonction et non constante : cf. core.paths."""
    return resolve_modules_dir()


def _legacy_state_file() -> Path:
    """Ancien stockage, supprimé par :func:`migrate_module_state`.

    Ne subsiste que pour détecter puis effacer un reliquat — on n'y écrit
    jamais. Fonction et non constante : cf. core.paths.resolve_data_dir.
    """
    return resolve_data_dir() / "modules_state.json"


_VALID_STATUS = {"active", "disabled"}
#: Sans lui, plus d'écran pour réactiver quoi que ce soit.
_MODULE_INDESACTIVABLE = "settings"
_lock = RLock()


def discover_manifests() -> list[dict]:
    """Lit tous les ``modules/<id>/manifest.json`` présents sur disque."""
    manifests: list[dict] = []
    if not _modules_dir().is_dir():
        return manifests
    for sub in sorted(_modules_dir().iterdir()):
        mf = sub / "manifest.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8-sig"))
            data.setdefault("id", sub.name)
            manifests.append(data)
        except Exception:
            logger.exception("Manifest illisible : %s", mf)
    return manifests


def installed_ids() -> list[str]:
    """Ids installés, dans l'ordre de :func:`discover_manifests` (alphabétique)."""
    return [str(m["id"]) for m in discover_manifests()]


def active_ids() -> list[str]:
    """Ids actifs, dans l'ordre de ``modules_activés``. SEULE lecture d'état.

    Trois règles, dans cet ordre :

    1. **Liste vide → tous les modules installés**, ordre ``discover_manifests``.
       C'est le défaut d'une installation neuve : quelqu'un qui clone le dépôt et
       démarre voit tous ses modules sans rien régler. L'invariant est sûr parce
       que la liste ne peut pas devenir vide par l'usage — ``settings`` est
       indésactivable —, donc « vide » signifie « jamais initialisée » et jamais
       « tout désactivé volontairement ».
    2. **Les ids non installés sont filtrés.** Un fantôme dans la config ne doit
       pas produire une entrée de barre qui ne mène nulle part. La migration les
       purge une fois pour toutes, ce filtre couvre l'entre-deux (module supprimé
       du disque pendant que l'instance tourne).
    3. **``settings`` est réinjecté** s'il est installé et absent : la liste
       pilote désormais le montage, l'en perdre débrancherait l'écran qui sert à
       le remettre.
    """
    installes = installed_ids()
    stockes = instance_config.enabled_modules()
    if not stockes:
        return installes
    connus = set(installes)
    actifs = [i for i in stockes if i in connus]
    if _MODULE_INDESACTIVABLE in connus and _MODULE_INDESACTIVABLE not in actifs:
        actifs.append(_MODULE_INDESACTIVABLE)
    return actifs


def list_modules() -> list[dict]:
    """Manifestes enrichis du ``status`` dérivé de l'appartenance à la liste.

    Le champ reste ``"active"|"disabled"`` : le frontend (``src/modules.ts``,
    ``ModuleManifest.status``) le lit tel quel, il n'a pas à connaître le
    changement de stockage. L'ordre reste celui de ``discover_manifests`` — c'est
    un catalogue, pas la barre ; l'ordre d'affichage vient de ``modules_activés``
    côté frontend.
    """
    actifs = set(active_ids())
    out: list[dict] = []
    for mf in discover_manifests():
        m = dict(mf)
        m["status"] = "active" if m["id"] in actifs else "disabled"
        out.append(m)
    return out


def get_module(module_id: str) -> Optional[dict]:
    return next((m for m in list_modules() if m.get("id") == module_id), None)


def register_routers(app) -> None:
    """Monte les routeurs des modules non-core actifs sur ``app``.

    Pour chaque module ``status="active"`` et ``core_module`` falsy :
    importe ``modules.<id>.router`` et fait ``app.include_router(router,
    prefix=manifest.backend.prefix)``. Les chemins déclarés dans le router sont
    donc relatifs au prefix (ex. prefix ``/hello`` + ``@router.get("/ping")`` →
    ``GET /hello/ping``).

    Traite TOUS les modules de façon uniforme (core ou non) : un module est
    monté dès qu'il est actif ET possède un ``router.py``. Les modules core pas
    encore migrés (sans router.py) restent décorés sur ``app`` dans main.py et
    sont simplement ignorés ici.

    « Actif » = appartenance à ``modules_activés`` (cf. :func:`active_ids`).
    L'ordre de MONTAGE reste celui de ``discover_manifests`` (alphabétique) et
    non celui de la liste : les modules générés sont montés avec un prefix vide,
    donc l'ordre décide qui gagne en cas de collision de chemin. Le laisser
    dépendre d'un glisser-déposer dans la barre ferait dépendre le routage de
    l'ordre d'affichage — deux choses qui n'ont rien à voir.
    """
    for m in list_modules():
        if m.get("status") != "active":
            continue
        mid = m.get("id")
        if not (_modules_dir() / str(mid) / "router.py").is_file():
            continue  # pas de backend pour ce module (ou core non migré)
        try:
            mod = importlib.import_module(f"modules.{mid}.router")
        except Exception:
            logger.exception("Module %s : import de modules.%s.router échoué", mid, mid)
            continue
        router = getattr(mod, "router", None)
        if router is None:
            logger.warning("Module %s : router.py ne définit pas 'router'", mid)
            continue
        prefix = (m.get("backend") or {}).get("prefix", "")
        try:
            app.include_router(router, prefix=prefix)
            logger.info("Module %s : routeur monté sur %s", mid, prefix or "/")
        except Exception:
            logger.exception("Module %s : include_router a échoué", mid)


def set_status(module_id: str, status: str) -> Optional[dict]:
    """Change le status (active|disabled). Retourne le module à jour ou None.

    Signature et contrat de retour INCHANGÉS — ``PUT /modules/{id}/status`` les
    expose. Seule l'implémentation change : au lieu d'écrire un second fichier
    d'état, la fonction ajoute l'id à ``modules_activés`` (en fin) ou l'en
    retire.

    Refuse : status invalide, module inconnu, ou désactivation de ``settings``.

    Activer met le module en FIN de liste : c'est la position d'un module
    « nouvellement vu », cohérente avec l'étape (c) de la migration. Désactiver
    puis réactiver perd donc la position d'origine — comportement assumé, la
    barre est réordonnable au glisser-déposer.
    """
    if status not in _VALID_STATUS:
        return None
    with _lock:
        if module_id not in set(installed_ids()):
            return None
        if module_id == _MODULE_INDESACTIVABLE and status != "active":
            return None
        actifs = active_ids()
        if status == "active":
            if module_id in actifs:
                return get_module(module_id)  # déjà actif : aucune écriture
            actifs.append(module_id)
        else:
            if module_id not in actifs:
                return get_module(module_id)  # déjà inactif : aucune écriture
            actifs = [i for i in actifs if i != module_id]
        instance_config.update({"modules_activés": actifs})
    return get_module(module_id)


def migrate_module_state() -> dict:
    """Migration unique au démarrage vers ``modules_activés`` seule source.

    Quatre étapes, dans cet ordre — chacune journalisée :

    a. tout module ``status: "disabled"`` d'un ancien ``modules_state.json`` est
       retiré de ``modules_activés`` **et mémorisé pour être exclu de (c)**.
       Sans cette exclusion, (a) retirerait le module et (c) le remettrait
       aussitôt : « désactivé » est une information explicite, « absent » n'en
       est pas une.
    b. tout id non installé est purgé (entrée fantôme).
    c. tout module installé absent de la liste est ajouté EN FIN, dans l'ordre
       de ``discover_manifests``. L'absence signifie « jamais vu ».
    d. ``modules_state.json`` est supprimé.

    ⚠️ (a) et (c) ne s'exécutent QUE lors d'une vraie bascule — ancien
    ``modules_state.json`` présent, ou liste vide (première initialisation). En
    régime établi, seule (b) tourne.

    C'est la correction d'un défaut réel, trouvé par le test d'idempotence :
    appliquer (c) à CHAQUE démarrage réajouterait tout module installé absent de
    la liste. Or, une fois la migration passée, « absent » ne veut plus dire
    « jamais vu » — ça veut dire « désactivé par l'utilisateur », puisque c'est
    exactement ce que fait :func:`set_status`. (c) à chaque boot annulait donc
    toute désactivation au redémarrage suivant, et la migration elle-même
    n'était pas idempotente : (a) exclut un module grâce à un fichier que (d)
    venait de supprimer, donc le passage suivant le réintégrait.

    (b) reste inconditionnelle, et reste idempotente : une fois les fantômes
    purgés il n'en reste pas, donc plus aucune écriture. Elle doit tourner à
    chaque démarrage — un module effacé du disque pendant que l'instance est
    arrêtée ne doit pas laisser une entrée morte dans la barre.

    Retourne un rapport (avant/après/détail) — consommé par les tests et par le
    journal de démarrage.
    """
    installes = installed_ids()
    connus = set(installes)
    with _lock:
        avant = list(instance_config.enabled_modules())
        actifs = list(avant)
        bascule = _legacy_state_file().is_file() or not avant

        # (a) désactivations héritées de l'ancien fichier d'état.
        desactives: list[str] = []
        legacy_present = _legacy_state_file().is_file()
        if legacy_present:
            etat = read_json(_legacy_state_file(), {})
            if isinstance(etat, dict):
                for mid, entree in etat.items():
                    if isinstance(entree, dict) and entree.get("status") == "disabled":
                        desactives.append(str(mid))
            for mid in desactives:
                if mid in actifs:
                    actifs.remove(mid)
                    logger.info("Migration modules (a) : %s désactivé (ancien état) → retiré", mid)
                else:
                    logger.info("Migration modules (a) : %s désactivé (ancien état) → restera hors liste", mid)

        # (b) fantômes : présents dans la config, absents du disque.
        fantomes = [i for i in actifs if i not in connus]
        for mid in fantomes:
            logger.info("Migration modules (b) : %s non installé → purgé", mid)
        actifs = [i for i in actifs if i in connus]

        # Dédoublonnage : un doublon rendrait l'ordre de la barre ambigu.
        vus: set[str] = set()
        dedup: list[str] = []
        for i in actifs:
            if i in vus:
                logger.info("Migration modules : %s en double → dédoublonné", i)
                continue
            vus.add(i)
            dedup.append(i)
        actifs = dedup

        # (c) installés jamais vus → ajoutés en fin. Les (a) restent exclus.
        # UNIQUEMENT en bascule : cf. l'avertissement de la docstring.
        exclus = set(desactives)
        ajoutes: list[str] = []
        if bascule:
            ajoutes = [i for i in installes if i not in vus and i not in exclus]
            for mid in ajoutes:
                logger.info("Migration modules (c) : %s installé et absent → ajouté en fin", mid)
            actifs.extend(ajoutes)
        else:
            manquants = [i for i in installes if i not in vus]
            if manquants:
                logger.debug(
                    "Migration modules : %r installés hors liste — laissés tels quels "
                    "(désactivations utilisateur, pas des oublis)", manquants,
                )

        # settings ne peut pas rester hors liste, même s'il était marqué disabled.
        if _MODULE_INDESACTIVABLE in connus and _MODULE_INDESACTIVABLE not in actifs:
            logger.info("Migration modules : %s réinjecté (indésactivable)", _MODULE_INDESACTIVABLE)
            actifs.append(_MODULE_INDESACTIVABLE)

        ecrit = actifs != avant
        if ecrit:
            instance_config.update({"modules_activés": actifs})
            logger.info("Migration modules : %r → %r", avant, actifs)
        else:
            logger.debug("Migration modules : rien à faire (liste déjà conforme)")

        # (d) suppression de l'ancien stockage.
        if legacy_present:
            try:
                _legacy_state_file().unlink()
                logger.info("Migration modules (d) : %s supprimé", _legacy_state_file().name)
            except OSError:
                logger.exception("Migration modules (d) : suppression de %s impossible", _legacy_state_file())

    return {
        "avant": avant,
        "après": actifs,
        "désactivés_hérités": desactives,
        "fantômes_purgés": fantomes,
        "ajoutés": ajoutes,
        "état_legacy_supprimé": legacy_present,
        "écrit": ecrit,
    }
