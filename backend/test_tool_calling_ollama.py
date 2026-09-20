#!/usr/bin/env python3
"""Tool-calling natif `web_search` sur Ollama — mécanisme SÉPARÉ et INDÉPENDANT
du classifieur heuristique (core/websearch.py::detecter_intention_recherche).
Ce fichier ne touche ni n'éprouve le classifieur ; il vérifie seulement que le
NOUVEAU chemin (le modèle décide, en cours de génération, d'appeler
`web_search` via `tools=`) fonctionne et ne casse rien d'existant.

**Tout est mocké ici** — `ollama_client.chat`, `core.ollama_memoire.
capacites_installees`, `core.websearch.rechercher`/`reformuler_requete`,
`core.webcontent.recuperer_contenu` — pour tourner sans Ollama ni réseau, donc
en CI. Le test bout en bout sur un VRAI modèle Ollama installé est dans
`integration_tool_calling_ollama.py` (nommé `integration_*` et non `test_*` :
`unittest discover` ne le ramasse pas, cf. CLAUDE.md §2 — la CI n'a pas
Ollama).

Ce qui est mesuré et figé ici, pas supposé :

* `ollama._types.Message.ToolCall` n'a PAS de champ `id` (vérifié sur le
  schéma installé, `ollama==0.6.2`) — la corrélation du message `role="tool"`
  se fait par `tool_name`, jamais un `tool_call_id` à la OpenAI ;
* le cap `_MAX_APPELS_OUTIL_WEB` compte des INVOCATIONS de l'outil, pas des
  rounds `chat()` — un modèle qui demande deux appels dans le même round les
  épuise d'un coup ;
* `outils=None` (le défaut des onze autres appelants de `stream()`) ne
  change RIEN à l'appel ni à la sortie — la boucle interne ne fait qu'un tour,
  comme avant ce paramètre (cf. `test_raisonnement_stream.py`, que ce fichier
  ne duplique pas) ;
* un seul `__stats__` sort par tour de `stream()`, agrégé sur tous les rounds.

Depuis le passage au registre `_SKILLS` (plusieurs skills, budgets
indépendants), ce fichier ne couvre QUE `web_search` — c'est sa portée
d'origine et sa non-régression. Les tests dédiés à `history_search` et à la
cohabitation des deux skills dans un même tour sont dans
`test_skills_history.py`, calqué sur les classes ci-dessous plutôt que de les
dupliquer ici.

Usage :
    python test_tool_calling_ollama.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.*

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import ollama  # noqa: E402

import core.ollama_memoire as ollama_memoire  # noqa: E402
import core.webcontent as webcontent_mod  # noqa: E402
import core.websearch as websearch_mod  # noqa: E402
from core import llm as module_llm  # noqa: E402
from core.llm import LLMEngine  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


def _tool_call(nom="web_search", **arguments):
    return ollama.Message.ToolCall(
        function=ollama.Message.ToolCall.Function(name=nom, arguments=arguments)
    )


def _chunk(content="", tool_calls=None, done=False, prompt_tokens=0, output_tokens=0,
           done_reason="stop"):
    """Un chunk de streaming Ollama, avec les vraies classes du client —
    `Message` est un `SubscriptableBaseModel`, cf. test_raisonnement_stream.py.
    """
    return ollama.ChatResponse(
        model="qwen2.5:7b",
        created_at="2026-09-15T12:00:00Z",
        done=done,
        done_reason=done_reason if done else None,
        message=ollama.Message(role="assistant", content=content, tool_calls=tool_calls),
        prompt_eval_count=prompt_tokens if done else None,
        eval_count=output_tokens if done else None,
        eval_duration=1_000_000_000 if done else None,
        prompt_eval_duration=100_000_000 if done else None,
    )


def _resultat(rang, titre="Titre", url="https://exemple.fr/page", extrait="Extrait."):
    return websearch_mod.ResultatWeb(rang=rang, titre=titre, url=url, extrait=extrait,
                                     moteur="ddg-html")


class _Rejoueur:
    """Remplace `ollama_client.chat` (un appel = un round), `capacites_installees`
    et le pipeline `core.websearch`/`core.webcontent`, le temps d'un test.

    `rounds` : une liste de listes de chunks — la N-ième liste est rendue par
    le N-ième appel à `ollama_client.chat`. Un round de trop lève `StopIteration`
    (signale un round non attendu, donc un défaut du code sous test).
    """

    def __init__(self, rounds, capacites=None, resultats_par_recherche=None,
                 leve_recherche=None):
        self.appels: list[dict] = []
        self._it = iter(rounds)
        self.capacites = capacites if capacites is not None else {
            "qwen2.5:7b": {"completion", "tools"},
        }
        #: Une recherche par défaut : 2 résultats. `resultats_par_recherche`
        #: (liste de listes) permet de varier par appel successif.
        self._resultats_it = iter(resultats_par_recherche or [])
        self._defaut_resultats = [_resultat(1), _resultat(2)]
        self.appels_recherche: list[str] = []
        self.appels_reformulation = 0
        self._leve_recherche = leve_recherche

    def _faux_chat(self, **kwargs):
        self.appels.append(kwargs)
        return iter(next(self._it))

    def _faux_rechercher(self, requete, on_etape=None):
        self.appels_recherche.append(requete)
        if self._leve_recherche is not None:
            raise self._leve_recherche
        if on_etape:
            on_etape({"etape": "recherche_debut", "requete": requete, "moteur": "ddg-html"})
        try:
            resultats = next(self._resultats_it)
        except StopIteration:
            resultats = list(self._defaut_resultats)
        if on_etape:
            on_etape({"etape": "recherche_resultats", "nombre": len(resultats),
                      "moteur": "ddg-html", "ms": 1, "resultats": []})
        return resultats

    def _fausse_reformulation(self, requete, on_etape=None):
        self.appels_reformulation += 1
        return requete

    def __enter__(self):
        self._chat_orig = module_llm.ollama_client.chat
        self._cap_orig = ollama_memoire.capacites_installees
        self._rechercher_orig = websearch_mod.rechercher
        self._reformuler_orig = websearch_mod.reformuler_requete
        self._recuperer_orig = webcontent_mod.recuperer_contenu
        self._cache_orig = module_llm._capacites_cache

        module_llm.ollama_client.chat = self._faux_chat
        ollama_memoire.capacites_installees = lambda: self.capacites
        websearch_mod.rechercher = self._faux_rechercher
        websearch_mod.reformuler_requete = self._fausse_reformulation
        # Identité : pas de reclassement par embedding dans ces tests.
        webcontent_mod.recuperer_contenu = lambda resultats, requete, on_etape=None: resultats
        module_llm._capacites_cache = (0.0, {})
        return self

    def __exit__(self, *exc):
        module_llm.ollama_client.chat = self._chat_orig
        ollama_memoire.capacites_installees = self._cap_orig
        websearch_mod.rechercher = self._rechercher_orig
        websearch_mod.reformuler_requete = self._reformuler_orig
        webcontent_mod.recuperer_contenu = self._recuperer_orig
        module_llm._capacites_cache = self._cache_orig


def _stream(rejoueur, messages=None, **kwargs):
    messages = messages if messages is not None else [{"role": "user", "content": "question"}]
    moteur = LLMEngine(config_path=_CONFIG)
    return list(moteur.stream(messages, model="qwen2.5:7b", **kwargs)), messages


def _appels_outil(sortie):
    return [p for p in sortie if isinstance(p, dict) and p.get("__tool_call__")]


def _textes(sortie):
    return [p for p in sortie if isinstance(p, str)]


def _stats(sortie):
    return [p for p in sortie if isinstance(p, dict) and p.get("__stats__")]


class CapaciteGateeTest(unittest.TestCase):
    """`tools=` n'est envoyé QUE si le modèle DÉCLARE la capacité — jamais
    supposé (même piège que `think=True`, CLAUDE.md §3.6)."""

    def test_modele_avec_capacite_recoit_tools(self):
        with _Rejoueur(
            rounds=[[_chunk(content="ok", done=True, output_tokens=1)]],
            capacites={"qwen2.5:7b": {"completion", "tools"}},
        ) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        self.assertIn("tools", r.appels[0])
        self.assertEqual(r.appels[0]["tools"], [module_llm._OUTIL_WEB_SEARCH])

    def test_modele_sans_capacite_ne_recoit_rien(self):
        """`moondream` n'a pas `tools` dans ses capacités (mesuré, ce poste)."""
        with _Rejoueur(
            rounds=[[_chunk(content="ok", done=True, output_tokens=1)]],
            capacites={"qwen2.5:7b": {"completion"}},
        ) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        self.assertNotIn("tools", r.appels[0])
        self.assertEqual(_textes(sortie), ["ok"])

    def test_capacites_inconnues_ne_recoit_rien(self):
        """Ollama injoignable / champ absent → `None`, traité comme indisponible."""
        with _Rejoueur(
            rounds=[[_chunk(content="ok", done=True, output_tokens=1)]],
            capacites={},  # qwen2.5:7b absent du dict → .get() rend None
        ) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        self.assertNotIn("tools", r.appels[0])

    def test_outils_absent_ne_sonde_meme_pas_les_capacites(self):
        """Défaut de `stream()` : comportement d'avant, à l'octet — aucune
        sonde `/api/tags`, aucun `tools=`."""
        with _Rejoueur(
            rounds=[[_chunk(content="ok", done=True, output_tokens=1)]],
        ) as r:
            sortie, _ = _stream(r)  # outils=None implicite
        self.assertNotIn("tools", r.appels[0])
        self.assertEqual(_textes(sortie), ["ok"])
        self.assertEqual(len(_stats(sortie)), 1)


class NonRegressionTest(unittest.TestCase):
    """`outils=None` (le défaut) : identique à `_stream_ollama` d'avant
    ce chantier, pour les onze autres appelants de `stream()`."""

    def test_une_seule_boucle_un_seul_stats(self):
        with _Rejoueur(rounds=[[
            _chunk(content="3"), _chunk(content="9"), _chunk(content="1"),
            _chunk(done=True, prompt_tokens=10, output_tokens=3),
        ]]) as r:
            sortie, msgs = _stream(r)
        self.assertEqual(_textes(sortie), ["3", "9", "1"])
        stats = _stats(sortie)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["prompt_tokens"], 10)
        self.assertEqual(stats[0]["output_tokens"], 3)
        self.assertEqual(len(r.appels), 1, "un seul round attendu")
        self.assertNotIn("tools", r.appels[0])

    def test_appelants_qui_ne_passent_rien_ne_voient_aucun_nouveau_parametre(self):
        """`appel` (le dict envoyé à `ollama_client.chat`) n'a NI `tools` NI
        aucune nouvelle clé quand `outils` n'est pas passé du tout."""
        with _Rejoueur(rounds=[[_chunk(done=True)]]) as r:
            _stream(r)
        self.assertEqual(set(r.appels[0]) - {"model", "messages", "stream", "options"}, set())


class ContexteTokensTest(unittest.TestCase):
    """`contexte_tokens` et `prompt_tokens` vivent dans la MÊME sentinelle et ne
    mesurent PAS la même chose. Les confondre est la régression la plus facile à
    introduire sur ce lot, et elle est silencieuse : elle n'affiche pas une
    erreur, elle affiche une fenêtre saturée sur une conversation à moitié
    pleine — au moment précis où l'utilisateur décide s'il peut encore poser sa
    question."""

    def test_mono_round_le_contexte_est_le_prompt(self):
        with _Rejoueur(rounds=[[_chunk(
            content="ok", done=True, prompt_tokens=613, output_tokens=12,
        )]]) as r:
            sortie, _ = _stream(r)
        stats = _stats(sortie)[0]
        self.assertEqual(stats["prompt_tokens"], 613)
        self.assertEqual(stats["contexte_tokens"], 613)

    def test_multi_round_le_contexte_est_le_dernier_round_pas_la_somme(self):
        """Chaque round renvoie le prompt ENTIER augmenté des résultats d'outil
        du précédent : la somme des `prompt_eval_count` vaut donc bien plus que
        le contexte réel. C'est elle qui se facture (`usage_tracker`), et c'est
        la DERNIÈRE qui décrit où en est la conversation."""
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(requete="q")], done=True, prompt_tokens=100)],
            [_chunk(content="Fini.", done=True, prompt_tokens=250, output_tokens=5)],
        ]) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        stats = _stats(sortie)[0]
        self.assertEqual(len(_appels_outil(sortie)), 1, "un round d'outil attendu")
        self.assertEqual(stats["prompt_tokens"], 350,
                         "la somme reste ce qui se facture — ne pas la changer")
        self.assertEqual(stats["contexte_tokens"], 250,
                         "le contexte est celui du DERNIER round")

    def test_le_champ_est_present_meme_sans_outil(self):
        """Champ additif au même titre que `tronqué` : sa présence ne doit pas
        dépendre du chemin emprunté."""
        with _Rejoueur(rounds=[[_chunk(content="ok", done=True, prompt_tokens=7)]]) as r:
            sortie, _ = _stream(r)
        self.assertIn("contexte_tokens", _stats(sortie)[0])


class RoundTripBasiqueTest(unittest.TestCase):
    """Un appel d'outil, un round de plus, le modèle conclut."""

    def test_tool_call_puis_reponse_finale(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(requete="météo Paris")],
                    done=True, prompt_tokens=10, output_tokens=5)],
            [_chunk(content="Il pleut"), _chunk(content=" [1].", done=True,
                    prompt_tokens=40, output_tokens=8)],
        ]) as r:
            sortie, msgs_appelant = _stream(r, outils=["web_search"])

        self.assertEqual(len(r.appels), 2, "deux rounds ollama_client.chat")
        self.assertEqual(r.appels_recherche, ["météo Paris"])
        # Pas de reformulation : le modèle a déjà écrit des mots-clés.
        self.assertEqual(r.appels_reformulation, 0)

        appels_outil = _appels_outil(sortie)
        self.assertEqual(len(appels_outil), 1)
        self.assertEqual(appels_outil[0]["outil"], "web_search")
        self.assertEqual(appels_outil[0]["arguments"], {"requete": "météo Paris"})
        self.assertEqual([r_.rang for r_ in appels_outil[0]["resultats"]], [1, 2])

        self.assertEqual("".join(_textes(sortie)), "Il pleut [1].")

        # Un seul `__stats__`, agrégé sur les deux rounds.
        stats = _stats(sortie)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["prompt_tokens"], 50)   # 10 + 40
        self.assertEqual(stats[0]["output_tokens"], 13)   # 5 + 8

        # Round 2 ne réexpose plus `tools` : le budget (1/2) reste, mais rien
        # n'oblige un round de conclusion à recevoir l'outil à nouveau — ce
        # test fige simplement ce qui a été observé, pas une règle nouvelle.
        second_appel_messages = r.appels[1]["messages"]
        self.assertEqual(second_appel_messages[-2]["role"], "assistant")
        self.assertIn("tool_calls", second_appel_messages[-2])
        self.assertEqual(second_appel_messages[-1]["role"], "tool")
        # Corrélation par NOM, jamais par id — `Message.ToolCall` n'en a pas.
        self.assertEqual(second_appel_messages[-1]["tool_name"], "web_search")
        self.assertNotIn("tool_call_id", second_appel_messages[-1])

        # Consigne de citation + contenu domaine-seul dans le message outil.
        contenu_outil = second_appel_messages[-1]["content"]
        self.assertIn("[1]", contenu_outil)
        self.assertIn("exemple.fr", contenu_outil)
        self.assertNotIn("https://exemple.fr/page", contenu_outil)
        self.assertIn("cite", contenu_outil.lower())

        # `messages`, l'objet de l'APPELANT, n'a jamais été modifié : les
        # messages assistant/tool construits pendant la boucle ne vivent que
        # dans la copie locale.
        self.assertEqual(msgs_appelant, [{"role": "user", "content": "question"}])

    def test_le_texte_du_premier_round_est_bien_transmis_au_second_appel(self):
        """Un round qui porte À LA FOIS du texte et un tool_call (jamais
        observé mais pas interdit par l'API) ne doit pas perdre ce texte."""
        with _Rejoueur(rounds=[
            [_chunk(content="Je cherche…", tool_calls=[_tool_call(requete="x")], done=True)],
            [_chunk(content="Fini.", done=True)],
        ]) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        self.assertEqual(_textes(sortie), ["Je cherche…", "Fini."])
        message_assistant = r.appels[1]["messages"][-2]
        self.assertEqual(message_assistant["content"], "Je cherche…")


class PlafondAppelsTest(unittest.TestCase):
    """`_MAX_APPELS_OUTIL_WEB` (2) compte des INVOCATIONS, pas des rounds."""

    def test_un_troisieme_round_ne_reexpose_plus_l_outil(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(requete="un")], done=True)],
            [_chunk(tool_calls=[_tool_call(requete="deux")], done=True)],
            [_chunk(content="Conclusion.", done=True)],
        ]) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        self.assertEqual(len(r.appels), 3)
        self.assertIn("tools", r.appels[0])
        self.assertIn("tools", r.appels[1])
        self.assertNotIn("tools", r.appels[2], "budget épuisé, l'outil ne doit plus être offert")
        self.assertEqual(len(_appels_outil(sortie)), 2)
        self.assertEqual(r.appels_recherche, ["un", "deux"])

    def test_trois_appels_parallèles_dans_un_seul_round_n_en_execute_que_deux(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[
                _tool_call(requete="un"), _tool_call(requete="deux"), _tool_call(requete="trois"),
            ], done=True)],
            [_chunk(content="Conclusion.", done=True)],
        ]) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        self.assertEqual(r.appels_recherche, ["un", "deux"], "le 3e ne doit pas chercher")
        self.assertEqual(len(_appels_outil(sortie)), 2)
        messages_round2 = r.appels[1]["messages"]
        # Les 3 réponses outil sont présentes ; la 3e dit le budget épuisé.
        reponses_outil = [m for m in messages_round2 if m["role"] == "tool"]
        self.assertEqual(len(reponses_outil), 3)
        self.assertIn("épuisé", reponses_outil[2]["content"])


class RenumerotationRangTest(unittest.TestCase):
    """Deux recherches dans le même tour ne doivent jamais partager un rang."""

    def test_deuxieme_appel_intra_tour_est_decale(self):
        with _Rejoueur(
            rounds=[
                [_chunk(tool_calls=[_tool_call(requete="un")], done=True)],
                [_chunk(tool_calls=[_tool_call(requete="deux")], done=True)],
                [_chunk(content="Fini.", done=True)],
            ],
            resultats_par_recherche=[[_resultat(1), _resultat(2)], [_resultat(1)]],
        ) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        appels_outil = _appels_outil(sortie)
        self.assertEqual([x.rang for x in appels_outil[0]["resultats"]], [1, 2])
        # Décalé de 2 (len du premier lot) : le rang 1 d'origine devient 3.
        self.assertEqual([x.rang for x in appels_outil[1]["resultats"]], [3])

    def test_rang_web_existant_decale_le_premier_appel(self):
        """Le classifieur heuristique a déjà 5 résultats ce tour — le premier
        appel d'outil ne doit pas repartir de [1]."""
        with _Rejoueur(
            rounds=[[_chunk(tool_calls=[_tool_call(requete="un")], done=True)],
                    [_chunk(content="Fini.", done=True)]],
            resultats_par_recherche=[[_resultat(1), _resultat(2)]],
        ) as r:
            sortie, _ = _stream(r, outils=["web_search"], rang_web_existant=5)
        appels_outil = _appels_outil(sortie)
        self.assertEqual([x.rang for x in appels_outil[0]["resultats"]], [6, 7])


class TraceEtRechercheEchoueeTest(unittest.TestCase):
    def test_on_etape_recherche_recoit_le_marqueur_dedie_puis_les_etapes_reelles(self):
        etapes: list[dict] = []
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(requete="q")], done=True)],
            [_chunk(content="Fini.", done=True)],
        ]) as r:
            _stream(r, outils=["web_search"], on_etape_recherche=etapes.append)
        types = [e["etape"] for e in etapes]
        self.assertEqual(types[0], "tool_call_web_search")
        self.assertEqual(etapes[0]["requete"], "q")
        self.assertIn("recherche_debut", types)
        self.assertIn("recherche_resultats", types)

    def test_recherche_qui_leve_rend_un_message_au_modele_sans_planter(self):
        with _Rejoueur(
            rounds=[
                [_chunk(tool_calls=[_tool_call(requete="q")], done=True)],
                [_chunk(content="Je ne sais pas.", done=True)],
            ],
            leve_recherche=websearch_mod.RechercheWebErreur("DNS injoignable"),
        ) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        appels_outil = _appels_outil(sortie)
        self.assertEqual(appels_outil[0]["resultats"], [])
        message_outil = r.appels[1]["messages"][-1]
        self.assertIn("impossible", message_outil["content"].lower())
        self.assertEqual(_textes(sortie), ["Je ne sais pas."])


class OutilInconnuTest(unittest.TestCase):
    """Un seul outil est déclaré : un nom inventé ne doit pas planter le tour."""

    def test_nom_inconnu_recoit_un_message_d_erreur_sans_recherche(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="autre_chose")], done=True)],
            [_chunk(content="ok", done=True)],
        ]) as r:
            sortie, _ = _stream(r, outils=["web_search"])
        self.assertEqual(r.appels_recherche, [])
        self.assertEqual(_appels_outil(sortie), [])
        message_outil = r.appels[1]["messages"][-1]
        self.assertEqual(message_outil["tool_name"], "autre_chose")
        self.assertIn("inconnu", message_outil["content"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
