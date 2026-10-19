#!/usr/bin/env python3
"""Tests de la lecture DIRECTE d'une URL collée dans le message
(`core.webcontent.lire_urls`) — sans DuckDuckGo.

Ce que ces tests verrouillent, dans l'ordre où un échec ferait le plus de
dégâts :

  1. **Aucune adresse locale ou privée n'est jamais contactée** (localhost,
     127.x, 10.x, 172.16-31.x, 192.168.x, 169.254.x, IPv6 de bouclage, nom
     qui RÉSOUT vers une adresse privée, redirection vers une adresse
     privée). L'URL vient d'un texte que l'utilisateur a pu copier d'une page
     hostile : sans ce refus, coller un lien suffirait à faire interroger le
     backend lui-même (`127.0.0.1:8000`) ou la box du réseau local par Épure.
  2. **Un échec est NOMMÉ, jamais silencieux** — page protégée par une
     connexion (401, redirection vers /login, formulaire de mot de passe
     sans contenu), 403, paywall, délai dépassé : chacun a sa raison lisible,
     qui part dans la trace persistée.
  3. **Mêmes budgets de temps que la phase 4** (`_TIMEOUT_PAGE_S`,
     `_TIMEOUT_ETAPE_S`) — pas une seconde série de constantes. Le budget de
     CARACTÈRES, lui, suit le contexte chargé : `test_budget_lecture_url.py`.

Isolé du réseau aux points d'entrée que le module expose pour ça :
`_ouvrir` (l'appel HTTP), `socket.getaddrinfo` (la résolution DNS) et
`_vecteurs_partages` (le store vectoriel partagé).

Usage :
    python test_lecture_url.py
"""

import os
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — avant tout import de core.*

import numpy as np  # noqa: E402

from core import webcontent  # noqa: E402
from core.citations import extraire_urls_ordonnees  # noqa: E402
from core.embedding_install import EmbeddingIndisponible  # noqa: E402

_IP_PUBLIQUE = "93.184.215.14"


def _dns_public(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_IP_PUBLIQUE, 443))]


def _dns_vers(ip):
    def _resoudre(*_args, **_kwargs):
        famille = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(famille, socket.SOCK_STREAM, 6, "", (ip, 443))]
    return _resoudre


class _FausseReponse:
    """Double minimal d'une réponse `urllib` : ce que `_recuperer_page_detaillee`
    lit, rien de plus."""

    def __init__(self, corps: str, statut=200, type_contenu="text/html; charset=utf-8",
                 url_finale=None):
        self._corps = corps.encode("utf-8")
        self.status = statut
        self.headers = {"Content-Type": type_contenu}
        self._url_finale = url_finale

    def geturl(self):
        return self._url_finale

    def read(self, n=-1):
        return self._corps if n is None or n < 0 else self._corps[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _article(nb_paragraphes=12, mot="contenu"):
    paragraphes = "".join(
        f"<p>Paragraphe {i} de l'article : {' '.join([mot] * 40)} fin du paragraphe {i}.</p>"
        for i in range(nb_paragraphes)
    )
    return (
        "<html><head><title>Un article de test</title></head><body>"
        "<nav><a href='/'>Accueil</a></nav>"
        f"<article><h1>Un article de test</h1>{paragraphes}</article>"
        "<footer>Pied de page</footer></body></html>"
    )


_PAGE_LOGIN = (
    "<html><head><title>Connexion</title></head><body>"
    "<form action='/session' method='post'>"
    "<label>Identifiant</label><input type='text' name='user'>"
    "<label>Mot de passe</label><input type='password' name='pwd'>"
    "<button type='submit'>Se connecter</button></form></body></html>"
)

_PAGE_PAYWALL = _article().replace(
    "<head>",
    "<head><script type='application/ld+json'>"
    '{"@type": "NewsArticle", "isAccessibleForFree": false}</script>',
)


class _FakeVectorStore:
    def __init__(self, motif="PERTINENT"):
        self.motif = motif
        self.appels = []

    def embed_texts(self, textes):
        self.appels.append(list(textes))
        return np.array([[1.0, 0.0] if self.motif in t else [0.0, 1.0] for t in textes],
                        dtype=np.float32)


class _VectorStoreIndisponible:
    def embed_texts(self, textes):
        raise EmbeddingIndisponible({"état": "absent", "detail": "modèle non téléchargé"})


class _Base(unittest.TestCase):
    def setUp(self):
        for cible, attribut, valeur in (
            (webcontent.socket, "getaddrinfo", _dns_public),
            (webcontent, "_vecteurs_partages", lambda: _FakeVectorStore()),
        ):
            patcher = mock.patch.object(cible, attribut, valeur)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _lire(self, urls, question="", ouvrir=None, rang_de_depart=0):
        etapes = []
        with mock.patch.object(webcontent, "_ouvrir", side_effect=ouvrir) as faux_ouvrir:
            lectures = webcontent.lire_urls(urls, question, rang_de_depart=rang_de_depart,
                                            on_etape=etapes.append)
        return lectures, etapes, faux_ouvrir


class ExtractionUrlsTest(unittest.TestCase):
    def test_ordre_d_apparition_sans_doublon_ponctuation_retiree(self):
        texte = ("Lis https://b.example/page. puis https://a.example/x?y=1, "
                 "et encore https://b.example/page !")
        self.assertEqual(extraire_urls_ordonnees(texte),
                         ["https://b.example/page", "https://a.example/x?y=1"])

    def test_extraire_urls_reste_l_ensemble_des_memes_urls(self):
        from core.citations import extraire_urls
        texte = "voir http://a.example/ et https://b.example/c."
        self.assertEqual(extraire_urls(texte), set(extraire_urls_ordonnees(texte)))


class AdressesRefuseesTest(_Base):
    """Refus AVANT tout appel réseau : `_ouvrir` ne doit jamais être appelé."""

    INTERDITES = (
        "http://localhost:8000/instance/config",
        "http://LOCALHOST/",
        "http://api.localhost/",
        "http://127.0.0.1:8000/pair",
        "http://127.8.9.10/",
        "http://10.0.0.1/",
        "http://172.16.5.4/",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8000/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
    )

    def test_adresses_locales_et_privees_refusees_sans_appel_reseau(self):
        for url in self.INTERDITES:
            with self.subTest(url=url):
                lectures, etapes, faux_ouvrir = self._lire([url])
                faux_ouvrir.assert_not_called()
                self.assertEqual(lectures[0].statut, "refusée")
                self.assertIsNone(lectures[0].resultat)
                self.assertIn("adresse locale ou privée", lectures[0].raison)
                self.assertEqual(etapes[-1]["etape"], "lecture_url")
                self.assertEqual(etapes[-1]["statut"], "refusée")

    def test_nom_de_domaine_qui_resout_vers_une_adresse_privee_refuse(self):
        for ip in ("192.168.0.10", "127.0.0.1", "10.1.2.3", "fe80::1"):
            with self.subTest(ip=ip), \
                 mock.patch.object(webcontent.socket, "getaddrinfo", _dns_vers(ip)):
                lectures, _etapes, faux_ouvrir = self._lire(["https://piege.example/"])
                faux_ouvrir.assert_not_called()
                self.assertEqual(lectures[0].statut, "refusée")

    def test_redirection_vers_adresse_privee_refusee(self):
        """`urllib` suit les redirections tout seul : sans contrôle à CHAQUE
        saut, une page publique pourrait rebondir vers 127.0.0.1."""
        gestionnaire = webcontent._RedirectionVerifiee()
        req = urllib.request.Request("https://public.example/")
        with self.assertRaises(webcontent._AdresseRefusee):
            gestionnaire.redirect_request(req, None, 302, "Found", {},
                                          "http://127.0.0.1:8000/pair")

    def test_redirection_vers_adresse_publique_suivie(self):
        gestionnaire = webcontent._RedirectionVerifiee()
        req = urllib.request.Request("https://public.example/")
        nouvelle = gestionnaire.redirect_request(req, None, 302, "Found", {},
                                                 "https://autre.example/page")
        self.assertEqual(nouvelle.full_url, "https://autre.example/page")

    def test_schema_non_http_refuse(self):
        lectures, _etapes, faux_ouvrir = self._lire(["file:///C:/Windows/win.ini"])
        faux_ouvrir.assert_not_called()
        self.assertEqual(lectures[0].statut, "refusée")


class ProprietesIpaddressTest(_Base):
    """Refus par les PROPRIÉTÉS d'`ipaddress` (is_private, is_loopback,
    is_link_local, is_reserved, is_multicast, is_unspecified) + l'espace
    partagé CGNAT, qu'aucune d'elles ne couvre — pas par une liste de plages
    écrite à la main. Mêmes règles en clair, après DNS et à chaque redirection.
    """

    CAS = (
        "http://[fc00::1]/",          # ULA IPv6 — is_private
        "http://[fe80::1]/",          # lien local IPv6 — is_link_local
        "http://100.64.0.1/",         # CGNAT (RFC 6598) — aucune des six propriétés
        "http://[::ffff:10.0.0.1]/",  # IPv4 privée encapsulée dans une IPv6
        "http://224.0.0.1/",          # multicast — pourtant `is_global` vrai
        "http://2130706433/",         # 127.0.0.1 en décimal, sans DNS
        "http://0x7f000001/",         # 127.0.0.1 en hexadécimal
        "http://127.1/",              # forme courte
    )

    def test_adresses_ecrites_en_clair(self):
        # DNS factice « public » : ces formes doivent être refusées SANS
        # résolution — sinon `2130706433` passerait pour un nom de domaine.
        for url in self.CAS:
            with self.subTest(url=url):
                lectures, _etapes, faux_ouvrir = self._lire([url])
                faux_ouvrir.assert_not_called()
                self.assertEqual(lectures[0].statut, "refusée", lectures[0].raison)
                self.assertIn("adresse locale ou privée", lectures[0].raison)

    def test_apres_resolution_dns(self):
        for ip in ("fc00::1", "fe80::1", "100.64.0.1", "::ffff:10.0.0.1", "224.0.0.1"):
            with self.subTest(ip=ip), \
                 mock.patch.object(webcontent.socket, "getaddrinfo", _dns_vers(ip)):
                lectures, _etapes, faux_ouvrir = self._lire(["https://piege.example/"])
                faux_ouvrir.assert_not_called()
                self.assertEqual(lectures[0].statut, "refusée")

    def test_a_chaque_redirection(self):
        gestionnaire = webcontent._RedirectionVerifiee()
        req = urllib.request.Request("https://public.example/")
        for cible in self.CAS:
            with self.subTest(cible=cible), self.assertRaises(webcontent._AdresseRefusee):
                gestionnaire.redirect_request(req, None, 302, "Found", {}, cible)

    def test_ipv4_publique_encapsulee_autorisee(self):
        """L'encapsulation est jugée sur l'IPv4 portée, sur TOUTES les versions
        de Python : avant 3.13, `::ffff:0:0/96` entier est « privé/réservé »,
        ce qui refuserait aussi une adresse publique."""
        self.assertIsNone(webcontent.adresse_refusee(f"http://[::ffff:{_IP_PUBLIQUE}]/"))

    def test_adresse_publique_autorisee(self):
        self.assertIsNone(webcontent.adresse_refusee(f"http://{_IP_PUBLIQUE}/"))
        self.assertIsNone(webcontent.adresse_refusee("https://site.example/page"))

    def test_schema_non_http_refuse_par_adresse_refusee(self):
        self.assertIn("schéma", webcontent.adresse_refusee("ftp://site.example/"))


class EchecsNommesTest(_Base):
    """Chaque échec porte une raison lisible — jamais un `None` muet."""

    def _echec(self, ouvrir, url="https://site.example/article"):
        lectures, etapes, _ = self._lire([url], "question", ouvrir=ouvrir)
        self.assertEqual(lectures[0].statut, "échec")
        self.assertIsNone(lectures[0].resultat)
        self.assertEqual(etapes[-1]["etape"], "lecture_url")
        self.assertEqual(etapes[-1]["statut"], "échec")
        self.assertEqual(etapes[-1]["raison"], lectures[0].raison)
        self.assertEqual(etapes[-1]["passages_retenus"], 0)
        return lectures[0].raison

    def test_page_protegee_par_login_http_401(self):
        def ouvrir(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        self.assertIn("connexion", self._echec(ouvrir))

    def test_page_protegee_par_login_redirection_vers_page_de_connexion(self):
        def ouvrir(req, timeout):
            return _FausseReponse(_PAGE_LOGIN,
                                  url_finale="https://site.example/login?next=%2Farticle")
        self.assertIn("connexion", self._echec(ouvrir))

    def test_page_protegee_par_login_formulaire_de_mot_de_passe_servi_en_200(self):
        def ouvrir(req, timeout):
            return _FausseReponse(_PAGE_LOGIN, url_finale="https://site.example/article")
        self.assertIn("connexion", self._echec(ouvrir))

    def test_http_403(self):
        def ouvrir(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        self.assertIn("403", self._echec(ouvrir))

    def test_paywall_http_402(self):
        def ouvrir(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, None)
        self.assertIn("abonnés", self._echec(ouvrir))

    def test_paywall_marque_par_schema_org(self):
        def ouvrir(req, timeout):
            return _FausseReponse(_PAGE_PAYWALL, url_finale="https://site.example/article")
        self.assertIn("abonnés", self._echec(ouvrir))

    def test_timeout(self):
        def ouvrir(req, timeout):
            raise urllib.error.URLError(socket.timeout("timed out"))
        self.assertIn("délai", self._echec(ouvrir))

    def test_timeout_brut(self):
        def ouvrir(req, timeout):
            raise TimeoutError("timed out")
        self.assertIn("délai", self._echec(ouvrir))

    def test_contenu_non_html(self):
        def ouvrir(req, timeout):
            return _FausseReponse("%PDF-1.7", type_contenu="application/pdf",
                                  url_finale="https://site.example/a.pdf")
        self.assertIn("application/pdf", self._echec(ouvrir))

    def test_page_sans_texte_extractible(self):
        def ouvrir(req, timeout):
            return _FausseReponse("<html><body><div id='app'></div></body></html>",
                                  url_finale="https://site.example/app")
        self.assertIn("aucun texte", self._echec(ouvrir))


class LectureReussieTest(_Base):
    def test_page_lue_trace_et_resultat_citable(self):
        appels = []

        def ouvrir(req, timeout):
            appels.append((req.full_url, timeout))
            return _FausseReponse(_article(), url_finale=req.full_url)

        lectures, etapes, _ = self._lire(["https://site.example/article"],
                                         "de quoi parle cet article", ouvrir=ouvrir,
                                         rang_de_depart=2)
        lecture = lectures[0]
        self.assertEqual(lecture.statut, "succès")
        self.assertEqual(lecture.raison, "")
        self.assertGreater(lecture.passages, 0)
        # Rang APRÈS ceux déjà attribués ce tour (résultats @web éventuels).
        self.assertEqual(lecture.resultat.rang, 3)
        self.assertEqual(lecture.resultat.url, "https://site.example/article")
        self.assertEqual(lecture.resultat.titre, "Un article de test")
        self.assertEqual(lecture.resultat.moteur, "lecture-directe")
        # Même budget de temps par page que la phase 4.
        self.assertEqual(appels, [("https://site.example/article", webcontent._TIMEOUT_PAGE_S)])
        etape = etapes[-1]
        self.assertEqual(etape["etape"], "lecture_url")
        self.assertEqual(etape["url"], "https://site.example/article")
        self.assertEqual(etape["statut"], "succès")
        self.assertEqual(etape["passages_retenus"], lecture.passages)
        self.assertIn("ms", etape)

    def test_budget_de_caracteres_plancher_sans_contexte_connu(self):
        """Sans contexte chargé connu, le plancher de `budget_lecture` — et
        plus les 800 caractères de la phase 4 (cf. test_budget_lecture_url)."""
        def ouvrir(req, timeout):
            return _FausseReponse(_article(nb_paragraphes=60), url_finale=req.full_url)

        lectures, _etapes, _ = self._lire(["https://site.example/long"], "question",
                                          ouvrir=ouvrir)
        self.assertLessEqual(len(lectures[0].resultat.extrait),
                             webcontent._BUDGET_LECTURE_PLANCHER)

    def test_reclassement_par_la_question(self):
        """Les passages retenus sont ceux qui répondent à la question, comme
        en phase 4 — pas simplement le début de la page.

        Ce test devait réduire la sélection à UN passage : avec trois, remis
        dans l'ordre de la page puis tronqués à 800 caractères, le passage
        pertinent tombait sous la coupe. Le budget proportionnel au contexte
        a levé cette limite (`test_budget_lecture_url.FinDePageTest`)."""
        html = _article(nb_paragraphes=30, mot="banal").replace(
            "Paragraphe 25 de l'article", "Paragraphe 25 PERTINENT de l'article")

        def ouvrir(req, timeout):
            return _FausseReponse(html, url_finale=req.full_url)

        lectures, _etapes, _ = self._lire(["https://site.example/a"], "PERTINENT ?",
                                          ouvrir=ouvrir)
        self.assertIn("PERTINENT", lectures[0].resultat.extrait)

    def test_embedding_indisponible_debut_de_page_garde_et_signale(self):
        def ouvrir(req, timeout):
            return _FausseReponse(_article(), url_finale=req.full_url)

        with mock.patch.object(webcontent, "_vecteurs_partages",
                               lambda: _VectorStoreIndisponible()):
            lectures, etapes, _ = self._lire(["https://site.example/a"], "question",
                                             ouvrir=ouvrir)
        self.assertEqual(lectures[0].statut, "succès")
        self.assertIn("Paragraphe 0", lectures[0].resultat.extrait)
        self.assertIn("reclassement_indisponible", [e["etape"] for e in etapes])

    def test_rangs_contigus_seulement_pour_les_pages_lues(self):
        def ouvrir(req, timeout):
            if "ko" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
            return _FausseReponse(_article(), url_finale=req.full_url)

        lectures, _etapes, _ = self._lire(
            ["https://site.example/ok1", "https://site.example/ko", "https://site.example/ok2"],
            "question", ouvrir=ouvrir,
        )
        self.assertEqual([lec.statut for lec in lectures], ["succès", "échec", "succès"])
        self.assertEqual([lec.resultat.rang for lec in lectures if lec.resultat], [1, 2])

    def test_nombre_d_urls_borne_comme_la_phase_4(self):
        def ouvrir(req, timeout):
            return _FausseReponse(_article(), url_finale=req.full_url)

        urls = [f"https://site.example/p{i}" for i in range(webcontent._MAX_PAGES_ENRICHIES + 3)]
        lectures, _etapes, faux_ouvrir = self._lire(urls, "question", ouvrir=ouvrir)
        self.assertEqual(len(lectures), webcontent._MAX_PAGES_ENRICHIES)
        self.assertEqual(faux_ouvrir.call_count, webcontent._MAX_PAGES_ENRICHIES)


class BudgetGlobalTest(_Base):
    def test_page_trop_lente_abandonnee_au_budget_de_l_etape(self):
        def ouvrir(req, timeout):
            time.sleep(1.5)
            return _FausseReponse(_article(), url_finale=req.full_url)

        with mock.patch.object(webcontent, "_TIMEOUT_ETAPE_S", 0.2):
            t0 = time.time()
            lectures, etapes, _ = self._lire(["https://site.example/lente"], "q", ouvrir=ouvrir)
            duree = time.time() - t0
        self.assertLess(duree, 1.0)
        self.assertEqual(lectures[0].statut, "échec")
        self.assertIn("délai", lectures[0].raison)
        self.assertEqual(etapes[-1]["statut"], "échec")


if __name__ == "__main__":
    unittest.main(verbosity=2)
