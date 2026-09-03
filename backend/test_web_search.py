#!/usr/bin/env python3
"""Tests pour core.websearch (recherche @web) et son compat modules/chat/router.

Par défaut, exécute des tests *offline* avec le HTTP mocké (aucun accès
réseau) : Instant Answer (avec/sans URL), fallback HTML sur un fixture RÉEL,
résolution des redirections DuckDuckGo, détection de structure changée,
cache LRU et chemins d'erreur.

Usage :
    python test_web_search.py            # tests offline (unittest)
    python test_web_search.py --live     # vraie recherche réseau (démo)
"""

import json
import os
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

# S'assurer de pouvoir importer main depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core import websearch
from core.citations import extraire_rangs_cites
from core.websearch import RechercheWebErreur, ResultatWeb

# perform_web_search est resté dans le module chat (compat websocket), en
# délégation vers core.websearch depuis le 2026-09-02.
from modules.chat import router as chat_router

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_HTML_FIXTURE = (_FIXTURES_DIR / "ddg_html_python.html").read_text(encoding="utf-8")


class _FakeResponse:
    """Réponse HTTP minimale compatible avec `with urlopen(...) as resp`."""

    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_EMPTY_JSON = '{"Abstract": "", "RelatedTopics": []}'


def _urlopen_router(responses):
    """Construit un faux urlopen choisissant la réponse selon l'URL appelée.

    `responses` mappe un fragment d'URL vers soit un corps (str), soit une
    Exception à lever (simulation d'erreur réseau).
    """

    def _fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        for needle, resp in responses.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return _FakeResponse(resp)
        raise AssertionError(f"URL inattendue dans le test : {url}")

    return _fake


class WebSearchOfflineTest(unittest.TestCase):
    def setUp(self):
        # Le cache est un état global au module : repartir propre à chaque test.
        websearch._cache.clear()

    def test_empty_query_returns_empty(self):
        self.assertEqual(websearch.rechercher("   "), [])
        self.assertEqual(chat_router.perform_web_search("   "), "")

    # ── Instant Answer ───────────────────────────────────────────────────

    def test_instant_answer_keeps_only_items_with_url(self):
        instant_json = json.dumps({
            "Abstract": "Python est un langage de programmation.",
            "AbstractSource": "Wikipedia",
            "AbstractURL": "https://fr.wikipedia.org/wiki/Python",
            "Definition": "Python : langage interprété.",
            "DefinitionSource": "Un dictionnaire",
            "DefinitionURL": "https://exemple.org/def/python",
            # RelatedTopics : bruit de désambiguïsation DDG, sans URL fiable
            # par élément — ne doit produire AUCUN résultat.
            "RelatedTopics": [
                {"Text": "Python (serpent)", "FirstURL": "https://duckduckgo.com/Python_(serpent)"},
                {"Topics": [{"Text": "Monty Python", "FirstURL": "https://duckduckgo.com/Monty_Python"}]},
            ],
        })
        fake = _urlopen_router({"api.duckduckgo.com": instant_json})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            out = websearch.rechercher("python instant")

        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].rang, 1)
        self.assertEqual(out[0].url, "https://fr.wikipedia.org/wiki/Python")
        self.assertEqual(out[0].moteur, "ddg-instant")
        self.assertEqual(out[1].url, "https://exemple.org/def/python")
        # Aucune trace des RelatedTopics dans les résultats retenus.
        blob = " ".join(f"{r.titre} {r.extrait}" for r in out)
        self.assertNotIn("serpent", blob)
        self.assertNotIn("Monty Python", blob)

    def test_related_topics_alone_produce_no_result(self):
        """Un Instant Answer ne portant QUE des RelatedTopics (aucune URL
        exploitable) ne doit produire aucun résultat — ni planter le fallback."""
        instant_only_related = json.dumps({
            "Abstract": "",
            "RelatedTopics": [{"Text": "Python (langage)", "FirstURL": "https://duckduckgo.com/Python"}],
        })
        fake = _urlopen_router({
            "api.duckduckgo.com": instant_only_related,
            "html.duckduckgo.com": "<html><body>rien ici</body></html>",  # <5000 octets, 0 résultat légitime
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            out = websearch.rechercher("python sans url")
        self.assertEqual(out, [])

    # ── Fallback HTML, sur un fixture RÉEL ──────────────────────────────

    def test_html_fallback_real_fixture(self):
        fake = _urlopen_router({
            "api.duckduckgo.com": _EMPTY_JSON,       # Instant Answer vide → fallback
            "html.duckduckgo.com": _HTML_FIXTURE,    # réponse RÉELLE enregistrée
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            out = websearch.rechercher("python programming")

        self.assertEqual(len(out), 5)  # plafonné à _MAX_RESULTS
        for r in out:
            self.assertIsInstance(r, ResultatWeb)
            self.assertTrue(r.url.startswith("http"), r.url)
            self.assertNotIn("duckduckgo.com/l/", r.url)
            # Le fixture réel porte deux placements Bing Ads en tête
            # (y.js/ad_domain) : doivent être filtrés, pas juste dépréfixés.
            self.assertNotIn("y.js", r.url)
            self.assertNotIn("aclick", r.url)
            self.assertEqual(r.moteur, "ddg-html")

        # Les deux publicités du fixture précédaient "Welcome to Python.org" :
        # une fois filtrées, il devient le RANG 1, pas un rang quelconque —
        # c'est ce qui prouve l'absence de trou dans la numérotation.
        self.assertEqual(out[0].url, "https://www.python.org/")
        self.assertEqual(out[0].rang, 1)
        self.assertEqual(out[0].titre, "Welcome to Python.org")

    def test_html_fallback_via_perform_web_search_formats_domain_not_url(self):
        """Depuis la phase 1.5 : le texte injecté au prompt porte le DOMAINE,
        jamais l'URL complète (core.citations valide les citations en aval à
        partir des `ResultatWeb`, pas en reparsant ce texte)."""
        fake = _urlopen_router({
            "api.duckduckgo.com": _EMPTY_JSON,
            "html.duckduckgo.com": _HTML_FIXTURE,
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            out = chat_router.perform_web_search("python programming")
        self.assertIn("[1]", out)
        self.assertIn("(python.org)", out)
        self.assertNotIn("https://www.python.org/", out)
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)

    def test_domaine_retire_le_prefixe_www(self):
        self.assertEqual(websearch._domaine("https://www.python.org/"), "python.org")
        self.assertEqual(websearch._domaine("https://fr.wikipedia.org/wiki/Python"), "fr.wikipedia.org")
        self.assertEqual(websearch._domaine("https://example.org"), "example.org")

    def test_formater_pour_llm_ne_porte_aucune_url_complete(self):
        resultats = [
            ResultatWeb(rang=1, titre="Python", url="https://www.python.org/", extrait="Un langage.", moteur="ddg-html"),
            ResultatWeb(rang=2, titre="Wikipedia", url="https://fr.wikipedia.org/wiki/Python", extrait="Encyclopédie.", moteur="ddg-instant"),
        ]
        out = websearch.formater_pour_llm(resultats)
        self.assertIn("[1] Python (python.org) — Un langage.", out)
        self.assertIn("[2] Wikipedia (fr.wikipedia.org) — Encyclopédie.", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("http://", out)

    # ── Filtrage publicitaire ─────────────────────────────────────────────
    # La phase suivante rend les résultats CITABLES par le modèle : un
    # placement payant en tête de liste deviendrait une source légitimée
    # dans une réponse d'assistant. cf. core.websearch._MOTIFS_PUBLICITAIRES.

    def test_est_publicitaire_motifs(self):
        self.assertTrue(websearch._est_publicitaire("https://duckduckgo.com/y.js?ad_domain=x"))
        self.assertTrue(websearch._est_publicitaire("https://www.bing.com/aclick?ld=abc123"))
        self.assertTrue(websearch._est_publicitaire("https://example.org/page?ad_provider=bingv7aa"))
        self.assertTrue(websearch._est_publicitaire("https://example.org/page?ad_type=txad"))
        self.assertFalse(websearch._est_publicitaire("https://www.python.org/"))
        self.assertFalse(websearch._est_publicitaire("https://docs.python.org/3/tutorial/"))

    def test_ad_in_first_position_filtered_ranks_stay_contiguous(self):
        ad_cible = (
            "https://duckduckgo.com/y.js?ad_domain=exemple-pub.test&ad_provider=bingv7aa"
            "&ad_type=txad&u3=" + urllib.parse.quote("https://www.bing.com/aclick?ld=xyz123", safe="")
        )
        ad_href = "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(ad_cible, safe="") + "&amp;rut=deadbeef"
        organique_1_href = "//duckduckgo.com/l/?uddg=" + urllib.parse.quote("https://exemple.org/un", safe="") + "&amp;rut=aaa"
        organique_2_href = "//duckduckgo.com/l/?uddg=" + urllib.parse.quote("https://exemple.org/deux", safe="") + "&amp;rut=bbb"
        html_avec_pub_en_tete = f"""
        <div class="result results_links results_links_deep web-result ">
          <h2 class="result__title"><a rel="nofollow" class="result__a" href="{ad_href}">Cours en ligne sponsorisé</a></h2>
          <a class="result__snippet" href="{ad_href}">Publicité pour un cours en ligne.</a>
        </div>
        <div class="result results_links results_links_deep web-result ">
          <h2 class="result__title"><a rel="nofollow" class="result__a" href="{organique_1_href}">Résultat organique un</a></h2>
          <a class="result__snippet" href="{organique_1_href}">Premier résultat organique.</a>
        </div>
        <div class="result results_links results_links_deep web-result ">
          <h2 class="result__title"><a rel="nofollow" class="result__a" href="{organique_2_href}">Résultat organique deux</a></h2>
          <a class="result__snippet" href="{organique_2_href}">Second résultat organique.</a>
        </div>
        """
        fake = _urlopen_router({
            "api.duckduckgo.com": _EMPTY_JSON,
            "html.duckduckgo.com": html_avec_pub_en_tete,
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            out = websearch.rechercher("requete avec pub en tete")

        self.assertEqual(len(out), 2)
        self.assertEqual([r.rang for r in out], [1, 2])  # pas de trou (l'ad occupait le rang 1)
        for r in out:
            self.assertNotIn("y.js", r.url)
            self.assertNotIn("aclick", r.url)
        self.assertEqual(out[0].url, "https://exemple.org/un")
        self.assertEqual(out[1].url, "https://exemple.org/deux")

    # ── Résolution des URLs DuckDuckGo ───────────────────────────────────

    def test_ddg_redirect_decoded_to_real_url(self):
        real_url = "https://www.python.org/"
        href = "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(real_url, safe="") + "&rut=abc123"
        self.assertEqual(websearch._resoudre_url_ddg(href), real_url)

    def test_ddg_redirect_double_encoded_uddg(self):
        real_url = "https://exemple.org/chemin?a=1&b=2"
        double_encoded = urllib.parse.quote(urllib.parse.quote(real_url, safe=""), safe="")
        href = "//duckduckgo.com/l/?uddg=" + double_encoded
        self.assertEqual(websearch._resoudre_url_ddg(href), real_url)

    def test_non_http_redirect_target_discarded(self):
        href = "//duckduckgo.com/l/?uddg=" + urllib.parse.quote("javascript:alert(1)", safe="")
        self.assertIsNone(websearch._resoudre_url_ddg(href))

    # ── Détection de structure changée (échec silencieux) ────────────────

    def test_structure_changee_leve_une_erreur_distincte(self):
        gros_corps_sans_resultat = "<html><body>" + ("x" * 20000) + "</body></html>"
        fake = _urlopen_router({
            "api.duckduckgo.com": _EMPTY_JSON,
            "html.duckduckgo.com": gros_corps_sans_resultat,
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            with self.assertRaises(RechercheWebErreur):
                websearch.rechercher("python structure cassee")

    def test_petite_reponse_vide_reste_un_zero_resultat_legitime(self):
        """Sous le seuil, une page sans résultat n'est PAS une structure
        cassée — juste une recherche vide (page d'erreur DDG, etc.)."""
        petit_corps = "<html><body>rien</body></html>"
        fake = _urlopen_router({
            "api.duckduckgo.com": _EMPTY_JSON,
            "html.duckduckgo.com": petit_corps,
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            out = websearch.rechercher("python vide")
        self.assertEqual(out, [])

    # ── Cache LRU ─────────────────────────────────────────────────────────

    def test_cache_avoids_second_fetch(self):
        instant_json = json.dumps({
            "Abstract": "Python.", "AbstractSource": "Wikipedia",
            "AbstractURL": "https://fr.wikipedia.org/wiki/Python",
            "RelatedTopics": [],
        })
        fake = mock.Mock(side_effect=_urlopen_router({"api.duckduckgo.com": instant_json}))
        with mock.patch.object(websearch.urllib.request, "urlopen", fake):
            first = websearch.rechercher("python cache")
            calls_after_first = fake.call_count
            second = websearch.rechercher("python cache")
        self.assertEqual(first, second)
        self.assertEqual(calls_after_first, 1)
        self.assertEqual(fake.call_count, 1)  # 2e appel servi par le cache

    def test_echec_reseau_non_mis_en_cache(self):
        """Un 403/timeout mis en cache 300s, c'est cinq minutes d'échecs sur
        une requête qui aurait pu réussir au 2e essai — ne doit jamais arriver."""
        err = websearch.urllib.error.URLError("offline")
        fake = mock.Mock(side_effect=_urlopen_router({"api.duckduckgo.com": err, "html.duckduckgo.com": err}))
        with mock.patch.object(websearch.urllib.request, "urlopen", fake):
            with self.assertRaises(RechercheWebErreur):
                websearch.rechercher("python echec cache")
            appels_apres_premier_echec = fake.call_count
            with self.assertRaises(RechercheWebErreur):
                websearch.rechercher("python echec cache")
        # Chaque échec retente le réseau — aucune entrée n'est passée en cache.
        self.assertGreater(fake.call_count, appels_apres_premier_echec)
        self.assertIsNone(websearch._cache_get("python echec cache"))

    def test_structure_changee_non_mise_en_cache(self):
        """Même exigence pour l'autre voie d'échec : une structure HTML
        détectée comme cassée ne doit pas non plus geler un « 0 résultat »
        pendant 300s si DuckDuckGo redevient lisible entre-temps."""
        gros_corps_sans_resultat = "<html><body>" + ("x" * 20000) + "</body></html>"
        fake = _urlopen_router({
            "api.duckduckgo.com": _EMPTY_JSON,
            "html.duckduckgo.com": gros_corps_sans_resultat,
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            with self.assertRaises(RechercheWebErreur):
                websearch.rechercher("python structure cache")
        self.assertIsNone(websearch._cache_get("python structure cache"))

    # ── Erreur réseau totale ──────────────────────────────────────────────

    def test_network_error_raises_and_perform_web_search_reports_it(self):
        err = websearch.urllib.error.URLError("offline")
        fake = _urlopen_router({"api.duckduckgo.com": err, "html.duckduckgo.com": err})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            with self.assertRaises(RechercheWebErreur):
                websearch.rechercher("python erreur")
            out = chat_router.perform_web_search("python erreur")
        self.assertTrue(out.startswith("Erreur de recherche web"), out)
        self.assertTrue(out.startswith(websearch.PREFIXE_ERREUR), out)

    def test_erreur_ne_casse_pas_la_boucle_et_reste_distinguable(self):
        """perform_web_search ne doit jamais laisser un RechercheWebErreur
        remonter tel quel (ça casserait le tour de chat), ni le convertir en
        "" (la boucle basculerait alors sur « aucun résultat », l'échec
        silencieux que la phase 1 a supprimé) : il doit rester distinguable
        d'un résultat réel ET d'un 0 résultat légitime."""
        with mock.patch.object(chat_router, "rechercher", side_effect=RechercheWebErreur("DNS injoignable")):
            out = chat_router.perform_web_search("python erreur distincte")
        self.assertNotEqual(out, "")
        self.assertTrue(out.startswith(websearch.PREFIXE_ERREUR))
        self.assertIn("DNS injoignable", out)

    def test_construire_web_ctx_distingue_erreur_resultat_et_vide(self):
        """Le contexte injecté au prompt ne doit jamais présenter un message
        d'erreur comme un « résultat récent à citer » — exactement le bug
        qui restait avant cette correction (web_results non vide == résultat,
        sans distinguer un message d'erreur d'un contenu réel)."""
        erreur = chat_router._construire_web_ctx(f"{websearch.PREFIXE_ERREUR}panne réseau")
        self.assertIn("panne réseau", erreur)
        self.assertNotIn("cite la source", erreur)
        self.assertIn("Ce n'est PAS un résultat", erreur)

        resultat = chat_router._construire_web_ctx("[1] Titre (exemple.org) — extrait")
        self.assertIn("Cite tes sources", resultat)
        self.assertIn("[1]", resultat)
        self.assertIn("JAMAIS d'URL", resultat)
        self.assertIn("numéro absent", resultat)

        vide = chat_router._construire_web_ctx("")
        self.assertIn("aucun résultat exploitable", vide)


class NonRegressionHistoriqueTest(unittest.TestCase):
    """LE test qui protège tout l'édifice de la phase 2 (et son suivi
    immédiat, qui sépare bloc Sources et contenu persisté).

    Un message assistant qui a cité des résultats web ne doit JAMAIS
    réinjecter leurs URLs complètes dans le prompt du tour SUIVANT — c'est
    exactement le bug corrigé : la version précédente appendait un bloc
    « Sources » (URLs complètes) au `content` persisté avant de l'écrire, et
    ce `content` repartait tel quel dans l'historique du prompt, réintroduisant
    par la porte de derrière ce que `formater_pour_llm` (phase 1.5) retire du
    contexte initial (domaine seulement).

    Écrit en simulant EXACTEMENT le geste de production, sur les deux points
    de couture réels : `chat_router._sources_citees` (ce qui part en
    métadonnée) et `HistoryEngine.append_messages`/`get_conversation` (ce qui
    revient au tour suivant) — pas une reconstruction approximative.
    """

    def test_aucune_url_du_tour_precedent_dans_le_prompt_du_tour_suivant(self):
        resultats = [
            ResultatWeb(rang=1, titre="Python", url="https://www.python.org/",
                        extrait="Un langage.", moteur="ddg-html"),
        ]
        conv = chat_router.history_engine.create_conversation()
        try:
            chat_router.history_engine.append_messages(
                conv["id"], [{"role": "user", "content": "Qu'est-ce que Python ?"}]
            )

            # Tour 1 : l'assistant cite [1]. Reproduit exactement le calcul
            # fait par `_enregistrer_reponse` avant persistance.
            reponse_tour_1 = "Python est un langage de programmation, voir [1]."
            sources = chat_router._sources_citees(reponse_tour_1, resultats)
            self.assertTrue(sources, "précondition : une citation à protéger, sinon le test ne teste rien")
            chat_router.history_engine.append_messages(
                conv["id"],
                [{"role": "assistant", "content": reponse_tour_1, "sources": sources}],
            )

            # Tour 2 : reconstruction du prompt EXACTEMENT comme le fait la
            # boucle websocket (`messages = list(conv["messages"])`, chaque
            # message ne portant que `role`/`content` au LLM).
            conv_relue = chat_router.history_engine.get_conversation(conv["id"])
            messages_pour_le_prompt = [
                {"role": m["role"], "content": m["content"]} for m in conv_relue["messages"]
            ]
            texte_du_prompt = json.dumps(messages_pour_le_prompt, ensure_ascii=False)
            self.assertNotIn("https://www.python.org/", texte_du_prompt)
            self.assertNotIn("https://", texte_du_prompt)
            self.assertNotIn("Sources", texte_du_prompt)

            # Le lien n'a pas disparu : il vit en métadonnée, à côté du prompt.
            self.assertEqual(
                conv_relue["messages"][-1]["sources"],
                [{"rang": 1, "titre": "Python", "url": "https://www.python.org/"}],
            )
        finally:
            chat_router.history_engine.delete_conversation(conv["id"])

    def test_trace_persistee_identique_en_direct_et_apres_rechargement(self):
        """L'événement `done` porte `_meta.get("trace_recherche", [])` —
        c'est-à-dire EXACTEMENT ce que `append_messages` vient d'écrire, la
        même donnée qu'une relecture via `get_conversation` (F5). Un seul
        magasin, une seule lecture : rien à synchroniser entre les deux vues."""
        resultats = [ResultatWeb(rang=1, titre="Python", url="https://www.python.org/", extrait="", moteur="ddg-html")]
        conv = chat_router.history_engine.create_conversation()
        try:
            etapes_recherche = [
                {"etape": "recherche_debut", "requete": "python", "moteur": "ddg-instant"},
                {"etape": "recherche_resultats", "nombre": 1, "moteur": "ddg-instant", "ms": 42, "resultats": []},
            ]
            reponse = "Python est un langage, voir [7]."  # [7] hors plage → anomalie
            sources, trace = chat_router._finaliser_citations_et_trace(
                conv["id"], reponse, resultats, "", set(), etapes_recherche,
            )
            # Ce que `_enregistrer_reponse` ferait : une seule écriture.
            conv_ecrite = chat_router.history_engine.append_messages(
                conv["id"], [{"role": "assistant", "content": reponse, "sources": sources, "trace_recherche": trace}],
            )
            meta_pour_done = dict(conv_ecrite["messages"][-1])  # ce que `done` enverrait

            conv_relue = chat_router.history_engine.get_conversation(conv["id"])
            trace_apres_f5 = conv_relue["messages"][-1]["trace_recherche"]

            self.assertEqual(meta_pour_done["trace_recherche"], trace_apres_f5)
            self.assertEqual(trace_apres_f5[-1]["etape"], "citations_invalides")
        finally:
            chat_router.history_engine.delete_conversation(conv["id"])


class SourcesCiteesTest(unittest.TestCase):
    """Item 4 (corrigé) : les sources citées sont une liste STRUCTURÉE,
    jamais un texte appendé au contenu — cf. `core.history.HistoryEngine.
    append_messages` (paramètre `sources`) et son docstring pour le pourquoi
    (l'historique réinjectait les URLs complètes au tour suivant)."""

    def setUp(self):
        self.resultats = [
            ResultatWeb(rang=1, titre="Python", url="https://www.python.org/", extrait="", moteur="ddg-html"),
            ResultatWeb(rang=2, titre="Wikipedia", url="https://fr.wikipedia.org/wiki/Python", extrait="", moteur="ddg-html"),
            ResultatWeb(rang=3, titre="W3Schools", url="https://www.w3schools.com/python/", extrait="", moteur="ddg-html"),
        ]

    def test_ne_liste_que_les_rangs_cites_dans_l_ordre_d_apparition(self):
        reponse = "D'après [2], puis en confirmant avec [1]."
        sources = chat_router._sources_citees(reponse, self.resultats)
        self.assertEqual(sources, [
            {"rang": 2, "titre": "Wikipedia", "url": "https://fr.wikipedia.org/wiki/Python"},
            {"rang": 1, "titre": "Python", "url": "https://www.python.org/"},
        ])  # [3]/W3Schools jamais cité, absent

    def test_aucune_citation_liste_vide(self):
        self.assertEqual(chat_router._sources_citees("Une réponse qui ne cite rien.", self.resultats), [])

    def test_sans_resultats_web_liste_vide(self):
        self.assertEqual(chat_router._sources_citees("Voir [1].", []), [])

    def test_sans_reponse_liste_vide(self):
        self.assertEqual(chat_router._sources_citees("", self.resultats), [])


class TraceDeRechercheTest(unittest.TestCase):
    """`core.websearch.rechercher(..., on_etape=...)` : le déroulé de la
    recherche, émis à CHAQUE étape significative. Exigence explicite de
    l'utilisateur — la requête réellement envoyée doit être visible mot pour
    mot, c'est l'audit de confidentialité, pas un confort de debug."""

    def setUp(self):
        websearch._cache.clear()

    def _capturer(self):
        etapes: list[dict] = []
        return etapes, etapes.append

    def test_instant_answer_emet_debut_puis_resultats(self):
        etapes, on_etape = self._capturer()
        instant_json = json.dumps({
            "Abstract": "Python.", "AbstractSource": "Wikipedia",
            "AbstractURL": "https://fr.wikipedia.org/wiki/Python", "RelatedTopics": [],
        })
        fake = _urlopen_router({"api.duckduckgo.com": instant_json})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            websearch.rechercher("python trace instant", on_etape=on_etape)

        self.assertEqual([e["etape"] for e in etapes], ["recherche_debut", "recherche_resultats"])
        self.assertEqual(etapes[0]["requete"], "python trace instant")
        self.assertEqual(etapes[0]["moteur"], "ddg-instant")
        self.assertEqual(etapes[1]["moteur"], "ddg-instant")
        self.assertEqual(etapes[1]["nombre"], 1)
        self.assertIn("ms", etapes[1])
        self.assertEqual(etapes[1]["resultats"][0]["url"], "https://fr.wikipedia.org/wiki/Python")

    def test_html_fallback_emet_deux_debuts_puis_resultats(self):
        etapes, on_etape = self._capturer()
        fake = _urlopen_router({
            "api.duckduckgo.com": _EMPTY_JSON,
            "html.duckduckgo.com": _HTML_FIXTURE,
        })
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            websearch.rechercher("python trace html", on_etape=on_etape)

        types = [e["etape"] for e in etapes]
        # Deux tentatives réseau réelles : instant (vide) PUIS html (fallback).
        self.assertEqual(types.count("recherche_debut"), 2)
        self.assertEqual(etapes[0]["moteur"], "ddg-instant")
        self.assertEqual(etapes[1]["moteur"], "ddg-html")
        self.assertIn("recherche_filtree", types)  # le fixture réel porte 2 pubs
        derniere = etapes[-1]
        self.assertEqual(derniere["etape"], "recherche_resultats")
        self.assertEqual(derniere["moteur"], "ddg-html")

    def test_resultats_publicitaires_ecartes_comptes(self):
        etapes, on_etape = self._capturer()
        fake = _urlopen_router({"api.duckduckgo.com": _EMPTY_JSON, "html.duckduckgo.com": _HTML_FIXTURE})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            websearch.rechercher("python trace pub", on_etape=on_etape)
        filtree = next(e for e in etapes if e["etape"] == "recherche_filtree")
        self.assertEqual(filtree["nombre_ecarte"], 2)
        self.assertEqual(filtree["raison"], "publicite")

    def test_cache_emet_recherche_cache_jamais_recherche_debut(self):
        """LE point sensible de l'audit : un résultat servi depuis le cache
        n'a envoyé AUCUNE requête réseau cette fois — la trace ne doit
        jamais laisser croire le contraire."""
        instant_json = json.dumps({
            "Abstract": "Python.", "AbstractSource": "Wikipedia",
            "AbstractURL": "https://fr.wikipedia.org/wiki/Python", "RelatedTopics": [],
        })
        fake = _urlopen_router({"api.duckduckgo.com": instant_json})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            websearch.rechercher("python trace cache")  # remplit le cache, sans trace

            etapes, on_etape = self._capturer()
            websearch.rechercher("python trace cache", on_etape=on_etape)

        types = [e["etape"] for e in etapes]
        self.assertNotIn("recherche_debut", types)
        self.assertEqual(types, ["recherche_cache", "recherche_resultats"])
        self.assertEqual(etapes[1]["moteur"], "cache")

    def test_erreur_reseau_emet_recherche_erreur(self):
        etapes, on_etape = self._capturer()
        err = websearch.urllib.error.URLError("offline")
        fake = _urlopen_router({"api.duckduckgo.com": err, "html.duckduckgo.com": err})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            with self.assertRaises(RechercheWebErreur):
                websearch.rechercher("python trace erreur", on_etape=on_etape)
        self.assertEqual(etapes[-1]["etape"], "recherche_erreur")
        self.assertIn("message", etapes[-1])

    def test_structure_cassee_emet_recherche_erreur(self):
        etapes, on_etape = self._capturer()
        gros_corps = "<html><body>" + ("x" * 20000) + "</body></html>"
        fake = _urlopen_router({"api.duckduckgo.com": _EMPTY_JSON, "html.duckduckgo.com": gros_corps})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            with self.assertRaises(RechercheWebErreur):
                websearch.rechercher("python trace cassee", on_etape=on_etape)
        self.assertEqual(etapes[-1]["etape"], "recherche_erreur")

    def test_zero_resultat_legitime_emet_recherche_resultats_zero(self):
        etapes, on_etape = self._capturer()
        fake = _urlopen_router({"api.duckduckgo.com": _EMPTY_JSON, "html.duckduckgo.com": "<html>rien</html>"})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            websearch.rechercher("python trace vide", on_etape=on_etape)
        self.assertEqual(etapes[-1], {
            "etape": "recherche_resultats", "nombre": 0, "moteur": "ddg-html",
            "ms": etapes[-1]["ms"], "resultats": [],
        })

    def test_requete_longue_tronquee_dans_la_trace(self):
        etapes, on_etape = self._capturer()
        requete_longue = "python " * 100  # bien au-delà de TRACE_TEXTE_MAX
        fake = _urlopen_router({"api.duckduckgo.com": _EMPTY_JSON, "html.duckduckgo.com": "<html>rien</html>"})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            websearch.rechercher(requete_longue, on_etape=on_etape)
        debut = next(e for e in etapes if e["etape"] == "recherche_debut")
        self.assertLessEqual(len(debut["requete"]), websearch.TRACE_TEXTE_MAX + 1)
        self.assertTrue(debut["requete"].endswith("…"))

    def test_on_etape_defaillant_ne_casse_pas_la_recherche(self):
        """Une trace est un à-côté observable, pas une dépendance : un
        callback qui lève ne doit jamais faire échouer la recherche elle-même."""
        instant_json = json.dumps({
            "Abstract": "Python.", "AbstractSource": "Wikipedia",
            "AbstractURL": "https://fr.wikipedia.org/wiki/Python", "RelatedTopics": [],
        })
        fake = _urlopen_router({"api.duckduckgo.com": instant_json})

        def _on_etape_casse(_e):
            raise RuntimeError("callback cassé")

        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            resultats = websearch.rechercher("python trace robuste", on_etape=_on_etape_casse)
        self.assertEqual(len(resultats), 1)

    def test_sans_on_etape_comportement_inchange(self):
        """`on_etape=None` (défaut) : coût nul, comportement identique à
        avant cette phase — non-régression du contrat existant."""
        instant_json = json.dumps({
            "Abstract": "Python.", "AbstractSource": "Wikipedia",
            "AbstractURL": "https://fr.wikipedia.org/wiki/Python", "RelatedTopics": [],
        })
        fake = _urlopen_router({"api.duckduckgo.com": instant_json})
        with mock.patch.object(websearch.urllib.request, "urlopen", side_effect=fake):
            resultats = websearch.rechercher("python sans trace")
        self.assertEqual(len(resultats), 1)


class VerifierCitationsTest(unittest.TestCase):
    """`_verifier_citations` : calcul PUR (core.citations), déplacé AVANT la
    persistance depuis cette phase — cf. sa docstring pour le pourquoi."""

    def setUp(self):
        self.resultats = [ResultatWeb(rang=1, titre="Python", url="https://python.org/", extrait="", moteur="ddg-html")]

    def test_reponse_vide_rend_none(self):
        self.assertIsNone(chat_router._verifier_citations("", self.resultats, "", set()))

    def test_citation_hors_plage_detectee(self):
        rapport = chat_router._verifier_citations("D'après [7], c'est vrai.", self.resultats, "", set())
        self.assertTrue(rapport.a_des_anomalies())
        self.assertEqual(rapport.rangs_hors_plage, [7])

    def test_citation_valide_aucune_anomalie(self):
        rapport = chat_router._verifier_citations("D'après [1], c'est vrai.", self.resultats, "", set())
        self.assertFalse(rapport.a_des_anomalies())

    def test_aucune_citation_malgre_contexte_signalee(self):
        rapport = chat_router._verifier_citations("Je ne sais pas.", self.resultats, "", set())
        self.assertTrue(rapport.aucune_citation_malgre_contexte)
        self.assertFalse(rapport.a_des_anomalies())  # signal faible, PAS une anomalie


class VerifierCitationsSansRechercheTest(unittest.TestCase):
    """Correctif phase 3.5 (suite) : sans recherche ce tour, `[n]` n'est plus
    un marqueur de citation (aucun contrat injecté au prompt, cf.
    `_construire_web_ctx`) — un `[1]` de note de bas de page ne doit produire
    aucune étape `citations_invalides`, contrairement à une URL inconnue."""

    def test_note_de_bas_de_page_sans_web_aucune_anomalie(self):
        rapport = chat_router._verifier_citations("Voir la référence [1] du cours.", [], "", set())
        self.assertFalse(rapport.a_des_anomalies())
        trace = chat_router._construire_trace_finale([], rapport, False)
        self.assertEqual(trace, [])

    def test_url_inconnue_sans_web_reste_signalee(self):
        rapport = chat_router._verifier_citations(
            "Réf [1] et voir https://invente.example/page.", [], "", set(),
        )
        self.assertEqual(rapport.rangs_hors_plage, [])
        self.assertEqual(rapport.urls_non_reconnues, ["https://invente.example/page"])
        trace = chat_router._construire_trace_finale([], rapport, False)
        self.assertEqual([e["etape"] for e in trace], ["citations_invalides"])

    def test_avec_recherche_rang_hors_plage_toujours_signale(self):
        """Non-régression phase 2, rejouée depuis le point d'entrée du router."""
        resultats = [ResultatWeb(rang=1, titre="Python", url="https://python.org/", extrait="", moteur="ddg-html")]
        rapport = chat_router._verifier_citations("D'après [7], c'est vrai.", resultats, "", set())
        self.assertEqual(rapport.rangs_hors_plage, [7])
        self.assertTrue(rapport.a_des_anomalies())


class TraceEtCitationsTest(unittest.TestCase):
    """`_construire_trace_finale` / `_finaliser_citations_et_trace` : la
    trace persistée réunit les étapes de recherche (poussées en direct
    pendant le tour, vide s'il n'y en a pas eu) et l'étape
    `citations_invalides` (calculée après) — désormais construite QUELLE QUE
    SOIT la présence d'une recherche ce tour (correctif du suivi de phase 3 :
    @web est un override rare, la majorité silencieuse des tours SANS
    recherche est précisément là où une URL inventée de mémoire doit être
    détectée)."""

    def setUp(self):
        self.resultats = [ResultatWeb(rang=1, titre="Python", url="https://python.org/", extrait="", moteur="ddg-html")]
        self.etapes_recherche = [
            {"etape": "recherche_debut", "requete": "python", "moteur": "ddg-instant"},
            {"etape": "recherche_resultats", "nombre": 1, "moteur": "ddg-instant", "ms": 100, "resultats": []},
        ]

    def test_pas_d_anomalie_trace_inchangee(self):
        rapport = chat_router._verifier_citations("D'après [1].", self.resultats, "", set())
        trace = chat_router._construire_trace_finale(self.etapes_recherche, rapport, True)
        self.assertEqual(trace, self.etapes_recherche)

    def test_anomalie_avec_recherche_verifiees_contre_recherche(self):
        """Affirmation FORTE : une recherche a rendu des résultats, l'URL
        n'en fait pas partie."""
        rapport = chat_router._verifier_citations("D'après [7].", self.resultats, "", set())
        trace = chat_router._construire_trace_finale(self.etapes_recherche, rapport, True)
        self.assertEqual(trace[-1], {
            "etape": "citations_invalides", "rangs": [7], "urls": [],
            "verifiees_contre": "recherche",
        })

    def test_anomalie_sans_recherche_verifiees_contre_aucune_source(self):
        """Affirmation FAIBLE : aucun résultat de recherche à comparer — le
        lien n'est pas prouvé faux, seulement NON VÉRIFIÉ."""
        rapport = chat_router._verifier_citations("Voir https://invente.example/page.", [], "", set())
        trace = chat_router._construire_trace_finale([], rapport, False)
        self.assertEqual(trace, [{
            "etape": "citations_invalides", "rangs": [],
            "urls": ["https://invente.example/page"],
            "verifiees_contre": "aucune_source",
        }])

    def test_trace_peut_ne_contenir_que_l_etape_citations_invalides(self):
        """Item 2 : aucune étape de recherche, uniquement citations_invalides."""
        rapport = chat_router._verifier_citations("Voir https://invente.example/.", [], "", set())
        trace = chat_router._construire_trace_finale([], rapport, False)
        self.assertEqual([e["etape"] for e in trace], ["citations_invalides"])

    def test_rapport_none_trace_inchangee(self):
        trace = chat_router._construire_trace_finale(self.etapes_recherche, None, True)
        self.assertEqual(trace, self.etapes_recherche)

    def test_borne_le_nombre_d_etapes(self):
        """Volume (tâche §4) : une trace surdimensionnée est effectivement
        tronquée à TRACE_MAX_ETAPES, citations_invalides compris dans le compte."""
        from core.websearch import TRACE_MAX_ETAPES
        etapes_surdimensionnees = [{"etape": "recherche_resultats", "nombre": 0} for _ in range(TRACE_MAX_ETAPES + 5)]
        rapport = chat_router._verifier_citations("D'après [7].", self.resultats, "", set())
        trace = chat_router._construire_trace_finale(etapes_surdimensionnees, rapport, True)
        self.assertEqual(len(trace), TRACE_MAX_ETAPES)

    def test_finaliser_sans_recherche_construit_quand_meme_la_trace(self):
        """LE correctif : une anomalie SANS @web ce tour doit désormais
        produire une trace persistée/affichable, pas seulement un log."""
        sources, trace = chat_router._finaliser_citations_et_trace(
            "conv-1", "Voir https://invente.example/.", [], "", set(), [],
        )
        self.assertEqual(trace[-1]["etape"], "citations_invalides")
        self.assertEqual(trace[-1]["verifiees_contre"], "aucune_source")

    def test_finaliser_avec_recherche_construit_la_trace(self):
        sources, trace = chat_router._finaliser_citations_et_trace(
            "conv-1", "D'après [7].", self.resultats, "", set(), self.etapes_recherche,
        )
        self.assertEqual(trace[-1]["etape"], "citations_invalides")
        self.assertEqual(trace[-1]["verifiees_contre"], "recherche")

    def test_finaliser_url_rag_non_signalee_meme_sans_recherche(self):
        """Une URL issue d'un fichier RAG attaché n'est jamais une anomalie —
        avec ou sans recherche web, cf. contrainte « inchangé par ailleurs »."""
        sources, trace = chat_router._finaliser_citations_et_trace(
            "conv-1", "Voir https://mon-cours.example/chap3.", [], "",
            {"https://mon-cours.example/chap3"}, [],
        )
        self.assertEqual(trace, [])

    def test_finaliser_sans_url_ni_rang_aucune_etape(self):
        """Une réponse sans URL ni [n] ne produit aucune étape, donc aucun
        panneau — rétrocompatibilité et bruit évités."""
        sources, trace = chat_router._finaliser_citations_et_trace(
            "conv-1", "Une réponse banale, sans rien de spécial.", [], "", set(), [],
        )
        self.assertEqual(trace, [])

    def test_finaliser_signal_faible_logue_pas_dans_la_trace(self):
        with self.assertLogs(chat_router.logger, level="WARNING") as capture:
            sources, trace = chat_router._finaliser_citations_et_trace(
                "conv-1", "Je ne sais pas.", self.resultats, "", set(), self.etapes_recherche,
            )
        self.assertEqual(trace, self.etapes_recherche)  # pas d'étape ajoutée
        self.assertTrue(any("sans aucune citation" in m for m in capture.output))


def _live():
    query = "Python programming"
    print(f"Query: {query}")
    etapes: list = []
    resultats = websearch.rechercher(query, on_etape=etapes.append)
    print(f"\n{len(etapes)} étape(s) de trace :")
    for e in etapes:
        print(f"  {e}")
    print(f"\n{len(resultats)} résultat(s) :")
    for r in resultats:
        print(f"  [{r.rang}] ({r.moteur}) {r.titre}")
        print(f"      {r.url}")
    print("\nFormaté pour le LLM :")
    print(websearch.formater_pour_llm(resultats))


if __name__ == "__main__":
    if "--live" in sys.argv:
        _live()
    else:
        unittest.main()
