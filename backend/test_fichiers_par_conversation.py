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

        Rend un texte NON VIDE, et c'est délibéré depuis que le vrai
        `RAGEngine.index_file` rend le texte qu'il a indexé (cf.
        `test_vision_images.py` — le bug où un second appel, statique, ne
        voyait jamais la description vision). Rendre `None` ici referait
        diverger ce double du contrat réel exactement de la façon qui a coûté
        ce bug : silencieusement, sur le seul champ qui comptait.
        """
        self.indexes.append(str(chemin))
        return f"[contenu indexé de {chemin}]"

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


class InstructionDeFilTest(_Base):
    """La consigne du fil entre dans le prompt, et n'en sort pas.

    Trois portées coexistent dans le prompt système et répondent à des questions
    différentes ; ce qui compte est qu'elles ne se contaminent pas :

      profil                permanent, toute l'instance
      instruction_générale  toute l'instance, et persistante
      instruction (ici)     CE fil, tant qu'il existe
    """

    def setUp(self):
        super().setUp()
        self.prompts: list = []
        original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", original)

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            self.prompts.append([dict(m) for m in messages])
            return iter(["ok"])
        routeur_chat.llm.stream = faux_stream

        espion = _RagEspion()
        rag_original = routeur_chat.rag
        routeur_chat.rag = espion
        self.addCleanup(setattr, routeur_chat, "rag", rag_original)

    def _tour(self, conv_id, texte="question"):
        corps = {"role": "user", "content": texte, "direct": True,
                 "conversation_id": conv_id}
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps(corps))
            while json.loads(ws.receive_text())["type"] != "done":
                pass
        return "\n".join(m["content"] for m in self.prompts[-1] if m["role"] == "system")

    def test_la_consigne_entre_dans_le_prompt_systeme(self):
        conv = history_engine.create_conversation()
        history_engine.set_instruction(conv["id"], "Réponds uniquement en anglais.")
        systeme = self._tour(conv["id"])
        self.assertIn("[INSTRUCTION DE CETTE CONVERSATION]", systeme)
        self.assertIn("Réponds uniquement en anglais.", systeme)

    def test_sans_consigne_aucun_bloc_n_est_ajoute(self):
        conv = history_engine.create_conversation()
        self.assertNotIn("[INSTRUCTION DE CETTE CONVERSATION]", self._tour(conv["id"]))

    def test_la_consigne_d_un_autre_fil_ne_fuit_pas(self):
        """Le défaut que la portée par conversation existe pour éviter."""
        autre = history_engine.create_conversation()
        history_engine.set_instruction(autre["id"], "CONSIGNE DE L AUTRE FIL")
        conv = history_engine.create_conversation()
        self.assertNotIn("CONSIGNE DE L AUTRE FIL", self._tour(conv["id"]))

    def test_elle_est_lue_APRES_la_consigne_generale(self):
        """De deux consignes qui se contredisent, la plus spécifique en dernier.

        L'instruction de session vaut pour toute l'instance ; celle du fil vise
        ce fil précis. Un modèle qui lit les deux doit rencontrer la seconde en
        dernier.
        """
        routeur_chat.memory.update_context(**{"instruction_générale": "CONSIGNE GLOBALE"})
        self.addCleanup(lambda: routeur_chat.memory.update_context(**{"instruction_générale": ""}))

        conv = history_engine.create_conversation()
        history_engine.set_instruction(conv["id"], "CONSIGNE DU FIL")
        systeme = self._tour(conv["id"])

        self.assertLess(systeme.index("CONSIGNE GLOBALE"), systeme.index("CONSIGNE DU FIL"))


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


class GenerateSummaryToggleTest(_Base):
    """`generate_summary=False` : le résumé automatique est sauté, jamais
    l'indexation ni l'attachement des fichiers.

    Motivé par le repli vision Ollama (§3.3 bis de CLAUDE.md) : plusieurs gros
    fichiers, sur un poste sans FLM, font une indexation séquentielle
    potentiellement longue — le résumé (un appel LLM de plus, après coup)
    n'est pas toujours voulu en plus de cette attente.
    """

    def setUp(self):
        super().setUp()
        self.racine = Path(tempfile.mkdtemp(prefix="epure-resume-toggle-"))
        self.addCleanup(shutil.rmtree, self.racine, True)
        from core import paths as core_paths
        original = core_paths.user_data_roots
        core_paths.user_data_roots = lambda: [self.racine.resolve()]
        self.addCleanup(setattr, core_paths, "user_data_roots", original)

        import modules.settings.router as routeur_reglages
        self.reglages = routeur_reglages
        original_rag = routeur_reglages.rag
        routeur_reglages.rag = _RagEspion()
        self.addCleanup(setattr, routeur_reglages, "rag", original_rag)

        self.appels_stream = 0
        original_llm = routeur_reglages.llm.stream
        self.addCleanup(setattr, routeur_reglages.llm, "stream", original_llm)

        def faux_stream(*a, **k):
            self.appels_stream += 1
            return iter(["Résumé."])
        routeur_reglages.llm.stream = faux_stream

        # Spy sur l'engin RÉEL (pas un double) : c'est l'ABSENCE d'appel qu'on
        # vérifie, donc le comportement normal doit rester atteignable pour le
        # cas `generate_summary=True` de ce même test.
        self.appels_resume: list = []
        original_resume = routeur_reglages.history_engine.set_resume_contexte
        self.addCleanup(setattr, routeur_reglages.history_engine,
                        "set_resume_contexte", original_resume)

        def espion_resume(conv_id, texte):
            self.appels_resume.append((conv_id, texte))
            return original_resume(conv_id, texte)
        routeur_reglages.history_engine.set_resume_contexte = espion_resume

    def _fichier(self, nom):
        p = self.racine / nom
        p.write_text("contenu de cours", encoding="utf-8")
        return str(p.resolve())

    def _charger(self, chemins, conversation_id=None, generate_summary=None):
        corps = {"paths": chemins}
        if conversation_id is not None:
            corps["conversation_id"] = conversation_id
        if generate_summary is not None:
            corps["generate_summary"] = generate_summary
        r = self.client.post("/files/load", json=corps, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_generate_summary_false_n_appelle_pas_set_resume_contexte(self):
        conv = history_engine.create_conversation()
        f = self._fichier("cours.txt")
        self._charger([f], conversation_id=conv["id"], generate_summary=False)
        self.assertEqual(self.appels_resume, [])
        self.assertEqual(self.appels_stream, 0)

    def test_generate_summary_false_n_efface_pas_un_resume_existant(self):
        """Le risque exact que ce toggle doit éviter : sauter le résumé ne doit
        jamais écraser silencieusement celui déjà présent sur le fil."""
        conv = history_engine.create_conversation()
        history_engine.set_resume_contexte(conv["id"], "Résumé déjà là.")
        f = self._fichier("cours.txt")
        self._charger([f], conversation_id=conv["id"], generate_summary=False)
        self.assertEqual(
            history_engine.get_conversation(conv["id"])["résumé_contexte"],
            "Résumé déjà là.")

    def test_generate_summary_false_indexe_et_attache_quand_meme(self):
        conv = history_engine.create_conversation()
        f = self._fichier("cours.txt")
        self._charger([f], conversation_id=conv["id"], generate_summary=False)
        self.assertEqual(
            history_engine.get_conversation(conv["id"])["fichiers_attachés"], [f])
        self.assertEqual(self.reglages.rag.indexes, [f])

    def test_generate_summary_true_reste_le_comportement_historique(self):
        """Sans rien changer côté appelant, le résumé continue de se produire —
        ce champ a un défaut à `True` justement pour ça."""
        conv = history_engine.create_conversation()
        f = self._fichier("cours.txt")
        self._charger([f], conversation_id=conv["id"])
        self.assertEqual(len(self.appels_resume), 1)
        self.assertEqual(self.appels_resume[0][0], conv["id"])


class ProgressionImportTest(_Base):
    """Un événement SSE par fichier, avant son traitement — l'écran n'est plus
    vide pendant toute la phase d'indexation."""

    def setUp(self):
        super().setUp()
        self.racine = Path(tempfile.mkdtemp(prefix="epure-progress-"))
        self.addCleanup(shutil.rmtree, self.racine, True)
        from core import paths as core_paths
        original = core_paths.user_data_roots
        core_paths.user_data_roots = lambda: [self.racine.resolve()]
        self.addCleanup(setattr, core_paths, "user_data_roots", original)

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

    def _evenements(self, chemins, **extra):
        corps = {"paths": chemins, **extra}
        r = self.client.post("/files/load", json=corps, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]

    def test_un_evenement_progress_par_fichier_dans_l_ordre(self):
        fichiers = [self._fichier("a.txt"), self._fichier("b.txt"),
                   self._fichier("c.txt")]
        progres = [e for e in self._evenements(fichiers, generate_summary=False)
                  if e["type"] == "progress"]
        self.assertEqual([e["index"] for e in progres], [1, 2, 3])
        self.assertEqual([e["total"] for e in progres], [3, 3, 3])
        self.assertEqual([e["fichier"] for e in progres], ["a.txt", "b.txt", "c.txt"])

    def test_un_fichier_ignore_au_milieu_compte_quand_meme_dans_la_numerotation(self):
        """Numérotation sur `paths` (la liste brute), pas `indexed_paths` — une
        extension non supportée reste une étape visible pour l'utilisateur, même
        si elle n'est pas indexée."""
        ignore = self.racine / "ignore.xyz"
        ignore.write_text("contenu", encoding="utf-8")
        fichiers = [self._fichier("a.txt"), str(ignore.resolve()),
                   self._fichier("c.txt")]
        progres = [e for e in self._evenements(fichiers, generate_summary=False)
                  if e["type"] == "progress"]
        self.assertEqual([e["index"] for e in progres], [1, 2, 3])
        self.assertEqual([e["total"] for e in progres], [3, 3, 3])
        self.assertEqual([e["fichier"] for e in progres],
                         ["a.txt", "ignore.xyz", "c.txt"])
        # Le fichier ignoré ne doit tout de même pas s'indexer.
        self.assertEqual(self.reglages.rag.indexes,
                         [fichiers[0], fichiers[2]])


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
