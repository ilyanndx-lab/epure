#!/usr/bin/env python3
"""Chantier « préfixes/skills personnalisés », phase A (fondations backend
seules — cf. le rapport de cette itération, aucun frontend touché).

Cinq couches, dans l'ordre où le rapport les décrit :

* `core.memory.normaliser_prefixes` — la forme acceptée : fusion clé par clé
  sur le défaut pour `integres` (même philosophie que
  `normaliser_tool_calling`, cf. `test_tool_calling_reglages.py`), validation
  SILENCIEUSE des objets de `personnalises` ;
* la PERSISTANCE au redémarrage (`MemoryEngine.__init__`) — `prefixes` a
  rejoint `_CLES_PERSISTANTES` pour la même raison que `tool_calling` :
  un déclencheur renommé/désactivé ne doit pas se réinitialiser en silence ;
* `@historique` RENOMMABLE/DÉSACTIVABLE — comportement réel STRICTEMENT
  inchangé (`modules/chat/router.py`), seul le littéral testé/retiré devient
  une donnée de configuration ;
* l'injection générique des préfixes personnalisés MANUELS — retrait du
  trigger du texte envoyé au modèle (et persisté), bloc
  `[INSTRUCTION PERSONNALISÉE — {nom}]` dans le prompt système, PLUSIEURS
  préfixes pouvant se déclencher sur le même message ;
* les skills personnalisés AGENTIQUES (`core.llm.construire_skills_personnalises`)
  — schéma exposé au modèle, exécuteur qui ne fait que RÉVÉLER l'instruction
  (jamais une vraie recherche, jamais citable), et coupés par l'interrupteur
  général de tool-calling au même titre que les 3 skills natifs.

Une dernière classe (`NonRegressionDefautTest`) fige le comportement d'un
utilisateur n'ayant JAMAIS ouvert le nouvel onglet : c'est la baseline de
non-régression de toute cette phase.

Usage :
    python test_prefixes_personnalises.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — isole les dossiers AVANT tout import de core.*/main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.jsonstore import write_json  # noqa: E402
from core.llm import construire_skills_personnalises  # noqa: E402
from core.memory import _CONTEXT_DEFAULT, MemoryEngine, normaliser_prefixes  # noqa: E402
from core.runtime import history_engine, memory  # noqa: E402

from test_tool_calling_ollama import (  # noqa: E402
    _Rejoueur, _appels_outil, _chunk, _stream, _tool_call,
)


def _obj_perso(id="p1", nom="Mon skill", trigger="@monskill", description="Fait un truc.",
                instruction="Instruction à révéler.", prefixe_actif=True, agentique=False,
                budget=4):
    return {
        "id": id, "nom": nom, "trigger": trigger, "description": description,
        "instruction": instruction, "prefixe_actif": prefixe_actif,
        "agentique": agentique, "budget": budget,
    }


# ── Normalisation ────────────────────────────────────────────────────────────

class NormaliserPrefixesTest(unittest.TestCase):
    def test_none_rend_le_defaut_complet(self):
        self.assertEqual(normaliser_prefixes(None), _CONTEXT_DEFAULT["prefixes"])

    def test_valeur_du_mauvais_type_rend_le_defaut(self):
        for mauvaise in ("oops", ["liste"], 42, True):
            with self.subTest(mauvaise=mauvaise):
                self.assertEqual(normaliser_prefixes(mauvaise), _CONTEXT_DEFAULT["prefixes"])

    def test_integre_renomme_est_conserve(self):
        resultat = normaliser_prefixes({"integres": {"historique": {"trigger": "@archives"}}})
        self.assertEqual(resultat["integres"]["historique"]["trigger"], "@archives")
        self.assertTrue(resultat["integres"]["historique"]["enabled"])
        self.assertEqual(resultat["integres"]["cours"]["trigger"], "@cours")

    def test_integre_desactive_reste_desactive(self):
        resultat = normaliser_prefixes({"integres": {"web": {"enabled": False}}})
        self.assertFalse(resultat["integres"]["web"]["enabled"])
        self.assertTrue(resultat["integres"]["strict"]["enabled"])

    def test_integre_trigger_vide_retombe_sur_le_defaut(self):
        resultat = normaliser_prefixes({"integres": {"cours": {"trigger": "   "}}})
        self.assertEqual(resultat["integres"]["cours"]["trigger"], "@cours")

    def test_integre_manquant_sur_disque_reapparait_au_defaut(self):
        """Simule un `context_session.json` écrit avant l'ajout d'une 6e
        commande intégrée un jour : la clé absente doit réapparaître,
        activée, plutôt que de rester manquante pour toujours."""
        resultat = normaliser_prefixes({"integres": {"web": {"enabled": False}}})
        self.assertIn("image", resultat["integres"])
        self.assertTrue(resultat["integres"]["image"]["enabled"])

    def test_rendu_ne_partage_jamais_un_sous_dict_avec_le_defaut(self):
        """`MemoryEngine.__init__` fait un `dict(_CONTEXT_DEFAULT)` SHALLOW :
        muter un sous-dict rendu ne doit jamais atteindre `_CONTEXT_DEFAULT`."""
        resultat = normaliser_prefixes(None)
        resultat["integres"]["cours"]["trigger"] = "MUTÉ"
        self.assertEqual(_CONTEXT_DEFAULT["prefixes"]["integres"]["cours"]["trigger"], "@cours")

    def test_personnalise_bien_forme_est_conserve(self):
        resultat = normaliser_prefixes({"personnalises": [_obj_perso()]})
        self.assertEqual(len(resultat["personnalises"]), 1)
        self.assertEqual(resultat["personnalises"][0]["nom"], "Mon skill")

    def test_personnalise_sans_id_est_ecarte(self):
        obj = _obj_perso()
        del obj["id"]
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"], [])

    def test_personnalise_sans_nom_est_ecarte(self):
        obj = _obj_perso(nom="")
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"], [])

    def test_personnalise_instruction_vide_est_ecarte(self):
        obj = _obj_perso(instruction="  ")
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"], [])

    def test_personnalise_ni_manuel_ni_agentique_est_ecarte(self):
        obj = _obj_perso(prefixe_actif=False, agentique=False)
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"], [])

    def test_personnalise_manuel_sans_trigger_est_ecarte(self):
        obj = _obj_perso(prefixe_actif=True, trigger="", agentique=False)
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"], [])

    def test_personnalise_purement_agentique_sans_trigger_est_conserve(self):
        """Pas besoin d'un trigger pour un skill purement agentique — c'est
        le cas explicitement autorisé par la spec de cette phase."""
        obj = _obj_perso(prefixe_actif=False, trigger=None, agentique=True)
        resultat = normaliser_prefixes({"personnalises": [obj]})
        self.assertEqual(len(resultat["personnalises"]), 1)
        self.assertIsNone(resultat["personnalises"][0]["trigger"])

    def test_personnalise_agentique_sans_description_est_ecarte(self):
        """Une description vide donnerait un schéma d'outil sans description
        exposé au modèle."""
        obj = _obj_perso(prefixe_actif=False, trigger=None, agentique=True, description="  ")
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"], [])

    def test_personnalise_manuel_sans_description_est_tolere(self):
        """`description` ne sert qu'à documenter/afficher un préfixe purement
        manuel — jamais injectée dans le prompt — donc pas de raison de
        l'exiger hors du cas agentique."""
        obj = _obj_perso(agentique=False, description="")
        resultat = normaliser_prefixes({"personnalises": [obj]})
        self.assertEqual(len(resultat["personnalises"]), 1)

    def test_budget_trop_haut_est_clampe_a_dix(self):
        obj = _obj_perso(agentique=True, budget=999)
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"][0]["budget"], 10)

    def test_budget_trop_bas_est_clampe_a_un(self):
        obj = _obj_perso(agentique=True, budget=0)
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"][0]["budget"], 1)

    def test_budget_non_numerique_retombe_sur_le_defaut(self):
        obj = _obj_perso(agentique=True, budget="beaucoup")
        self.assertEqual(normaliser_prefixes({"personnalises": [obj]})["personnalises"][0]["budget"], 4)

    def test_element_non_dict_est_ignore(self):
        self.assertEqual(normaliser_prefixes({"personnalises": ["oops", 42, None]})["personnalises"], [])

    def test_deux_personnalises_bien_formes_sont_tous_les_deux_conserves(self):
        resultat = normaliser_prefixes({"personnalises": [
            _obj_perso(id="p1", nom="Un"), _obj_perso(id="p2", nom="Deux", trigger="@deux"),
        ]})
        self.assertEqual([o["nom"] for o in resultat["personnalises"]], ["Un", "Deux"])


# ── Persistance au redémarrage ───────────────────────────────────────────────

class _DossierNeuf(unittest.TestCase):
    """Pose EPURE_DATA_DIR sur un temporaire, APRÈS les imports ci-dessus —
    même fixture que `test_tool_calling_reglages.py`, non partagée entre
    fichiers (convention du dépôt : chaque `test_*.py` est autonome)."""

    def setUp(self):
        self._prev = os.environ.get("EPURE_DATA_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-prefixes-"))
        os.environ["EPURE_DATA_DIR"] = str(self.tmp)
        self.addCleanup(self._restaurer)

    def _restaurer(self):
        if self._prev is None:
            os.environ.pop("EPURE_DATA_DIR", None)
        else:
            os.environ["EPURE_DATA_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redemarrer(self) -> MemoryEngine:
        return MemoryEngine()


class PersistanceRedemarrageTest(_DossierNeuf):
    def test_integre_renomme_et_desactive_survit_a_un_redemarrage(self):
        moteur = self._redemarrer()
        moteur.update_context(prefixes={
            "integres": {"historique": {"trigger": "@archives", "enabled": False}},
            "personnalises": [],
        })
        contexte = self._redemarrer().get_context()
        self.assertEqual(contexte["prefixes"]["integres"]["historique"]["trigger"], "@archives")
        self.assertFalse(contexte["prefixes"]["integres"]["historique"]["enabled"])
        # Les autres intégrés ne doivent pas avoir disparu — fusion, pas remplacement.
        self.assertTrue(contexte["prefixes"]["integres"]["cours"]["enabled"])

    def test_personnalise_survit_a_un_redemarrage(self):
        moteur = self._redemarrer()
        moteur.update_context(prefixes={"integres": {}, "personnalises": [_obj_perso()]})
        contexte = self._redemarrer().get_context()
        self.assertEqual(len(contexte["prefixes"]["personnalises"]), 1)
        self.assertEqual(contexte["prefixes"]["personnalises"][0]["nom"], "Mon skill")

    def test_survit_a_plusieurs_redemarrages(self):
        self._redemarrer().update_context(prefixes={
            "integres": {"cours": {"enabled": False}}, "personnalises": [],
        })
        for _ in range(3):
            moteur = self._redemarrer()
        self.assertFalse(moteur.get_context()["prefixes"]["integres"]["cours"]["enabled"])

    def test_sans_prefixes_prealable_rend_le_defaut(self):
        self.assertEqual(self._redemarrer().get_context()["prefixes"], _CONTEXT_DEFAULT["prefixes"])


class DemarrageRobusteTest(_DossierNeuf):
    def test_valeur_du_mauvais_type_sur_disque_ne_bloque_pas_le_demarrage(self):
        write_json(self.tmp / "context_session.json", {"prefixes": "n'importe quoi"})
        contexte = self._redemarrer().get_context()
        self.assertEqual(contexte["prefixes"], _CONTEXT_DEFAULT["prefixes"])

    def test_fichier_illisible_ne_bloque_pas_le_demarrage(self):
        (self.tmp / "context_session.json").write_text("{pas du JSON", encoding="utf-8")
        contexte = self._redemarrer().get_context()
        self.assertEqual(contexte["prefixes"], _CONTEXT_DEFAULT["prefixes"])


# ── Skills personnalisés agentiques — core.llm ──────────────────────────────

class SkillPersonnaliseAgentiqueTest(unittest.TestCase):
    def test_expose_le_bon_schema_et_revele_l_instruction_sans_alimenter_web_resultats(self):
        obj = _obj_perso(
            id="p1", nom="Ambiance sombre", trigger=None, prefixe_actif=False,
            agentique=True, description="Bascule le ton en sombre et mystérieux.",
            instruction="À partir de maintenant, adopte un ton sombre et mystérieux.",
            budget=3,
        )
        skills_dynamiques = construire_skills_personnalises([obj])
        self.assertIn("ambiance_sombre", skills_dynamiques)
        entree = skills_dynamiques["ambiance_sombre"]
        schema = entree["schema"]
        self.assertEqual(schema["function"]["name"], "ambiance_sombre")
        self.assertEqual(schema["function"]["description"], obj["description"])
        self.assertEqual(schema["function"]["parameters"], {"type": "object", "properties": {}})
        self.assertFalse(entree["citable"])
        self.assertEqual(entree["budget_max"], 3)

        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="ambiance_sombre")], done=True)],
            [_chunk(content="D'accord.", done=True)],
        ]) as r:
            sortie, _ = _stream(r, outils=["ambiance_sombre"], skills_dynamiques=skills_dynamiques)

        self.assertIn("tools", r.appels[0])
        self.assertEqual(r.appels[0]["tools"], [schema])
        appels_outil = _appels_outil(sortie)
        self.assertEqual(len(appels_outil), 1)
        self.assertEqual(appels_outil[0]["outil"], "ambiance_sombre")
        self.assertEqual(appels_outil[0]["resultats"], [], "un skill personnalisé ne rend jamais de ResultatWeb")
        message_outil = r.appels[1]["messages"][-1]
        self.assertEqual(message_outil["tool_name"], "ambiance_sombre")
        self.assertEqual(message_outil["content"], obj["instruction"])

    def test_nom_assaini_ne_peut_pas_masquer_un_skill_natif(self):
        obj = _obj_perso(id="p1", nom="Web Search", agentique=True,
                          description="Faux.", instruction="Faux.")
        self.assertNotIn("web_search", construire_skills_personnalises([obj]))

    def test_nom_vide_apres_assainissement_est_ecarte(self):
        obj = _obj_perso(id="p1", nom="🎉🎉🎉", agentique=True,
                          description="Faux.", instruction="Faux.")
        self.assertEqual(construire_skills_personnalises([obj]), {})

    def test_collision_entre_deux_personnalises_priorite_au_premier(self):
        obj1 = _obj_perso(id="p1", nom="Mode Sombre", agentique=True,
                           description="Premier.", instruction="Premier.")
        obj2 = _obj_perso(id="p2", nom="mode sombre", agentique=True,
                           description="Second.", instruction="Second.")
        registre = construire_skills_personnalises([obj1, obj2])
        self.assertEqual(len(registre), 1)
        self.assertEqual(registre["mode_sombre"]["schema"]["function"]["description"], "Premier.")


# ── Point d'appel réel : modules/chat/router.py ──────────────────────────────

_WS = "ws://localhost/ws/chat?token={t}"


class _ClientChatWS(unittest.TestCase):
    """Fixture partagée par les classes de tests ci-dessous : client
    WebSocket, interception de `llm.stream`, restauration du contexte.

    Chaque sous-classe DOIT redéfinir `_PORT` avec une valeur distincte —
    `cls._PORT += 1` créerait un attribut sur la sous-classe sans jamais
    muter celui de la classe de base, donc toutes les sous-classes
    retomberaient sur le même port (déjà vu ailleurs dans ce dépôt, cf.
    `test_tool_calling_reglages.py`, qui code chaque port en dur pour la
    même raison)."""

    _PORT = 54340

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", cls._PORT))
        cls.token = get_api_token()

    def setUp(self):
        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)
        self._search_original = history_engine.search_history
        self.addCleanup(setattr, history_engine, "search_history", self._search_original)
        self._contexte_original = memory.get_context()
        self.addCleanup(memory.update_context, **self._contexte_original)

    def _capturer(self):
        appels: list[dict] = []

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            appels.append({"messages": messages, **kw})
            yield "réponse"
        routeur_chat.llm.stream = faux_stream
        return appels

    def _envoyer(self, ws, texte, conversation_id):
        ws.send_text(json.dumps({
            "role": "user", "content": texte, "direct": True,
            "conversation_id": conversation_id,
        }))
        while True:
            t = json.loads(ws.receive_text())
            if t["type"] in ("done", "error"):
                return t


class RouterHistoriqueRenommeTest(_ClientChatWS):
    _PORT = 54350

    def test_ancien_nom_ne_declenche_plus_apres_renommage(self):
        memory.update_context(prefixes={
            "integres": {"historique": {"trigger": "@archives", "enabled": True}},
            "personnalises": [],
        })
        appels_recherche: list[str] = []
        history_engine.search_history = lambda q: (appels_recherche.append(q), [])[1]
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "@historique question", conv["id"])
        self.assertEqual(appels_recherche, [], "l'ancien nom ne doit plus rien déclencher")

    def test_nouveau_nom_declenche_bien_la_recherche(self):
        memory.update_context(prefixes={
            "integres": {"historique": {"trigger": "@archives", "enabled": True}},
            "personnalises": [],
        })
        appels_recherche: list[str] = []

        def fausse_recherche(q):
            appels_recherche.append(q)
            return [{"titre": "T", "date": "2026-01-01", "extrait": "E"}]
        history_engine.search_history = fausse_recherche
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "@archives question", conv["id"])
        self.assertEqual(appels_recherche, ["question"])
        self.assertIn("Extraits de conversations précédentes", json.dumps(appels[0]["messages"], ensure_ascii=False))

    def test_desactive_ne_declenche_plus_meme_sous_son_nom_par_defaut(self):
        memory.update_context(prefixes={
            "integres": {"historique": {"trigger": "@historique", "enabled": False}},
            "personnalises": [],
        })
        appels_recherche: list[str] = []
        history_engine.search_history = lambda q: (appels_recherche.append(q), [])[1]
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "@historique question", conv["id"])
        self.assertEqual(appels_recherche, [], "désactivé → aucune recherche, même sous le nom historique")


class RouterPrefixesPersonnalisesTest(_ClientChatWS):
    _PORT = 54351

    def test_prefixe_personnalise_injecte_l_instruction_et_retire_le_trigger(self):
        memory.update_context(prefixes={
            "integres": {},
            "personnalises": [_obj_perso(
                id="p1", nom="Ton de pirate", trigger="@pirate",
                instruction="Réponds comme un pirate.", prefixe_actif=True, agentique=False,
            )],
        })
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "@pirate raconte une blague", conv["id"])

        contenu_prompt = json.dumps(appels[0]["messages"], ensure_ascii=False)
        self.assertIn("[INSTRUCTION PERSONNALISÉE — Ton de pirate]", contenu_prompt)
        self.assertIn("Réponds comme un pirate.", contenu_prompt)

        conv_relue = history_engine.get_conversation(conv["id"])
        messages_utilisateur = [m for m in conv_relue["messages"] if m["role"] == "user"]
        self.assertNotIn("@pirate", messages_utilisateur[-1]["content"],
                          "le trigger persisté sur le disque doit avoir été retiré")

    def test_deux_prefixes_personnalises_se_declenchent_sur_le_meme_message(self):
        memory.update_context(prefixes={
            "integres": {},
            "personnalises": [
                _obj_perso(id="p1", nom="Un", trigger="@un", instruction="Instruction UN."),
                _obj_perso(id="p2", nom="Deux", trigger="@deux", instruction="Instruction DEUX."),
            ],
        })
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "@un et @deux, vas-y", conv["id"])

        contenu_prompt = json.dumps(appels[0]["messages"], ensure_ascii=False)
        self.assertIn("[INSTRUCTION PERSONNALISÉE — Un]", contenu_prompt)
        self.assertIn("Instruction UN.", contenu_prompt)
        self.assertIn("[INSTRUCTION PERSONNALISÉE — Deux]", contenu_prompt)
        self.assertIn("Instruction DEUX.", contenu_prompt)

    def test_prefixe_inactif_ne_se_declenche_pas(self):
        """`prefixe_actif=False` (mais `agentique=True`) ne doit jamais être
        détecté à partir du texte, même si son `trigger` apparaît dedans."""
        memory.update_context(prefixes={
            "integres": {},
            "personnalises": [_obj_perso(
                id="p1", nom="Agentique seul", trigger="@agentique", instruction="Ne doit pas apparaître.",
                prefixe_actif=False, agentique=True,
            )],
        })
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "@agentique test", conv["id"])
        contenu_prompt = json.dumps(appels[0]["messages"], ensure_ascii=False)
        self.assertNotIn("INSTRUCTION PERSONNALISÉE", contenu_prompt)


class RouterInterrupteurGeneralTest(_ClientChatWS):
    _PORT = 54352

    def test_skill_personnalise_agentique_apparait_dans_outils_actifs(self):
        memory.update_context(
            tool_calling={"enabled": True, "skills": {
                "web_search": {"enabled": False}, "history_search": {"enabled": False},
                "recherche_approfondie": {"enabled": False, "budget": 4},
            }},
            prefixes={"integres": {}, "personnalises": [_obj_perso(
                id="p1", nom="Ambiance sombre", trigger=None, prefixe_actif=False,
                agentique=True, description="Bascule le ton.", instruction="Sois sombre.",
                budget=2,
            )]},
        )
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "question", conv["id"])
        self.assertIn("ambiance_sombre", appels[0]["outils"])
        self.assertEqual(appels[0]["budgets_override"]["ambiance_sombre"], 2)

    def test_interrupteur_general_coupe_aussi_le_skill_personnalise(self):
        memory.update_context(
            tool_calling={"enabled": False, "skills": {}},
            prefixes={"integres": {}, "personnalises": [_obj_perso(
                id="p1", nom="Ambiance sombre", trigger=None, prefixe_actif=False,
                agentique=True, description="Bascule le ton.", instruction="Sois sombre.",
            )]},
        )
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "question", conv["id"])
        self.assertFalse(appels[0]["outils"],
                          "interrupteur général coupé → aucun outil, même personnalisé")


# ── Baseline de non-régression : réglages jamais touchés ────────────────────

class NonRegressionDefautTest(_ClientChatWS):
    """Un utilisateur qui n'a jamais ouvert Réglages › Préfixes doit garder
    EXACTEMENT le comportement d'avant cette phase : `@historique` sous son
    nom historique, aucun skill personnalisé."""

    _PORT = 54353

    def test_historique_par_defaut_fonctionne_sous_son_nom_historique(self):
        appels_recherche: list[str] = []

        def fausse_recherche(q):
            appels_recherche.append(q)
            return []
        history_engine.search_history = fausse_recherche
        self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "@historique question", conv["id"])
        self.assertEqual(appels_recherche, ["question"])

    def test_aucun_skill_personnalise_par_defaut(self):
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "question normale", conv["id"])
        # Seuls les 3 skills natifs, tous activés par défaut — rien de plus.
        self.assertEqual(set(appels[0]["outils"]), {"web_search", "history_search", "recherche_approfondie"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
