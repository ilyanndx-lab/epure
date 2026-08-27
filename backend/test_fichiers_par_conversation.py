"""Les fichiers appartiennent à la conversation — étape 5 du chantier.

`fichiers_actifs` et `résumé_contexte` ont quitté `context_session.json`. Ce
fichier verrouille les deux moitiés du changement :

* **le retrait** — les clés ne reviennent pas, les routes `/files/active` non
  plus. Un garde-fou de non-régression au sens strict : rien n'empêcherait de
  les recréer « pour dépanner », et deux notions de « fichiers actifs » qui
  coexistent divergent mécaniquement (la leçon de `modules_state.json`) ;
* **la bascule** — le RAG du chat interroge bien les fichiers de LA conversation,
  et le résumé injecté est celui de CE fil.

Ce que le retrait corrige, concrètement : la liste était UNIQUE, écrasée à chaque
import et relue à chaque message. Importer un fichier dans un fil détachait en
silence ceux de tous les autres, et il n'existait aucun moyen de choisir lesquels
servir.

Usage :
    python test_fichiers_par_conversation.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les dossiers AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.memory import MemoryEngine  # noqa: E402
from core.runtime import history_engine  # noqa: E402

_WS = "ws://localhost/ws/chat?token={t}"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def setUp(self):
        self.auth = {"Authorization": f"Bearer {self.token}"}


class RetraitDesClesGlobalesTest(_Base):
    """Elles ne doivent pas revenir — ni dans le défaut, ni par une route."""

    def test_le_contexte_de_session_ne_les_porte_plus(self):
        corps = self.client.get("/context", headers=self.auth).json()
        self.assertNotIn("fichiers_actifs", corps)
        self.assertNotIn("résumé_contexte", corps)

    def test_le_defaut_du_moteur_memoire_ne_les_porte_plus(self):
        """Le défaut est la source : s'il les réintroduit, chaque démarrage les
        recrée, puisque `context_session.json` est réécrit à l'import."""
        from core import memory as module_memory
        self.assertNotIn("fichiers_actifs", module_memory._CONTEXT_DEFAULT)
        self.assertNotIn("résumé_contexte", module_memory._CONTEXT_DEFAULT)

    def test_les_routes_files_active_ont_disparu(self):
        self.assertEqual(self.client.get("/files/active", headers=self.auth).status_code, 404)
        self.assertEqual(self.client.delete("/files/active", headers=self.auth).status_code, 404)

    def test_le_profil_n_injecte_plus_le_contexte_actif(self):
        """`MemoryEngine` ne connaît pas les conversations, donc ne peut pas
        décider quel résumé injecter. Même si la clé traîne dans un
        `context_session.json` ancien, elle ne doit plus atteindre le prompt.
        """
        moteur = MemoryEngine()
        moteur.update_context(**{"résumé_contexte": "RESIDU D UN ANCIEN FICHIER"})
        rendu = moteur.build_system_context("une question assez longue pour compter")
        self.assertNotIn("RESIDU D UN ANCIEN FICHIER", rendu)
        self.assertNotIn("[CONTEXTE ACTIF]", rendu)


class _RagEspion:
    """Capture ce que le chat demande au RAG, sans construire de moteur réel."""

    def __init__(self):
        self.filtres: list = []
        self.globales: list = []
        self.indexes: list = []

    def index_file(self, chemin):
        """`_stream_load_sse` l'appelle par fichier.

        Sans cette méthode, l'`AttributeError` était **avalée** par le
        `try/except` de la boucle d'indexation : aucun chemin n'entrait dans
        `indexed_paths`, donc rien n'était attaché, et le test échouait en
        accusant l'attachement plutôt que le double.
        """
        self.indexes.append(str(chemin))

    def query(self, texte, n_results=None):
        self.globales.append(texte)
        return "CHUNK GLOBAL"

    def query_filtered(self, texte, paths, n_results=None):
        self.filtres.append((texte, list(paths)))
        return "CHUNK FILTRE"

    def get_indexed_files(self):
        return []


class BasculeDuRagTest(_Base):
    """Le chat interroge les fichiers de LA conversation."""

    def setUp(self):
        super().setUp()
        self.espion = _RagEspion()
        original_rag = routeur_chat.rag
        routeur_chat.rag = self.espion
        self.addCleanup(setattr, routeur_chat, "rag", original_rag)

        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)
        self.prompts: list = []

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            self.prompts.append([dict(m) for m in messages])
            return iter(["ok"])
        routeur_chat.llm.stream = faux_stream

    def _tour(self, conv_id, texte="question", **extra):
        corps = {"role": "user", "content": texte, "direct": True,
                 "conversation_id": conv_id, **extra}
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps(corps))
            while json.loads(ws.receive_text())["type"] != "done":
                pass

    def test_les_fichiers_interroges_sont_ceux_de_la_conversation(self):
        a = history_engine.create_conversation(fichiers=["/a/un.pdf"])
        b = history_engine.create_conversation(fichiers=["/b/deux.pdf"])

        self._tour(a["id"])
        self._tour(b["id"])

        self.assertEqual([chemins for _, chemins in self.espion.filtres],
                         [["/a/un.pdf"], ["/b/deux.pdf"]])

    def test_une_conversation_sans_fichier_n_interroge_pas_le_rag(self):
        conv = history_engine.create_conversation()
        self._tour(conv["id"])
        self.assertEqual(self.espion.filtres, [])
        self.assertEqual(self.espion.globales, [])

    def test_le_mode_corpus_entier_reste_orthogonal(self):
        """`rag_override == "all"` veut dire « cherche partout », pas « attache
        tout » : il ignore les attachements au lieu de les compléter."""
        conv = history_engine.create_conversation(fichiers=["/a/un.pdf"])
        self._tour(conv["id"], rag_override="all")
        self.assertEqual(self.espion.globales, ["question"])
        self.assertEqual(self.espion.filtres, [])

    def test_le_resume_de_la_conversation_entre_dans_le_prompt(self):
        conv = history_engine.create_conversation()
        history_engine.set_resume_contexte(conv["id"], "Ce fil parle de thermodynamique.")
        self._tour(conv["id"])
        systeme = "\n".join(m["content"] for m in self.prompts[-1] if m["role"] == "system")
        self.assertIn("[CONTEXTE ACTIF]", systeme)
        self.assertIn("thermodynamique", systeme)

    def test_le_resume_d_un_autre_fil_ne_fuit_pas(self):
        """Le défaut exact que le retrait corrige : un résumé global était servi
        à toutes les conversations."""
        autre = history_engine.create_conversation()
        history_engine.set_resume_contexte(autre["id"], "SECRET DE L AUTRE FIL")
        conv = history_engine.create_conversation()

        self._tour(conv["id"])
        systeme = "\n".join(m["content"] for m in self.prompts[-1] if m["role"] == "system")
        self.assertNotIn("SECRET DE L AUTRE FIL", systeme)


class ImportVersUneConversationTest(_Base):
    """`/files/load` attache à la conversation visée, et complète au lieu de
    remplacer."""

    def setUp(self):
        super().setUp()
        self.racine = Path(tempfile.mkdtemp(prefix="epure-import-conv-"))
        self.addCleanup(shutil.rmtree, self.racine, True)
        from core import paths as core_paths
        original = core_paths.user_data_roots
        core_paths.user_data_roots = lambda: [self.racine.resolve()]
        self.addCleanup(setattr, core_paths, "user_data_roots", original)

        # `_stream_load_sse` indexe puis résume : on neutralise les deux, le
        # sujet ici est l'ATTACHEMENT, pas l'indexation ni le modèle.
        from modules import settings as _  # noqa: F401
        import modules.settings.router as routeur_reglages
        self.reglages = routeur_reglages
        original_rag = routeur_reglages.rag
        routeur_reglages.rag = _RagEspion()
        self.addCleanup(setattr, routeur_reglages, "rag", original_rag)
        original_llm = routeur_reglages.llm.stream
        self.addCleanup(setattr, routeur_reglages.llm, "stream", original_llm)
        routeur_reglages.llm.stream = lambda *a, **k: iter(["Résumé."])

    def _fichier(self, nom):
        p = self.racine / nom
        p.write_text("contenu de cours", encoding="utf-8")
        return str(p.resolve())

    def _charger(self, chemins, conversation_id=None):
        corps = {"paths": chemins}
        if conversation_id is not None:
            corps["conversation_id"] = conversation_id
        r = self.client.post("/files/load", json=corps, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_l_import_attache_a_la_conversation_visee(self):
        conv = history_engine.create_conversation()
        f = self._fichier("cours.txt")
        self._charger([f], conversation_id=conv["id"])
        self.assertEqual(
            history_engine.get_conversation(conv["id"])["fichiers_attachés"], [f])

    def test_l_import_COMPLETE_les_attachements(self):
        """Un import ajoute au contexte en cours ; il ne détache pas ce que
        l'utilisateur y avait déjà mis. C'est précisément ce que l'ancien
        `update_context(fichiers_actifs=…)` faisait — il écrasait."""
        deja = self._fichier("deja.txt")
        conv = history_engine.create_conversation(fichiers=[deja])
        nouveau = self._fichier("nouveau.txt")

        self._charger([nouveau], conversation_id=conv["id"])
        self.assertEqual(
            history_engine.get_conversation(conv["id"])["fichiers_attachés"],
            [deja, nouveau])

    def test_sans_conversation_l_import_indexe_seulement(self):
        """Cas légitime : alimenter le corpus depuis les Réglages, sans viser un
        fil. Aucune conversation ne doit être créée ni modifiée."""
        avant = len(history_engine.list_conversations(0))
        self._charger([self._fichier("libre.txt")])
        self.assertEqual(len(history_engine.list_conversations(0)), avant)


class ResumeSkillTest(_Base):
    """`/skills/résumé` doit savoir DE QUELLE conversation il parle."""

    def test_sans_identifiant_une_erreur_lisible_dans_le_flux(self):
        """Erreur SSE et non 422 : le consommateur n'écoute que des `data:`, une
        erreur de validation FastAPI n'y serait pas lisible."""
        r = self.client.post("/skills/résumé", json={}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        evenements = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
        self.assertEqual(evenements[0]["type"], "error")

    def test_une_conversation_sans_fichier_le_dit(self):
        conv = history_engine.create_conversation()
        r = self.client.post("/skills/résumé", json={"conversation_id": conv["id"]},
                             headers=self.auth)
        evenements = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
        self.assertEqual(evenements[0]["type"], "error")
        self.assertIn("attaché", evenements[0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
