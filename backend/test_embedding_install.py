#!/usr/bin/env python3
"""L'installation à la demande de la pile d'embedding — cf. `core/embedding_install.py`.

**L'INCIDENT.** Dans tout paquet livré, `sentence-transformers` n'est pas installé
(`HORS_PAQUET_PIP` l'exclut : il tire ~2 Go de torch). `VectorStore.__init__`
l'importait quand même, l'`ImportError` remontait au gestionnaire générique de
`main.py`, et `GET /rag/files` répondait
``500 {"detail": "Erreur interne du serveur", "type": "ImportError"}`` — un corps
sans champ `files`, qui a tué le panneau fichiers du module Docs. Et rien dans
l'application ne pouvait installer ce qui manquait, `pip` étant lui-même purgé du
paquet (« écarts 2 et 3 » de `docs/distribution-empaquetee.md`).

**CE QUE CES TESTS GARDENT**, et ce n'est pas « ça marche » :

1. **Une dépendance absente n'est plus une erreur terminale.** Construire un
   `VectorStore` sans `sentence_transformers` lève `EmbeddingIndisponible` —
   porteuse d'un état — et pas `ImportError`. C'est ce qui permet un 503 lisible
   au lieu d'un 500 opaque.
2. **Un appel concurrent ne relance pas l'installation.** C'est LE risque du
   dispositif : l'ouverture du panneau fichiers déclenche `GET /rag/files` et
   `GET /rag/capabilities` presque en même temps, et deux `pip install torch` sur
   le même `site-packages` sont bien pires qu'un seul.
3. **« Pas de réseau » et « pip a échoué » ne disent pas la même chose.** Un
   message unique pour les deux ferait chercher un problème d'installation à
   quelqu'un dont le wifi est coupé.
4. **L'ordre et l'index des commandes.** `torch` d'abord, depuis
   `download.pytorch.org`, sinon l'installation échoue sur Windows ARM64 — et
   c'est le genre d'inversion qui ne se voit que sur une machine ARM64, donc
   jamais ici.

**Aucun test ne lance de vrai `pip`.** `_pip` est remplacé par un double qui
compte les appels et peut bloquer (ce qui rend « en cours » observable sans
course), et `_index_joignable` par un booléen. `_test_env` pose par ailleurs
`EPURE_EMBEDDING_AUTOINSTALL=0` pour toute la suite : les tests qui exercent
l'installation lèvent cette garde explicitement, pour eux seuls.

Usage :
    python test_embedding_install.py
"""

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
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
from core.vector_store import VectorStore  # noqa: E402

_BACKEND = Path(__file__).resolve().parent


class _PipFactice:
    """Double de `_pip` : compte les appels, et peut bloquer sur demande.

    Le blocage n'est pas un raffinement. Sans lui, l'état « en cours » ne dure que
    le temps d'un `subprocess.run` factice, donc quelques microsecondes, et tout
    test qui l'observe devient une course perdue d'avance. Avec, la fenêtre est
    tenue ouverte aussi longtemps que le test en a besoin.
    """

    def __init__(self, bloquer: bool = False, code: int = 0, sortie: str = ""):
        self.appels: list[list[str]] = []
        self.code = code
        self.sortie = sortie
        self._bloquer = bloquer
        self.demarre = threading.Event()
        self.liberer = threading.Event()

    def __call__(self, cmd):
        self.appels.append(list(cmd))
        self.demarre.set()
        if self._bloquer:
            # Timeout de sécurité : un test qui oublie `liberer.set()` doit
            # échouer, pas figer la suite entière.
            self.liberer.wait(timeout=10)
        return self.code, self.sortie


@contextlib.contextmanager
def _pile(absents=("torch", "sentence_transformers"), *, joignable=True, pip=None,
          present_apres=False, autoinstall=True):
    """Installe un environnement d'installation entièrement factice.

    `absents` : les modules que `_module_importable` doit déclarer manquants.
    `present_apres` : ils deviennent présents une fois toutes les commandes
    jouées — c'est ainsi qu'on éprouve le chemin du succès sans installer 2 Go.

    Tout est reposé en sortie, et l'état du module remis à zéro : ces globales
    sont partagées par tout le process, et `unittest discover` fait tourner ce
    fichier au milieu de vingt autres.
    """
    pip = pip if pip is not None else _PipFactice()
    manquants = set(absents)
    faits: set[str] = set()

    def importable(nom: str) -> bool:
        if nom in manquants and not (present_apres and nom in faits):
            return False
        return True

    def pip_suivi(cmd):
        resultat = pip(cmd)
        if resultat[0] == 0:
            # Une commande réussie « installe » son paquet : c'est la seule façon
            # de vérifier que la boucle ne rejoue pas ce qui est déjà là. Le
            # rapprochement se fait sur l'argument exact ou son épinglage — un
            # simple `in cmd` rate « sentence-transformers==5.5.1 ».
            for module in list(manquants):
                paquet = module.replace("_", "-")
                if any(a == paquet or a.startswith(paquet + "==") for a in cmd):
                    faits.add(module)
        return resultat

    originaux = (ei._module_importable, ei._index_joignable, ei._pip)
    precedent = os.environ.get("EPURE_EMBEDDING_AUTOINSTALL")
    ei._module_importable = importable
    ei._index_joignable = lambda *_a, **_k: joignable
    ei._pip = pip_suivi
    os.environ["EPURE_EMBEDDING_AUTOINSTALL"] = "1" if autoinstall else "0"
    ei._reinitialiser_pour_tests()
    with contextlib.suppress(OSError):
        ei._fichier_etat().unlink(missing_ok=True)
    try:
        yield pip
    finally:
        pip.liberer.set()
        _attendre_fin(bornes=(ei.PRET, ei.ECHEC), timeout=5, tolerer_en_cours=True)
        _joindre_le_thread_d_installation()
        ei._module_importable, ei._index_joignable, ei._pip = originaux
        if precedent is None:
            os.environ.pop("EPURE_EMBEDDING_AUTOINSTALL", None)
        else:
            os.environ["EPURE_EMBEDDING_AUTOINSTALL"] = precedent
        ei._reinitialiser_pour_tests()
        with contextlib.suppress(OSError):
            ei._fichier_etat().unlink(missing_ok=True)


def _joindre_le_thread_d_installation(timeout=10) -> None:
    """Attend que le thread d'installation soit VRAIMENT fini.

    Observer l'état ne suffit pas, et l'écart a produit un échec instable :
    `_poser` publie le verdict terminal puis le thread continue quelques
    instructions (journalisation, sortie de fonction). `_attendre_fin` rend donc
    la main pendant que le thread vit encore. Si le test suivant démarre à cet
    instant, ce thread-là écrit `_etat` par-dessus le sien — et comme le dernier
    verdict d'un thread qui va au bout est « Préparation incomplète »
    (`cause=pip`, `étape=""`), le symptôme observé était un `étape` vide dans un
    test qui attendait « torch ». Un quart d'heure de recherche pour une course
    de quelques microsecondes, rendue probable par la charge de la machine.

    On joint par le NOM plutôt qu'en gardant une référence :
    `declencher_installation` ne rend pas son thread — c'est son contrat, il ne
    doit rien faire attendre à son appelant — donc le test n'a que `threading`
    pour le retrouver.
    """
    for fil in threading.enumerate():
        if fil.name == "epure-embedding-install" and fil.is_alive():
            fil.join(timeout=timeout)


@contextlib.contextmanager
def _compter_installations():
    """Compte les entrées dans `_installer`, c'est-à-dire les lancements réels.

    Observer `_pip` ne suffit pas : deux threads d'installation dont l'un est
    bloqué avant son premier `pip` ne se distinguent pas d'un seul. C'est le
    LANCEMENT qu'on veut compter.
    """
    lancements: list[int] = []
    vrai = ei._installer

    def compter():
        lancements.append(1)
        vrai()

    ei._installer = compter
    try:
        yield lancements
    finally:
        ei._installer = vrai


def _attendre_fin(bornes=(ei.PRET, ei.ECHEC), timeout=10, tolerer_en_cours=False) -> dict:
    """Attend un verdict terminal du thread d'installation.

    Le thread est démoniaque et personne n'en garde la référence (c'est voulu :
    `declencher_installation` ne doit rien faire attendre à son appelant). On
    observe donc l'état, ce qui est de toute façon la seule chose que le reste du
    code peut voir.
    """
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        etat = ei.etat_installation()
        if etat["état"] in bornes:
            return etat
        time.sleep(0.01)
    if tolerer_en_cours:
        return ei.etat_installation()
    raise AssertionError(f"installation jamais terminée (état : {ei.etat_installation()})")


class CommandesTest(unittest.TestCase):
    """L'ordre et l'index — deux invariants dont l'erreur ne se voit qu'en ARM64."""

    def test_torch_avant_sentence_transformers(self):
        etapes = [etape for etape, _cmd in ei.commandes_installation()]
        self.assertEqual(etapes, ["torch", "sentence-transformers"])

    def test_torch_vient_de_l_index_pytorch(self):
        """PyPI ne publie que des wheels `win_amd64` pour torch ; l'index PyTorch
        publie bien `torch-…-cp312-cp312-win_arm64.whl`. Sans `--index-url`, un
        paquet ARM64 ne peut pas installer sa pile d'embedding du tout.
        """
        (_, torch), (_, st) = ei.commandes_installation()
        self.assertIn("--index-url", torch)
        self.assertEqual(torch[torch.index("--index-url") + 1],
                         "https://download.pytorch.org/whl/cpu")
        # Et l'inverse : la seconde commande n'a rien à faire sur cet index (elle
        # y trouverait un miroir partiel de PyPI).
        self.assertNotIn("--index-url", st)

    def test_aucune_commande_ne_passe_par_un_shell(self):
        """`shell=True` est interdit dans ce dépôt (CLAUDE.md §6). Une liste
        d'arguments dont le premier est l'interpréteur courant est la seule forme
        acceptable — et `sys.executable`, pas « python », qui résoudrait
        l'interpréteur du PATH plutôt que celui du paquet.
        """
        for etape, cmd in ei.commandes_installation():
            self.assertIsInstance(cmd, list, etape)
            self.assertEqual(cmd[0], sys.executable, etape)
            self.assertEqual(cmd[1:4], ["-m", "pip", "install"], etape)

    def test_version_alignee_sur_requirements(self):
        """La version est écrite deux fois — ici et dans `requirements.txt`. Deux
        endroits pour une valeur divergent mécaniquement si rien ne les compare.
        """
        lignes = (_BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines()
        declarees = [l.split("==")[1].strip() for l in lignes
                     if l.strip().lower().startswith("sentence-transformers==")]
        self.assertEqual(declarees, [ei.VERSION_SENTENCE_TRANSFORMERS])


class ConstructionSansPileTest(unittest.TestCase):
    """La configuration d'un paquet livré : `sentence_transformers` absent."""

    def test_vector_store_leve_embedding_indisponible_et_pas_import_error(self):
        """LE point de la correction.

        Avant, cette construction levait `ImportError` — donc 500 « Erreur
        interne du serveur » sur toute route touchant le RAG, pour toujours.
        Maintenant elle lève un type qui porte un ÉTAT, et l'installation part.
        """
        with _pile(pip=_PipFactice(bloquer=True)) as pip:
            with tempfile.TemporaryDirectory(prefix="epure-test-vect-") as tmp:
                with self.assertRaises(EmbeddingIndisponible) as leve:
                    VectorStore(tmp)
            self.assertNotIsInstance(leve.exception, ImportError)
            # L'état voyage AVEC l'exception : c'est ce qui permet au 503 de dire
            # où en est l'installation, et pas seulement qu'elle manque.
            self.assertEqual(leve.exception.etat["état"], ei.EN_COURS)
            self.assertIn("connexion réseau", leve.exception.etat["message"])
            self.assertEqual(leve.exception.etat["taille_estimée_mo"], ei.TAILLE_ESTIMEE_MO)
            # Et l'installation a réellement démarré, sur la bonne commande.
            self.assertTrue(pip.demarre.wait(timeout=5))
            self.assertIn("torch", pip.appels[0])

    def test_l_etat_dit_en_cours_et_non_un_echec(self):
        """« En cours » et « échoué » sont deux réponses différentes, et c'est la
        distinction que le frontend affiche. Les confondre remettrait une erreur
        à l'écran pendant une installation qui se passe bien.
        """
        with _pile(pip=_PipFactice(bloquer=True)):
            ei.declencher_installation()
            etat = ei.etat_installation()
            self.assertEqual(etat["état"], ei.EN_COURS)
            self.assertFalse(etat["disponible"])
            self.assertEqual(etat["cause"], "")

    def test_l_installation_reussie_rend_la_pile_disponible(self):
        """Le chemin nominal, sans installer 2 Go : les modules « arrivent » une
        fois leurs commandes jouées.
        """
        with _pile(present_apres=True) as pip:
            ei.declencher_installation()
            etat = _attendre_fin()
            self.assertEqual(etat["état"], ei.PRET, etat)
            self.assertTrue(etat["disponible"])
            self.assertEqual([c[4] for c in pip.appels],
                             ["torch", f"sentence-transformers=={ei.VERSION_SENTENCE_TRANSFORMERS}"])


class ConcurrenceTest(unittest.TestCase):
    """Un appel concurrent pendant l'installation ne la relance pas."""

    def test_douze_appels_simultanes_ne_lancent_qu_une_installation(self):
        """Le risque réel : `GET /rag/files` et `GET /rag/capabilities` partent
        ensemble à l'ouverture du panneau fichiers, et les modules qui offrent le
        contexte documentaire les rejouent. Douze appels et non deux, parce qu'un
        verrou qui ne tient pas se voit mieux sous pression.
        """
        with _pile(pip=_PipFactice(bloquer=True)) as pip, _compter_installations() as lancements:
            resultats: list[dict] = []
            barriere = threading.Barrier(12)

            def appeler():
                barriere.wait(timeout=5)
                resultats.append(ei.declencher_installation())

            fils = [threading.Thread(target=appeler) for _ in range(12)]
            for f in fils:
                f.start()
            for f in fils:
                f.join(timeout=10)

            self.assertEqual(len(resultats), 12)
            # Un seul thread d'installation, un seul `pip` lancé.
            self.assertEqual(sum(lancements), 1, "installation relancée")
            self.assertTrue(pip.demarre.wait(timeout=5))
            self.assertEqual(len(pip.appels), 1, pip.appels)
            # Et les onze autres ont bien reçu une réponse utile, pas un refus.
            self.assertTrue(all(r["état"] == ei.EN_COURS for r in resultats), resultats)

    def test_un_echec_ne_se_relance_pas_tout_seul(self):
        """Une seule tentative automatique par process.

        Sans cette garde, une machine hors ligne relancerait une tentative à
        chaque ouverture de panneau — et chacune paierait la sonde réseau.
        """
        with _pile(joignable=False), _compter_installations() as lancements:
            ei.declencher_installation()
            self.assertEqual(_attendre_fin()["état"], ei.ECHEC)
            self.assertEqual(sum(lancements), 1)
            # Deuxième et troisième appels automatiques : même verdict, aucun
            # nouveau lancement.
            self.assertEqual(ei.declencher_installation()["état"], ei.ECHEC)
            self.assertEqual(ei.declencher_installation()["cause"], ei.CAUSE_RESEAU)
            self.assertEqual(sum(lancements), 1, "échec relancé automatiquement")

    def test_une_demande_explicite_relance_apres_un_echec(self):
        """…mais le bouton « Réessayer » doit marcher : la cause la plus probable
        d'un échec se corrige en dehors de l'application (rebrancher le réseau),
        et l'utilisateur est le seul à savoir quand.
        """
        pip = _PipFactice()
        with _pile(joignable=False, pip=pip):
            ei.declencher_installation()
            _attendre_fin()
            self.assertEqual(pip.appels, [])          # la sonde a refusé avant pip
            ei._index_joignable = lambda *_a, **_k: True
            self.assertEqual(ei.declencher_installation(explicite=True)["état"], ei.EN_COURS)
            _attendre_fin()
            self.assertTrue(pip.appels, "la relance explicite n'a rien lancé")


class DistinctionDesEchecsTest(unittest.TestCase):
    """« Pas de réseau » n'est pas « pip a échoué », et ça se voit dans la réponse."""

    def test_reseau_injoignable(self):
        with _pile(joignable=False) as pip:
            ei.declencher_installation()
            etat = _attendre_fin()
            self.assertEqual(etat["état"], ei.ECHEC)
            self.assertEqual(etat["cause"], ei.CAUSE_RESEAU)
            self.assertIn("connexion réseau", etat["message"])
            # Et surtout : rien n'a été téléchargé. La sonde répond en secondes,
            # là où pip aurait attendu ses propres timeouts avant de renoncer.
            self.assertEqual(pip.appels, [])

    def test_echec_pip_reel(self):
        sortie = "ERROR: Could not build wheels for torch, which is required"
        with _pile(pip=_PipFactice(code=1, sortie=sortie)):
            ei.declencher_installation()
            etat = _attendre_fin()
            self.assertEqual(etat["cause"], ei.CAUSE_PIP)
            self.assertEqual(etat["étape"], "torch")
            # Le message nomme l'étape et le code : « erreur » tout court
            # n'apprendrait rien à personne.
            self.assertIn("torch", etat["message"])
            self.assertIn("1", etat["message"])

    def test_les_deux_messages_different(self):
        """La contre-épreuve : deux causes distinctes qui rendraient le même
        message ne serviraient à rien. Le test échouerait si quelqu'un
        « simplifiait » en un message unique.
        """
        with _pile(joignable=False):
            ei.declencher_installation()
            reseau = _attendre_fin()["message"]
        with _pile(pip=_PipFactice(code=1, sortie="ERROR: no matching distribution")):
            ei.declencher_installation()
            pip_reel = _attendre_fin()["message"]
        self.assertNotEqual(reseau, pip_reel)

    def test_une_coupure_pendant_le_telechargement_compte_comme_reseau(self):
        """La sonde passe, puis la connexion tombe au milieu des 2 Go. La sortie
        de pip est le seul témoin ; la lire évite d'annoncer « échec de pip » pour
        un câble débranché.
        """
        sortie = ("WARNING: Retrying … after connection broken by "
                  "'NewConnectionError: Failed to establish a new connection: "
                  "[Errno 11001] getaddrinfo failed'")
        with _pile(pip=_PipFactice(code=1, sortie=sortie)):
            ei.declencher_installation()
            self.assertEqual(_attendre_fin()["cause"], ei.CAUSE_RESEAU)

    def test_pip_absent_est_une_cause_a_part(self):
        """Le cas de l'« écart 3 » : `pip` purgé du paquet. Distinct parce qu'il
        ne se répare pas en réessayant — d'où l'absence de bouton côté interface.
        """
        with _pile():
            ei._module_importable = lambda nom: nom not in (
                "torch", "sentence_transformers", "pip")
            ei.declencher_installation()
            etat = _attendre_fin()
            self.assertEqual(etat["cause"], ei.CAUSE_PIP_ABSENT)


class PersistanceTest(unittest.TestCase):
    """Ce qui atterrit sur le disque, et surtout ce qui n'y atterrit pas."""

    def test_en_cours_n_est_jamais_ecrit(self):
        """Un « en cours » persisté survivrait au process qui l'a écrit.

        Un backend tué au milieu d'un `pip install` laisserait alors un fichier
        qui annonce une installation vivante pour toujours, sans que rien puisse
        le contredire. L'état transitoire n'appartient qu'à la mémoire.
        """
        with _pile(pip=_PipFactice(bloquer=True)) as pip:
            ei.declencher_installation()
            self.assertTrue(pip.demarre.wait(timeout=5))
            self.assertEqual(ei.etat_installation()["état"], ei.EN_COURS)
            fichier = ei._fichier_etat()
            contenu = json.loads(fichier.read_text(encoding="utf-8-sig")) if fichier.is_file() else {}
            self.assertNotEqual(contenu.get("état"), ei.EN_COURS)

    def test_un_echec_survit_au_redemarrage(self):
        """…tandis qu'un verdict terminal, lui, se relit.

        C'est ce qui permet à l'interface de dire « la dernière tentative a
        échoué faute de réseau » tout de suite après un redémarrage, au lieu de
        l'apprendre en refaisant la tentative.
        """
        with _pile(joignable=False):
            ei.declencher_installation()
            _attendre_fin()
            # Simule un nouveau process : mémoire vide, disque intact.
            ei._reinitialiser_pour_tests()
            relu = ei.etat_installation()
            self.assertEqual(relu["état"], ei.ECHEC)
            self.assertEqual(relu["cause"], ei.CAUSE_RESEAU)

    def test_le_fichier_passe_par_jsonstore(self):
        """Écrit en UTF-8 sans BOM et relu en `utf-8-sig` : ce fichier n'échappe
        pas à la règle du dépôt (CLAUDE.md §3.4), et il porte des clés accentuées
        (`état`, `étape`) qu'un mauvais encodage abîmerait en silence.
        """
        with _pile(joignable=False):
            ei.declencher_installation()
            _attendre_fin()
            octets = ei._fichier_etat().read_bytes()
            self.assertFalse(octets.startswith(b"\xef\xbb\xbf"))
            self.assertIn("état", json.loads(octets.decode("utf-8")))


class InterrupteurTest(unittest.TestCase):
    """`EPURE_EMBEDDING_AUTOINSTALL=0` — le garde-fou que la suite elle-même pose."""

    def test_desactive_ne_lance_rien_et_le_dit(self):
        with _pile(autoinstall=False) as pip:
            etat = ei.declencher_installation()
            self.assertEqual(etat["état"], ei.ECHEC)
            self.assertEqual(etat["cause"], ei.CAUSE_DESACTIVE)
            self.assertEqual(pip.appels, [])
            # Et l'état lu séparément dit la même chose : sans ça, l'interface
            # annoncerait « il se prépare au premier usage » sur une instance où
            # il ne se préparera jamais.
            self.assertEqual(ei.etat_installation()["cause"], ei.CAUSE_DESACTIVE)

    def test_desactive_ne_persiste_pas_un_verdict(self):
        """C'est une configuration, pas un échec : l'écrire ferait croire à une
        panne après que la variable a disparu.
        """
        with _pile(autoinstall=False):
            ei.declencher_installation()
            self.assertFalse(ei._fichier_etat().is_file())

    def test_la_suite_tourne_bien_avec_le_garde_fou(self):
        """L'invariant qui protège la CI : `_test_env` a posé la variable à 0.

        Le job `backend` n'installe ni torch ni sentence-transformers, donc
        `pile_presente()` y est faux : sans cette variable, le premier test qui
        touche une route du RAG lancerait 2 Go de téléchargement sur le runner.
        """
        self.assertEqual(os.environ.get("EPURE_EMBEDDING_AUTOINSTALL"), "0")


class SondeReseauTest(unittest.TestCase):
    """La sonde ne doit pas confondre « serveur qui refuse » et « pas de réseau »."""

    def test_une_reponse_http_compte_comme_joignable(self):
        """403, 404, 405 sur un HEAD veulent dire qu'un serveur a lu la requête.

        Les compter comme une panne réseau afficherait « vérifiez votre
        connexion » à quelqu'un dont la connexion va parfaitement bien — et
        masquerait la vraie cause de l'échec de pip qui suit.
        """
        import urllib.error
        import urllib.request

        def refuser(*_a, **_k):
            raise urllib.error.HTTPError("https://exemple.test", 403, "Forbidden", {}, None)

        original = urllib.request.urlopen
        urllib.request.urlopen = refuser
        try:
            self.assertTrue(ei._index_joignable("https://exemple.test"))
        finally:
            urllib.request.urlopen = original

    def test_une_erreur_de_transport_compte_comme_hors_ligne(self):
        import socket
        import urllib.request

        def couper(*_a, **_k):
            raise socket.gaierror(11001, "getaddrinfo failed")

        original = urllib.request.urlopen
        urllib.request.urlopen = couper
        try:
            self.assertFalse(ei._index_joignable("https://exemple.test"))
        finally:
            urllib.request.urlopen = original


class _RagIndisponible:
    """Double du proxy `rag` dont toute méthode lève, comme dans un paquet livré.

    Le vrai proxy ne peut pas servir ici : sur le poste d'Ilyann la pile est
    installée, donc `rag.get_indexed_files()` réussit et le test passerait sans
    rien éprouver. C'est la FRONTIÈRE HTTP qui est testée — « une
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
        etat = ei._verdict(ei.EN_COURS, message=ei._MSG_EN_COURS, etape="torch")
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
        """Cette route est interrogée en boucle par le frontend pendant
        l'installation : si elle déclenchait quoi que ce soit, chaque
        interrogation lancerait une tentative.
        """
        with _compter_installations() as lancements:
            r = self.client.get("/rag/capabilities", headers=self._auth())
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(sum(lancements), 0, "la lecture d'état a déclenché une installation")
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
        route répond, et n'installe rien.
        """
        with _compter_installations() as lancements:
            r = self.client.post("/rag/install", headers=self._auth())
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(sum(lancements), 0)
        self.assertIn(r.json()["état"], (ei.PRET, ei.ECHEC))


if __name__ == "__main__":
    unittest.main(verbosity=2)
