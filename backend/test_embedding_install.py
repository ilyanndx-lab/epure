#!/usr/bin/env python3
"""La mise à disposition du modèle d'embedding — cf. `core/embedding_install.py`.

**L'INCIDENT D'ORIGINE.** Dans tout paquet livré, la recherche documentaire
répondait ``500 {"detail": "Erreur interne du serveur", "type": "ImportError"}``
— un corps sans champ `files`, qui a tué le panneau fichiers du module Docs — et
rien dans l'application ne pouvait réparer ce qui manquait (« écarts 2 et 3 » de
`docs/distribution-empaquetee.md`).

**CE QUI A CHANGÉ le 2026-08-26, et pourquoi ce fichier a été réécrit.** La pile
était `pip install torch` puis `pip install sentence-transformers` : 198,3 Mo de
wheels, 843 Mo sur disque, et un blocage dur sur la machine ARM64 du destinataire
(Smart App Control bloque `sklearn/utils/_isfinite`, importé sans condition par
`sentence-transformers`). Elle est désormais `onnxruntime` — **déclaré, embarqué
dans le paquet** — plus un tokeniseur en Python pur. Il n'y a donc plus aucune
installation de paquet à faire ; ce qui reste différé, ce sont les **90 Mo de
poids** du modèle, téléchargés au premier usage et vérifiés par sha256, exactement
comme `core/voice.py` fait des 76 Mo du modèle Piper.

Les tests de commandes `pip` (ordre torch → sentence-transformers, index
`download.pytorch.org`) ont disparu avec ce qu'ils gardaient. Le reste du contrat
n'a pas bougé d'un pouce, et c'est lui qui est éprouvé ici :

1. **Un modèle absent n'est pas une erreur terminale.** Construire un
   `VectorStore` sans les poids lève `EmbeddingIndisponible` — porteuse d'un état
   — et pas une `ImportError` ni une `FileNotFoundError`. C'est ce qui permet un
   503 lisible au lieu d'un 500 opaque.
2. **Un appel concurrent ne relance pas le téléchargement.** C'est LE risque du
   dispositif : l'ouverture du panneau fichiers déclenche `GET /rag/files` et
   `GET /rag/capabilities` presque en même temps, et deux téléchargements de
   90 Mo sur le même dossier sont bien pires qu'un seul.
3. **Les causes d'échec ne se confondent pas.** « Pas de réseau », « le
   téléchargement a raté », « l'empreinte est fausse » et « `onnxruntime` est
   absent » demandent quatre gestes différents à l'utilisateur.
4. **L'intégrité est vérifiée, et la vérification protège vraiment.** Un fichier
   dont le sha256 ne correspond pas ne doit JAMAIS atterrir à sa place finale :
   un `.onnx` tronqué qui *existe* serait cru valide au démarrage suivant, et
   ferait planter ONNX Runtime sans jamais retenter.
5. **La source est épinglée.** Une révision `main` servirait un jour un autre
   fichier, et l'échec de sha256 qui suivrait n'expliquerait rien.

**Aucun test ne télécharge quoi que ce soit.** `_telecharger` est remplacé par un
double qui compte les appels, peut bloquer (ce qui rend « en cours » observable
sans course) et écrit un fichier de la bonne taille ; `_hote_joignable` par un
booléen. Le seul test qui exerce le vrai `_telecharger` remplace `urlopen`.
`_test_env` pose par ailleurs `EPURE_EMBEDDING_AUTOINSTALL=0` et
`EPURE_EMBEDDING_DIR` sur un temporaire vide pour toute la suite : les tests qui
exercent la préparation lèvent cette garde explicitement, pour eux seuls.

Usage :
    python test_embedding_install.py
"""

import contextlib
import io
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.*

# Lues À L'IMPORT de `main` : les figer rend le test indépendant du poste et de
# `backend/.env` (même raison que dans test_auth_surface.py).
os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402  — monte l'app entière ; cf. test_auth_surface.py
import modules.settings.router as routeur_reglages  # noqa: E402
from core import embedding_install as ei  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.embedding_install import EmbeddingIndisponible  # noqa: E402
from core.jsonstore import read_json  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402

_BACKEND = Path(__file__).resolve().parent

#: Le VRAI `_telecharger`, capturé AVANT que `_env` ne le remplace. Sans cette
#: référence, `IntegriteTest` — le seul test qui exerce le téléchargement réel —
#: réinstallerait le double au lieu de l'original, et vérifierait le sha256 d'un
#: faux : un test qui passe en ne testant rien.
_VRAI_TELECHARGER = ei._telecharger


class _TelechargementFactice:
    """Double de `_telecharger` : compte, peut bloquer, peut échouer.

    Le blocage n'est pas un raffinement. Sans lui, l'état « en cours » ne dure que
    le temps d'un appel factice, donc quelques microsecondes, et tout test qui
    l'observe devient une course perdue d'avance. Avec, la fenêtre est tenue
    ouverte aussi longtemps que le test en a besoin.
    """

    def __init__(self, *, bloquer: bool = False, erreur: Exception | None = None,
                 ecrire: bool = True):
        self.appels: list[str] = []
        self._bloquer = bloquer
        self._erreur = erreur
        self._ecrire = ecrire
        self.demarre = threading.Event()
        self.liberer = threading.Event()

    def __call__(self, nom: str) -> None:
        self.appels.append(nom)
        self.demarre.set()
        if self._bloquer:
            # Timeout de sécurité : un test qui oublie `liberer.set()` doit
            # échouer, pas figer la suite entière.
            self.liberer.wait(timeout=10)
        if self._erreur is not None:
            raise self._erreur
        if self._ecrire:
            _poser_fichier(nom)


def _poser_fichier(nom: str) -> Path:
    """Écrit un fichier de la TAILLE attendue, sans son contenu réel.

    La taille et pas seulement le nom : `fichiers_manquants()` compare
    `st_size` — c'est ce qui attrape un `.onnx` de douze octets (page d'erreur
    HTML enregistrée par un proxy captif) qui passerait un test de présence.
    """
    chemin = ei.chemin_fichier_modele(nom)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with open(chemin, "wb") as sortie:
        sortie.truncate(ei.FICHIERS_MODELE[nom][2])
    return chemin


@contextlib.contextmanager
def _env(*, deja=(), joignable=True, telechargement=None, runtime=True,
         autoinstall=True):
    """Un environnement de préparation entièrement factice.

    `deja` : les fichiers déjà présents (à la bonne taille) au départ.
    `runtime` : `onnxruntime` est-il importable ? (faux = installation abîmée)

    Tout est reposé en sortie, et l'état du module remis à zéro : ces globales
    sont partagées par tout le process, et `unittest discover` fait tourner ce
    fichier au milieu de vingt autres.
    """
    telechargement = telechargement if telechargement is not None else _TelechargementFactice()
    dossier = tempfile.mkdtemp(prefix="epure-test-modele-")
    anciens = {
        "EPURE_EMBEDDING_DIR": os.environ.get("EPURE_EMBEDDING_DIR"),
        "EPURE_EMBEDDING_AUTOINSTALL": os.environ.get("EPURE_EMBEDDING_AUTOINSTALL"),
    }
    os.environ["EPURE_EMBEDDING_DIR"] = dossier
    os.environ["EPURE_EMBEDDING_AUTOINSTALL"] = "1" if autoinstall else "0"
    vrai_telecharger = ei._telecharger
    vrai_joignable = ei._hote_joignable
    vrai_runtime = ei.runtime_present
    ei._telecharger = telechargement
    ei._hote_joignable = lambda *a, **k: joignable
    ei.runtime_present = lambda: runtime
    ei._reinitialiser_pour_tests()
    for nom in deja:
        _poser_fichier(nom)
    try:
        yield telechargement
    finally:
        # ATTENDRE LE THREAD AVANT DE RENDRE L'ENVIRONNEMENT, et ce n'est pas une
        # précaution : `_poser_fichier` lit `EPURE_EMBEDDING_DIR` À CHAQUE APPEL
        # (règle de CLAUDE.md §3.5). Un thread de téléchargement encore vivant
        # après la restitution écrit donc dans le dossier GLOBAL de la suite, et
        # `pile_presente()` devient vrai pour tous les tests suivants. Observé :
        # deux échecs dans `unittest discover` là où le fichier seul passait.
        for fil in threading.enumerate():
            if fil.name == "epure-embedding-download":
                fil.join(timeout=10)
        ei._telecharger = vrai_telecharger
        ei._hote_joignable = vrai_joignable
        ei.runtime_present = vrai_runtime
        ei._reinitialiser_pour_tests()
        for cle, valeur in anciens.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur


@contextlib.contextmanager
def _compter_declenchements():
    """Compte les appels à `declencher_installation` sans en laisser passer un.

    Sert aux routes qui doivent LIRE l'état sans rien lancer : le frontend
    interroge `GET /rag/capabilities` en boucle pendant la préparation, donc un
    déclenchement caché là deviendrait une tentative par sondage.
    """
    lancements: list[int] = []
    vrai = ei.declencher_installation

    def espion(explicite: bool = False):
        lancements.append(1)
        return ei.etat_installation()

    ei.declencher_installation = espion
    try:
        yield lancements
    finally:
        ei.declencher_installation = vrai


def _attendre(condition, timeout: float = 5.0) -> bool:
    """Attend une condition sans `sleep` fixe — les threads de ce module sont
    rapides, et un `sleep(0.5)` partout rendrait le fichier lent pour rien.
    """
    fin = threading.Event()
    debut = threading.Event()
    debut.set()
    import time
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condition():
            return True
        time.sleep(0.01)
    return False


class SourceDuModeleTest(unittest.TestCase):
    """D'où viennent les poids, et pourquoi c'est vérifiable."""

    def test_la_revision_est_epinglee_et_pas_main(self):
        """`main` servirait un jour un autre fichier, et l'échec de sha256 qui
        suivrait n'expliquerait rien à personne.
        """
        self.assertRegex(ei.REVISION_MODELE, r"^[0-9a-f]{40}$")
        for nom in ei.FICHIERS_MODELE:
            url = ei.url_fichier(nom)
            self.assertIn(ei.REVISION_MODELE, url)
            self.assertNotIn("/resolve/main/", url)

    def test_les_empreintes_sont_des_sha256_complets(self):
        for nom, (_, sha, taille) in ei.FICHIERS_MODELE.items():
            self.assertRegex(sha, r"^[0-9a-f]{64}$", nom)
            self.assertGreater(taille, 0, nom)

    def test_le_modele_est_l_export_fp32_et_pas_une_variante_quantifiee(self):
        """Les variantes int8 pèsent 23 Mo au lieu de 90 et sont tentantes pour
        cette raison. Mesurées à un cosinus de 0,988–0,994 contre la référence :
        les prendre imposerait de réindexer les 180 chunks existants. Le test
        existe parce que le nom de fichier est le SEUL endroit où ce choix se
        voit.
        """
        chemin_distant = ei.FICHIERS_MODELE["model.onnx"][0]
        self.assertEqual(chemin_distant, "onnx/model.onnx")
        self.assertNotIn("qint8", chemin_distant)
        self.assertNotIn("quint8", chemin_distant)

    def test_la_taille_annoncee_est_celle_des_fichiers_et_non_2000(self):
        """`TAILLE_ESTIMEE_MO` valait 2000 à l'époque torch — pour 198 Mo de
        wheels réelles, soit un facteur 10 dans une phrase d'interface. Elle est
        maintenant dérivée des tailles déclarées, donc elle ne peut plus mentir.
        """
        attendu = round(sum(t for _, _, t in ei.FICHIERS_MODELE.values()) / 1e6)
        self.assertEqual(ei.TAILLE_ESTIMEE_MO, attendu)
        self.assertLess(ei.TAILLE_ESTIMEE_MO, 200)
        self.assertIn(str(ei.TAILLE_ESTIMEE_MO), ei._MSG_EN_COURS)

    def test_le_vocabulaire_est_bien_dans_la_liste(self):
        """Il ne fait que 231 ko et décide de la TOKENISATION : l'oublier ne
        lèverait rien, ça produirait des vecteurs faux en silence.
        """
        self.assertIn("vocab.txt", ei.FICHIERS_MODELE)
        self.assertIn("model.onnx", ei.FICHIERS_MODELE)


class DetectionDesFichiersTest(unittest.TestCase):
    """Ce que « le modèle est là » veut dire exactement."""

    def test_tout_present_a_la_bonne_taille_donne_pile_presente(self):
        with _env(deja=("model.onnx", "vocab.txt")):
            self.assertEqual([], ei.fichiers_manquants())
            self.assertTrue(ei.pile_presente())
            self.assertEqual(ei.PRET, ei.etat_installation()["état"])

    def test_un_fichier_tronque_compte_comme_manquant(self):
        """LE cas qu'une vérification de présence seule laisserait passer : un
        `.onnx` de douze octets (page d'erreur d'un proxy captif) existe, et
        ferait planter ONNX Runtime au chargement.
        """
        with _env(deja=("vocab.txt",)):
            chemin = ei.chemin_fichier_modele("model.onnx")
            chemin.write_bytes(b"<html>403</html>")
            self.assertIn("model.onnx", ei.fichiers_manquants())
            self.assertFalse(ei.pile_presente())

    def test_le_runtime_absent_est_une_cause_a_part_et_prioritaire(self):
        """`onnxruntime` est une dépendance DÉCLARÉE : son absence n'est pas un
        téléchargement en attente mais une installation abîmée. Annoncer
        « préparation en cours » dans ce cas serait faux, et attendre ne
        réparerait rien.
        """
        with _env(deja=("model.onnx", "vocab.txt"), runtime=False):
            etat = ei.etat_installation()
            self.assertEqual(ei.ECHEC, etat["état"])
            self.assertEqual(ei.CAUSE_RUNTIME_ABSENT, etat["cause"])
            self.assertIn("onnxruntime", etat["message"])
            # Et rien n'est tenté : il n'y a rien à télécharger.
            with _env(runtime=False, telechargement=_TelechargementFactice()) as t:
                ei.declencher_installation(explicite=True)
                self.assertEqual([], t.appels)


class ConstructionSansModeleTest(unittest.TestCase):
    """La frontière qui a produit le 500 : `VectorStore.__init__`."""

    def test_vector_store_leve_embedding_indisponible_et_pas_autre_chose(self):
        """Le type importe autant que le fait de lever : `main.py` a un
        gestionnaire dédié pour celui-ci (503 avec état) et un gestionnaire
        générique pour tout le reste (500 « Erreur interne du serveur »), qui est
        exactement le corps qui a tué le panneau fichiers.
        """
        with _env(telechargement=_TelechargementFactice(bloquer=True)) as t:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(EmbeddingIndisponible) as capture:
                    VectorStore(tmp)
            t.liberer.set()
        self.assertEqual(ei.EN_COURS, capture.exception.etat["état"])
        self.assertIn("état", capture.exception.etat)

    def test_l_etat_dit_en_cours_et_non_un_echec(self):
        """Un téléchargement qui commence n'est pas une panne. Si l'état disait
        « échec », l'interface afficherait un bouton « Réessayer » pendant que le
        téléchargement tourne.
        """
        with _env(telechargement=_TelechargementFactice(bloquer=True)) as t:
            ei.declencher_installation()
            self.assertTrue(t.demarre.wait(timeout=5))
            etat = ei.etat_installation()
            t.liberer.set()
        self.assertEqual(ei.EN_COURS, etat["état"])
        self.assertFalse(etat["disponible"])

    def test_un_telechargement_reussi_rend_le_moteur_disponible(self):
        with _env() as t:
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.PRET),
                            f"resté à {ei.etat_installation()}")
            self.assertEqual(sorted(t.appels), ["model.onnx", "vocab.txt"])
            self.assertTrue(ei.pile_presente())
            # Et `exiger_pile()` ne lève plus.
            ei.exiger_pile()

    def test_seuls_les_fichiers_manquants_sont_telecharges(self):
        """90 Mo déjà sur le disque ne se retéléchargent pas parce que 231 ko
        manquent à côté.
        """
        with _env(deja=("model.onnx",)) as t:
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.PRET))
            self.assertEqual(["vocab.txt"], t.appels)


class ConcurrenceTest(unittest.TestCase):
    """Le risque propre au dispositif : deux téléchargements de 90 Mo."""

    def test_douze_appels_simultanes_ne_lancent_qu_un_telechargement(self):
        with _env(telechargement=_TelechargementFactice(bloquer=True)) as t:
            fils = [threading.Thread(target=ei.declencher_installation) for _ in range(12)]
            for f in fils:
                f.start()
            for f in fils:
                f.join(timeout=5)
            self.assertTrue(t.demarre.wait(timeout=5))
            t.liberer.set()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.PRET))
            # Deux appels au total (un par fichier), pas vingt-quatre.
            self.assertEqual(sorted(t.appels), ["model.onnx", "vocab.txt"])

    def test_un_echec_ne_se_relance_pas_tout_seul(self):
        """Une machine hors ligne ne doit pas repartir en boucle sur un
        téléchargement voué.
        """
        with _env(joignable=False) as t:
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
            for _ in range(5):
                ei.declencher_installation()
            self.assertEqual([], t.appels)

    def test_une_demande_explicite_relance_apres_un_echec(self):
        """C'est le bouton « Réessayer » : la cause la plus probable — pas de
        réseau — se corrige dehors, et l'utilisateur est le seul à savoir quand.
        """
        with _env(joignable=False) as t:
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
            ei._hote_joignable = lambda *a, **k: True
            ei.declencher_installation(explicite=True)
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.PRET))
            self.assertEqual(sorted(t.appels), ["model.onnx", "vocab.txt"])


class DistinctionDesEchecsTest(unittest.TestCase):
    """Quatre causes, quatre gestes différents pour l'utilisateur."""

    def test_reseau_injoignable(self):
        with _env(joignable=False):
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
            etat = ei.etat_installation()
            self.assertEqual(ei.CAUSE_RESEAU, etat["cause"])
            self.assertIn("connexion réseau", etat["message"])

    def test_echec_de_telechargement_reel(self):
        erreur = OSError("HTTP Error 500: Internal Server Error")
        with _env(telechargement=_TelechargementFactice(erreur=erreur)):
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
            etat = ei.etat_installation()
            self.assertEqual(ei.CAUSE_TELECHARGEMENT, etat["cause"])
            self.assertEqual("model.onnx", etat["étape"])

    def test_une_coupure_pendant_le_telechargement_compte_comme_reseau(self):
        """La sonde a déjà répondu avant de commencer : ceci attrape la coupure
        qui arrive PENDANT les 90 Mo, en relisant le message d'erreur.
        """
        erreur = OSError("<urlopen error [Errno 11001] getaddrinfo failed>")
        with _env(telechargement=_TelechargementFactice(erreur=erreur)):
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
            self.assertEqual(ei.CAUSE_RESEAU, ei.etat_installation()["cause"])

    def test_une_empreinte_fausse_est_un_echec_de_telechargement(self):
        """Pas un échec réseau : le réseau a parfaitement fonctionné, c'est le
        CONTENU qui est faux. Dire « vérifiez votre connexion » enverrait
        chercher au mauvais endroit.
        """
        erreur = ValueError("empreinte incorrecte pour model.onnx : attendu …, obtenu …")
        with _env(telechargement=_TelechargementFactice(erreur=erreur)):
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
            self.assertEqual(ei.CAUSE_TELECHARGEMENT, ei.etat_installation()["cause"])

    def test_les_quatre_messages_different(self):
        messages = {ei._MSG_RESEAU, ei._MSG_RUNTIME_ABSENT, ei._MSG_DESACTIVE,
                    ei._MSG_EN_COURS}
        self.assertEqual(4, len(messages))


class IntegriteTest(unittest.TestCase):
    """Le VRAI `_telecharger`, avec `urlopen` remplacé — sha256 et `.part`."""

    @contextlib.contextmanager
    def _urlopen(self, charge: bytes):
        class _Reponse:
            headers = {"Content-Length": str(len(charge))}

            def __init__(self):
                self._flux = io.BytesIO(charge)

            def read(self, taille=-1):
                return self._flux.read(taille)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        vrai = ei.urllib.request.urlopen
        ei.urllib.request.urlopen = lambda *a, **k: _Reponse()
        try:
            yield
        finally:
            ei.urllib.request.urlopen = vrai

    def test_un_contenu_faux_ne_laisse_aucun_fichier(self):
        """L'ordre `.part` → sha256 → renommage est ce qui garantit qu'un fichier
        présent est un fichier valide. Écrire directement sur la cible laisserait
        un `.onnx` tronqué que le démarrage suivant croirait bon, pour toujours.
        """
        with _env():
            ei._telecharger = _VRAI_TELECHARGER
            with self._urlopen(b"ceci n'est pas un modele"):
                with self.assertRaises(ValueError) as capture:
                    ei._telecharger("vocab.txt")
            self.assertIn("empreinte incorrecte", str(capture.exception))
            cible = ei.chemin_fichier_modele("vocab.txt")
            self.assertFalse(cible.exists(), "un fichier invalide est resté en place")
            self.assertFalse(cible.with_name(cible.name + ".part").exists(),
                             "le .part n'a pas été nettoyé")

    def test_un_contenu_juste_est_accepte_et_renomme(self):
        """Le pendant positif : sans lui, le test précédent passerait aussi avec
        un `_telecharger` qui refuserait tout.
        """
        # On fabrique un contenu dont on connaît le sha256, et on le déclare.
        charge = b"vocabulaire factice mais coherent\n"
        import hashlib
        empreinte = hashlib.sha256(charge).hexdigest()
        with _env():
            ei._telecharger = _VRAI_TELECHARGER
            ancien = ei.FICHIERS_MODELE["vocab.txt"]
            ei.FICHIERS_MODELE["vocab.txt"] = ("vocab.txt", empreinte, len(charge))
            try:
                with self._urlopen(charge):
                    ei._telecharger("vocab.txt")
                cible = ei.chemin_fichier_modele("vocab.txt")
                self.assertTrue(cible.is_file())
                self.assertEqual(charge, cible.read_bytes())
                self.assertFalse(cible.with_name(cible.name + ".part").exists())
            finally:
                ei.FICHIERS_MODELE["vocab.txt"] = ancien


class PersistanceTest(unittest.TestCase):
    """Ce que le disque garde, et ce qu'il ne doit surtout pas garder."""

    def test_en_cours_n_est_jamais_ecrit(self):
        """Un process tué au milieu d'un téléchargement laisserait sinon un
        fichier qui dit « en cours » pour toujours, indistinguable d'un
        téléchargement vivant.
        """
        fichier = ei._fichier_etat()
        fichier.unlink(missing_ok=True)
        with _env(telechargement=_TelechargementFactice(bloquer=True)) as t:
            ei.declencher_installation()
            self.assertTrue(t.demarre.wait(timeout=5))
            self.assertEqual(ei.EN_COURS, ei.etat_installation()["état"])
            sur_disque = read_json(fichier, {})
            t.liberer.set()
            _attendre(lambda: ei.etat_installation()["état"] == ei.PRET)
        self.assertNotEqual(ei.EN_COURS, sur_disque.get("état"))

    def test_un_echec_survit_au_redemarrage(self):
        """Sert à expliquer un échec passé sans attendre une nouvelle tentative
        pour l'apprendre.
        """
        with _env(joignable=False):
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
            # Un « redémarrage » : on oublie tout ce qui est en mémoire.
            ei._reinitialiser_pour_tests()
            etat = ei.etat_installation()
            self.assertEqual(ei.ECHEC, etat["état"])
            self.assertEqual(ei.CAUSE_RESEAU, etat["cause"])

    def test_le_fichier_passe_par_jsonstore(self):
        """CLAUDE.md §3.4 : jamais de `json.dump` direct. Lu ici en `utf-8-sig`
        via `read_json`, donc un BOM ne rendrait pas l'état invisible.
        """
        with _env(joignable=False):
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.ECHEC))
        brut = ei._fichier_etat().read_bytes()
        self.assertFalse(brut.startswith(b"\xef\xbb\xbf"), "BOM écrit")
        self.assertEqual(ei.ECHEC, read_json(ei._fichier_etat(), {})["état"])


class InterrupteurTest(unittest.TestCase):
    """`EPURE_EMBEDDING_AUTOINSTALL=0` — 90 Mo ne partent pas sans accord."""

    def test_desactive_ne_lance_rien_et_le_dit(self):
        with _env(autoinstall=False) as t:
            etat = ei.declencher_installation()
            self.assertEqual(ei.ECHEC, etat["état"])
            self.assertEqual(ei.CAUSE_DESACTIVE, etat["cause"])
            self.assertIn("EPURE_EMBEDDING_AUTOINSTALL", etat["message"])
            self.assertEqual([], t.appels)

    def test_desactive_ne_persiste_pas_un_verdict(self):
        """C'est une CONFIGURATION, pas une panne : l'écrire ferait croire à un
        échec au démarrage suivant, quand la variable ne sera plus là.
        """
        fichier = ei._fichier_etat()
        fichier.unlink(missing_ok=True)
        with _env(autoinstall=False):
            ei.declencher_installation()
        self.assertEqual({}, read_json(fichier, {}))

    def test_l_activer_suffit_sans_demande_explicite(self):
        """`_tentee` reste faux quand la garde a refusé : sinon il faudrait
        redémarrer après avoir activé la variable.
        """
        with _env(autoinstall=False) as t:
            ei.declencher_installation()
            os.environ["EPURE_EMBEDDING_AUTOINSTALL"] = "1"
            ei.declencher_installation()
            self.assertTrue(_attendre(lambda: ei.etat_installation()["état"] == ei.PRET))
            self.assertEqual(sorted(t.appels), ["model.onnx", "vocab.txt"])

    def test_la_suite_tourne_bien_avec_le_garde_fou(self):
        """Le garde-fou de `_test_env` est-il réellement posé pour tout le monde ?

        Hors des blocs `_env` ci-dessus, la variable doit valoir `0` et le
        dossier du modèle être vide : sans ça, n'importe quel test touchant le RAG
        téléchargerait 90 Mo, ici comme sur le runner de la CI.
        """
        self.assertEqual("0", os.environ.get("EPURE_EMBEDDING_AUTOINSTALL"))
        self.assertFalse(ei.autoinstall_actif())
        self.assertNotEqual([], ei.fichiers_manquants(),
                            "le dossier du modèle n'est pas vide pendant la suite")


class SondeReseauTest(unittest.TestCase):
    """« Hors ligne » ne doit pas être dit à quelqu'un dont le réseau va bien."""

    def test_une_reponse_http_compte_comme_joignable(self):
        """403, 404, 405 sur un HEAD prouvent qu'un serveur a lu la requête. Les
        compter comme une panne réseau afficherait « vérifiez votre connexion » à
        tort.
        """
        import urllib.error

        def refuser(*a, **k):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

        vrai = ei.urllib.request.urlopen
        ei.urllib.request.urlopen = refuser
        try:
            self.assertTrue(ei._hote_joignable())
        finally:
            ei.urllib.request.urlopen = vrai

    def test_une_erreur_de_transport_compte_comme_hors_ligne(self):
        def couper(*a, **k):
            raise OSError("[Errno 11001] getaddrinfo failed")

        vrai = ei.urllib.request.urlopen
        ei.urllib.request.urlopen = couper
        try:
            self.assertFalse(ei._hote_joignable())
        finally:
            ei.urllib.request.urlopen = vrai


class _RagIndisponible:
    """Double du proxy `rag` dont toute méthode lève, comme dans un paquet livré.

    Le vrai proxy ne peut pas servir ici : sur le poste d'Ilyann le modèle finira
    par être là, donc `rag.get_indexed_files()` réussirait et le test passerait
    sans rien éprouver. C'est la FRONTIÈRE HTTP qui est testée — « une
    `EmbeddingIndisponible` levée par le moteur devient un 503 portant l'état » —
    pas la construction du moteur, qui l'est ailleurs dans ce fichier.
    """

    def __init__(self, etat: dict):
        self._etat = etat

    def get_indexed_files(self):
        raise EmbeddingIndisponible(self._etat)


class RouteRagTest(unittest.TestCase):
    """Vu du réseau : 503 avec un état, plus jamais un 500 « ImportError »."""

    @classmethod
    def setUpClass(cls):
        # base_url/client : sans eux, TrustedHostMiddleware répond 400 partout et
        # /pair 403 (cf. test_auth_surface._client).
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def test_rag_files_repond_503_avec_l_etat_et_non_500(self):
        """LE symptôme d'origine, à l'envers.

        Avant : `500 {"detail": "Erreur interne du serveur", "type":
        "ImportError"}`. Le frontend lisait `d.files` sur ce corps, obtenait
        `undefined`, et le panneau fichiers du module Docs levait « Cannot read
        properties of undefined (reading 'length') » au rendu suivant.
        """
        etat = ei._verdict(ei.EN_COURS, message=ei._MSG_EN_COURS, etape="model.onnx")
        original = routeur_reglages.rag
        routeur_reglages.rag = _RagIndisponible(etat)
        try:
            r = self.client.get("/rag/files", headers=self._auth())
        finally:
            routeur_reglages.rag = original

        self.assertEqual(r.status_code, 503, r.text)
        corps = r.json()
        # Le corps porte l'état, donc de quoi écrire une phrase à l'écran.
        self.assertEqual(corps["état"], ei.EN_COURS)
        self.assertIn("connexion réseau", corps["detail"])
        self.assertEqual(corps["taille_estimée_mo"], ei.TAILLE_ESTIMEE_MO)
        # Et surtout, plus la signature de l'ancien 500.
        self.assertNotEqual(corps.get("type"), "ImportError")

    def test_rag_capabilities_repond_un_etat_connu_sans_rien_declencher(self):
        """Cette route est interrogée en boucle par le frontend pendant la
        préparation : si elle déclenchait quoi que ce soit, chaque interrogation
        lancerait une tentative.
        """
        with _compter_declenchements() as lancements:
            r = self.client.get("/rag/capabilities", headers=self._auth())
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(sum(lancements), 0,
                             "la lecture d'état a déclenché un téléchargement")
        corps = r.json()
        self.assertIn(corps["état"], (ei.ABSENT, ei.EN_COURS, ei.PRET, ei.ECHEC))
        # Les champs dont `frontend/src/recherche.ts` dépend, nommément.
        for champ in ("état", "disponible", "message", "cause", "taille_estimée_mo"):
            self.assertIn(champ, corps)

    def test_rag_capabilities_exige_le_token(self):
        """Rien n'est exempt d'authentification hors /health et /pair."""
        self.assertEqual(self.client.get("/rag/capabilities").status_code, 401)

    def test_rag_install_est_une_route_post(self):
        """Le « Réessayer » de l'interface. Ici la garde
        `EPURE_EMBEDDING_AUTOINSTALL=0` posée par `_test_env` s'applique : la
        route répond, et ne télécharge rien.
        """
        with _compter_declenchements() as lancements:
            r = self.client.post("/rag/install", headers=self._auth())
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(sum(lancements), 0)
        self.assertIn(r.json()["état"], (ei.PRET, ei.ECHEC))


class RequirementsTest(unittest.TestCase):
    """Ce module et `requirements.txt` ne doivent pas se contredire."""

    def test_le_runtime_nomme_ici_est_declare_dans_requirements(self):
        """`MODULE_RUNTIME` est le module importé ; `requirements.txt` déclare le
        paquet. Les deux portent le même nom pour `onnxruntime`, et c'est cette
        coïncidence qui rend le test possible — cf.
        `test_dependances_declarees.py` pour la garde complète.
        """
        texte = (_BACKEND / "requirements.txt").read_text(encoding="utf-8")
        declarees = [l.split("==")[0].strip().lower()
                     for l in texte.splitlines()
                     if l.strip() and not l.strip().startswith("#")]
        self.assertIn(ei.MODULE_RUNTIME.replace("_", "-"), declarees)

    def test_sentence_transformers_et_torch_ont_disparu(self):
        """La pile d'avant ne doit pas revenir par la bande : une ligne
        `sentence-transformers` réinstallerait scikit-learn, donc le binaire que
        Smart App Control bloque sur la machine cible.
        """
        texte = (_BACKEND / "requirements.txt").read_text(encoding="utf-8")
        for ligne in texte.splitlines():
            nu = ligne.strip()
            if not nu or nu.startswith("#"):
                continue
            nom = re.split(r"[=<>!~\[]", nu)[0].strip().lower()
            self.assertNotIn(nom, {"sentence-transformers", "torch", "scikit-learn",
                                   "transformers", "tokenizers"}, ligne)


if __name__ == "__main__":
    unittest.main(verbosity=2)
