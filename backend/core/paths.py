"""Résolution centralisée et portable des chemins de données d'Épure.

Évite les chemins Windows absolus en dur : le dossier des fiches est résolu
via la variable d'environnement ``EPURE_FICHES_DIR`` si elle est définie, sinon
via un défaut relatif au dépôt (``<racine_du_repo>/data/fiches``). Ce module est
auto-suffisant (il charge ``.env`` lui-même) afin d'être indépendant de l'ordre
d'import des autres modules du package.
"""

import ntpath
import os
from pathlib import Path

from dotenv import load_dotenv

# backend/core/paths.py → backend/ → racine du dépôt
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent

# Charge backend/.env si présent (idempotent ; n'écrase pas les vars déjà posées).
load_dotenv(_BACKEND_DIR / ".env")


def resolve_fiches_dir() -> Path:
    """Dossier racine des fiches PDF (portable, configurable).

    Priorité : ``$EPURE_FICHES_DIR`` (``~`` accepté) puis défaut
    ``<racine_du_repo>/data/fiches``.
    """
    env = os.environ.get("EPURE_FICHES_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return _REPO_ROOT / "data" / "fiches"


def resolve_under_fiches(folder: str) -> Path:
    """Résout un dossier de config : absolu tel quel, sinon relatif à FICHES_DIR."""
    p = Path(folder).expanduser()
    return p if p.is_absolute() else (FICHES_DIR / folder)


def resolve_workspace() -> Path:
    """Répertoire de travail du codeagent (portable, configurable).

    Priorité : ``$EPURE_WORKSPACE`` (``~`` accepté) puis défaut
    ``<racine_du_repo>/workspace``. Toujours renvoyé résolu (``.resolve()``)
    pour servir de base sûre au confinement des chemins (cf. codeagent._safe_path).
    """
    env = os.environ.get("EPURE_WORKSPACE", "").strip()
    base = Path(env).expanduser() if env else (_REPO_ROOT / "workspace")
    return base.resolve()


#: Racine du dépôt et dossier backend — anchors STATIQUES, dérivés de
#: ``__file__``. Pas de surcharge d'environnement : ce sont des repères de
#: code source, pas des dossiers de données. À utiliser explicitement partout où
#: on veut « la racine du projet », et surtout PAS en remontant depuis un
#: dossier de données (``MODULES_DIR.parent.parent`` donnait la racine du dépôt
#: tant que MODULES_DIR n'était pas déplaçable — il l'est désormais).
BACKEND_DIR = _BACKEND_DIR
REPO_ROOT = _REPO_ROOT


def resolve_modules_dir() -> Path:
    """Dossier des modules backend (l'ancien ``backend/modules/`` en dur).

    Priorité : ``$EPURE_MODULES_DIR`` (``~`` accepté) puis défaut
    ``<backend>/modules``. Toujours résolu.

    ⚠️ **À APPELER, JAMAIS À FIGER** — cf. :func:`resolve_data_dir`.

    Pourquoi c'est surchargeable : ``DELETE /settings/modules/{id}`` fait un
    ``rmtree`` sur ``<modules>/<id>``, et ses tests détruiraient les vrais
    modules de l'utilisateur. Un endpoint destructif dont les tests visent le
    dossier de production est un accident qui n'attend que d'être écrit.
    """
    env = os.environ.get("EPURE_MODULES_DIR", "").strip()
    base = Path(env).expanduser() if env else (_BACKEND_DIR / "modules")
    return base.resolve()


def resolve_generated_dir() -> Path:
    """Dossier des composants générés (``frontend/src/modules/generated``).

    Priorité : ``$EPURE_GENERATED_DIR`` (``~`` accepté) puis le défaut ci-dessus.
    Toujours résolu. Même règle d'appel que :func:`resolve_modules_dir`.

    Le dossier PARENT (``frontend/src/modules``) est déduit de celui-ci
    (``.parent``) plutôt que d'avoir sa propre variable : les deux doivent
    bouger ensemble — un `generated/` détourné sous un parent resté en place
    ferait chercher le composant d'un module core dans un arbre et son composant
    généré dans un autre. Une seule variable rend l'incohérence impossible.
    """
    env = os.environ.get("EPURE_GENERATED_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (_REPO_ROOT / "frontend" / "src" / "modules" / "generated").resolve()


def resolve_models_dir() -> Path:
    """Cache des modèles de synthèse vocale (l'ancien ``piper_models`` relatif).

    Priorité : ``$EPURE_MODELS_DIR`` (``~`` accepté) puis défaut
    ``<backend>/piper_models``. Toujours résolu.

    ⚠️ **À APPELER, JAMAIS À FIGER** — cf. :func:`resolve_data_dir`.

    Pourquoi la fonction existe : ``PiperEngine`` recevait ``"piper_models"``,
    un chemin **relatif** résolu contre le répertoire courant. Ça n'a jamais
    fonctionné que parce que ``epure_tray.py`` lance uvicorn depuis
    ``backend/``. Lancé d'ailleurs, le moteur créait un dossier vide à côté du
    cwd et retéléchargeait 76 Mo — sans rien signaler, puisque « le modèle est
    absent » est un état normal désormais.

    ⚠️ Ce n'est **pas** un dossier de données utilisateur, c'est un **cache**.
    Il est délibérément absent de la liste surveillée par
    ``test_zz_donnees_reelles`` : un téléchargement légitime y écrirait 76 Mo et
    ferait tomber un garde-fou qui ne parle pas de ça. Le contenu est
    reconstructible à l'identique — c'est vérifié par sha256 à chaque
    téléchargement — donc rien d'irremplaçable n'y vit.
    """
    env = os.environ.get("EPURE_MODELS_DIR", "").strip()
    base = Path(env).expanduser() if env else (_BACKEND_DIR / "piper_models")
    return base.resolve()


def resolve_web_dir() -> Path:
    """Frontend **construit** que FastAPI sert lui-même (paquet distribué).

    Priorité : ``$EPURE_WEB_DIR`` (``~`` accepté) puis défaut
    ``<racine_du_repo>/frontend/dist``. Toujours résolu.

    ⚠️ **À APPELER, JAMAIS À FIGER** — cf. :func:`resolve_data_dir`.

    Pourquoi ce dossier est surchargeable alors que c'est du code construit et
    non des données : c'est la variable qui rend le service statique
    **déterministe en test**. Le montage est conditionné à la présence d'un
    ``index.html`` — sans surcharge, la suite se comporterait différemment selon
    que quelqu'un a lancé ``npm run build`` sur le poste ou non, et un test de
    l'interface servie passerait en local pour échouer en CI (ou l'inverse).
    ``_test_env`` la pose donc sur un temporaire VIDE, comme
    ``EPURE_MODELS_DIR`` : par défaut, en test, le service statique est éteint,
    et le test qui le vise fabrique son propre ``dist/``.

    En mode développement le dossier n'existe pas (ou est ignoré) : Vite sert
    l'interface sur :5173 et rien n'est monté ici.
    """
    env = os.environ.get("EPURE_WEB_DIR", "").strip()
    base = Path(env).expanduser() if env else (_REPO_ROOT / "frontend" / "dist")
    return base.resolve()


def resolve_vector_dir() -> Path:
    """Dossier du store vectoriel qui remplace ``backend/chroma_db/``.

    Priorité : ``$EPURE_VECTOR_DIR`` (``~`` accepté) puis défaut
    ``<backend>/vector_db``. Toujours résolu.

    ⚠️ **À APPELER, JAMAIS À FIGER** — cf. :func:`resolve_data_dir`.

    Pourquoi un dossier NEUF et pas ``chroma_db/`` réutilisé : la migration
    (``migrer_vectoriel.py``) doit pouvoir écrire le nouveau store pendant que
    l'ancien reste intact et interrogeable, puisque la comparaison de parité les
    fait tourner **côte à côte** sur les mêmes données. Écrire dans le dossier
    source rendrait impossible de revenir en arrière — et le plan
    (``docs/remplacement-vectoriel.md``, étape C) interdit explicitement de
    supprimer ``chroma_db/`` avant d'avoir fait tourner l'application réelle sur
    le nouveau store.

    Pourquoi c'est surchargeable : c'est ce qui rend la comparaison de parité et
    les tests reproductibles ailleurs que sur le vrai dossier de l'utilisateur.
    L'ancien chemin ne l'était pas — ``RAGEngine`` le calculait en
    ``dirname(config.yaml)/chroma_db``, donc un test qui aurait construit le
    moteur aurait écrit dans l'index réel.
    """
    env = os.environ.get("EPURE_VECTOR_DIR", "").strip()
    base = Path(env).expanduser() if env else (_BACKEND_DIR / "vector_db")
    return base.resolve()


def resolve_data_dir() -> Path:
    """Dossier des JSON de runtime (l'ancien ``backend/memory/`` en dur).

    Priorité : ``$EPURE_DATA_DIR`` (``~`` accepté) puis défaut
    ``<backend>/memory``. Toujours renvoyé résolu, comme
    :func:`resolve_workspace`.

    ⚠️ **À APPELER, JAMAIS À FIGER DANS UNE CONSTANTE DE MODULE.** C'est tout
    l'intérêt de la fonction, et c'est une contrainte, pas un détail de style.
    Neuf modules construisaient leur chemin en ``Path(__file__).parent.parent /
    "memory" / …`` au niveau module. Un chemin calculé à l'import est figé à
    l'import : une variable d'environnement posée ensuite n'a plus aucun effet,
    et l'ordre d'import devient une dépendance invisible. La conséquence a été
    payée : la suite de tests écrivait dans les données réelles, au point
    d'exécuter pour de bon la migration de ``modules_activés`` sur la
    configuration de l'utilisateur.

    Attention particulière aux **arguments par défaut** : ``def __init__(self,
    path=_CONSTANTE)`` est évalué à la définition de la fonction, donc à
    l'import — même piège, moins visible. Utiliser ``path=None`` puis résoudre
    dans le corps.

    Le comportement est verrouillé par ``test_data_dir.py``, qui pose la
    variable APRÈS avoir importé les modules et vérifie que l'écriture suit.
    """
    env = os.environ.get("EPURE_DATA_DIR", "").strip()
    base = Path(env).expanduser() if env else (_BACKEND_DIR / "memory")
    return base.resolve()


#: Dossier racine des fiches, résolu une fois au chargement du module.
FICHES_DIR = resolve_fiches_dir()

#: Dépôt des PDF envoyés au module Docs (analyse documentaire).
DOC_UPLOADS_DIR = _BACKEND_DIR / "doc_uploads"


class PathOutsideDataError(ValueError):
    """Un chemin venant du client désigne une cible hors des dossiers de données."""


def user_data_roots() -> list[Path]:
    """Dossiers qu'une requête du client a le droit de désigner.

    Une seule définition, partagée par tous les endpoints qui acceptent un
    chemin (``/admin/open``, ``/files/load``, ``/docanalysis/load``) : sans ça
    chacun se construit sa propre liste et elles divergent — c'est exactement
    comme ça que les dossiers surveillés (``fiches.watch_folders``, souvent hors
    de la racine des fiches) se retrouvent refusés dans un endpoint et acceptés
    dans un autre.

    Volontairement absents : ``backend/`` (``.env``, ``memory/`` — donc le token
    d'API) et le dépôt lui-même.
    """
    # Import local : core.instance importe core.paths pour FICHES_DIR, un import
    # en tête de fichier créerait un cycle.
    from core.instance import fiches_root, fiches_watch_paths

    roots = [fiches_root(), *fiches_watch_paths(), resolve_workspace(), DOC_UPLOADS_DIR]
    out: list[Path] = []
    for r in roots:
        try:
            resolved = Path(r).expanduser().resolve()
        except OSError:          # racine mal configurée : on l'ignore
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def safe_upload_name(filename: str, default: str) -> str:
    """Nom de fichier d'un upload, réduit à un segment sûr — ou refus explicite.

    ``dossier / filename`` n'est pas une composition anodine quand ``filename``
    vient du client : ``Path("/a") / "/etc/passwd"`` vaut ``/etc/passwd`` (un nom
    absolu remplace la base) et ``../../x`` sort du dossier.

    Le découpage passe par ``ntpath`` **sur les deux plateformes**, et c'est
    volontaire : ``PurePosixPath("..\\..\\evil.json").name`` renvoie la chaîne
    entière sous Linux, où l'antislash n'est pas un séparateur. Un nom refusé
    sous Windows passerait donc en CI Linux, et le fichier créé serait un piège
    pour une exécution ultérieure sous Windows. ``ntpath`` connaît aussi les
    préfixes de lecteur (``C:evil.json``).

    Refus (et non simple nettoyage) dès que le nom n'était pas déjà un segment
    nu : le navigateur envoie ``f.name``, jamais un chemin — une tentative doit
    donc rester visible plutôt que d'être acceptée sous un autre nom.
    """
    raw = (filename or "").strip()
    if not raw:
        return default
    name = ntpath.basename(ntpath.normpath(raw))
    if name != raw or name in (".", ".."):
        raise PathOutsideDataError(f"Nom de fichier invalide : {filename!r}")
    return name


def resolve_user_path(path: str) -> Path:
    """Résout un chemin venant du client, ou lève :class:`PathOutsideDataError`.

    Confinement par ``resolve()`` puis ``is_relative_to()``, jamais par
    ``startswith`` de chaînes (contournable par un dossier frère du type
    ``data/fiches-autre/``). Le chemin résolu est renvoyé : les appelants doivent
    utiliser CETTE valeur et pas la chaîne d'origine, sinon la vérification ne
    porte pas sur ce qui est réellement ouvert.
    """
    try:
        target = Path(path).expanduser().resolve()
    except OSError as exc:
        raise PathOutsideDataError(f"Chemin invalide : {path!r}") from exc
    if not any(target.is_relative_to(root) for root in user_data_roots()):
        raise PathOutsideDataError("Chemin hors des dossiers de données d'Épure")
    return target
