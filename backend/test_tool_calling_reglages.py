#!/usr/bin/env python3
"""Pilotage utilisateur du registre de tool-calling natif (`core.llm._SKILLS`)
— interrupteur général, activation par skill, budget de `recherche_approfondie`
— et le troisième skill lui-même.

Trois couches, trois classes de tests :

* `core.memory.normaliser_tool_calling` — la forme acceptée pour
  `tool_calling` (fusion sur le défaut, jamais un remplacement ; clamp du
  budget) ;
* la PERSISTANCE au redémarrage (`MemoryEngine.__init__`) — le test le plus
  important de ce fichier, cf. `PersistanceRedemarrageTest` : c'est celui qui
  aurait échoué SILENCIEUSEMENT (aucune exception, juste une valeur ignorée)
  sans le correctif de `_CLES_PERSISTANTES`/la restauration en `dict` ;
* le point d'appel réel (`modules/chat/router.py`) — l'interrupteur général
  vide `outils` même si des skills individuels sont actifs, et le budget
  configuré est bien celui transmis à `_stream_ollama` via `budgets_override`,
  pas la constante `_MAX_APPELS_RECHERCHE_APPROFONDIE` de repli.

`recherche_approfondie` réutilise `_executer_outil_web_search` tel quel (cf.
`test_skills_history.py::RegistreSkillsTest` pour l'identité de fonction) :
ce fichier n'a donc pas à retester le pipeline de recherche lui-même, mais
seulement que le registre lui donne le bon budget et que le routeur classe ses
résultats comme CITABLES (`skill_citable`), au même titre que `web_search` —
un `==  "web_search"` en dur les aurait jetés en silence, cf. `SkillCitableTest`
et `RouterOutilsActifsTest`.

Usage :
    python test_tool_calling_reglages.py
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

import core.llm as module_llm  # noqa: E402
import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.jsonstore import read_json, write_json  # noqa: E402
from core.memory import _CONTEXT_DEFAULT, MemoryEngine, normaliser_tool_calling  # noqa: E402
from core.runtime import history_engine, memory, skill_citable  # noqa: E402

from test_tool_calling_ollama import (  # noqa: E402
    _Rejoueur, _appels_outil, _chunk, _stream, _tool_call,
)


# ── Normalisation / clamp ────────────────────────────────────────────────────

class NormaliserToolCallingTest(unittest.TestCase):
    def test_none_rend_le_defaut_complet(self):
        resultat = normaliser_tool_calling(None)
        self.assertEqual(resultat, _CONTEXT_DEFAULT["tool_calling"])

    def test_valeur_du_mauvais_type_rend_le_defaut(self):
        for mauvaise in ("oops", ["liste"], 42, True):
            with self.subTest(mauvaise=mauvaise):
                self.assertEqual(normaliser_tool_calling(mauvaise), _CONTEXT_DEFAULT["tool_calling"])

    def test_dict_partiel_est_fusionne_pas_remplace(self):
        """Un skill désactivé ne doit pas faire disparaître les deux autres —
        c'est le remplacement wholesale que ce chantier a fermé (cf. docstring
        de `normaliser_tool_calling`)."""
        resultat = normaliser_tool_calling({"skills": {"web_search": {"enabled": False}}})
        self.assertFalse(resultat["skills"]["web_search"]["enabled"])
        self.assertTrue(resultat["skills"]["history_search"]["enabled"])
        self.assertTrue(resultat["skills"]["recherche_approfondie"]["enabled"])
        self.assertEqual(resultat["skills"]["recherche_approfondie"]["budget"], 4)

    def test_skill_manquante_sur_disque_reapparait_au_defaut(self):
        """Simule un `context_session.json` écrit AVANT que `recherche_approfondie`
        n'existe : la clé est absente de `skills`, elle doit réapparaître,
        activée, plutôt que de rester manquante pour toujours."""
        resultat = normaliser_tool_calling({
            "enabled": True,
            "skills": {"web_search": {"enabled": False}, "history_search": {"enabled": True}},
        })
        self.assertIn("recherche_approfondie", resultat["skills"])
        self.assertTrue(resultat["skills"]["recherche_approfondie"]["enabled"])

    def test_skill_inconnue_est_ignoree(self):
        resultat = normaliser_tool_calling({"skills": {"invente": {"enabled": True}}})
        self.assertNotIn("invente", resultat["skills"])

    def test_budget_trop_haut_est_clampe_a_dix(self):
        resultat = normaliser_tool_calling({"skills": {"recherche_approfondie": {"budget": 999}}})
        self.assertEqual(resultat["skills"]["recherche_approfondie"]["budget"], 10)

    def test_budget_trop_bas_est_clampe_a_un(self):
        resultat = normaliser_tool_calling({"skills": {"recherche_approfondie": {"budget": 0}}})
        self.assertEqual(resultat["skills"]["recherche_approfondie"]["budget"], 1)

    def test_budget_negatif_est_clampe_a_un(self):
        resultat = normaliser_tool_calling({"skills": {"recherche_approfondie": {"budget": -5}}})
        self.assertEqual(resultat["skills"]["recherche_approfondie"]["budget"], 1)

    def test_budget_non_numerique_retombe_sur_le_defaut(self):
        resultat = normaliser_tool_calling({"skills": {"recherche_approfondie": {"budget": "beaucoup"}}})
        self.assertEqual(resultat["skills"]["recherche_approfondie"]["budget"], 4)

    def test_interrupteur_general_absent_retombe_sur_active(self):
        resultat = normaliser_tool_calling({"skills": {"web_search": {"enabled": False}}})
        self.assertTrue(resultat["enabled"])

    def test_interrupteur_general_coupe_est_conserve(self):
        resultat = normaliser_tool_calling({"enabled": False})
        self.assertFalse(resultat["enabled"])


class CoherenceBudgetDefautTest(unittest.TestCase):
    """Le budget de repli du registre (`core.llm`) et le défaut persistant
    (`core.memory`) ne doivent jamais diverger en silence — `core.memory` ne
    peut pas importer `core.llm` (cycle via `core.runtime`), donc rien
    d'automatique ne les tiendrait égaux sans cette assertion."""

    def test_les_deux_budgets_par_defaut_sont_identiques(self):
        self.assertEqual(
            _CONTEXT_DEFAULT["tool_calling"]["skills"]["recherche_approfondie"]["budget"],
            module_llm._MAX_APPELS_RECHERCHE_APPROFONDIE,
        )


class SkillCitableTest(unittest.TestCase):
    def test_web_search_et_recherche_approfondie_sont_citables(self):
        self.assertTrue(skill_citable("web_search"))
        self.assertTrue(skill_citable("recherche_approfondie"))

    def test_history_search_ne_l_est_pas(self):
        self.assertFalse(skill_citable("history_search"))

    def test_nom_inconnu_n_est_pas_citable(self):
        self.assertFalse(skill_citable("invente"))
        self.assertFalse(skill_citable(""))


# ── Persistance au redémarrage ───────────────────────────────────────────────

class _DossierNeuf(unittest.TestCase):
    """Pose EPURE_DATA_DIR sur un temporaire, APRÈS les imports ci-dessus —
    même fixture que `test_consigne_generale.py`, non partagée entre fichiers
    (convention du dépôt : chaque `test_*.py` est autonome)."""

    def setUp(self):
        self._prev = os.environ.get("EPURE_DATA_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-tool-calling-"))
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
    """Le test le plus important de ce fichier : sans la fusion en `dict` de
    `_CLES_PERSISTANTES`, un `tool_calling` désactivé aurait été ignoré en
    silence par le filtre `isinstance(valeur, str)` d'avant ce chantier —
    aucune exception, juste un réglage de sécurité/coût qui se réactive tout
    seul."""

    def test_interrupteur_general_desactive_survit_a_un_redemarrage(self):
        moteur = self._redemarrer()
        moteur.update_context(tool_calling={"enabled": False, "skills": {}})

        contexte = self._redemarrer().get_context()
        self.assertFalse(contexte["tool_calling"]["enabled"])

    def test_recherche_approfondie_desactivee_et_budget_modifie_survivent(self):
        moteur = self._redemarrer()
        moteur.update_context(tool_calling={
            "enabled": True,
            "skills": {
                "web_search": {"enabled": True},
                "history_search": {"enabled": True},
                "recherche_approfondie": {"enabled": False, "budget": 2},
            },
        })

        contexte = self._redemarrer().get_context()
        self.assertFalse(contexte["tool_calling"]["skills"]["recherche_approfondie"]["enabled"])
        self.assertEqual(contexte["tool_calling"]["skills"]["recherche_approfondie"]["budget"], 2)

    def test_survit_a_plusieurs_redemarrages(self):
        self._redemarrer().update_context(tool_calling={"enabled": False, "skills": {}})
        for _ in range(3):
            moteur = self._redemarrer()
        self.assertFalse(moteur.get_context()["tool_calling"]["enabled"])

    def test_sans_tool_calling_prealable_rend_le_defaut(self):
        self.assertEqual(self._redemarrer().get_context()["tool_calling"], _CONTEXT_DEFAULT["tool_calling"])

    def test_reactiver_puis_redemarrer_tient_aussi(self):
        """La persistance ne doit pas jouer que dans un sens."""
        self._redemarrer().update_context(tool_calling={"enabled": False, "skills": {}})
        self._redemarrer().update_context(tool_calling={"enabled": True, "skills": {}})
        self.assertTrue(self._redemarrer().get_context()["tool_calling"]["enabled"])


class DemarrageRobusteTest(_DossierNeuf):
    def test_valeur_du_mauvais_type_sur_disque_ne_bloque_pas_le_demarrage(self):
        write_json(self.tmp / "context_session.json", {"tool_calling": "n'importe quoi"})
        contexte = self._redemarrer().get_context()
        self.assertEqual(contexte["tool_calling"], _CONTEXT_DEFAULT["tool_calling"])

    def test_fichier_illisible_ne_bloque_pas_le_demarrage(self):
        (self.tmp / "context_session.json").write_text("{pas du JSON", encoding="utf-8")
        contexte = self._redemarrer().get_context()
        self.assertEqual(contexte["tool_calling"], _CONTEXT_DEFAULT["tool_calling"])


# ── `budgets_override` — le budget réel, pas la constante de repli ─────────

class BudgetsOverrideLLMTest(unittest.TestCase):
    """`_MAX_APPELS_RECHERCHE_APPROFONDIE` (registre) n'est qu'un REPLI :
    quand l'appelant fournit `budgets_override`, c'est cette valeur qui plafonne
    les invocations, jamais celle du registre."""

    def test_budgets_override_plafonne_plus_bas_que_le_registre(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="recherche_approfondie", requete="un")], done=True)],
            [_chunk(tool_calls=[_tool_call(nom="recherche_approfondie", requete="deux")], done=True)],
            [_chunk(content="Conclusion.", done=True)],
        ]) as r:
            sortie, _ = _stream(
                r, outils=["recherche_approfondie"],
                budgets_override={"recherche_approfondie": 1},
            )
        self.assertIn("tools", r.appels[0])
        self.assertNotIn("tools", r.appels[1], "budget=1, épuisé après le premier appel")
        self.assertEqual(len(_appels_outil(sortie)), 1)
        self.assertEqual(r.appels_recherche, ["un"], "le deuxième appel ne doit pas chercher")
        messages_finaux = r.appels[-1]["messages"]
        reponses = [m for m in messages_finaux if m.get("tool_name") == "recherche_approfondie"]
        self.assertIn("épuisé", reponses[-1]["content"])

    def test_sans_override_le_budget_par_defaut_du_registre_s_applique(self):
        """`_MAX_APPELS_RECHERCHE_APPROFONDIE` vaut 4 : un cinquième appel ne
        doit plus recevoir l'outil."""
        rounds = [
            [_chunk(tool_calls=[_tool_call(nom="recherche_approfondie", requete=str(i))], done=True)]
            for i in range(module_llm._MAX_APPELS_RECHERCHE_APPROFONDIE)
        ]
        rounds.append([_chunk(content="Conclusion.", done=True)])
        with _Rejoueur(rounds=rounds) as r:
            sortie, _ = _stream(r, outils=["recherche_approfondie"])
        for i in range(module_llm._MAX_APPELS_RECHERCHE_APPROFONDIE):
            self.assertIn("tools", r.appels[i], f"round {i} : budget pas encore épuisé")
        self.assertNotIn("tools", r.appels[-1], "budget par défaut (4) épuisé")
        self.assertEqual(len(_appels_outil(sortie)), module_llm._MAX_APPELS_RECHERCHE_APPROFONDIE)

    def test_recherche_approfondie_appelle_bien_le_pipeline_de_recherche_partage(self):
        """Même exécuteur que `web_search` (`_executer_outil_web_search`),
        preuve par le comportement et pas seulement par identité de fonction
        (déjà couverte par `test_skills_history.py::RegistreSkillsTest`)."""
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="recherche_approfondie", requete="dérivées")],
                    done=True)],
            [_chunk(content="Réponse [1].", done=True)],
        ]) as r:
            sortie, _ = _stream(r, outils=["recherche_approfondie"])
        self.assertEqual(r.appels_recherche, ["dérivées"])
        appels_outil = _appels_outil(sortie)
        self.assertEqual(appels_outil[0]["outil"], "recherche_approfondie")
        self.assertEqual([x.rang for x in appels_outil[0]["resultats"]], [1, 2])


# ── Point d'appel réel : modules/chat/router.py ──────────────────────────────

_WS = "ws://localhost/ws/chat?token={t}"


class RouterOutilsActifsTest(unittest.TestCase):
    """`ctx["tool_calling"]` (lu dans la boucle `while True` du WebSocket)
    pilote directement `outils`/`budgets_override` transmis à `llm.stream` —
    vérifié en interceptant l'appel réel, pas en relisant le code."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54331))
        cls.token = get_api_token()

    def setUp(self):
        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)
        self._contexte_original = memory.get_context()
        self.addCleanup(memory.update_context, **self._contexte_original)

    def _capturer(self):
        appels: list[dict] = []

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            appels.append(kw)
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

    def test_interrupteur_general_coupe_tout_meme_si_des_skills_sont_actifs(self):
        memory.update_context(tool_calling={
            "enabled": False,
            "skills": {
                "web_search": {"enabled": True},
                "history_search": {"enabled": True},
                "recherche_approfondie": {"enabled": True, "budget": 7},
            },
        })
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "question", conv["id"])
        self.assertFalse(appels[0]["outils"], "interrupteur général coupé → aucun outil, quel que soit l'état par skill")

    def test_skills_actifs_individuellement_sont_listes(self):
        memory.update_context(tool_calling={
            "enabled": True,
            "skills": {
                "web_search": {"enabled": True},
                "history_search": {"enabled": False},
                "recherche_approfondie": {"enabled": True, "budget": 4},
            },
        })
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "question", conv["id"])
        self.assertEqual(set(appels[0]["outils"]), {"web_search", "recherche_approfondie"})

    def test_budget_configure_est_transmis_en_override(self):
        memory.update_context(tool_calling={
            "enabled": True,
            "skills": {
                "web_search": {"enabled": True},
                "history_search": {"enabled": True},
                "recherche_approfondie": {"enabled": True, "budget": 7},
            },
        })
        appels = self._capturer()
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "question", conv["id"])
        self.assertEqual(appels[0]["budgets_override"]["recherche_approfondie"], 7)


# ── Endpoint PATCH /context/settings — validation à l'écriture ─────────────

class PatchContextSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54332))
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        self._contexte_original = memory.get_context()
        self.addCleanup(memory.update_context, **self._contexte_original)

    def test_budget_hors_bornes_est_clampe_avant_ecriture(self):
        res = self.client.patch("/context/settings", headers=self.headers, json={
            "tool_calling": {
                "enabled": True,
                "skills": {"recherche_approfondie": {"enabled": True, "budget": 999}},
            },
        })
        self.assertEqual(res.status_code, 200)
        contexte = self.client.get("/context", headers=self.headers).json()
        self.assertEqual(contexte["tool_calling"]["skills"]["recherche_approfondie"]["budget"], 10)

    def test_corps_partiel_ne_perd_pas_les_autres_skills(self):
        res = self.client.patch("/context/settings", headers=self.headers, json={
            "tool_calling": {"skills": {"web_search": {"enabled": False}}},
        })
        self.assertEqual(res.status_code, 200)
        contexte = self.client.get("/context", headers=self.headers).json()
        self.assertFalse(contexte["tool_calling"]["skills"]["web_search"]["enabled"])
        self.assertTrue(contexte["tool_calling"]["skills"]["history_search"]["enabled"])

    def test_champ_hors_liste_allowed_est_ignore(self):
        res = self.client.patch("/context/settings", headers=self.headers, json={
            "tool_calling": {"enabled": False, "skills": {}},
            "champ_invente": "valeur",
        })
        self.assertEqual(res.status_code, 200)
        contexte = self.client.get("/context", headers=self.headers).json()
        self.assertNotIn("champ_invente", contexte)


if __name__ == "__main__":
    unittest.main(verbosity=2)
