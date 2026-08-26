"""Isolation du dossier des conversations : EPURE_HISTORY_DIR et liaison tardive.

Jumeau de ``test_data_dir.py``, pour la variable qui n'existait pas. Deux
garanties, la seconde n'ayant de valeur que si la première tient :

1. **``resolve_history_dir()`` lit l'environnement À CHAQUE APPEL.** Vérifié par
   l'exécution, pas par relecture : chaque test pose ``EPURE_HISTORY_DIR``
   **après** que les modules ont été importés, puis constate où les fichiers
   atterrissent réellement.

2. **``HistoryEngine`` suit la variable.** C'est le point de ce fichier :
   ``core/history.py`` calculait ses deux chemins en constantes de module
   (``Path(__file__).parent.parent / "history"``), donc figés à l'import — le
   motif que CLAUDE.md §3.5 interdit, et qu'aucun test ne pouvait attraper
   puisqu'aucun n'atteignait ce moteur.

Pourquoi ça n'a jamais mordu jusqu'ici, et pourquoi ça allait mordre :
``history/`` n'était écrit qu'à **un** moment, la déconnexion du WebSocket de
chat, une fois par conversation. L'invariant tenait par accident. Le chantier
« conversations persistées » en fait le magasin vivant du chat — une écriture par
tour d'assistant — c'est-à-dire, sans ce détournement, une écriture par tour dans
les vraies conversations de l'utilisateur, que ``test_zz_donnees_reelles`` ne
voyait même pas (``history/`` n'était pas dans ``REAL_DIRS``).

Usage :
    python test_history_dir.py
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les dossiers AVANT tout import de core.* / main

from core.history import HistoryEngine  # noqa: E402
from core.paths import PathOutsideDataError, resolve_history_dir  # noqa: E402


class _FausseCollection:
    """Ce dont ``HistoryEngine.__init__`` a besoin du store, et rien de plus."""

    def __init__(self):
        self.upserts: list = []
        self.supprimes: list = []

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)

    def delete(self, ids=None):
        self.supprimes.append(ids)

    def count(self):
        return 0


class _FauxStore:
    """Évite de construire un vrai ``VectorStore``.

    Le vrai exigerait le modèle d'embedding, absent pendant la suite par
    construction (``EPURE_EMBEDDING_DIR`` est un temporaire VIDE et
    ``EPURE_EMBEDDING_AUTOINSTALL=0``). Ce fichier parle de chemins, pas de
    vecteurs.
    """

    def __init__(self):
        self.collections: dict = {}

    def collection(self, nom):
        return self.collections.setdefault(nom, _FausseCollection())


class _FauxLLM:
    """``_generate_title`` ne doit appeler AUCUN modèle pendant la suite."""

    def generate(self, messages, model=None):
        return "Titre de test"


class _DossierTemporaire(unittest.TestCase):
    """Pose EPURE_HISTORY_DIR sur un neuf, APRÈS les imports ci-dessus."""

    def setUp(self):
        self._prev = os.environ.get("EPURE_HISTORY_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-histdir-"))
        os.environ["EPURE_HISTORY_DIR"] = str(self.tmp)
        self.addCleanup(self._restaurer)

    def _restaurer(self):
        if self._prev is None:
            os.environ.pop("EPURE_HISTORY_DIR", None)
        else:
            os.environ["EPURE_HISTORY_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _moteur(self) -> HistoryEngine:
        return HistoryEngine(_FauxLLM(), _FauxStore())


class ResolutionTest(_DossierTemporaire):
    def test_lit_la_variable_a_chaque_appel(self):
        """La preuve de la liaison tardive : la variable change, le résultat suit."""
        self.assertEqual(resolve_history_dir(), self.tmp.resolve())

        autre = Path(tempfile.mkdtemp(prefix="epure-histdir2-"))
        try:
            os.environ["EPURE_HISTORY_DIR"] = str(autre)
            self.assertEqual(
                resolve_history_dir(), autre.resolve(),
                "resolve_history_dir() doit relire l'environnement, pas servir un cache",
            )
        finally:
            shutil.rmtree(autre, ignore_errors=True)

    def test_defaut_sans_variable(self):
        os.environ.pop("EPURE_HISTORY_DIR", None)
        import core.history as core_history
        attendu = (Path(core_history.__file__).resolve().parent.parent / "history").resolve()
        self.assertEqual(resolve_history_dir(), attendu)

    def test_toujours_resolu(self):
        self.assertTrue(resolve_history_dir().is_absolute())


class LiaisonTardiveTest(_DossierTemporaire):
    """Le moteur construit après le changement de variable écrit au bon endroit.

    Si ``core/history.py`` figeait encore ses chemins à l'import, ils
    pointeraient le dossier de ``_test_env`` (ou le vrai ``history/``) et ces
    tests échoueraient.
    """

    def test_les_chemins_du_moteur_suivent_la_variable(self):
        moteur = self._moteur()
        self.assertEqual(moteur._dir, self.tmp.resolve())
        self.assertEqual(moteur._index_path.parent, self.tmp.resolve())
        self.assertEqual(moteur._index_path.name, "conversations.json")

    def test_le_dossier_est_cree_a_la_construction(self):
        """``mkdir(parents=True)`` et non ``mkdir(exist_ok=True)`` seul.

        L'ancien appel ne créait pas les parents : un ``EPURE_HISTORY_DIR``
        pointant deux niveaux plus bas aurait levé ``FileNotFoundError`` à la
        construction du moteur.
        """
        profond = self.tmp / "a" / "b"
        os.environ["EPURE_HISTORY_DIR"] = str(profond)
        self._moteur()
        self.assertTrue(profond.is_dir(), f"{profond} n'a pas été créé")

    def test_une_conversation_atterrit_dans_le_dossier_courant(self):
        """Le bout en bout : sauvegarder écrit ici, et nulle part ailleurs."""
        moteur = self._moteur()
        conv_id = moteur.save_conversation(
            [{"role": "user", "content": "bonjour"},
             {"role": "assistant", "content": "salut"}],
            model="qwen2.5:7b",
        )
        fichier = self.tmp / f"{conv_id}.json"
        self.assertTrue(fichier.is_file(), f"rien écrit dans {self.tmp}")
        self.assertTrue((self.tmp / "conversations.json").is_file(), "index non écrit")

        relus = moteur.list_conversations(days=0)
        self.assertEqual([c["id"] for c in relus], [conv_id])

    def test_le_decompte_part_de_zero(self):
        """Ce que garantit le temporaire VIDE de ``_test_env``.

        Si ``EPURE_HISTORY_DIR`` était une COPIE des vraies conversations, cette
        assertion passerait sur ce poste (18 conversations) et échouerait en CI
        (dossier vide) — ou l'inverse. C'est la raison d'être du choix « vide »
        pour un dossier qui contient pourtant des données utilisateur.
        """
        self.assertEqual(self._moteur().list_conversations(days=0), [])


class ConfinementTest(_DossierTemporaire):
    """``_conv_path`` : le chemin d'une conversation reste un enfant DIRECT.

    ``conv_id`` vient du client sur ``GET`` comme sur ``DELETE
    /history/{conv_id}``, et le second finit en ``unlink()``.

    À lire avec sa limite : **aucune de ces entrées n'est atteignable par le
    routage aujourd'hui** — un paramètre de chemin Starlette ne peut pas contenir
    de ``/``, même percent-encodé. La garde rend le confinement vrai par
    construction plutôt que par une propriété du routage qui n'est écrite nulle
    part dans ce fichier, et elle prend son sens à l'étape 3 du chantier, où un
    ``PUT`` écrit sous un identifiant fourni par le client.
    """

    def test_un_identifiant_normal_passe(self):
        moteur = self._moteur()
        cible = moteur._conv_path("0f13f23a-c07b-4b91-af4e-509fed34b572")
        self.assertEqual(cible.parent, self.tmp.resolve())
        self.assertTrue(cible.name.endswith(".json"))

    def test_les_evasions_sont_refusees(self):
        moteur = self._moteur()
        for mauvais in ("../evil", "../../backend/.env", "sub/x", "a\\b"):
            with self.subTest(conv_id=mauvais):
                with self.assertRaises(PathOutsideDataError):
                    moteur._conv_path(mauvais)

    def test_les_points_seuls_restent_dedans(self):
        """Contre-intuitif, et c'est pour ça que c'est écrit : ``..`` ne s'évade pas.

        Le suffixe est concaténé AVANT la résolution : ``".."`` donne
        ``<history>/...json``, un enfant direct au nom bizarre, pas le dossier
        parent. Attendre un refus ici serait une erreur — et c'en était une, dans
        la première version de ce test. Ce qui protège, c'est la position du
        ``.json``, et le noter évite de « corriger » un jour la garde pour un
        danger qui n'existe pas.
        """
        cible = self._moteur()._conv_path("..")
        self.assertEqual(cible.parent, self.tmp.resolve())
        self.assertEqual(cible.name, "...json")

    def test_lire_un_identifiant_refuse_rend_none(self):
        """Refus, pas exception : la route rend un 404, pas un 500."""
        self.assertIsNone(self._moteur().get_conversation("../evil"))

    def test_supprimer_un_identifiant_refuse_ne_touche_rien(self):
        moteur = self._moteur()
        temoin = self.tmp.parent / "temoin.json"
        temoin.write_text("{}", encoding="utf-8")
        self.addCleanup(temoin.unlink, True)

        self.assertFalse(moteur.delete_conversation(f"../{temoin.stem}"))
        self.assertTrue(temoin.is_file(), "le fichier hors dossier a été supprimé")


if __name__ == "__main__":
    unittest.main(verbosity=2)
