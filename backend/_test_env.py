"""Isolation des données de runtime pendant les tests. **À IMPORTER EN PREMIER.**

Huit arborescences sont détournées vers des temporaires :

    EPURE_DATA_DIR       backend/memory/                 (JSON de runtime)
    EPURE_HISTORY_DIR    backend/history/                (temporaire VIDE)
    EPURE_MODULES_DIR    backend/modules/                (copie)
    EPURE_GENERATED_DIR  frontend/src/modules/generated/ (copie)
    EPURE_MODELS_DIR     backend/piper_models/           (temporaire VIDE)
    EPURE_WEB_DIR        frontend/dist/                  (temporaire VIDE)
    EPURE_VECTOR_DIR     backend/vector_db/              (temporaire VIDE)
    EPURE_EMBEDDING_DIR  backend/embedding_model/        (temporaire VIDE)

(Cet en-tête annonçait « cinq » et en listait cinq sur six : `EPURE_VECTOR_DIR`
existait déjà et n'y figurait pas. Le compte est repris avec l'arrivée de
`EPURE_EMBEDDING_DIR` le 2026-08-26, puis de `EPURE_HISTORY_DIR` le 2026-08-27.)

⚠️ « Temporaire VIDE » ne dit RIEN du fait d'être surveillé ou non — les deux
propriétés sont indépendantes et les confondre est l'erreur naturelle ici.
`EPURE_HISTORY_DIR` est vide *et* surveillé (des conversations sont des données
utilisateur, irremplaçables) ; `EPURE_MODELS_DIR` est vide et *non* surveillé
(un cache de 76 Mo, reconstructible). Chaque bloc plus bas dit laquelle des deux
raisons s'applique.

Pourquoi ce fichier existe : la suite écrivait dans les données réelles de
l'utilisateur. Neuf modules construisaient leur chemin en
``Path(__file__).parent.parent / "memory" / …``, donc importer ``main`` suffisait
à toucher ``backend/memory/``. Le cas s'est produit pour de bon — la migration de
``modules_activés`` s'est exécutée sur la configuration de l'utilisateur au
premier passage de la suite, parce que ``main.py`` la lance à l'import et que
plusieurs tests montent l'app via ``TestClient``.

Les deux arborescences de modules ont été ajoutées **avant** d'écrire
``DELETE /settings/modules/{id}`` : cet endpoint fait un ``rmtree`` sur
``<modules>/<id>`` et ``<generated>/<id>``. Un test de suppression visant les
dossiers de production ne se rate qu'une fois.

Elles sont COPIÉES et non vides : les tests existants s'appuient sur un arbre
réaliste (``module_exists("chat")``, les manifestes du cœur, le ``hello`` de
référence). ``_backups`` est exclu de la copie — 1,2 Mo des 1,6 Mo, et ce n'est
qu'un historique de runtime.

Usage, dans chaque test qui importe ``core.*`` ou ``main`` :

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _test_env  # noqa: F401  — AVANT tout import de core.* ou main

L'ordre est la seule chose qui compte : ``core.paths.resolve_data_dir`` lit
``$EPURE_DATA_DIR`` À CHAQUE APPEL (jamais figée dans une constante de module),
mais les moteurs sont construits à l'import de ``core.runtime`` — si la variable
est posée après, le mal est déjà fait.

UN SEUL dossier pour toute la suite, et pas un par fichier : ``unittest
discover`` importe tous les modules de test dans le même process. Un dossier par
fichier ferait gagner le dernier importé, et les tests se marcheraient dessus de
façon dépendante de l'ordre de découverte.

Ce fichier ne s'appelle pas ``test_*.py`` : il ne doit pas être ramassé par la
découverte automatique.
"""

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent

#: Les arborescences réelles qu'aucun test ne doit toucher.
REAL_DATA_DIR = _BACKEND / "memory"
#: Les conversations sauvegardées. Surveillé au même titre que `memory/` et pour
#: la même raison — ce sont des données utilisateur que rien ne reconstruit — et
#: NON au titre des caches (`piper_models/`, `embedding_model/`, `vector_db/`),
#: qui sont détournés sans être surveillés.
#:
#: Absent de cette liste jusqu'au 2026-08-27, ce qui ne se voyait pas : le
#: dossier n'était atteignable que par un chemin figé à l'import de
#: `core/history.py`, donc aucun test ne pouvait y écrire *ni* le détourner. Le
#: chantier « conversations persistées » en fait le magasin vivant du chat —
#: c'est-à-dire, sans cette ligne, la cible d'une écriture par tour d'assistant
#: pendant toute la suite.
REAL_HISTORY_DIR = _BACKEND / "history"
REAL_MODULES_DIR = _BACKEND / "modules"
REAL_FRONTEND_MODULES = _REPO / "frontend" / "src" / "modules"
#: Le catalogue est du code VERSIONNÉ, source des modules installables. Aucune
#: variable ne le détourne — `core.catalogue.catalogue_dir()` est ancré sur
#: REPO_ROOT — et c'est voulu : personne ne doit y écrire. Un test qui a besoin
#: d'un catalogue variable détourne `catalogue_dir` lui-même (cf.
#: CycleReinstallationTest). Surveillé pour que l'oublier se voie.
REAL_CATALOGUE_DIR = _REPO / "modules-catalogue"
REAL_DIRS = (
    REAL_DATA_DIR, REAL_HISTORY_DIR, REAL_MODULES_DIR,
    REAL_FRONTEND_MODULES, REAL_CATALOGUE_DIR,
)

#: Non copiés dans l'arbre temporaire : `_backups` pèse 1,2 Mo des 1,6 Mo de
#: modules/ et n'est qu'un historique de runtime ; `_staging` et les caches sont
#: recréés à la demande par le code testé.
_IGNORES = shutil.ignore_patterns("_backups", "_staging", "__pycache__", "*.pyc")

#: Origines dont la présence dans ``backend/modules/`` dépend du POSTE et non du
#: dépôt : un module de catalogue s'installe et se désinstalle à volonté
#: (``POST /settings/catalogue/{id}/install``), un module d'Atelier n'existe que
#: chez qui l'a généré. Les deux sont gitignorés — un clone frais n'en a aucun.
#:
#: Les copier rendait l'arbre de test DIFFÉRENT sur chaque machine, et ça s'est
#: payé : ``test_catalogue.test_catalogue_liste_les_six_avec_installe`` affirme
#: « aucun installable ne doit l'être par défaut » et échouait en permanence sur
#: le poste de dev — parce que le module ``code`` y était réellement installé,
#: donc ``installé: True`` était la BONNE réponse à une question posée au mauvais
#: arbre. En CI, où le clone n'a que les modules versionnés, le même test passait.
#: Un échec qui ne se reproduit que chez son auteur finit par se lire comme du
#: bruit ; c'est ainsi qu'il a survécu longtemps.
#:
#: Même esprit que ``EPURE_WEB_DIR``, vidé pour le DÉTERMINISME (§3.5) : sans ça
#: le comportement de la suite dépend de ce que l'utilisateur a installé.
_ORIGINES_LOCALES = frozenset({"catalogue", "workshop"})


def _modules_du_poste() -> frozenset[str]:
    """Ids de ``backend/modules/`` qui viennent du poste et non du dépôt.

    Lu dans les manifestes plutôt qu'écrit en dur : la liste des installables
    change avec ``modules-catalogue/``, et une liste figée ici divergerait en
    silence. Le champ ``origin`` est déjà la source de vérité de cette
    distinction (§3.3).

    **Le défaut est de COPIER.** Un dossier sans manifeste (``_atelier``, ou un
    reliquat de désinstallation), un manifeste illisible ou sans ``origin`` ne
    sont pas exclus : mieux vaut un arbre trop riche — l'état d'avant, connu —
    qu'un module versionné qui disparaîtrait de la copie sans que personne le
    demande.
    """
    if not REAL_MODULES_DIR.is_dir():
        return frozenset()
    locaux: set[str] = set()
    for sub in REAL_MODULES_DIR.iterdir():
        mf = sub / "manifest.json"
        if not mf.is_file():
            continue
        try:
            origine = json.loads(mf.read_text(encoding="utf-8-sig")).get("origin")
        except Exception:
            continue
        if origine in _ORIGINES_LOCALES:
            locaux.add(sub.name)
    return frozenset(locaux)


#: Calculé une fois, avant toute copie. Exposé : les tests de détermination
#: (`test_arbre_modules_deterministe.py`) s'en servent pour dire ce qui a été
#: écarté, et un rapport « rien à écarter » est un résultat valide (cas de la CI).
MODULES_DU_POSTE = _modules_du_poste()


def _ignorer_aussi(par_dossier: dict[Path, frozenset[str]]):
    """Compose ``_IGNORES`` avec une exclusion par dossier source.

    Un rappel plutôt qu'un motif : ``shutil.ignore_patterns`` filtre sur les
    NOMS, et la distinction qu'on veut se lit dans le CONTENU des manifestes.
    Et par dossier, pour n'écarter ``code`` qu'à la racine des modules — pas un
    sous-dossier qui porterait le même nom quelque part dans l'arbre.
    """
    cibles = {p.resolve(): noms for p, noms in par_dossier.items()}

    def _filtre(src, names):
        exclus = set(_IGNORES(src, names))
        exclus |= cibles.get(Path(src).resolve(), frozenset())
        return exclus

    return _filtre


def _derive(chemin: Path) -> bool:
    """Artefact dérivé, pas une donnée : exclu de l'empreinte.

    Les ``__pycache__`` sont écrits par l'interpréteur lui-même dès qu'un module
    est importé — ``register_routers`` fait ``import modules.admin.router``, et
    le bytecode atterrit à côté de la source, dans le VRAI ``backend/modules/``.
    Rien ne l'empêche : ``EPURE_MODULES_DIR`` détourne la lecture des manifestes,
    pas la résolution du package ``modules`` par ``sys.path``.

    Les compter comme une écriture rendrait le garde-fou ingérable — il
    échouerait sur tout clone frais au premier import, ce qui est exactement ce
    qui s'est produit : l'arbre de travail avait déjà ses ``.pyc``, donc le
    problème ne s'y voyait pas. Un ``.pyc`` est régénérable, gitignoré, et ne
    porte aucune donnée utilisateur ; ce que le garde-fou surveille, ce sont les
    sources et les données.
    """
    return "__pycache__" in chemin.parts or chemin.suffix in (".pyc", ".pyo")


def _instantaner(dossier: Path) -> dict[str, tuple[int, float]]:
    """Empreinte (taille, mtime) par fichier — sert de témoin d'écriture."""
    if not dossier.is_dir():
        return {}
    out: dict[str, tuple[int, float]] = {}
    for p in sorted(dossier.rglob("*")):
        if p.is_file() and not _derive(p.relative_to(dossier)):
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(dossier))] = (st.st_size, st.st_mtime)
    return out


def _installer() -> Path:
    """Pose ``EPURE_DATA_DIR`` sur un temporaire, une seule fois par process.

    Respecte une valeur déjà posée : la CI ou un lanceur externe peut vouloir
    imposer son propre dossier, et deux imports de ce module ne doivent pas
    produire deux dossiers différents (le cache d'import de Python garantit
    déjà l'unicité, la vérification couvre le cas d'un ``importlib.reload``).
    """
    existant = os.environ.get("EPURE_DATA_DIR", "").strip()
    if existant:
        return Path(existant).expanduser().resolve()
    d = Path(tempfile.mkdtemp(prefix="epure-test-data-"))
    os.environ["EPURE_DATA_DIR"] = str(d)
    atexit.register(shutil.rmtree, d, True)
    return d


def _installer_vide(var: str, nom: str) -> Path:
    """Pose ``var`` sur un temporaire VIDE — pas une copie.

    Deux familles d'appelants, pour deux raisons différentes de ne pas copier :

    * les **caches reconstructibles** (``piper_models``, ``embedding_model``,
      ``vector_db``, ``dist``), dont on veut seulement qu'un test ne puisse pas
      écrire dans le vrai. Copier n'aurait aucun sens : le modèle vocal pèse
      76 Mo et la suite ne le lit jamais ;
    * les **données utilisateur dont le décompte doit être déterministe**
      (``history``). Copier les vraies conversations rendrait le résultat de
      ``list_conversations()`` dépendant de l'historique du poste : un test
      « la liste contient 2 conversations » passerait ici et échouerait en CI,
      sur un dossier vide. Vide = l'état d'une installation neuve, qui est
      précisément celui qu'il faut éprouver.

    Ce que cette fonction ne décide PAS : la surveillance. Un dossier vide peut
    être surveillé (``history``) ou non (les caches) — cf. ``REAL_DIRS``.
    """
    existant = os.environ.get(var, "").strip()
    if existant:
        return Path(existant).expanduser().resolve()
    d = Path(tempfile.mkdtemp(prefix=f"epure-test-{nom}-"))
    atexit.register(shutil.rmtree, d, True)
    os.environ[var] = str(d)
    return d


def _installer_arbre(var: str, source: Path, nom: str, exclusions=None) -> Path:
    """Pose ``var`` sur une COPIE de ``source`` dans un temporaire.

    Une copie et non un dossier vide : les tests existants s'appuient sur un
    arbre réaliste (``module_exists("chat")``, ``discover_manifests`` qui doit
    trouver les manifestes du cœur, le ``hello`` de référence lu par l'Atelier).
    Un dossier vide les ferait tous tomber pour de mauvaises raisons.

    C'est aussi ce qui rend testable ``DELETE /settings/modules/{id}`` : son
    ``rmtree`` frappe la copie. Sans ça, le premier test de suppression
    détruirait un vrai module.

    ``exclusions`` — ``{dossier source: noms à ne pas copier}`` — retire de la
    copie ce qui dépend du poste (cf. ``MODULES_DU_POSTE``). Filtré à la COPIE
    et non élagué après : un élagage laisserait une fenêtre où l'arbre est
    faux, et ce paramètre ne vaut pas pour tous les arbres — c'est pour ça
    qu'il est passé par l'appelant et non câblé ici.
    """
    existant = os.environ.get(var, "").strip()
    if existant:
        return Path(existant).expanduser().resolve()
    racine = Path(tempfile.mkdtemp(prefix=f"epure-test-{nom}-"))
    atexit.register(shutil.rmtree, racine, True)
    cible = racine / source.name
    if source.is_dir():
        filtre = _ignorer_aussi(exclusions) if exclusions else _IGNORES
        shutil.copytree(source, cible, ignore=filtre)
    else:
        cible.mkdir(parents=True, exist_ok=True)
    os.environ[var] = str(cible)
    return cible


#: Empreintes des VRAIS dossiers, prises avant tout import de core.* — témoins
#: comparés par test_zz_donnees_reelles.
REAL_SNAPSHOT = _instantaner(REAL_DATA_DIR)
REAL_SNAPSHOTS = {str(d): _instantaner(d) for d in REAL_DIRS}

#: Dossier temporaire où toute la suite écrit ses JSON de runtime.
DATA_DIR = _installer()

#: Conversations sauvegardées — temporaire VIDE, et **surveillé** (REAL_DIRS).
#: L'inverse exact des blocs ci-dessous : eux sont vides parce que ce sont des
#: caches qu'on ne surveille pas, celui-ci est vide pour le DÉTERMINISME du
#: décompte (cf. `_installer_vide`) alors que ce sont bel et bien des données
#: utilisateur, surveillées comme celles de `memory/`.
HISTORY_DIR = _installer_vide("EPURE_HISTORY_DIR", "history")

#: Copie de backend/modules/ — EPURE_MODULES_DIR pointe dessus. Les modules
#: installés sur CE poste (catalogue, Atelier) en sont écartés : l'arbre de test
#: doit être celui d'un clone frais, sinon la suite ne mesure pas la même chose
#: ici et en CI. Cf. `MODULES_DU_POSTE`.
MODULES_DIR = _installer_arbre(
    "EPURE_MODULES_DIR", REAL_MODULES_DIR, "modules",
    exclusions={REAL_MODULES_DIR: MODULES_DU_POSTE},
)

#: Cache des modèles vocaux — temporaire VIDE, et volontairement ABSENT de
#: REAL_DIRS. Deux raisons distinctes, à ne pas confondre :
#:
#:   * détourné, parce qu'un test qui construirait `PiperEngine` par accident
#:     tirerait 76 Mo dans le vrai cache (aucun ne le fait aujourd'hui, mais
#:     c'est le genre de chose qui ne se rate qu'une fois) ;
#:   * non surveillé, parce que ce n'est pas une donnée utilisateur mais un
#:     cache reconstructible : un téléchargement légitime pendant la suite ferait
#:     tomber un garde-fou qui parle d'autre chose (cf. resolve_models_dir).
MODELS_DIR = _installer_vide("EPURE_MODELS_DIR", "models")

#: Frontend construit — temporaire VIDE, et absent de REAL_DIRS comme MODELS_DIR.
#: Détourné pour une raison qui n'est pas la protection des données mais le
#: DÉTERMINISME : `main._register_web` ne monte le service statique que si le
#: dossier contient un `index.html`. Sur le vrai chemin, la suite se comporterait
#: donc différemment selon que quelqu'un a lancé `npm run build` — un test de
#: l'interface servie passerait en local et échouerait en CI, ou l'inverse, et le
#: pire est que ni l'un ni l'autre ne serait un bug. Vide = service éteint par
#: défaut ; le test qui le vise (`test_web_statique.py`) fabrique son propre
#: `dist/` et appelle `_register_web` dessus.
#:
#: Non surveillé : `frontend/dist/` est un artefact de build gitignoré, pas une
#: donnée utilisateur. Le surveiller ferait échouer la suite chez quiconque a
#: construit le front entre deux exécutions.
WEB_DIR = _installer_vide("EPURE_WEB_DIR", "web")

#: Store vectoriel (core/vector_store.py) — temporaire VIDE, et absent de
#: REAL_DIRS, pour les deux mêmes raisons distinctes que MODELS_DIR :
#:
#:   * détourné, parce qu'un test qui construirait `RAGEngine` par accident
#:     écrirait dans le VRAI index de l'utilisateur — ce qui était structurellement
#:     impossible à éviter avant, l'ancien chemin étant calculé en
#:     `dirname(config.yaml)/chroma_db` sans aucune surcharge possible ;
#:   * non surveillé, parce que c'est un index DÉRIVÉ, reconstructible en
#:     réindexant les fichiers surveillés — pas une donnée utilisateur. Le
#:     surveiller ferait tomber le garde-fou sur une réindexation légitime, qui
#:     ne prouve rien sur la propreté de `backend/memory/`.
VECTOR_DIR = _installer_vide("EPURE_VECTOR_DIR", "vecteurs")

#: Cache du modèle d'embedding ONNX — temporaire VIDE, absent de REAL_DIRS, pour
#: les deux mêmes raisons distinctes que MODELS_DIR, dont il est le jumeau :
#:
#:   * détourné, parce qu'un test qui construirait `MoteurEmbedding` par accident
#:     tirerait 90 Mo dans le vrai cache ;
#:   * non surveillé, parce que c'est un cache reconstructible, vérifié par
#:     sha256 (cf. resolve_embedding_dir), pas une donnée utilisateur.
#:
#: Vide, donc `pile_presente()` est FAUX pendant toute la suite : c'est la
#: configuration d'un paquet fraîchement installé, celle qu'il faut éprouver.
EMBEDDING_DIR = _installer_vide("EPURE_EMBEDDING_DIR", "embedding")

#: **Aucun test n'a le droit de télécharger les 90 Mo du modèle d'embedding.**
#:
#: `core/embedding_install.py` récupère `model.onnx` + `vocab.txt` à la demande
#: quand ils manquent — c'est la correction des « écarts 2 et 3 » de
#: `docs/distribution-empaquetee.md`. Or la suite tourne précisément dans cette
#: configuration, et pas par accident : `EMBEDDING_DIR` ci-dessus est un
#: temporaire VIDE, donc `pile_presente()` est faux pour tout le monde. Sans cette
#: ligne, le premier test qui touche une route du RAG lancerait 90 Mo de
#: téléchargement — sur le runner de la CI comme sur ce poste.
#:
#: Le volume a changé le 2026-08-26 (la pile était `pip install torch` +
#: `sentence-transformers`, ~2 Go), la règle non.
#:
#: Posée ici et non dans chaque fichier de test : la protection ne vaut que si
#: elle couvre aussi les tests qui n'ont pas conscience de toucher au RAG — c'est
#: exactement ce qui rend un garde-fou utile plutôt que décoratif. Un test qui
#: veut éprouver l'installation elle-même remet la variable à `1` pour sa durée
#: (`test_embedding_install.py`).
#:
#: `setdefault` : la CI ou un lanceur peut vouloir imposer autre chose, et deux
#: imports de ce module ne doivent pas se contredire.
os.environ.setdefault("EPURE_EMBEDDING_AUTOINSTALL", "0")


def _rebrancher_package_modules(cible: Path) -> None:
    """Fait résoudre ``import modules.<id>.…`` depuis l'arbre TEMPORAIRE.

    Sans ça, deux arbres coexistent pendant les tests et le code en lit un
    pendant qu'il en importe un autre :

      * ``EPURE_MODULES_DIR`` détourne la lecture des manifestes,
        ``_modules_safe_path`` et le ``rmtree`` de la suppression ;
      * ``sys.path`` ne bouge pas, donc ``importlib.import_module`` continue de
        servir le VRAI ``backend/modules/``.

    Mesuré, dans les deux sens :

      * après suppression d'un module du temporaire,
        ``import_module("modules.hello.router")`` RÉUSSIT encore depuis le vrai
        arbre — un test « ce module n'est plus montable » passerait pour la
        mauvaise raison ;
      * après installation d'un module dans le temporaire, l'import ÉCHOUE en
        ``ModuleNotFoundError`` — le montage à chaud de l'installation était donc
        purement intestable.

    ``modules`` est un *namespace package* (pas d'``__init__.py``) : son
    ``__path__`` est une liste, et lui réaffecter une liste d'un seul élément
    supprime la portion du vrai dépôt. Un ajout à ``sys.path`` ne suffirait pas —
    les portions d'un namespace package se cumulent, le vrai arbre resterait
    visible et la fuite du DELETE avec.

    Effet de bord bienvenu : le bytecode part maintenant dans le temporaire, et
    plus à côté des sources réelles (cf. ``_derive``, qui reste par ceinture et
    bretelles).
    """
    import modules  # noqa: PLC0415 — doit venir APRÈS le réglage de sys.path

    modules.__path__ = [str(cible)]


#: Copie de frontend/src/modules/ — EPURE_GENERATED_DIR pointe sur son
#: sous-dossier `generated`, dont core.paths déduit le parent.
#:
#: Mêmes exclusions, appliquées à `generated/` : c'est la MOITIÉ SYMÉTRIQUE du
#: même arbre. `catalogue.install()` écrit les deux côtés ensemble et
#: `uninstall()` les retire ensemble ; n'assainir que le backend laisserait un
#: `generated/code/` du poste face à un `modules/` de clone frais, c'est-à-dire
#: un état que ni ce poste ni la CI n'ont jamais.
_FRONTEND_COPIE = _installer_arbre(
    "EPURE_GENERATED_DIR", REAL_FRONTEND_MODULES, "frontend",
    exclusions={REAL_FRONTEND_MODULES / "generated": MODULES_DU_POSTE},
)
if _FRONTEND_COPIE.name != "generated":
    # _installer_arbre a copié `modules/` ; la variable doit désigner
    # `modules/generated`, le parent en étant déduit par core.paths.
    GENERATED_DIR = _FRONTEND_COPIE / "generated"
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["EPURE_GENERATED_DIR"] = str(GENERATED_DIR)
else:
    GENERATED_DIR = _FRONTEND_COPIE

# Un seul arbre de modules pour toute la suite : lu, importé, supprimé au même
# endroit. À faire APRÈS que MODULES_DIR existe, et AVANT tout import de core.*
# ou main — c'est-à-dire ici, à l'import de ce fichier.
_rebrancher_package_modules(MODULES_DIR)
