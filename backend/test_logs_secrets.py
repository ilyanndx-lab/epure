#!/usr/bin/env python3
"""Le token d'API ne doit pas atterrir dans un journal — CLAUDE.md §6.

La fuite mesurée : uvicorn journalise le chemin AVEC sa query, et le token du
WebSocket voyage en query param faute d'en-tête possible sur
``new WebSocket()``. La ligne observée, telle quelle, dans le journal du
backend :

    INFO: 127.0.0.1:51320 - "WebSocket /ws/chat?token=YGdSbk…" [accepted]

Ce fichier verrouille les deux moitiés du correctif :

  * le masquage lui-même (:class:`MasquageTest`) — y compris la ligne exacte
    d'uvicorn, reproduite avec son format et ses arguments, parce qu'un filtre
    qui marche sur une chaîne d'exemple mais casse sur ``%s`` et ``%d`` ne sert
    à rien ;
  * son **installation effective** sur les loggers d'uvicorn
    (:class:`InstallationTest`). C'est la moitié fragile : ces loggers ont leurs
    propres handlers et ``propagate = False``, donc un filtre posé sur la racine
    ne les voit jamais. Un test qui n'affirmerait que ``masquer()`` laisserait
    passer une régression où plus personne n'appelle le filtre.

Usage :
    python test_logs_secrets.py
"""

import io
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

from core.logs import (  # noqa: E402
    MASQUE,
    FiltreSecrets,
    LOGGERS_FILTRES,
    masquer,
    masquer_secrets_dans_logs,
)

#: Un vrai token a cette forme (43 caractères base64url, cf. core.auth).
_TOKEN = "YGdSbkNxz4jKf-sll5zDefw5zOeLBGds4ac5qbiSINk"


class MasquageTest(unittest.TestCase):
    def test_la_ligne_websocket_d_uvicorn(self):
        """Le cas réel, avec le format et les arguments d'uvicorn."""
        record = logging.LogRecord(
            name="uvicorn.error", level=logging.INFO, pathname=__file__, lineno=1,
            msg='%s - "WebSocket %s" [accepted]',
            args=("127.0.0.1:51320", f"/ws/chat?token={_TOKEN}"),
            exc_info=None,
        )
        FiltreSecrets().filter(record)
        ligne = record.getMessage()
        self.assertNotIn(_TOKEN, ligne)
        self.assertIn(MASQUE, ligne)
        # Le reste de la ligne est intact : c'est un journal, il doit rester utile.
        self.assertIn("127.0.0.1:51320", ligne)
        self.assertIn("/ws/chat?token=", ligne)
        self.assertIn("[accepted]", ligne)

    def test_la_ligne_d_acces_http_garde_son_code_de_statut(self):
        """Le format d'accès mêle %s et %d : masquer ne doit pas casser le %d.

        C'est la raison pour laquelle le filtre ne touche que les arguments de
        type str. Remplacer 200 par une chaîne lèverait un TypeError au
        formatage, donc à l'émission de la ligne — un correctif de journal qui
        casse le journal.
        """
        record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:1", "GET", f"/ws/chat?token={_TOKEN}", "1.1", 200),
            exc_info=None,
        )
        FiltreSecrets().filter(record)
        ligne = record.getMessage()
        self.assertNotIn(_TOKEN, ligne)
        self.assertTrue(ligne.endswith(" 200"), ligne)

    def test_variantes_de_parametre_et_de_position(self):
        cas = [
            (f"/ws/chat?token={_TOKEN}", "token en seul paramètre"),
            (f"/ws/chat?x=1&token={_TOKEN}", "token après un autre paramètre"),
            (f"/ws/chat?token={_TOKEN}&x=1", "token suivi d'un autre paramètre"),
            (f"/x?api_key={_TOKEN}", "api_key"),
            (f"/x?apikey={_TOKEN}", "apikey"),
            (f"/x?access_token={_TOKEN}", "access_token"),
            (f"/x?TOKEN={_TOKEN}", "casse différente"),
        ]
        for texte, quoi in cas:
            with self.subTest(quoi=quoi):
                masque = masquer(texte)
                self.assertNotIn(_TOKEN, masque)
                self.assertIn(MASQUE, masque)

    def test_le_parametre_qui_suit_n_est_pas_avale(self):
        """La valeur s'arrête au `&` — sinon on masquerait toute la fin de query."""
        self.assertEqual(
            masquer(f"/ws/chat?token={_TOKEN}&modele=qwen2.5"),
            f"/ws/chat?token={MASQUE}&modele=qwen2.5",
        )

    def test_ce_qui_n_est_pas_un_secret_reste_intact(self):
        for texte in ("/models", "/ws/chat", "GET /modules HTTP/1.1",
                      "?modele=qwen2.5&n=3", "127.0.0.1:51320"):
            with self.subTest(texte=texte):
                self.assertEqual(masquer(texte), texte)

    def test_arguments_non_textuels_et_dictionnaire(self):
        """Ni None, ni int, ni un dict d'arguments ne doivent faire tomber le filtre."""
        for args in ((None, 200, 1.5), {"chemin": f"/x?token={_TOKEN}", "code": 200}):
            with self.subTest(args=args):
                record = logging.LogRecord(
                    name="x", level=logging.INFO, pathname=__file__, lineno=1,
                    msg="%s", args=args, exc_info=None,
                )
                self.assertTrue(FiltreSecrets().filter(record))
        self.assertNotIn(_TOKEN, str(record.args))

    def test_un_token_dans_le_message_lui_meme(self):
        """Cas d'un f-string journalisé sans arguments (le nôtre, un jour)."""
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=1,
            msg=f"connexion refusée : /ws/workshop?token={_TOKEN}", args=None,
            exc_info=None,
        )
        FiltreSecrets().filter(record)
        self.assertNotIn(_TOKEN, record.getMessage())


class InstallationTest(unittest.TestCase):
    """Le filtre est-il RÉELLEMENT posé sur les loggers d'uvicorn ?

    Ces loggers ont ``propagate = False`` et leurs propres handlers : un filtre
    posé sur la racine ne les verrait jamais passer. C'est la moitié du
    correctif qui peut disparaître sans que rien ne le dise.
    """

    def setUp(self):
        self._sauvegarde = {
            nom: list(logging.getLogger(nom).filters) for nom in LOGGERS_FILTRES
        }

    def tearDown(self):
        for nom, filtres in self._sauvegarde.items():
            logging.getLogger(nom).filters = filtres

    def test_installe_sur_les_loggers_d_uvicorn_et_la_racine(self):
        for nom in LOGGERS_FILTRES:
            logging.getLogger(nom).filters = []
        masquer_secrets_dans_logs()
        for nom in LOGGERS_FILTRES:
            with self.subTest(logger=nom or "<racine>"):
                self.assertTrue(
                    any(isinstance(f, FiltreSecrets)
                        for f in logging.getLogger(nom).filters),
                    f"aucun FiltreSecrets sur {nom or '<racine>'}",
                )

    def test_idempotent(self):
        """main peut être importé plusieurs fois (suite de tests, --reload)."""
        for nom in LOGGERS_FILTRES:
            logging.getLogger(nom).filters = []
        masquer_secrets_dans_logs()
        avant = {nom: len(logging.getLogger(nom).filters) for nom in LOGGERS_FILTRES}
        self.assertEqual(masquer_secrets_dans_logs(), [])
        for nom in LOGGERS_FILTRES:
            self.assertEqual(len(logging.getLogger(nom).filters), avant[nom])

    def test_le_token_ne_sort_pas_d_un_handler_reel(self):
        """Bout en bout : logger réel, handler réel, formatage réel."""
        for nom in LOGGERS_FILTRES:
            logging.getLogger(nom).filters = []
        masquer_secrets_dans_logs()

        tampon = io.StringIO()
        handler = logging.StreamHandler(tampon)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("uvicorn.error")
        logger.addHandler(handler)
        niveau, propage = logger.level, logger.propagate
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info('%s - "WebSocket %s" [accepted]',
                        "127.0.0.1:51320", f"/ws/chat?token={_TOKEN}")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(niveau)
            logger.propagate = propage

        sortie = tampon.getvalue()
        self.assertNotIn(_TOKEN, sortie)
        self.assertIn(MASQUE, sortie)
        self.assertIn("[accepted]", sortie)


class BranchementDansMainTest(unittest.TestCase):
    """L'installation est-elle branchée là où elle doit l'être ?

    Sans cette affirmation, ``core/logs.py`` pourrait rester parfait et ne jamais
    être appelé — c'est exactement la forme qu'avait prise la fuite d'origine :
    un invariant écrit dans CLAUDE.md, jamais vérifié.

    **Cette classe ne vide PAS les filtres**, contrairement à
    :class:`InstallationTest`, et son nom la fait passer avant lui (``unittest``
    ordonne les classes alphabétiquement dans un module). Les deux précautions
    visent le même piège : ``unittest discover`` importe tous les fichiers dans
    un seul process, donc ``import main`` ici peut être un no-op — un autre test
    l'a déjà importé, et le filtre a été posé à ce moment-là. Un ``setUp`` qui
    vide les filtres avant un import qui ne se reproduira pas ferait échouer ce
    test pour une raison qui n'a rien à voir avec le sujet.
    """

    def test_importer_main_installe_le_masquage(self):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import main  # noqa: F401  — import lourd, assumé : c'est le sujet du test

        for nom in LOGGERS_FILTRES:
            with self.subTest(logger=nom or "<racine>"):
                self.assertTrue(
                    any(isinstance(f, FiltreSecrets)
                        for f in logging.getLogger(nom).filters),
                    "importer main n'installe pas le masquage",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
