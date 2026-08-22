#!/usr/bin/env python3
"""Assemble un paquet Épure pour un destinataire donné — étape B.

`docs/distribution-empaquetee.md`. **Ce script ne part JAMAIS chez le
destinataire** : il vit dans `tools/`, qui n'est pas copié dans le paquet, et
c'est vérifié par `backend/test_paquet.py`. Il tourne sur le poste de build
d'Ilyann, qui est le seul endroit où il a un sens — il a besoin de `npm`, du
dépôt complet, et d'une architecture identique à celle de la cible.

    python tools/faire_paquet.py --destinataire sandr --modules flashcards,reviseur

Ce qu'il produit, sous `--sortie` (défaut `dist-paquets/`) :

    epure-<destinataire>-<horodatage>.zip
      python/                  runtime embeddable 3.12 + site-packages
      app/backend/             le code, sans les données ni l'Atelier
      app/backend/.env         ÉCRIT ici (jamais copié) — éteint l'Atelier
      app/backend/modules/<id> les modules choisis, déjà installés
      app/frontend/dist/       l'interface construite en mode paquet
      PAQUET.json              ce qui a été mis dedans, et avec quoi

L'arborescence reproduit celle du dépôt (`app/` tient le rôle de racine) pour que
**tous les défauts de `core/paths.py` tombent juste sans une seule variable
d'environnement** : `resolve_web_dir()` trouve `app/frontend/dist`,
`resolve_modules_dir()` trouve `app/backend/modules`, `resolve_data_dir()` crée
`app/backend/memory`. Poser cinq variables dans un lanceur serait cinq occasions
d'en oublier une, et l'oubli se verrait tard.

Trois décisions qui méritent d'être écrites ici, parce qu'elles ne se déduisent
pas du code :

**1. L'Atelier est désactivé, pas retiré.** `core/catalogue.py` importe sept
symboles de `core/module_workshop.py`, qui importe lui-même `core/
module_validate.py` : supprimer ces fichiers casserait l'écran Réglages du
destinataire, pas l'Atelier. Ce qu'on retire, ce sont les routes
(`EPURE_ATELIER=0`) et l'écran (`VITE_ATELIER=0`, qui le sort du bundle au lieu
de le cacher).

**2. `modules-catalogue/` ne part pas.** Installer un module depuis le catalogue
écrit un `Component.tsx` dans les sources du frontend — ce qui suppose un build.
Dans un paquet il n'y a ni `npm` ni sources : le backend monterait le module et
l'interface n'aurait pas son composant. Le destinataire peut donc **activer et
désactiver** ce qu'il a reçu (`docs/distribution-empaquetee.md` étape D), pas
installer autre chose. Sans catalogue livré, `GET /settings/catalogue` renvoie une
liste vide et le bouton n'apparaît pas — l'incapacité est honnête plutôt que
cassée.

**3. `torch` n'est pas embarqué** (décision du plan) : il s'installe au premier
usage du RAG documentaire. `sentence-transformers` est donc exclu de
l'installation.

⚠️ **Sur Windows ARM64, ce téléchargement doit viser l'index PyTorch et non
PyPI** : `pip install torch --index-url https://download.pytorch.org/whl/cpu`,
AVANT `sentence-transformers` (sinon torch se résout depuis PyPI). PyPI ne
publie que des wheels `win_amd64` pour torch ; l'index PyTorch publie bien
`torch-2.13.0+cpu-cp312-cp312-win_arm64.whl` — donc cp312, la version embarquée
ici. Vérifié le 2026-08-13, cf. `backend/requirements.txt`, qui porte la
consigne là où le destinataire la lira (ce fichier-ci ne part pas dans le
paquet). Tout le reste de la grappe du premier usage a déjà ce qu'il faut pour
ARM64 : torch était le seul manquant.

**4. `google-generativeai` et son arbre transitif ne partent pas** — il tire à
lui seul `googleapiclient` (97,9 Mo) et toute la chaîne
`google-api-core`/`google-auth`/`google-ai-generativelanguage`. Rien d'autre
n'en dépend, donc l'exclure de l'installation (`HORS_PAQUET_PIP`) suffit à faire
disparaître tout l'arbre.

**5. Sur ARM64, la voix ne part pas — décision du 2026-08-22.** `--arch arm64`
retire `faster-whisper` et `piper-tts` de l'installation (`HORS_PAQUET_PIP_ARM64`,
qui explique pourquoi ces deux-là et pas d'autres). Ce n'est pas une optimisation
de taille : **sans cette exclusion le paquet ARM64 n'est pas installable du
tout.** `ctranslate2`, dont dépend `faster-whisper`, ne publie aucune wheel
`win_arm64` ni aucune sdist — `pip install -r requirements.txt` échoue avant
que le backend ait la moindre chance de démarrer. Le blocage est à
l'installation, pas à l'usage, et c'est ce qui le rend non contournable côté
code.

L'alternative écartée : compiler `piper-tts` depuis sa sdist sur la machine
cible, ce qui demande un toolchain C++ chez le destinataire — exactement ce que
`docs/remplacement-vectoriel.md` a refusé pour `chromadb`. Refuser là et
accepter ici aurait été incohérent.

Ce que le backend en fait : `core/voice.py::capacites_vocales()` détecte
l'absence des paquets (par `find_spec`, sans les importer), `GET
/voice/capabilities` l'expose, et l'interface masque micro et lecture à voix
haute. Un paquet sans voix est donc une instance cohérente, pas une instance
amputée — et `PAQUET.json` porte `arch` et `voix` pour que ça se lise sans
deviner.

**Ce qui n'est plus ici, et pourquoi c'est la vraie nouvelle.** Ce docstring a
longtemps décrit un second mécanisme, bien plus lourd, pour contenir la grappe de
`chromadb` : une purge par lecture des `RECORD` de `.dist-info`
(`PURGE_DISTRIBUTIONS`) pour `grpcio` et
`opentelemetry-exporter-otlp-proto-grpc`, que `chromadb` déclarait en
dépendances **directes et inconditionnelles** — donc que `pip` réinstallait quoi
qu'on écrive dans `requirements.txt` — plus un `sitecustomize.py` stubant
`OTLPSpanExporter`, sans lequel `import chromadb` cassait une fois ses propres
dépendances retirées. Trois couches de contournement, chacune posée parce que la
précédente avait exposé la suivante.

Tout cela a disparu avec `chromadb` lui-même (`docs/remplacement-vectoriel.md`,
étape D). Vérifié par carte de dépendances inverse avant retrait, pas supposé :
`grpcio`, `kubernetes`, `opentelemetry-exporter-otlp-proto-grpc`,
`googleapis-common-protos`, `pypika` et `mmh3` n'étaient réclamés que par
`chromadb` (ou par sa propre grappe). `onnxruntime`, qu'on aurait pu croire du
lot, RESTE : `faster-whisper` et `piper-tts` en dépendent aussi.

C'est la démonstration que le remplacement traitait la cause et pas le symptôme —
le script rétrécit au lieu de s'allonger. Ne pas réintroduire ces mécanismes pour
un autre paquet sans se demander d'abord si c'est le paquet qui est mal choisi.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

# Sortie en UTF-8 : la console Windows est en cp1252 par défaut, et les messages
# de ce script sont en français. Sans ça, « module installé » ressort en
# « module installÃ© » dans le journal du build — le même mojibake que celui déjà
# payé sur la sortie d'aider (CLAUDE.md §8). `errors="replace"` plutôt que de
# laisser lever : un paquet ne doit pas échouer sur un accent.
for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(encoding="utf-8", errors="replace")

#: tools/faire_paquet.py → racine du dépôt. Anchor statique, comme
#: `core.paths.REPO_ROOT` : ce script n'a aucune raison d'être déplaçable.
REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
CATALOGUE = REPO / "modules-catalogue"

#: Python visé — aligné sur `docs/installeur.md` étape B et sur la CI (3.12).
#: Dernière 3.12 à avoir une release binaire Windows, donc un zip embeddable.
VERSION_PYTHON = "3.12.10"

#: Architectures cibles. `amd64` reste le défaut : c'est celle du poste de build
#: d'Ilyann (`platform.machine()` → AMD64) et celle de tous les paquets produits
#: jusqu'ici. `arm64` existe parce que la cible sandr en est une, et parce que
#: l'architecture change ce qui s'INSTALLE (cf. `HORS_PAQUET_PIP_ARM64`), pas
#: seulement l'exécutable Python.
ARCHS = ("amd64", "arm64")

#: python.org publie bien les deux zips embeddables — vérifié par requête HEAD
#: réelle le 2026-08-22, pas supposé :
#:   python-3.12.10-embed-amd64.zip → 200, 11 133 606 o
#:   python-3.12.10-embed-arm64.zip → 200, 10 413 299 o
URL_EMBEDDABLE = "https://www.python.org/ftp/python/{v}/python-{v}-embed-{arch}.zip"
URL_GET_PIP = "https://bootstrap.pypa.io/pip/get-pip.py"


def arch_hote() -> str:
    """Architecture du poste de build, pour servir de défaut à `--arch`.

    Appelée et non figée dans une constante : c'est la règle du dépôt pour tout ce
    qui dépend de l'environnement (cf. `core/paths.py`), et ça rend la fonction
    testable sans monkey-patcher un global.
    """
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"

#: `pip freeze` du paquet du 2026-08-10 (§0.3 de `docs/distribution-empaquetee.md`) —
#: fige l'arbre transitif, que `requirements.txt` seul laisse dériver (mesuré :
#: `google-generativeai==0.8.6` rétrograde `protobuf` selon ce qui est publié sur
#: PyPI au moment du build). Utilisé par défaut ; `--sans-contraintes` régénère ce
#: fichier lui-même, `--contraintes` en pointe un autre.
CONTRAINTES_DEFAUT = REPO / "tools" / "contraintes-paquet.txt"

#: Exclus de l'installation : `sentence-transformers` tire torch (~2 Go),
#: téléchargé au premier usage du RAG. `google-generativeai` tire à lui seul
#: `googleapiclient` (97,9 Mo — le plus gros poste du paquet) et toute la
#: chaîne `google-api-core`/`google-auth`/`google-ai-generativelanguage` :
#: rien d'autre dans `requirements.txt` n'en dépend, donc l'exclure suffit à
#: faire disparaître tout l'arbre transitif de résolution. Cf. décisions 3 et
#: 4 du docstring.
HORS_PAQUET_PIP = ("sentence-transformers", "google-generativeai")

#: Exclus **de l'installation ARM64 seulement** : la voix y est déclarée
#: indisponible (décision du 2026-08-22, `docs/remplacement-vectoriel.md`). Le x64
#: n'est pas touché.
#:
#: Ces deux-là précisément, et pas une liste au jugé — chacun a été vérifié sur
#: ce qu'il PUBLIE (étape E du même document, mesurée le 2026-08-13) :
#:
#: - **`faster-whisper`** : le paquet lui-même est du Python pur, mais il dépend
#:   de **`ctranslate2`**, qui ne publie AUCUNE wheel `win_arm64` **et aucune
#:   sdist**. Rien à compiler même en acceptant de compiler : il n'y a pas de
#:   source sur PyPI. C'est le blocage dur, et il est à l'INSTALLATION.
#: - **`piper-tts`** : extension compilée publiée en `cp39-abi3-win_amd64`
#:   seulement. Une sdist existe, donc `pip` la tenterait — c'est-à-dire un
#:   toolchain C++ à installer sur la machine du destinataire, exactement ce que
#:   ce chantier a écarté pour `chromadb` (§0 du plan). Décision prise : on
#:   n'essaie pas.
#:
#: Ce qui n'est PAS ici et qui pourrait surprendre : **`onnxruntime` reste**. On
#: pourrait croire qu'il part avec la voix, mais il publie une wheel `win_arm64`
#: et rien n'oblige à l'écarter. **`torch` non plus** : sa wheel `win_arm64`
#: existe sur l'index PyTorch (cf. décision 3) — l'écarter par confusion coûterait
#: le RAG, qui marche parfaitement sur ARM64.
#:
#: Le point à retenir si cette liste doit changer un jour : le critère est
#: « pip échoue-t-il à INSTALLER, ou exige-t-il un compilateur sur la machine
#: cible ? ». Pas « est-ce que ça a l'air lié à la voix ? ».
HORS_PAQUET_PIP_ARM64 = ("faster-whisper", "piper-tts")

#: Retiré du `site-packages` APRÈS installation, par simple nom de dossier
#: sous `site-packages/`. `pip` et ses compagnons n'ont rien à faire dans le
#: paquet : le destinataire n'installe rien. N'y mettre que des paquets qui
#: s'installent bien comme UN dossier `site-packages/<nom>/`.
#:
#: `kubernetes` y a figuré (37,8 Mo, dépendance déclarée de chromadb que seul
#: son chemin de déploiement distribué importe). Retiré de cette liste avec
#: chromadb lui-même : il n'est plus installé du tout, donc il n'y a plus rien à
#: purger. Une entrée de purge qui ne correspond à rien n'est pas neutre — elle
#: fait croire, à la relecture, qu'un paquet est encore là et surveillé.
PURGE_SITE_PACKAGES = ("pip", "setuptools", "pkg_resources")

#: Dossiers de DONNÉES, exclus **à la racine de `backend/` seulement**. Ce sont
#: les données d'Ilyann : `memory/` contient son token d'API et son profil,
#: `history/`, `chroma_db/` et `vector_db/` ses conversations, `doc_uploads/`
#: ses PDF. Un paquet qui en emporterait un seul serait à rappeler auprès de son
#: destinataire — ce dont on ne se remet pas. C'est la raison d'être de
#: `backend/test_paquet.py`.
#:
#: ⚠️ `vector_db/` (le store qui remplace chromadb) est arrivé dans cette liste
#: EN MÊME TEMPS que le code qui l'écrit, et c'est la seule façon acceptable de
#: procéder : il contient le TEXTE des fiches et des PDF indexés, pas seulement
#: des vecteurs. Remplacer un stockage sans étendre cette liste aurait livré au
#: destinataire les documents d'Ilyann dans un fichier au nom neuf que personne
#: ne surveillait. `chroma_db/` y reste tant que l'ancien index existe sur le
#: disque (étape C.4 de `docs/remplacement-vectoriel.md`) — une entrée qui ne
#: correspond à aucun dossier ne coûte rien ici, contrairement à une purge.
#:
#: ⚠️ **Ancré à la racine, et ce n'est pas un détail.** Testé en écrivant ces
#: noms comme excluables à n'importe quelle profondeur : `modules/history/` — le
#: module core Historique — disparaissait du paquet, parce qu'il porte le même
#: nom que le dossier de données `backend/history/`. Le paquet se construisait
#: sans erreur et le destinataire n'avait tout simplement pas d'historique.
EXCLUS_RACINE = frozenset({
    "memory", "history", "chroma_db", "vector_db", "doc_uploads", "piper_models",
})

#: Dossiers exclus à **n'importe quelle profondeur** : caches, et dossiers de
#: travail de l'Atelier (staging en cours, sauvegardes, prompts).
EXCLUS_PARTOUT = frozenset({
    "__pycache__", ".pytest_cache", "_staging", "_backups", "_atelier",
})

#: Fichiers de `backend/` qui ne partent jamais. `.env` porte toutes les clés
#: d'API cloud d'Ilyann.
EXCLUS_FICHIERS = frozenset({".env", ".env.local", ".aider.conf.yml"})

#: Motifs exclus, appliqués au NOM du fichier.
EXCLUS_MOTIFS = ("*.pyc", "*.pyo", "*.log", ".aider.*", "*.onnx", "*.onnx.json")

#: Code de test et outillage de développement : sans intérêt pour le
#: destinataire, et `_test_env.py` détourne des chemins.
EXCLUS_PREFIXES_FICHIERS = ("test_", "integration_", "_test_env")

#: Outils de maintenance à la racine de `backend/`, pour le poste d'Ilyann
#: uniquement. Ils ne sont ni des tests (les préfixes ci-dessus ne les attrapent
#: pas) ni du code applicatif : ce sont les scripts uniques du remplacement de
#: chromadb (`docs/remplacement-vectoriel.md`, étape C). Les livrer serait doublement
#: faux — ils exigent `chromadb`, qui n'est plus installé nulle part, et ils
#: parlent d'un ancien index que le destinataire n'a jamais eu. Constatés partis
#: dans le paquet du 2026-08-13 avant d'être exclus ici.
EXCLUS_MAINTENANCE = frozenset({"migrer_vectoriel.py", "parite_vectorielle.py"})

#: Fichiers de `core/` que seul l'Atelier utilise et qu'aucun autre module
#: n'importe. Vérifié : `smoke_runner.py` est lancé en sous-process par
#: `module_workshop`, `module_worker.py` n'a aucun importeur (chantier CLAUDE.md
#: §7). `module_validate.py` n'est PAS ici — `module_workshop` l'importe au
#: niveau module, donc le retirer casserait `catalogue.py`.
EXCLUS_CORE_ATELIER = frozenset({"smoke_runner.py", "module_worker.py"})

#: Contenu du `.env` écrit DANS le paquet — le seul fichier de configuration que
#: le destinataire reçoit pré-rempli.
#:
#: Pourquoi il existe : `VITE_ATELIER=0` sort l'Atelier du BUNDLE, donc de
#: l'écran, mais les ROUTES backend (`/workshop*`, `/ws/workshop`,
#: `/settings/test/`, `/settings/gateway/`) sont gouvernées par une variable
#: d'environnement lue au démarrage — `main.py` : ``os.environ.get(
#: "EPURE_ATELIER", "1")``, donc **actif par défaut**. Le paquet ne contenant
#: aucun lanceur, personne ne posait cette variable : jusqu'à ce commit,
#: l'Atelier d'un paquet livré était invisible et pourtant **joignable en HTTP**.
#: `PAQUET.json` affirmait `"atelier": false` — une métadonnée exacte sur
#: l'intention et fausse sur l'état réel de l'instance.
#:
#: Pourquoi un `.env` et pas un lanceur : `core/paths.py` fait
#: ``load_dotenv(_BACKEND_DIR / ".env")`` à l'import, et `main.py` importe
#: `core.admin` (donc `core.paths`) AVANT de lire `EPURE_ATELIER`. Le fichier est
#: donc honoré quelle que soit la façon dont uvicorn est lancé — raccourci,
#: service, ligne de commande tapée à la main. Un lanceur ne couvrirait que sa
#: propre invocation.
#:
#: ⚠️ Ce fichier porte le même nom que celui d'Ilyann, qui contient toutes ses
#: clés d'API cloud et que `EXCLUS_FICHIERS` interdit de copier. Les deux règles
#: coexistent parce qu'elles parlent de deux gestes différents : **on ne copie
#: jamais**, on **écrit** un contenu connu, sans valeur secrète, entièrement
#: visible ci-dessous. `backend/test_paquet.py` tient les deux bouts.
#:
#: `PUT /settings/api-keys` (`dotenv_set_key`) ajoutera les clés du destinataire à
#: la suite de ce fichier : le pré-remplir ne lui coûte rien.
ENV_PAQUET = """# Épure — configuration de cette instance.
#
# Écrit par tools/faire_paquet.py à l'assemblage du paquet. Vos clés d'API
# s'ajoutent ici automatiquement quand vous les saisissez dans Réglages ; vous
# pouvez aussi les écrire à la main (cf. .env.example, à côté de ce fichier).

# L'Atelier (génération de modules par un LLM) n'est pas livré dans ce paquet.
# Cette ligne coupe ses routes côté serveur ; l'écran, lui, est absent du bundle.
# Ne pas la retirer : sans elle les routes redeviennent joignables alors que
# l'interface qui va avec n'existe pas.
EPURE_ATELIER=0
"""


def atelier_actif_selon(env: Path) -> bool:
    """L'Atelier serait-il actif dans une instance démarrée avec ce `.env` ?

    Rejoue la règle de `main.py` (``os.environ.get("EPURE_ATELIER", "1").strip()
    != "0"``) sur le fichier **réellement écrit**, plutôt que de la supposer.

    C'est ce qui fait de `PAQUET.json` un constat et non une déclaration : la
    version précédente écrivait `"atelier": False` en dur, ce qui restait vrai sur
    le papier pendant que les routes répondaient. Une métadonnée qui affirme un
    état sans le mesurer finit par le décrire faux, et c'est arrivé ici.

    Absent ou non renseigné → `True`, comme le défaut de `main.py` : ne jamais
    rendre une absence rassurante.
    """
    if not env.is_file():
        return True
    for ligne in env.read_text(encoding="utf-8").splitlines():
        nu = ligne.strip()
        if not nu or nu.startswith("#") or "=" not in nu:
            continue
        cle, _, valeur = nu.partition("=")
        if cle.strip() == "EPURE_ATELIER":
            return valeur.strip().strip("'\"") != "0"
    return True


def ecrire_env(cible_backend: Path, journal=print) -> Path:
    """Pose :data:`ENV_PAQUET` dans `app/backend/.env`. Cf. son commentaire."""
    env = cible_backend / ".env"
    env.write_text(ENV_PAQUET, encoding="utf-8")
    journal("  .env : Atelier éteint côté serveur (EPURE_ATELIER=0)")
    return env



class ErreurPaquet(RuntimeError):
    """Assemblage refusé — message destiné à Ilyann, pas une trace de pile."""


# ── Sélection des modules ────────────────────────────────────────────────────

def modules_disponibles() -> dict[str, dict]:
    """Manifestes du catalogue, par id. C'est la seule source de modules livrables.

    Pas `backend/modules/` : l'arbre installé d'Ilyann contient son `hello` de
    référence, ses modules d'Atelier en cours, et les dossiers de travail. Le
    catalogue est versionné et revu — c'est ce qu'on envoie.
    """
    trouves: dict[str, dict] = {}
    if not CATALOGUE.is_dir():
        return trouves
    for entree in sorted(CATALOGUE.iterdir()):
        manifeste = entree / "manifest.json"
        if not manifeste.is_file():
            continue
        data = json.loads(manifeste.read_text(encoding="utf-8-sig"))
        data.setdefault("id", entree.name)
        trouves[str(data["id"])] = data
    return trouves


def valider_modules(demandes: list[str]) -> list[dict]:
    """Résout les ids demandés, ou refuse en nommant ce qui existe.

    Refuser tôt et en listant les possibles : l'alternative est un paquet
    assemblé pendant cinq minutes puis livré sans le module attendu, ce qui ne se
    voit qu'à l'ouverture chez le destinataire.
    """
    dispo = modules_disponibles()
    inconnus = [m for m in demandes if m not in dispo]
    if inconnus:
        raise ErreurPaquet(
            f"Modules inconnus du catalogue : {', '.join(inconnus)}.\n"
            f"Disponibles : {', '.join(sorted(dispo)) or '(catalogue vide)'}"
        )
    manquants = [
        m for m in demandes
        if not all((CATALOGUE / m / f).is_file()
                   for f in ("manifest.json", "router.py", "Component.tsx"))
    ]
    if manquants:
        raise ErreurPaquet(
            f"Modules incomplets dans le catalogue (il faut manifest.json, "
            f"router.py et Component.tsx) : {', '.join(manquants)}"
        )
    return [dispo[m] for m in demandes]


# ── Frontend ─────────────────────────────────────────────────────────────────

@contextmanager
def generated_restreint(ids: list[str]):
    """Ne laisse dans `generated/` que les composants des modules choisis.

    Pourquoi c'est nécessaire : `registry.ts` découvre les modules ajoutés par un
    `import.meta.glob('./generated/**/*.tsx')`, donc **tout** ce qui traîne dans
    cet arbre entre dans le bundle. Sans ce filtre, le paquet de quelqu'un
    contiendrait le code source des modules faits pour quelqu'un d'autre — ce que
    l'étape D interdit explicitement (« aucune trace l'une de l'autre »). Le
    module ne s'afficherait pas, faute de manifeste backend, mais son code serait
    là et lisible.

    Le vrai dossier est mis de côté par un `rename` (atomique) et restauré dans un
    `finally`. Si un dossier de garde existe déjà, on refuse : ça veut dire qu'un
    build précédent a été interrompu, et écraser ferait perdre les composants
    installés d'Ilyann.
    """
    gen = FRONTEND / "src" / "modules" / "generated"
    garde = gen.parent / "_generated_hors_paquet"
    if garde.exists():
        raise ErreurPaquet(
            f"{garde} existe déjà : un assemblage précédent a été interrompu.\n"
            f"Vérifie son contenu, puis restaure-le à la main en le renommant "
            f"en 'generated' (après avoir écarté le 'generated' actuel)."
        )
    avait_gen = gen.is_dir()
    if avait_gen:
        gen.rename(garde)
    try:
        gen.mkdir(parents=True, exist_ok=True)
        for mid in ids:
            (gen / mid).mkdir(parents=True, exist_ok=True)
            shutil.copy2(CATALOGUE / mid / "Component.tsx", gen / mid / "Component.tsx")
        yield gen
    finally:
        shutil.rmtree(gen, ignore_errors=True)
        if avait_gen:
            garde.rename(gen)


def construire_frontend(ids: list[str], journal=print) -> Path:
    """`npm run build` en mode paquet. Renvoie le `dist/` produit.

    `VITE_API_URL=/` et non la chaîne vide : sous Windows `$env:VAR = ''`
    SUPPRIME la variable (mesuré — le process enfant la voit non définie), et le
    build repartirait silencieusement sur `http://localhost:8000`, ce qui casse
    dès que le destinataire ouvre `127.0.0.1`. Même raison pour `VITE_ATELIER=0`.
    """
    dist = FRONTEND / "dist"
    env = {**os.environ, "VITE_API_URL": "/", "VITE_ATELIER": "0"}
    with generated_restreint(ids):
        if dist.exists():
            shutil.rmtree(dist)
        journal(f"  npm run build (VITE_API_URL=/, VITE_ATELIER=0, modules={ids or '∅'})")
        # shell=False et argv en liste (CLAUDE.md §6). npm.cmd sous Windows.
        npm = "npm.cmd" if os.name == "nt" else "npm"
        res = subprocess.run(
            [npm, "run", "build"], cwd=FRONTEND, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if res.returncode != 0:
            raise ErreurPaquet(
                "npm run build a échoué :\n"
                + (res.stdout or "")[-2000:] + (res.stderr or "")[-2000:]
            )
        if not (dist / "index.html").is_file():
            raise ErreurPaquet(f"build terminé mais {dist / 'index.html'} est absent")
        # Le paquet doit être copié AVANT de restaurer `generated/` : le
        # gestionnaire de contexte ne touche pas à `dist/`, mais un build
        # ultérieur l'écraserait.
        return dist


# ── Runtime Python ───────────────────────────────────────────────────────────

def _telecharger(url: str, cible: Path, journal=print) -> Path:
    import urllib.request

    journal(f"  téléchargement {url}")
    with urllib.request.urlopen(url, timeout=120) as flux, cible.open("wb") as sortie:
        shutil.copyfileobj(flux, sortie)
    journal(f"    {cible.stat().st_size / 1024 / 1024:.1f} Mo")
    return cible


def preparer_python(destination: Path, embeddable: Path | None,
                    contraintes: Path | None, journal=print,
                    arch: str = "amd64") -> dict:
    """Runtime embeddable + dépendances, dans `destination`.

    Trois gestes que le Python embeddable impose et qui ne se devinent pas :
    décommenter `import site` dans `python312._pth` (sinon `site-packages` est
    ignoré et `pip` reste introuvable), amener `pip` par `get-pip.py` (il n'est
    pas embarqué), et purger ce qui n'a pas à partir.

    `arch` choisit le zip embeddable ET les exigences installées. Les deux
    ensemble, jamais séparément : un runtime ARM64 avec les exigences x64 échoue
    au `pip install`, l'inverse produit un paquet qui ne démarre pas chez le
    destinataire. C'est le genre d'écart qui ne se voit qu'à l'ouverture.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="epure-paquet-py-") as tmp:
        zip_py = Path(embeddable) if embeddable else _telecharger(
            URL_EMBEDDABLE.format(v=VERSION_PYTHON, arch=arch),
            Path(tmp) / "embed.zip", journal)
        if not zip_py.is_file():
            raise ErreurPaquet(f"embeddable introuvable : {zip_py}")
        journal(f"  extraction {zip_py.name}")
        with zipfile.ZipFile(zip_py) as z:
            z.extractall(destination)

        pth = next(destination.glob("python*._pth"), None)
        if pth is None:
            raise ErreurPaquet(
                f"aucun python*._pth dans {destination} — ce zip n'est pas une "
                f"distribution embeddable"
            )
        lignes = pth.read_text(encoding="utf-8").splitlines()
        pth.write_text(
            "\n".join("import site" if l.strip() == "#import site" else l for l in lignes)
            + "\n",
            encoding="ascii",
        )
        journal(f"  {pth.name} : import site activé")

        python = destination / "python.exe"
        get_pip = _telecharger(URL_GET_PIP, Path(tmp) / "get-pip.py", journal)
        _executer([str(python), str(get_pip), "--no-warn-script-location"],
                  "get-pip.py", journal)

        exigences = _exigences_du_paquet(Path(tmp) / "requirements-paquet.txt", arch)
        if arch == "arm64":
            journal("  ARM64 : voix retirée de l'installation "
                    f"({', '.join(HORS_PAQUET_PIP_ARM64)})")
        cmd = [str(python), "-m", "pip", "install", "--no-warn-script-location",
               "--disable-pip-version-check", "-r", str(exigences)]
        if contraintes:
            if not Path(contraintes).is_file():
                raise ErreurPaquet(f"fichier de contraintes introuvable : {contraintes}")
            cmd += ["-c", str(contraintes)]
        journal("  pip install (plusieurs minutes)")
        _executer(cmd, "pip install", journal)

        gel = _executer([str(python), "-m", "pip", "freeze"], "pip freeze", journal,
                        capturer=True)

    purges = purger_site_packages(destination, journal)
    return {"version_python": VERSION_PYTHON, "arch": arch, "purges": purges,
            # Ce que le destinataire N'A PAS, écrit noir sur blanc dans PAQUET.json.
            # Un paquet sans voix doit se reconnaître à son manifeste : sinon le
            # premier micro absent se lit comme une interface cassée.
            "voix": arch != "arm64",
            "gel": sorted(gel.splitlines()) if gel else []}


def _exigences_du_paquet(cible: Path, arch: str = "amd64") -> Path:
    """`backend/requirements.txt` moins les paquets hors paquet, selon l'architecture.

    Anciennement `_exigences_sans_torch` : le nom a cessé d'être vrai quand la
    fonction s'est mise à retirer aussi les paquets vocaux sur ARM64. Un nom qui
    décrit une seule de deux exclusions fait chercher la seconde ailleurs.

    Deux motifs d'exclusion, et ils ne disent pas la même chose au lecteur du
    fichier produit :

    - `HORS_PAQUET_PIP` (toutes architectures) — reporté au premier usage, donc
      **récupérable** : `sentence-transformers` s'installera quand le destinataire
      utilisera le RAG.
    - `HORS_PAQUET_PIP_ARM64` (ARM64 seulement) — **définitif** : il n'existe rien
      à installer pour cette architecture, ni maintenant ni plus tard.

    D'où deux commentaires distincts dans le fichier généré, et pas un seul
    « RETIRÉ » indifférencié : le destinataire qui relit ses exigences doit pouvoir
    distinguer « ça viendra » de « ça ne viendra pas ».

    Filtrage ligne par ligne en gardant les commentaires : ils portent
    l'explication de chaque épinglage, et un fichier d'exigences dérivé qui les
    perd devient un fichier qu'on ne sait plus relire.
    """
    if arch not in ARCHS:
        raise ErreurPaquet(f"architecture inconnue : {arch!r} (attendu {' ou '.join(ARCHS)})")
    lignes = (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines()
    gardees = []
    for ligne in lignes:
        nom = ligne.split("==")[0].split(">=")[0].strip().lower()
        est_exigence = bool(nom) and not ligne.lstrip().startswith("#")
        if est_exigence and nom in HORS_PAQUET_PIP:
            gardees.append(f"# RETIRÉ DU PAQUET (installé au premier usage) : {ligne}")
            continue
        if est_exigence and arch == "arm64" and nom in HORS_PAQUET_PIP_ARM64:
            gardees.append(
                f"# RETIRÉ DU PAQUET ARM64 (voix indisponible sur cette "
                f"architecture — aucune wheel win_arm64) : {ligne}"
            )
            continue
        gardees.append(ligne)
    cible.write_text("\n".join(gardees) + "\n", encoding="utf-8")
    return cible



def purger_site_packages(racine_python: Path, journal=print) -> dict[str, float]:
    """Retire de `site-packages` ce qui n'a pas à être livré. Renvoie les Mo gagnés."""
    sp = racine_python / "Lib" / "site-packages"
    gagne: dict[str, float] = {}
    for nom in PURGE_SITE_PACKAGES:
        cible = sp / nom
        if not cible.is_dir():
            continue
        octets = sum(f.stat().st_size for f in cible.rglob("*") if f.is_file())
        shutil.rmtree(cible, ignore_errors=True)
        gagne[nom] = round(octets / 1024 / 1024, 1)
        journal(f"  purge {nom}/ : {gagne[nom]} Mo")
    for cache in list(racine_python.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    return gagne


def _executer(cmd: list[str], quoi: str, journal=print, capturer: bool = False) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise ErreurPaquet(f"{quoi} a échoué (code {res.returncode}) :\n"
                           + (res.stdout or "")[-2000:] + (res.stderr or "")[-2000:])
    return res.stdout if capturer else ""


# ── Backend ──────────────────────────────────────────────────────────────────

def doit_exclure(relatif: Path) -> bool:
    """Ce chemin (relatif à `backend/`) doit-il rester hors du paquet ?

    Fonction pure, et c'est délibéré : c'est la règle dont une erreur enverrait
    le `.env` d'Ilyann — donc toutes ses clés d'API cloud — chez quelqu'un
    d'autre. `backend/test_paquet.py` l'interroge directement, sans rien copier.
    """
    parties = relatif.parts
    nom = relatif.name
    if parties[0] in EXCLUS_RACINE:
        return True
    if any(p in EXCLUS_PARTOUT for p in parties):
        return True
    if nom in EXCLUS_FICHIERS:
        return True
    if len(parties) == 1 and nom in EXCLUS_MAINTENANCE:
        return True
    if any(nom.startswith(p) for p in EXCLUS_PREFIXES_FICHIERS):
        return True
    if any(Path(nom).match(m) for m in EXCLUS_MOTIFS):
        return True
    if len(parties) == 2 and parties[0] == "core" and nom in EXCLUS_CORE_ATELIER:
        return True
    # `modules/` est reconstruit à partir du catalogue, pas copié : l'arbre
    # d'Ilyann contient ses modules d'Atelier et son `hello` de référence.
    # Les modules du CŒUR, eux, doivent partir.
    if parties[0] == "modules" and len(parties) > 1:
        return parties[1] not in MODULES_COEUR
    return False


#: Modules du cœur, livrés avec Épure et non désinstallables. Seuls ceux-là sont
#: copiés depuis `backend/modules/` ; les autres viennent du catalogue.
MODULES_COEUR = frozenset({"admin", "chat", "history", "settings"})


def copier_backend(cible: Path, journal=print) -> int:
    """Copie `backend/` en appliquant :func:`doit_exclure`. Renvoie le nb de fichiers."""
    n = 0
    for source in sorted(BACKEND.rglob("*")):
        if not source.is_file():
            continue
        relatif = source.relative_to(BACKEND)
        if doit_exclure(relatif):
            continue
        destination = cible / relatif
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        n += 1
    journal(f"  backend : {n} fichiers")
    return n


def installer_modules(cible_modules: Path, manifestes: list[dict], journal=print) -> None:
    """Pose les modules choisis DÉJÀ INSTALLÉS dans le paquet.

    Déjà installés, et non « installables » : cf. décision 2 du docstring du
    module. Le composant, lui, est déjà entré dans le bundle au moment du build
    (`generated_restreint`) — ici on ne pose que le côté backend.
    """
    for manifeste in manifestes:
        mid = str(manifeste["id"])
        dossier = cible_modules / mid
        dossier.mkdir(parents=True, exist_ok=True)
        for fichier in ("manifest.json", "router.py"):
            shutil.copy2(CATALOGUE / mid / fichier, dossier / fichier)
        journal(f"  module installé : {mid}")


# ── Assemblage ───────────────────────────────────────────────────────────────

def assembler(staging: Path, dist: Path, manifestes: list[dict],
              runtime: dict | None, journal=print, arch: str = "amd64") -> dict:
    """Compose l'arborescence du paquet dans `staging`."""
    app = staging / "app"
    copier_backend(app / "backend", journal)
    env = ecrire_env(app / "backend", journal)
    installer_modules(app / "backend" / "modules", manifestes, journal)

    web = app / "frontend" / "dist"
    web.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist, web)
    journal(f"  interface : {sum(1 for _ in web.rglob('*') if _.is_file())} fichiers")

    infos = {
        "modules": [{"id": m["id"], "nom": m.get("nom"), "version": m.get("version")}
                    for m in manifestes],
        # RELU sur le `.env` qu'on vient d'écrire, jamais écrit en dur : ce champ
        # doit décrire l'instance que le destinataire va démarrer, pas l'intention
        # de celui qui assemble. En dur, il est resté `False` pendant que les
        # routes de l'Atelier répondaient — cf. le commentaire d'`ENV_PAQUET`.
        "atelier": atelier_actif_selon(env),
        # Au premier niveau et pas seulement dans `python` : avec
        # `--sauter-python` il n'y a pas de bloc runtime, et l'architecture visée
        # reste l'information la plus utile du manifeste.
        "arch": arch,
        "voix": arch != "arm64",
        "frontend": {"VITE_API_URL": "/", "VITE_ATELIER": "0"},
        "python": runtime or {"non_installe": True},
    }
    return infos


def zipper(staging: Path, archive: Path, journal=print) -> float:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive.unlink()
    journal(f"  compression → {archive.name}")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for fichier in sorted(staging.rglob("*")):
            if fichier.is_file():
                z.write(fichier, fichier.relative_to(staging))
    mo = archive.stat().st_size / 1024 / 1024
    journal(f"  {mo:.1f} Mo")
    return mo


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Assemble un paquet Épure pour un destinataire.",
        epilog="Ce script ne part jamais dans le paquet (cf. tools/).",
    )
    # Pas `required=True` : `--lister-modules` doit marcher seul, et argparse
    # refuserait la commande avant d'arriver au code qui sait s'en passer.
    p.add_argument("--destinataire",
                   help="nom court, sert à nommer l'archive (ex : sandr)")
    p.add_argument("--modules", default="",
                   help="ids du catalogue, séparés par des virgules (vide = aucun)")
    p.add_argument("--sortie", type=Path, default=REPO / "dist-paquets")
    p.add_argument("--embeddable", type=Path,
                   help="zip embeddable local (évite le téléchargement)")
    p.add_argument("--contraintes", type=Path, default=CONTRAINTES_DEFAUT,
                   help="fichier -c pour pip (reproductibilité — cf. étape B) ; "
                        f"défaut {CONTRAINTES_DEFAUT.relative_to(REPO)}")
    p.add_argument("--sans-contraintes", action="store_true",
                   help="ignore le fichier de contraintes par défaut — pour en "
                        "régénérer un nouveau après un changement de requirements.txt")
    p.add_argument("--horodatage", default="",
                   help="suffixe de l'archive ; défaut : aucun (nom stable)")
    p.add_argument("--arch", choices=ARCHS, default=arch_hote(),
                   help="architecture de la MACHINE CIBLE ; défaut : celle de ce "
                        f"poste ({arch_hote()}). En arm64, la voix "
                        "(faster-whisper, piper-tts) est retirée de l'installation.")
    p.add_argument("--sauter-python", action="store_true",
                   help="n'installe pas le runtime (paquet incomplet, pour essai)")
    p.add_argument("--lister-modules", action="store_true",
                   help="affiche le catalogue et sort")
    args = p.parse_args(argv)

    if args.lister_modules:
        for mid, m in sorted(modules_disponibles().items()):
            print(f"  {mid:<12} {m.get('nom', '')} — {m.get('description', '')}")
        return 0
    if not args.destinataire:
        p.error("--destinataire est requis (sauf avec --lister-modules)")

    if args.sans_contraintes:
        args.contraintes = None

    demandes = [m.strip() for m in args.modules.split(",") if m.strip()]
    try:
        manifestes = valider_modules(demandes)
        print(f"Paquet pour « {args.destinataire} » — modules : {demandes or '∅'} "
              f"— cible : {args.arch}")
        if args.arch != arch_hote():
            # Averti, pas refusé : le zip embeddable et les wheels viennent de
            # PyPI, pas du poste, donc un build croisé produit une archive
            # correcte. Ce qui n'est PAS vérifiable ainsi, c'est qu'elle démarre.
            print(f"  ⚠ build croisé (ce poste est en {arch_hote()}) — "
                  f"l'archive est correcte mais non testable ici")

        print("Frontend :")
        dist = construire_frontend(demandes)

        with tempfile.TemporaryDirectory(prefix="epure-paquet-") as tmp:
            staging = Path(tmp) / "paquet"
            staging.mkdir()

            runtime = None
            if not args.sauter_python:
                print("Runtime Python :")
                runtime = preparer_python(staging / "python", args.embeddable,
                                          args.contraintes, arch=args.arch)
            else:
                print("Runtime Python : SAUTÉ (--sauter-python) — paquet incomplet")

            print("Assemblage :")
            infos = assembler(staging, dist, manifestes, runtime, arch=args.arch)
            infos["destinataire"] = args.destinataire
            (staging / "PAQUET.json").write_text(
                json.dumps(infos, ensure_ascii=False, indent=2), encoding="utf-8")

            suffixe = f"-{args.horodatage}" if args.horodatage else ""
            archive = args.sortie / f"epure-{args.destinataire}{suffixe}.zip"
            print("Archive :")
            zipper(staging, archive)
            print(f"\nFait : {archive}")
    except ErreurPaquet as exc:
        print(f"\nÉCHEC : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
