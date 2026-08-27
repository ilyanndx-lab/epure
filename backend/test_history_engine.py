"""HistoryEngine comme magasin VIVANT : créer, ajouter, attacher, reconstruire.

Étape 2 du chantier ``docs/conversations-persistees.md``. Ce fichier couvre ce
que l'étape ajoute au moteur ; ``test_history_dir.py`` couvre ses chemins.

Trois choses y sont éprouvées plus durement que le reste, parce que ce sont
celles qui ont déjà mordu ce dépôt sous une autre forme :

1. **Un fichier illisible n'est jamais écrasé.** ``transaction(chemin, {})`` rend
   ``{}`` sur un JSON corrompu, et le corps du ``with`` écrirait alors une
   conversation neuve par-dessus — le contenu réel serait perdu au moment précis
   où on essaie de l'enrichir. C'est l'« effacement silencieux » de l'en-tête de
   ``core/jsonstore.py``, rejoué sur un autre fichier.

2. **Un fichier attaché mais désindexé reste visible, marqué absent.** Le filtrer
   rendrait la liste « propre » et la réponse du modèle inexplicable. Forme
   inverse du symptôme « indexé à zéro chunk, en silence » (CLAUDE.md §3.3 bis).

3. **Une conversation d'avant ce chantier se lit sans être touchée.** La
   normalisation est en mémoire ; le disque n'est écrit qu'à une vraie
   modification.

Usage :
    python test_history_engine.py
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

from core.history import HistoryEngine, croiser_fichiers  # noqa: E402


class _FausseCollection:
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
    """Évite de construire un vrai ``VectorStore``, qui exigerait le modèle
    d'embedding — absent pendant la suite par construction."""

    def __init__(self):
        self.collections: dict = {}

    def collection(self, nom):
        return self.collections.setdefault(nom, _FausseCollection())


class _FauxLLM:
    """Compte ses appels : plusieurs tests affirment qu'il n'y en a AUCUN."""

    def __init__(self):
        self.appels: list = []

    def generate(self, messages, model=None):
        self.appels.append((messages, model))
        return "Titre de test"


class _Base(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("EPURE_HISTORY_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-histeng-"))
        os.environ["EPURE_HISTORY_DIR"] = str(self.tmp)
        self.addCleanup(self._restaurer)
        self.llm = _FauxLLM()
        self.moteur = HistoryEngine(self.llm, _FauxStore())

    def _restaurer(self):
        if self._prev is None:
            os.environ.pop("EPURE_HISTORY_DIR", None)
        else:
            os.environ["EPURE_HISTORY_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brut(self, conv_id: str) -> dict:
        """Le fichier tel qu'il est SUR LE DISQUE, sans passer par le moteur."""
        return json.loads((self.tmp / f"{conv_id}.json").read_text(encoding="utf-8"))


class CreationTest(_Base):
    def test_cree_le_fichier_et_l_entree_d_index(self):
        conv = self.moteur.create_conversation()
        self.assertTrue((self.tmp / f"{conv['id']}.json").is_file())
        index = self.moteur.list_conversations(days=0)
        self.assertEqual([e["id"] for e in index], [conv["id"]])

    def test_ne_genere_aucun_titre_donc_aucun_appel_llm(self):
        """Une conversation vide n'a rien à résumer — et le titrage coûte un LLM.

        C'est aussi ce qui garantit qu'ouvrir une nouvelle conversation est
        instantané : le titre viendra après le premier tour (étape 4).
        """
        conv = self.moteur.create_conversation()
        self.assertEqual(conv["titre"], "")
        self.assertEqual(self.llm.appels, [])

    def test_les_cles_du_nouveau_modele_sont_toutes_la(self):
        conv = self.moteur.create_conversation()
        for cle in ("créée", "modifiée", "fichiers_attachés",
                    "résumé_contexte", "dernière_consolidation"):
            with self.subTest(cle=cle):
                self.assertIn(cle, self._brut(conv["id"]))


class AjoutTest(_Base):
    def test_ajoute_et_recalcule_le_compte(self):
        conv = self.moteur.create_conversation()
        self.moteur.append_messages(conv["id"], [
            {"role": "user", "content": "bonjour"},
            {"role": "assistant", "content": "salut"},
        ])
        brut = self._brut(conv["id"])
        self.assertEqual(len(brut["messages"]), 2)
        self.assertEqual(brut["n_messages"], 2)

    def test_le_compte_est_derive_jamais_cru_sur_parole(self):
        """``n_messages`` est une projection de ``messages``, pas une donnée.

        Deux sources pour un même fait divergent — la leçon de
        ``modules_state.json`` (CLAUDE.md §3.3). On pose donc un compte
        mensonger sur le disque et on vérifie qu'il est corrigé, pas propagé.
        """
        conv = self.moteur.create_conversation()
        chemin = self.tmp / f"{conv['id']}.json"
        doc = json.loads(chemin.read_text(encoding="utf-8"))
        doc["messages"] = [{"role": "user", "content": "un"}]
        doc["n_messages"] = 99
        chemin.write_text(json.dumps(doc), encoding="utf-8")

        self.assertEqual(self.moteur.get_conversation(conv["id"])["n_messages"], 1)

    def test_l_index_remonte_la_conversation_touchee(self):
        """L'ordre attendu d'une liste de conversations : activité la plus récente."""
        a = self.moteur.create_conversation()
        b = self.moteur.create_conversation()
        self.assertEqual([e["id"] for e in self.moteur.list_conversations(0)],
                         [b["id"], a["id"]])

        self.moteur.append_messages(a["id"], [{"role": "user", "content": "x"}])
        self.assertEqual([e["id"] for e in self.moteur.list_conversations(0)],
                         [a["id"], b["id"]])

    def test_conversation_absente_rend_none_sans_rien_creer(self):
        self.assertIsNone(self.moteur.append_messages(
            "11111111-2222-3333-4444-555555555555",
            [{"role": "user", "content": "x"}],
        ))
        restants = [p.name for p in self.tmp.glob("*.json")
                    if p.name != "conversations.json"]
        self.assertEqual(restants, [], "un fichier a été créé pour une conversation absente")

    def test_le_modele_vide_n_ecrase_pas_l_existant(self):
        conv = self.moteur.create_conversation(model="qwen2.5:7b")
        self.moteur.append_messages(conv["id"], [{"role": "user", "content": "x"}])
        self.assertEqual(self._brut(conv["id"])["modèle"], "qwen2.5:7b")


class MetadonneesParMessageTest(_Base):
    """`horodatage` sur tout message, `modèle` sur les réponses SEULEMENT.

    Un message tapé par l'utilisateur n'est produit par aucun modèle : lui coller
    le modèle actif dirait quelque chose de faux, et rendrait indistinguables
    deux situations que l'interface doit séparer — « pas de modèle par nature »
    et « antérieur à ce champ, on ne sait pas ».
    """

    def test_chaque_message_recoit_un_horodatage(self):
        conv = self.moteur.create_conversation()
        self.moteur.append_messages(conv["id"], [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "réponse"},
        ], model="qwen2.5:7b")
        for m in self._brut(conv["id"])["messages"]:
            with self.subTest(role=m["role"]):
                self.assertRegex(m["horodatage"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_seule_la_reponse_porte_un_modele(self):
        conv = self.moteur.create_conversation()
        self.moteur.append_messages(conv["id"], [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "réponse"},
        ], model="qwen2.5:7b")
        utilisateur, assistant = self._brut(conv["id"])["messages"]
        self.assertNotIn("modèle", utilisateur,
                         "un message tapé n'est produit par aucun modèle")
        self.assertEqual(assistant["modèle"], "qwen2.5:7b")

    def test_le_modele_par_message_survit_a_un_changement_de_modele(self):
        """LE point : la conversation ne porte que le DERNIER modèle utilisé.

        C'est pour ça qu'on ne peut pas s'en servir pour combler un message : il
        a pu changer plusieurs fois, et l'utiliser attribuerait des réponses à un
        modèle qui ne les a pas produites.
        """
        conv = self.moteur.create_conversation()
        self.moteur.append_messages(conv["id"], [
            {"role": "assistant", "content": "première"}], model="qwen2.5:7b")
        self.moteur.append_messages(conv["id"], [
            {"role": "assistant", "content": "seconde"}], model="gemini-2.0-flash")

        brut = self._brut(conv["id"])
        self.assertEqual([m["modèle"] for m in brut["messages"]],
                         ["qwen2.5:7b", "gemini-2.0-flash"])
        self.assertEqual(brut["modèle"], "gemini-2.0-flash",
                         "la conversation garde le dernier — d'où l'impossibilité d'en déduire les autres")

    def test_sans_modele_connu_le_champ_reste_absent(self):
        """Absent, et surtout pas une chaîne vide : l'interface distingue les deux."""
        conv = self.moteur.create_conversation()
        self.moteur.append_messages(conv["id"], [{"role": "assistant", "content": "x"}])
        self.assertNotIn("modèle", self._brut(conv["id"])["messages"][0])


class RetrocompatibiliteMetadonneesTest(_Base):
    """Les messages d'AVANT ces champs ne sont ni complétés ni devinés.

    Demande explicite : ne rien rétro-remplir depuis le `modèle` de la
    conversation. L'absence est une information ; l'inventer serait une erreur
    silencieuse, la pire espèce — une réponse attribuée à un modèle qui ne l'a
    pas produite, sans que rien ne le signale.
    """

    _ANCIENNE = {
        "id": "3a2f3207-ee38-4099-986e-49d6404f203d",
        "date": "2026-06-14",
        "titre": "Avant les métadonnées",
        "modèle": "gemini-2.0-flash",   # changé depuis : ne doit RIEN combler
        "modules": ["chat"],
        "n_messages": 2,
        "messages": [
            {"role": "user", "content": "vieille question"},
            {"role": "assistant", "content": "vieille réponse"},
        ],
    }

    def _poser(self):
        (self.tmp / f"{self._ANCIENNE['id']}.json").write_text(
            json.dumps(self._ANCIENNE, ensure_ascii=False), encoding="utf-8")

    def test_la_lecture_n_invente_ni_heure_ni_modele(self):
        self._poser()
        conv = self.moteur.get_conversation(self._ANCIENNE["id"])
        for m in conv["messages"]:
            with self.subTest(role=m["role"]):
                self.assertNotIn("horodatage", m)
                self.assertNotIn("modèle", m)

    def test_le_modele_de_la_conversation_ne_deborde_pas_sur_les_messages(self):
        """Le piège nommé dans la demande, éprouvé directement."""
        self._poser()
        conv = self.moteur.get_conversation(self._ANCIENNE["id"])
        self.assertEqual(conv["modèle"], "gemini-2.0-flash")
        for m in conv["messages"]:
            self.assertIsNone(m.get("modèle"))

    def test_un_nouveau_tour_n_affecte_pas_les_anciens_messages(self):
        """Aucune migration : les anciens restent nus, les nouveaux sont complets."""
        self._poser()
        self.moteur.append_messages(self._ANCIENNE["id"], [
            {"role": "assistant", "content": "réponse récente"}], model="qwen2.5:7b")

        messages = self._brut(self._ANCIENNE["id"])["messages"]
        self.assertNotIn("horodatage", messages[0])
        self.assertNotIn("horodatage", messages[1])
        self.assertIn("horodatage", messages[2])
        self.assertEqual(messages[2]["modèle"], "qwen2.5:7b")

    def test_la_reprise_ne_date_pas_d_aujourd_hui_des_messages_d_hier(self):
        """`create_conversation(messages=…)` reprend ce qui est là, n'invente rien.

        Leur donner l'instant présent daterait de ce soir une conversation
        d'avant-hier.
        """
        conv = self.moteur.create_conversation(messages=[
            {"role": "user", "content": "repris"},
            {"role": "assistant", "content": "repris aussi"},
        ])
        for m in self._brut(conv["id"])["messages"]:
            with self.subTest(role=m["role"]):
                self.assertNotIn("horodatage", m)
                self.assertNotIn("modèle", m)

    def test_la_reprise_conserve_des_metadonnees_deja_presentes(self):
        conv = self.moteur.create_conversation(messages=[
            {"role": "assistant", "content": "x",
             "horodatage": "2026-06-14T20:14:00", "modèle": "qwen2.5:7b"},
        ])
        m = self._brut(conv["id"])["messages"][0]
        self.assertEqual(m["horodatage"], "2026-06-14T20:14:00")
        self.assertEqual(m["modèle"], "qwen2.5:7b")


class FichierCorrompuTest(_Base):
    """Le danger central de l'étape : ne JAMAIS écrire par-dessus l'illisible."""

    def _corrompre(self) -> str:
        conv = self.moteur.create_conversation()
        (self.tmp / f"{conv['id']}.json").write_text("{ceci n'est pas du JSON",
                                                     encoding="utf-8")
        return conv["id"]

    def test_ajouter_sur_un_fichier_corrompu_ne_l_ecrase_pas(self):
        conv_id = self._corrompre()
        avant = (self.tmp / f"{conv_id}.json").read_text(encoding="utf-8")

        self.assertIsNone(self.moteur.append_messages(
            conv_id, [{"role": "user", "content": "x"}]))

        apres = (self.tmp / f"{conv_id}.json").read_text(encoding="utf-8")
        self.assertEqual(avant, apres, "le fichier illisible a été remplacé")

    def test_renommer_un_fichier_corrompu_echoue_proprement(self):
        conv_id = self._corrompre()
        avant = (self.tmp / f"{conv_id}.json").read_text(encoding="utf-8")
        self.assertFalse(self.moteur.rename_conversation(conv_id, "Neuf"))
        self.assertEqual((self.tmp / f"{conv_id}.json").read_text(encoding="utf-8"), avant)

    def test_lire_un_fichier_corrompu_rend_none(self):
        """``None`` et non ``{}`` : un dict vide se propagerait comme une
        conversation valide et sans messages."""
        self.assertIsNone(self.moteur.get_conversation(self._corrompre()))


class LectureTolerantTest(_Base):
    """Une conversation d'avant le chantier se lit — sans être touchée."""

    _ANCIENNE = {
        "id": "028b2136-1ac6-4e47-bc0b-f0f6952e7423",
        "date": "2026-06-03",
        "titre": "Ancienne conversation",
        "modèle": "qwen2.5:7b",
        "modules": ["chat"],
        "n_messages": 2,
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "réponse"},
        ],
    }

    def _poser_ancienne(self) -> Path:
        chemin = self.tmp / f"{self._ANCIENNE['id']}.json"
        chemin.write_text(json.dumps(self._ANCIENNE, ensure_ascii=False), encoding="utf-8")
        return chemin

    def test_les_cles_absentes_sont_comblees_en_memoire(self):
        self._poser_ancienne()
        conv = self.moteur.get_conversation(self._ANCIENNE["id"])
        self.assertEqual(conv["fichiers_attachés"], [])
        self.assertEqual(conv["résumé_contexte"], "")
        self.assertEqual(conv["dernière_consolidation"], 0)
        self.assertEqual(conv["créée"], "2026-06-03T00:00:00",
                         "`créée` doit dériver de `date`, seule information d'époque")
        self.assertTrue(conv["modifiée"])

    def test_la_lecture_ne_touche_pas_le_disque(self):
        """Ce qui rend la migration sûre : relire n'est pas migrer."""
        chemin = self._poser_ancienne()
        avant = (chemin.stat().st_size, chemin.stat().st_mtime_ns,
                 chemin.read_text(encoding="utf-8"))

        self.moteur.get_conversation(self._ANCIENNE["id"])
        self.moteur.list_conversations(days=0)

        apres = (chemin.stat().st_size, chemin.stat().st_mtime_ns,
                 chemin.read_text(encoding="utf-8"))
        self.assertEqual(avant, apres, "une simple lecture a réécrit le fichier")

    def test_le_fichier_gagne_ses_cles_a_la_premiere_ecriture(self):
        self._poser_ancienne()
        self.moteur.append_messages(self._ANCIENNE["id"],
                                    [{"role": "user", "content": "suite"}])
        brut = self._brut(self._ANCIENNE["id"])
        self.assertIn("fichiers_attachés", brut)
        self.assertEqual(brut["n_messages"], 3)


class CroisementFichiersTest(unittest.TestCase):
    """``présent: bool`` plutôt qu'un filtrage silencieux.

    Classe de bug déjà payée par ce dépôt dans l'autre sens : un fichier accepté
    que le moteur ne sait pas lire s'indexe à zéro chunk, en silence. Ici, un
    fichier attaché puis désindexé cesserait de contribuer au contexte sans que
    rien ne l'explique — l'utilisateur verrait la réponse changer sans cause.
    """

    def test_marque_present_et_absent_sans_rien_retirer(self):
        croise = croiser_fichiers(["/a/present.pdf", "/a/parti.pdf"], ["/a/present.pdf"])
        self.assertEqual(len(croise), 2, "un fichier a été filtré en silence")
        self.assertEqual(croise[0], {"chemin": "/a/present.pdf", "présent": True})
        self.assertEqual(croise[1], {"chemin": "/a/parti.pdf", "présent": False})

    def test_l_ordre_de_l_utilisateur_est_conserve(self):
        chemins = ["/a/c.pdf", "/a/a.pdf", "/a/b.pdf"]
        self.assertEqual([f["chemin"] for f in croiser_fichiers(chemins, chemins)], chemins)

    def test_listes_vides(self):
        self.assertEqual(croiser_fichiers([], ["/a/x.pdf"]), [])
        self.assertEqual(croiser_fichiers(None, None), [])

    def test_corpus_vide_et_corpus_inconnu_ne_disent_pas_la_meme_chose(self):
        """Trois états, pas deux — et la nuance est la raison d'être du champ.

        ``[]`` : le corpus est vraiment vide, donc le fichier est vraiment
        absent → ``False``, une information.

        ``None`` : le corpus n'est pas interrogeable (paquet neuf, les 90 Mo du
        modèle d'embedding pas encore téléchargés, ``EmbeddingIndisponible``)
        → ``None``, une ignorance. Répondre ``False`` ici serait pire qu'un
        filtrage silencieux : l'interface annoncerait « plus indexé » à propos de
        fichiers parfaitement présents, et l'utilisateur les ré-importerait pour
        rien.
        """
        self.assertEqual(croiser_fichiers(["/a/x.pdf"], []),
                         [{"chemin": "/a/x.pdf", "présent": False}])
        self.assertEqual(croiser_fichiers(["/a/x.pdf"], None),
                         [{"chemin": "/a/x.pdf", "présent": None}])

    def test_les_separateurs_ne_font_pas_mentir_le_croisement(self):
        """``a/b`` et ``a\\b`` désignent le même fichier — sous Windows.

        Sans ``normpath``, un attachement stocké avec un séparateur et un index
        rendu avec l'autre déclarerait absent un fichier bel et bien indexé. Le
        symptôme serait un contexte qui rétrécit sans raison visible.

        Sous POSIX le backslash n'est pas un séparateur : les deux chemins sont
        alors réellement différents, et les déclarer identiques serait le bug.
        L'attente suit donc la plateforme — le piège rencontré à l'étape 1.
        """
        croise = croiser_fichiers(["C:/docs/x.pdf"], ["C:\\docs\\x.pdf"])
        self.assertEqual(croise[0]["présent"], os.name == "nt")

    def test_la_redondance_de_chemin_est_reduite(self):
        croise = croiser_fichiers(["/a/./sous/../x.pdf"], ["/a/x.pdf"])
        self.assertTrue(croise[0]["présent"])

    @unittest.skipUnless(os.name == "nt", "la casse n'est ignorée que sous Windows")
    def test_la_casse_est_ignoree_sous_windows(self):
        croise = croiser_fichiers(["C:/Docs/X.pdf"], ["c:/docs/x.pdf"])
        self.assertTrue(croise[0]["présent"])


class AttachementTest(_Base):
    def test_remplace_l_ensemble(self):
        conv = self.moteur.create_conversation(fichiers=["/a/x.pdf"])
        self.moteur.set_conversation_files(conv["id"], ["/a/y.pdf", "/a/z.pdf"])
        self.assertEqual(self._brut(conv["id"])["fichiers_attachés"],
                         ["/a/y.pdf", "/a/z.pdf"])

    def test_les_doublons_partent_l_ordre_reste(self):
        conv = self.moteur.create_conversation()
        rendu = self.moteur.set_conversation_files(
            conv["id"], ["/a/b.pdf", "/a/a.pdf", "/a/b.pdf"])
        self.assertEqual(rendu, ["/a/b.pdf", "/a/a.pdf"])

    def test_conversation_absente_rend_none(self):
        self.assertIsNone(self.moteur.set_conversation_files(
            "11111111-2222-3333-4444-555555555555", ["/a/x.pdf"]))

    def test_la_vue_marque_les_fichiers_absents(self):
        conv = self.moteur.create_conversation(fichiers=["/a/present.pdf", "/a/parti.pdf"])
        vue = self.moteur.conversation_view(conv["id"], ["/a/present.pdf"])
        self.assertEqual([f["présent"] for f in vue["fichiers_attachés"]], [True, False])

    def test_la_vue_ne_modifie_pas_la_forme_stockee(self):
        """``conversation_view`` est une VUE : le disque garde des chaînes."""
        conv = self.moteur.create_conversation(fichiers=["/a/x.pdf"])
        self.moteur.conversation_view(conv["id"], [])
        self.assertEqual(self._brut(conv["id"])["fichiers_attachés"], ["/a/x.pdf"])

    def test_la_vue_d_une_conversation_absente_rend_none(self):
        self.assertIsNone(self.moteur.conversation_view("11111111-2222-3333-4444-555555555555", []))


class SansPileEmbeddingTest(unittest.TestCase):
    """Le stockage JSON ne doit dépendre en RIEN du modèle d'embedding.

    Bug ANTÉRIEUR à ce chantier, mesuré avant correction : `HistoryEngine.__init__`
    appelait `store.collection("history")`, ce qui construit le `VectorStore`,
    donc `MoteurEmbedding`, qui lève `EmbeddingIndisponible` tant que les 90 Mo
    du modèle ne sont pas là. Résultat dans un paquet fraîchement installé —
    vérifié en exécutant l'app dans cette configuration :

        GET /history      -> 503
        GET /history/abc  -> 503

    Autrement dit le module Historique était mort chez tout destinataire n'ayant
    pas téléchargé le modèle, et les conversations en auraient hérité : plus
    moyen d'ouvrir le moindre fil de discussion.

    La collection est donc obtenue au PREMIER USAGE. Ce qui en dépend vraiment —
    `search_history`, `_indexer_vectoriel` — reste indisponible sans modèle, ce
    qui est normal : c'est de la recherche sémantique. Lister ses conversations
    n'en est pas.
    """

    class _StoreIndisponible:
        def __init__(self):
            self.appels = 0

        def collection(self, nom):
            self.appels += 1
            raise RuntimeError("EmbeddingIndisponible simulé")

    def setUp(self):
        self._prev = os.environ.get("EPURE_HISTORY_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-histsans-"))
        os.environ["EPURE_HISTORY_DIR"] = str(self.tmp)
        self.addCleanup(self._restaurer)
        self.store = self._StoreIndisponible()
        self.moteur = HistoryEngine(_FauxLLM(), self.store)

    def _restaurer(self):
        if self._prev is None:
            os.environ.pop("EPURE_HISTORY_DIR", None)
        else:
            os.environ["EPURE_HISTORY_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_construire_le_moteur_ne_touche_pas_le_vecteur(self):
        self.assertEqual(self.store.appels, 0,
                         "la collection a été demandée dès la construction")

    def test_le_cycle_json_complet_fonctionne_sans_modele(self):
        conv = self.moteur.create_conversation(titre="Sans modèle")
        self.assertIsNotNone(self.moteur.append_messages(
            conv["id"], [{"role": "user", "content": "bonjour"}]))
        self.assertTrue(self.moteur.rename_conversation(conv["id"], "Renommée"))
        self.assertIsNotNone(self.moteur.set_conversation_files(conv["id"], []))
        self.assertEqual(self.moteur.get_conversation(conv["id"])["titre"], "Renommée")
        self.assertEqual(len(self.moteur.list_conversations(0)), 1)
        self.assertEqual(self.store.appels, 0,
                         "une opération JSON a réclamé la collection vectorielle")

    def test_la_recherche_semantique_degrade_au_lieu_de_lever(self):
        """Elle, en revanche, a VRAIMENT besoin du modèle — liste vide, pas 500."""
        self.assertEqual(self.moteur.search_history("quoi que ce soit"), [])
        self.assertGreater(self.store.appels, 0)

    def test_supprimer_reste_possible_sans_modele(self):
        """Le fichier et l'index partent ; seul le nettoyage vectoriel est perdu."""
        conv = self.moteur.create_conversation()
        self.assertTrue(self.moteur.delete_conversation(conv["id"]))
        self.assertFalse((self.tmp / f"{conv['id']}.json").exists())
        self.assertEqual(self.moteur.list_conversations(0), [])


class ReconstructionIndexTest(_Base):
    """L'index est un CACHE — c'est ce qui autorise à ne pas lui donner fsync."""

    def test_reconstruit_depuis_les_fichiers(self):
        a = self.moteur.create_conversation(titre="A")
        b = self.moteur.create_conversation(titre="B")
        self.moteur._index_path.unlink()

        self.assertEqual(self.moteur.rebuild_index(), 2)
        ids = {e["id"] for e in self.moteur.list_conversations(0)}
        self.assertEqual(ids, {a["id"], b["id"]})

    def test_l_index_lui_meme_n_est_pas_pris_pour_une_conversation(self):
        """``conversations.json`` vit dans le même dossier que ses entrées."""
        self.moteur.create_conversation()
        self.assertEqual(self.moteur.rebuild_index(), 1)

    def test_un_fichier_illisible_est_ignore_pas_fatal(self):
        bon = self.moteur.create_conversation()
        (self.tmp / "casse.json").write_text("{pas du JSON", encoding="utf-8")

        self.assertEqual(self.moteur.rebuild_index(), 1)
        self.assertEqual([e["id"] for e in self.moteur.list_conversations(0)], [bon["id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
