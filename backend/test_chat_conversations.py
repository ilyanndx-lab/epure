"""Les routes `/chat/conversations*` — étape 3 du chantier conversations.

Ce que ce fichier verrouille au-delà du cas nominal, parce que c'est là que les
décisions de conception se jouent (docs/conversations-persistees.md §3) :

1. **Lire une conversation ne 503 JAMAIS.** `rag` est un `_LazyEngine` dont le
   premier accès peut lever `EmbeddingIndisponible`, traduite en 503 par un
   gestionnaire GLOBAL de `main.py`. Dans un paquet fraîchement installé — les
   90 Mo du modèle pas encore téléchargés — ouvrir une conversation répondrait
   donc 503 si la route ne rattrapait pas. Ce serait l'incident du §8 déplacé
   d'un cran, sur une fonction qui n'a rien à voir avec la recherche.

2. **`présent` a trois états.** `true` / `false` / `null`. Le dernier veut dire
   « on ne sait pas », pas « absent » : annoncer `false` faute de corpus ferait
   croire à une désindexation et pousserait à ré-importer pour rien.

3. **Attacher, en revanche, PEUT échouer en 503.** Asymétrie volontaire : lire
   est passif et doit toujours marcher ; attacher est une action explicite dont
   on doit expliquer l'échec.

4. **Le préfixe est écrit à la main** (`prefix: ""` dans le manifeste du chat).
   Une route qui l'oublierait entrerait en collision avec le cœur.

Usage :
    python test_chat_conversations.py
"""

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
from core import paths as core_paths  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.embedding_install import EmbeddingIndisponible  # noqa: E402
from core.runtime import history_engine  # noqa: E402


class _RagStub:
    """Corpus indexé maîtrisé — le vrai construirait `RAGEngine`.

    Sur le poste d'Ilyann le modèle d'embedding finit par être là, donc le vrai
    proxy réussirait et le test passerait sans rien éprouver ; en CI il lèverait.
    Un double rend le résultat identique des deux côtés.
    """

    def __init__(self, fichiers):
        self.fichiers = list(fichiers)

    def get_indexed_files(self):
        return list(self.fichiers)


class _RagIndisponible:
    """La configuration d'un paquet fraîchement installé : pile absente."""

    def get_indexed_files(self):
        raise EmbeddingIndisponible({"état": "absent", "detail": "modèle non téléchargé"})


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # base_url/client : sans eux TrustedHostMiddleware répond 400 partout
        # (cf. test_auth_surface._client).
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def setUp(self):
        self.auth = {"Authorization": f"Bearer {self.token}"}

        # Racine de données maîtrisée. On détourne `user_data_roots` plutôt que
        # d'écrire dans les vraies fiches de l'utilisateur : `EPURE_FICHES_DIR`
        # n'est PAS détourné par `_test_env`, donc un fichier déposé sous
        # `fiches_root()` atterrirait pour de bon chez lui. Le confinement
        # lui-même est éprouvé ailleurs (test_upload_paths, test_safe_path) ; ce
        # qui est testé ici, c'est l'usage qu'en fait la route.
        self.racine = Path(tempfile.mkdtemp(prefix="epure-conv-data-"))
        self._roots_originaux = core_paths.user_data_roots
        core_paths.user_data_roots = lambda: [self.racine.resolve()]
        self.addCleanup(setattr, core_paths, "user_data_roots", self._roots_originaux)
        self.addCleanup(shutil.rmtree, self.racine, True)

        self._rag_original = routeur_chat.rag
        self.addCleanup(setattr, routeur_chat, "rag", self._rag_original)

    def _fichier(self, nom: str) -> str:
        """Crée un fichier dans la racine de données et rend son chemin résolu."""
        p = self.racine / nom
        p.write_text("contenu", encoding="utf-8")
        return str(p.resolve())

    def _creer(self, **corps) -> dict:
        r = self.client.post("/chat/conversations", json=corps or {}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class AuthTest(_Base):
    def test_les_routes_exigent_le_token(self):
        """Le middleware n'exempte que /health et /pair — vérifié, pas supposé."""
        for methode, url in (
            ("get", "/chat/conversations"),
            ("post", "/chat/conversations"),
            ("get", "/chat/conversations/x"),
            ("delete", "/chat/conversations/x"),
        ):
            with self.subTest(route=f"{methode.upper()} {url}"):
                self.assertEqual(getattr(self.client, methode)(url).status_code, 401)


class CreationListeTest(_Base):
    def test_creer_puis_retrouver_dans_la_liste(self):
        conv = self._creer()
        self.assertTrue(conv["id"])
        r = self.client.get("/chat/conversations", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        corps = r.json()
        self.assertIn(conv["id"], [c["id"] for c in corps["conversations"]])
        self.assertGreaterEqual(corps["total"], 1)

    def test_la_liste_ne_porte_pas_les_messages(self):
        """~6,7 Ko par conversation : les inclure ferait grossir la liste avec
        l'historique entier, pour un panneau qui n'affiche que des titres."""
        self._creer()
        entrees = self.client.get("/chat/conversations", headers=self.auth).json()["conversations"]
        for e in entrees:
            self.assertNotIn("messages", e)

    def test_pagination(self):
        for _ in range(3):
            self._creer()
        corps = self.client.get("/chat/conversations?limit=2", headers=self.auth).json()
        self.assertEqual(len(corps["conversations"]), 2)
        self.assertGreaterEqual(corps["total"], 3, "`total` doit compter AVANT le découpage")

    def test_creer_avec_des_fichiers_indexes(self):
        f = self._fichier("cours.pdf")
        routeur_chat.rag = _RagStub([f])
        conv = self._creer(fichiers=[f])
        self.assertEqual(conv["fichiers_attachés"], [f])


class RepriseAncienChatTest(_Base):
    """`POST /chat/conversations` avec des messages — étape 7 du chantier.

    Un seul appelant : la reprise de ce qui était à l'écran au moment de la mise
    à jour. Les messages n'avaient jusque-là aucun fichier où vivre — ils étaient
    dans `localStorage['epure.chat.messages']`, que le chat ne lit plus.
    """

    def test_les_messages_fournis_sont_conserves(self):
        conv = self._creer(titre="Conversation reprise", messages=[
            {"role": "user", "content": "ma question"},
            {"role": "assistant", "content": "ma réponse"},
        ])
        stocke = history_engine.get_conversation(conv["id"])
        self.assertEqual(
            [(m["role"], m["content"]) for m in stocke["messages"]],
            [("user", "ma question"), ("assistant", "ma réponse")],
        )
        self.assertEqual(stocke["n_messages"], 2)
        self.assertEqual(stocke["titre"], "Conversation reprise")

    def test_un_role_inconnu_devient_user(self):
        """Ces messages repartent tels quels dans le prompt du tour suivant : un
        rôle inventé ferait échouer l'appel au modèle."""
        conv = self._creer(messages=[{"role": "systeme", "content": "x"}])
        stocke = history_engine.get_conversation(conv["id"])
        self.assertEqual(stocke["messages"], [{"role": "user", "content": "x"}])

    def test_les_entrees_inexploitables_sont_ecartees_pas_fatales(self):
        """Filtrer plutôt que refuser : perdre la conversation entière pour un
        message malformé serait pire que d'en perdre un."""
        conv = self._creer(messages=[
            {"role": "user", "content": "bon"},
            {"role": "user"},                      # pas de contenu
            {"role": "user", "content": ""},       # contenu vide
            {"role": "user", "content": 42},       # contenu non textuel
            "pas un objet",
        ])
        stocke = history_engine.get_conversation(conv["id"])
        self.assertEqual(stocke["messages"], [{"role": "user", "content": "bon"}])

    def test_sans_messages_la_conversation_naît_vide(self):
        """Le cas normal : les conversations se remplissent tour par tour."""
        conv = self._creer()
        self.assertEqual(conv["messages"], [])
        self.assertEqual(conv["n_messages"], 0)


class LectureTest(_Base):
    def test_conversation_inconnue_rend_404(self):
        r = self.client.get("/chat/conversations/11111111-2222-3333-4444-555555555555",
                            headers=self.auth)
        self.assertEqual(r.status_code, 404)

    def test_les_fichiers_sont_marques_presents(self):
        f = self._fichier("present.pdf")
        routeur_chat.rag = _RagStub([f])
        conv = self._creer(fichiers=[f])

        corps = self.client.get(f"/chat/conversations/{conv['id']}", headers=self.auth).json()
        self.assertEqual(corps["fichiers_attachés"], [{"chemin": f, "présent": True}])
        self.assertTrue(corps["corpus_interrogeable"])

    def test_un_fichier_desindexe_reste_visible_marque_absent(self):
        """Le point de vigilance du chantier : pas de filtrage silencieux.

        Le filtrer rendrait la liste « propre » et la réponse du modèle
        inexplicable — l'utilisateur verrait son contexte rétrécir sans cause.
        """
        f = self._fichier("parti.pdf")
        routeur_chat.rag = _RagStub([f])
        conv = self._creer(fichiers=[f])

        routeur_chat.rag = _RagStub([])  # le fichier a été désindexé entre-temps
        corps = self.client.get(f"/chat/conversations/{conv['id']}", headers=self.auth).json()
        self.assertEqual(corps["fichiers_attachés"], [{"chemin": f, "présent": False}])
        self.assertTrue(corps["corpus_interrogeable"],
                        "un corpus VIDE reste un corpus interrogeable")

    def test_lire_ne_503_jamais_meme_sans_pile_d_embedding(self):
        """LA décision de l'étape 3, à l'envers.

        Sans le rattrapage de `_corpus_ou_inconnu`, le gestionnaire global de
        `main.py` traduirait `EmbeddingIndisponible` en 503 et l'utilisateur ne
        pourrait plus ouvrir ses conversations tant que les 90 Mo du modèle ne
        sont pas téléchargés — c'est-à-dire dans tout paquet fraîchement
        installé.
        """
        f = self._fichier("inconnu.pdf")
        routeur_chat.rag = _RagStub([f])
        conv = self._creer(fichiers=[f])

        routeur_chat.rag = _RagIndisponible()
        r = self.client.get(f"/chat/conversations/{conv['id']}", headers=self.auth)

        self.assertEqual(r.status_code, 200, r.text)
        corps = r.json()
        self.assertFalse(corps["corpus_interrogeable"])
        self.assertIsNone(corps["fichiers_attachés"][0]["présent"],
                          "sans corpus, `présent` doit valoir « inconnu », pas « absent »")
        self.assertEqual(corps["fichiers_attachés"][0]["chemin"], f)


class RenommageSuppressionTest(_Base):
    def test_renommer(self):
        conv = self._creer()
        r = self.client.patch(f"/chat/conversations/{conv['id']}",
                              json={"titre": "  Thermodynamique  "}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["titre"], "Thermodynamique")

    def test_titre_vide_refuse(self):
        conv = self._creer()
        r = self.client.patch(f"/chat/conversations/{conv['id']}",
                              json={"titre": "   "}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_renommer_une_inconnue_rend_404(self):
        r = self.client.patch("/chat/conversations/11111111-2222-3333-4444-555555555555",
                              json={"titre": "x"}, headers=self.auth)
        self.assertEqual(r.status_code, 404)

    def test_supprimer_puis_re_supprimer_rend_404(self):
        """`delete_conversation` est idempotent et rend toujours True ; un
        `{"ok": true}` sur un identifiant inconnu ferait passer un bug d'id pour
        un succès."""
        conv = self._creer()
        self.assertEqual(
            self.client.delete(f"/chat/conversations/{conv['id']}", headers=self.auth).status_code,
            200)
        self.assertEqual(
            self.client.delete(f"/chat/conversations/{conv['id']}", headers=self.auth).status_code,
            404)


class AttachementTest(_Base):
    def test_remplacer_l_ensemble(self):
        a, b = self._fichier("a.pdf"), self._fichier("b.pdf")
        routeur_chat.rag = _RagStub([a, b])
        conv = self._creer(fichiers=[a])

        r = self.client.put(f"/chat/conversations/{conv['id']}/fichiers",
                            json={"paths": [b]}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([f["chemin"] for f in r.json()["fichiers_attachés"]], [b])

    def test_un_chemin_hors_des_dossiers_de_donnees_rend_403(self):
        """403 et non 400 : c'est une tentative de sortie, pas une erreur de
        saisie. Cible dépendante de la plateforme — la leçon de l'étape 1."""
        dehors = r"C:\Windows\System32\drivers\etc\hosts" if os.name == "nt" else "/etc/passwd"
        routeur_chat.rag = _RagStub([dehors])
        conv = self._creer()

        r = self.client.put(f"/chat/conversations/{conv['id']}/fichiers",
                            json={"paths": [dehors]}, headers=self.auth)
        self.assertEqual(r.status_code, 403, r.text)

    def test_un_fichier_non_indexe_rend_400(self):
        """Attacher un fichier que le moteur n'a pas indexé produirait une
        conversation dont le contexte ne contient rien, sans que rien ne le dise
        — le symptôme « indexé à zéro chunk, en silence » (§3.3 bis)."""
        f = self._fichier("jamais-indexe.pdf")
        routeur_chat.rag = _RagStub([])
        conv = self._creer()

        r = self.client.put(f"/chat/conversations/{conv['id']}/fichiers",
                            json={"paths": [f]}, headers=self.auth)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("jamais-indexe.pdf", r.json()["detail"])

    def test_attacher_a_une_conversation_inconnue_rend_404(self):
        f = self._fichier("a.pdf")
        routeur_chat.rag = _RagStub([f])
        r = self.client.put(
            "/chat/conversations/11111111-2222-3333-4444-555555555555/fichiers",
            json={"paths": [f]}, headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)

    def test_attacher_sans_pile_d_embedding_rend_503(self):
        """L'asymétrie assumée : lire ne 503 jamais, attacher peut.

        Attacher est une action explicite de l'utilisateur ; lui répondre en
        silence, ou pire accepter un attachement invérifiable, serait pire qu'un
        503 porteur de l'état d'installation.
        """
        conv = self._creer()
        routeur_chat.rag = _RagIndisponible()
        r = self.client.put(f"/chat/conversations/{conv['id']}/fichiers",
                            json={"paths": [self._fichier("x.pdf")]}, headers=self.auth)
        self.assertEqual(r.status_code, 503, r.text)


class HistoriqueSansModeleTest(_Base):
    """`GET /history` répondait 503 sans le modèle d'embedding — mesuré.

    Ce test ne porte pas sur une route du chantier mais sur celle du module
    Historique, et c'est délibéré : le bug était le même et sa correction est
    commune (la collection vectorielle de `HistoryEngine` est obtenue au premier
    usage, plus à la construction). Le vérifier ici, sur la surface HTTP, est ce
    qui empêche la panne de revenir en silence — c'est sous cette forme qu'elle
    se voyait, pas au niveau du moteur.

    La suite tourne précisément dans la configuration qui la déclenchait :
    `EPURE_EMBEDDING_DIR` est un temporaire VIDE et `EPURE_EMBEDDING_AUTOINSTALL=0`.
    """

    def test_lister_l_historique_ne_503_pas(self):
        r = self.client.get("/history", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsInstance(r.json(), list)

    def test_une_conversation_inconnue_rend_404_et_non_503(self):
        r = self.client.get("/history/11111111-2222-3333-4444-555555555555",
                            headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)


class PrefixeTest(_Base):
    def test_les_routes_sont_bien_sous_chat(self):
        """Le module chat est monté avec `prefix: ""` : sans le `/chat/` écrit à
        la main, la route entrerait en collision avec le cœur (§3.3)."""
        chemins = {r.path for r in main.app.routes if hasattr(r, "path")}
        for attendu in ("/chat/conversations", "/chat/conversations/{conv_id}",
                        "/chat/conversations/{conv_id}/fichiers"):
            with self.subTest(route=attendu):
                self.assertIn(attendu, chemins)

    def test_aucune_route_de_conversation_a_la_racine(self):
        chemins = {r.path for r in main.app.routes if hasattr(r, "path")}
        self.assertNotIn("/conversations", chemins)


if __name__ == "__main__":
    unittest.main(verbosity=2)
