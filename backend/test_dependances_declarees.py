#!/usr/bin/env python3
"""Les dépendances porteuses sont DÉCLARÉES, jamais héritées d'un tiers.

**L'INCIDENT QUE CE FICHIER GÉNÉRALISE.** `chromadb` déclarait
`uvicorn[standard]`, dont l'extra tire `websockets` — la seule implémentation
WebSocket de l'arbre. Personne ne l'avait choisie, personne ne l'avait écrite
nulle part, et elle est partie avec `chromadb` le 2026-08-13. Dix jours plus tard,
dans un paquet livré : « No supported WebSocket library detected », puis 401 sur
tout `/ws/*` — le chat, l'Atelier et la dictée morts d'un coup, sur toutes les
architectures. Le poste de dev ne pouvait pas le voir : il en gardait un
orphelin. Le correctif fut `wsproto==1.3.2`, déclaré (CLAUDE.md §8).

**POURQUOI IL EXISTE MAINTENANT.** Le remplacement de la pile d'embedding
(2026-08-26) a créé le même risque, à l'identique. `onnxruntime` était déjà
installé sur ce poste, en TRANSITIF — `faster-whisper` et `piper-tts` le
déclarent. Il porte désormais l'embedding, c'est-à-dire toute la recherche
documentaire. Or ces deux paquets vocaux sont retirés des paquets ARM64
(`HORS_PAQUET_PIP_ARM64`, `ctranslate2` ne publiant aucune wheel `win_arm64` ni
sdist) : sans déclaration directe, la pile d'embedding aurait donc dépendu de
paquets absents **sur l'architecture même qui a motivé le chantier**, et le poste
de dev — où la voix est installée — n'aurait rien pu voir. Mot pour mot le
scénario `websockets`.

**LA RÈGLE QUE CE FICHIER TIENT** : un paquet dont on dépend directement est
déclaré directement, même s'il est déjà là. « Il est installé » n'est pas
« il est déclaré », et la différence n'apparaît que chez quelqu'un d'autre.

**CE QU'IL NE FAIT PAS.** Il ne calcule pas l'arbre de dépendances (ce serait un
appel réseau, ou la lecture du site-packages du poste — donc un test dont le
résultat dépend de la machine). Il vérifie une **liste nommée**, chaque entrée
portant la raison pour laquelle elle est porteuse. Ajouter une entrée est un geste
volontaire, comme il l'a été pour `wsproto`.

Usage :
    python test_dependances_declarees.py
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — avant tout import core.*

from core.paths import BACKEND_DIR, REPO_ROOT  # noqa: E402

REQUIREMENTS = BACKEND_DIR / "requirements.txt"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRAINTES = REPO_ROOT / "tools" / "contraintes-paquet.txt"

#: Les paquets dont Épure dépend DIRECTEMENT et qui sont arrivés (ou pourraient
#: arriver) par un tiers. Chaque entrée dit ce qui casse en silence si la ligne
#: disparaît de `requirements.txt` — c'est ce texte, et non le nom, qui rend le
#: test relisible dans trois semaines.
PORTEUSES = {
    "onnxruntime": (
        "moteur d'embedding (core/embedding.py). Arrivait par faster-whisper et "
        "piper-tts, tous deux retirés des paquets ARM64 : sans déclaration, plus "
        "de recherche documentaire sur ARM64, et invisible depuis le poste de dev."
    ),
    "wsproto": (
        "seule implémentation WebSocket de l'arbre. Arrivait par l'extra "
        "`standard` d'uvicorn, déclaré par chromadb : son retrait a tué tout "
        "/ws/* dans un paquet livré (CLAUDE.md §8)."
    ),
    "numpy": (
        "core/vector_store.py (cosinus par force brute). Arrivait par pandas."
    ),
    "python-multipart": (
        "endpoints à UploadFile/File(...) : FastAPI lève à l'import du router si "
        "le paquet manque. Était présent par hasard sur le poste de dev."
    ),
    "httpx": (
        "fastapi.testclient.TestClient. Arrivait par ollama et openai."
    ),
    "transformers": (
        "core/hmer.py fait `from transformers import AutoProcessor`. Arrive aussi "
        "par optimum-onnx, donc « déjà installé » masquerait sa disparition — le "
        "scénario websockets, une troisième fois."
    ),
    "pystray": (
        "epure_tray.py, la façon documentée de lancer Épure. Non déclaré, la "
        "commande du README échouait sur un poste neuf."
    ),
}

#: Ce qui ne doit PAS être déclaré. L'ancienne pile d'embedding réinstallerait
#: `scikit-learn`, donc `sklearn/utils/_isfinite` — le binaire non signé que
#: Smart App Control bloque durablement sur la machine ARM64 du destinataire.
#:
#: ⚠️ **CETTE LISTE A PERDU TROIS ENTRÉES LE 2026-09-07, ET C'EST UN
#: AFFAIBLISSEMENT RÉEL — à lire avant de croire que le garde-fou dit encore la
#: même chose.** Elle portait aussi `torch`, `transformers` et `tokenizers`, sur
#: l'invariant « ces paquets n'existent nulle part dans ce dépôt ». La phase 2 du
#: module `encre` (`core/hmer.py`) les ramène : `pix2text-mfr` se charge par
#: `optimum-onnx`, qui déclare `optimum`, qui fait un `import torch` de niveau
#: module — vérifié dans un venv propre, pas supposé (`pip uninstall torch` →
#: `optimum/onnxruntime/modeling_seq2seq.py:23, ModuleNotFoundError`).
#:
#: Ce qui est PRÉSERVÉ, et qui était la vraie raison d'être des trois entrées
#: retirées : **aucun de ces binaires n'atteint un destinataire**. L'invariant
#: change de mécanisme, pas de but — « jamais déclaré » devient « déclaré, jamais
#: livré », et c'est `HmerHorsPaquetTest` plus bas qui le tient, en vérifiant
#: `HORS_PAQUET_PIP` **et** le fichier d'exigences réellement produit, sur les
#: deux architectures. `BANNIES_TRANSITIVES` ci-dessous, qui lit le `pip freeze`
#: d'un paquet réellement assemblé, continue de les interdire : c'est la mesure,
#: là où `HmerHorsPaquetTest` est l'intention.
#:
#: Ne pas remettre ces trois noms ici sans retirer `core/hmer.py` : le test
#: deviendrait rouge en permanence, et le réflexe serait alors de le désarmer.
BANNIES = ("sentence-transformers", "scikit-learn")

#: Les paquets qui ont le droit d'être déclarés **à la condition stricte** de ne
#: jamais partir dans un paquet distribué. Sous-liste de ce qui vivait dans
#: `BANNIES` avant le 2026-09-07 — cf. l'explication au-dessus.
#:
#: `torch` n'y figure pas parce qu'il n'est pas DÉCLARÉ : il arrive par
#: `optimum`. Il est couvert deux fois quand même — retirer `optimum-onnx` du
#: paquet emporte tout son arbre (même mécanisme que `google-generativeai`), et
#: `BANNIES_TRANSITIVES` le vérifie sur le `pip freeze` réel.
DECLARES_MAIS_JAMAIS_LIVRES = ("optimum-onnx", "transformers")

#: Ce qui ne doit apparaître **NULLE PART DANS L'ARBRE RÉSOLU**, transitif
#: compris — et c'est une liste différente de la précédente, pas un doublon.
#:
#: L'INCIDENT QUI LA JUSTIFIE : le 2026-08-26, Smart App Control a bloqué
#: `regex/_regex.pyd` sur la machine ARM64 du destinataire, et plus aucun import
#: de fichier ne fonctionnait. `regex` n'était déclaré nulle part — il arrivait
#: par `sentence-transformers` -> `transformers` -> `regex`. Vérifier les
#: déclarations DIRECTES ne l'aurait jamais vu : c'est la troisième génération de
#: la chaîne.
#:
#: `tokenizers` n'y est PAS, et la nuance compte : il est légitimement dans
#: l'arbre résolu, tiré par `faster-whisper` pour la transcription vocale. Ce
#: qu'on lui interdit, c'est d'être une dépendance DIRECTE (liste ci-dessus),
#: parce que le tokeniseur d'Épure est en Python pur. Sur ARM64 la voix est
#: retirée et il disparaît avec elle.
BANNIES_TRANSITIVES = ("sentence-transformers", "torch", "scikit-learn",
                       "transformers", "scipy", "regex")


def lignes_declarees() -> list[str]:
    """Les lignes ACTIVES de requirements.txt, commentaires exclus."""
    texte = REQUIREMENTS.read_text(encoding="utf-8")
    return [l.strip() for l in texte.splitlines()
            if l.strip() and not l.strip().startswith("#")]


def noms_declares() -> set[str]:
    """Les noms de paquets déclarés, normalisés (PEP 503 : `_` → `-`, minuscules)."""
    noms = set()
    for ligne in lignes_declarees():
        nom = re.split(r"[=<>!~\[;\s]", ligne, maxsplit=1)[0]
        if nom:
            noms.add(nom.strip().lower().replace("_", "-"))
    return noms


class DeclarationDirecteTest(unittest.TestCase):

    def test_chaque_porteuse_est_declaree(self):
        declares = noms_declares()
        for paquet, raison in PORTEUSES.items():
            with self.subTest(paquet=paquet):
                self.assertIn(paquet, declares,
                              f"{paquet} n'est plus déclaré — {raison}")

    def test_la_liste_des_porteuses_n_est_pas_vide(self):
        """Garde-fou du garde-fou : une liste vidée ferait tout passer."""
        self.assertGreaterEqual(len(PORTEUSES), 5)
        self.assertIn("onnxruntime", PORTEUSES)

    def test_onnxruntime_est_epingle_comme_le_reste(self):
        """Une dépendance porteuse non épinglée est une version subie.

        Même raison que pour `fastapi`/`starlette` : le jour où un `pip install`
        neuf résout une version différente de celle qui a été mesurée, la
        divergence ne se voit pas au build mais chez le destinataire.
        """
        lignes = [l for l in lignes_declarees() if l.lower().startswith("onnxruntime")]
        self.assertEqual(1, len(lignes), lignes)
        self.assertRegex(lignes[0], r"^onnxruntime==\d+\.\d+\.\d+$")

    def test_l_ancienne_pile_ne_revient_pas(self):
        declares = noms_declares()
        for banni in BANNIES:
            with self.subTest(paquet=banni):
                self.assertNotIn(banni, declares,
                                 f"{banni} est de retour : il réintroduit un "
                                 "binaire non signé sur le chemin de l'embedding")


class ArbreResoluTest(unittest.TestCase):
    """Ce qui atterrit vraiment sur le disque du destinataire.

    `requirements.txt` dit ce qu'on demande ; `tools/contraintes-paquet.txt` dit
    ce que `pip` a RÉELLEMENT installé — c'est un `pip freeze` du site-packages
    du dernier paquet assemblé, pas une intention. C'est donc le seul endroit,
    hors ligne, où un retour transitif se voit.
    """

    def setUp(self):
        self.resolu = {}
        for ligne in CONTRAINTES.read_text(encoding="utf-8").splitlines():
            nu = ligne.strip()
            if nu and not nu.startswith("#") and "==" in nu:
                nom, version = nu.split("==", 1)
                self.resolu[nom.strip().lower().replace("_", "-")] = version.strip()

    def test_l_arbre_resolu_n_est_pas_vide(self):
        """Garde-fou du garde-fou : un fichier vidé ferait tout passer."""
        self.assertGreaterEqual(len(self.resolu), 40)
        self.assertIn("onnxruntime", self.resolu)

    def test_aucune_bannie_ne_revient_par_le_transitif(self):
        """Il ne suffit pas qu'un paquet ne soit pas déclaré : il faut
        qu'aucune dépendance ne le ramène.

        **Ce test n'aurait PAS attrapé l'incident `regex` du 2026-08-26**, et il
        faut le dire plutôt que de le laisser croire : `regex` n'est jamais entré
        par le paquet — il n'y a jamais été. Il est entré par le `pip install
        sentence-transformers` que l'APPLICATION lançait au premier usage, donc
        dans un arbre que ce fichier ne décrit pas. Ce qui ferme ce chemin-là est
        `AucuneInstallationALExecutionTest` dans `test_embedding_install.py` :
        plus aucun sous-processus sur le chemin d'embedding.

        Celui-ci ferme l'AUTRE porte, celle qui reste ouverte : qu'une dépendance
        directe, un jour, ramène `regex`, `scikit-learn` ou `torch` dans le paquet
        lui-même. La leçon des deux incidents est la même — un binaire non signé
        n'a pas besoin d'être choisi pour arriver — mais les deux portes ne se
        ferment pas au même endroit.
        """
        for banni in BANNIES_TRANSITIVES:
            with self.subTest(paquet=banni):
                self.assertNotIn(banni, self.resolu,
                                 f"{banni} est de retour dans l'arbre résolu — "
                                 "vérifier quelle dépendance le tire")

    def test_onnxruntime_resolu_est_celui_qui_est_declare(self):
        """La contradiction qui a cassé le build par défaut le 2026-08-26 :
        `requirements.txt` épinglait 1.26.0, ce fichier 1.28.0, et
        `pip install -c` échouait sur la résolution avant la première wheel. Un
        fichier de contraintes périmé est inoffensif jusqu'au jour où il épingle,
        à une autre valeur, quelque chose qui vient de devenir direct.
        """
        declare = [l for l in lignes_declarees() if l.lower().startswith("onnxruntime==")]
        self.assertEqual(1, len(declare))
        self.assertEqual(declare[0].split("==")[1].strip(), self.resolu["onnxruntime"])


class PaquetTest(unittest.TestCase):
    """La déclaration ne suffit pas : il faut aussi que le paquet l'emporte."""

    def setUp(self):
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import faire_paquet  # noqa: PLC0415
        self.paquet = faire_paquet

    def test_onnxruntime_n_est_exclu_d_aucune_architecture(self):
        """Les deux listes d'exclusion, et la confusion à éviter dans la seconde.

        `onnxruntime` a l'air d'appartenir à la voix — c'est par elle qu'il
        arrivait. L'exclure avec les paquets vocaux sur ARM64 livrerait un paquet
        sans moteur d'embedding, sur l'architecture pour laquelle tout ce chantier
        a été fait.
        """
        self.assertNotIn("onnxruntime", self.paquet.HORS_PAQUET_PIP)
        self.assertNotIn("onnxruntime", self.paquet.HORS_PAQUET_PIP_ARM64)

    def test_la_pile_d_embedding_n_est_plus_reportee(self):
        """`HORS_PAQUET_PIP` ne doit plus contenir de paquet d'embedding.

        Il portait `sentence-transformers` avec la promesse « s'installe au
        premier usage » — une promesse qui a été de la prose pendant tout un été,
        puis 198 Mo de wheels chez le destinataire.

        Ce que ce test NE dit pas, depuis que la liste est un dict à motifs :
        que `HORS_PAQUET_PIP` soit vide de tout ce qui est lourd. La pile de
        transcription manuscrite y est délibérément, et c'est
        `HmerHorsPaquetTest` qui l'exige — deux affirmations opposées sur la même
        constante, pour deux paquets dont la nature diffère.
        """
        for interdit in BANNIES:
            self.assertNotIn(interdit, self.paquet.HORS_PAQUET_PIP)


class HmerHorsPaquetTest(unittest.TestCase):
    """La pile de transcription manuscrite est déclarée, et jamais livrée.

    **C'est le test qui remplace trois entrées de `BANNIES`** (cf. le
    commentaire de cette constante). Avant le 2026-09-07, `torch`,
    `transformers` et `tokenizers` étaient interdits de déclaration, ce qui
    garantissait par construction qu'aucun destinataire ne les recevrait.
    `core/hmer.py` les ramène dans l'arbre de DÉVELOPPEMENT ; il faut donc
    vérifier explicitement ce qui était vrai gratuitement.

    Ce que ça protège concrètement, si quelqu'un retirait ces entrées de
    `HORS_PAQUET_PIP` en croyant simplifier : un paquet distribué de plusieurs
    gigaoctets, contenant `tokenizers` et `regex` — les deux `.pyd` non signés
    dont le blocage par Smart App Control est MESURÉ dans ce dépôt (`core/rag.py`
    perdu sur la machine ARM64 d'un destinataire, deux fois) — pour une capacité
    qu'aucun module livré n'appelle.
    """

    def setUp(self):
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import faire_paquet  # noqa: PLC0415
        self.paquet = faire_paquet

    def test_declares_dans_requirements(self):
        """Sans déclaration, pas de pile : `core/hmer.py` lève `HmerIndisponible`.

        La moitié qu'on oublie quand on ne regarde que l'exclusion — un paquet
        exclu du livrable mais jamais déclaré n'est installé nulle part.
        """
        declares = noms_declares()
        for nom in DECLARES_MAIS_JAMAIS_LIVRES:
            with self.subTest(paquet=nom):
                self.assertIn(nom, declares)

    def test_exclus_de_l_installation_sur_les_deux_architectures(self):
        """Dans `HORS_PAQUET_PIP` (toutes archs), pas seulement en ARM64.

        La confusion à écarter est la même que celle documentée pour
        `onnxruntime`, à l'envers : `HORS_PAQUET_PIP_ARM64` répond à « pip
        échoue-t-il à installer ? ». Ici pip installerait parfaitement. Le motif
        du retrait est autre — personne n'en a l'usage — donc il vaut pour toutes
        les architectures, et l'écrire dans la liste ARM64 laisserait des
        gigaoctets dans le paquet x64, celui que tout le monde reçoit.
        """
        for nom in DECLARES_MAIS_JAMAIS_LIVRES:
            with self.subTest(paquet=nom):
                self.assertIn(nom, self.paquet.HORS_PAQUET_PIP)

    def test_absents_du_fichier_d_exigences_produit(self):
        """La liste dit l'intention ; ceci vérifie le fichier RÉELLEMENT écrit.

        `_exigences_du_paquet` est ce que `pip install` lira chez le
        destinataire. Un filtrage qui laisserait passer une ligne — une
        normalisation de nom qui diverge, un `optimum-onnx` écrit
        `optimum_onnx` — ne se verrait dans aucune des deux assertions
        ci-dessus.
        """
        for arch in self.paquet.ARCHS:
            texte = self._exigences(arch)
            actives = [l for l in texte.splitlines()
                       if l.strip() and not l.lstrip().startswith("#")]
            for nom in DECLARES_MAIS_JAMAIS_LIVRES:
                with self.subTest(arch=arch, paquet=nom):
                    self.assertFalse(
                        [l for l in actives if l.lower().startswith(nom)],
                        f"{nom} serait installé chez le destinataire ({arch})")

    def test_le_motif_du_retrait_ne_promet_pas_une_installation(self):
        """`google-generativeai` s'installe au premier usage ; celles-ci, jamais.

        Le motif est écrit dans le fichier livré, en face de la ligne commentée.
        Un « installé au premier usage » indifférencié — ce que faisait le code
        avant que `HORS_PAQUET_PIP` devienne un dict — ferait attendre à
        quelqu'un une installation que rien ne déclenchera : aucun module livré
        n'importe `core/hmer.py`, et l'application ne lance plus aucun `pip`
        (`test_embedding_install.AucuneInstallationALExecutionTest`).
        """
        texte = self._exigences("amd64")
        for nom in DECLARES_MAIS_JAMAIS_LIVRES:
            ligne = next(l for l in texte.splitlines()
                         if l.lstrip().startswith("#") and nom in l and "RETIRÉ" in l)
            with self.subTest(paquet=nom):
                self.assertNotIn("installé au premier usage", ligne)
                self.assertIn("aucun module livré", ligne)

    def _exigences(self, arch: str) -> str:
        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            return self.paquet._exigences_du_paquet(
                Path(tmp) / "req.txt", arch).read_text(encoding="utf-8")


class CiTest(unittest.TestCase):
    """La CI doit tourner dans une configuration qui existe."""

    def test_onnxruntime_est_installe_par_le_job_rapide(self):
        """Le job `backend` installe un jeu minimal, délibérément sans deps ML.
        `onnxruntime` y est depuis le 2026-08-26 : ce n'est plus une dep ML de
        198 Mo mais 14 Mo embarqués dans le paquet, et l'en garder dehors ferait
        tourner la CI dans une configuration qui n'existe plus nulle part.
        """
        texte = CI.read_text(encoding="utf-8")
        etape = texte.split("Tests unitaires")[0]
        self.assertIn("onnxruntime", etape,
                      "le job rapide n'installe pas onnxruntime")

    def test_la_ci_ne_reinstalle_pas_l_ancienne_pile(self):
        """Un `pip install torch` glissé dans le job rapide le ferait passer de
        trois minutes à quinze, et masquerait le fait que la pile légère suffit.
        """
        texte = CI.read_text(encoding="utf-8")
        lignes_actives = [l for l in texte.splitlines()
                          if l.strip() and not l.strip().startswith("#")]
        actif = "\n".join(lignes_actives)
        # Le job `integration` installe requirements.txt en entier : c'est
        # légitime, et il n'y a plus de torch dedans de toute façon.
        for banni in ("sentence-transformers", "scikit-learn"):
            self.assertNotIn(banni, actif, f"{banni} réapparaît dans la CI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
