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
    "pystray": (
        "epure_tray.py, la façon documentée de lancer Épure. Non déclaré, la "
        "commande du README échouait sur un poste neuf."
    ),
}

#: Ce qui ne doit PAS revenir. L'ancienne pile d'embedding réinstallerait
#: `scikit-learn`, donc `sklearn/utils/_isfinite` — le binaire non signé que
#: Smart App Control bloque durablement sur la machine ARM64 du destinataire.
#: `tokenizers` est dans la liste pour la même raison (son `.pyd` n'est pas signé
#: non plus), et c'est précisément pourquoi `core/wordpiece.py` existe.
BANNIES = ("sentence-transformers", "torch", "scikit-learn", "transformers",
           "tokenizers")

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
        """
        for interdit in BANNIES:
            self.assertNotIn(interdit, self.paquet.HORS_PAQUET_PIP)


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
