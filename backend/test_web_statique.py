#!/usr/bin/env python3
"""Tests de l'interface servie par FastAPI (étape A du paquet distribué).

Dans le paquet envoyé à un proche, il n'y a plus de serveur Vite : FastAPI sert
lui-même ``frontend/dist``. Ce fichier verrouille les trois choses qui rendent ce
service correct, et dont deux sont contre-intuitives.

  1. **Pas de catch-all.** ``module_workshop._remount`` fait un
     ``app.include_router`` qui AJOUTE EN FIN de ``app.router.routes``, et
     Starlette sert la première route qui correspond. Un mount sur ``/`` posé au
     démarrage passerait donc devant les routes de tout module installé ensuite,
     et ``index.html`` répondrait à la place du module. Or l'installation depuis
     le catalogue est justement ce que le proche garde. C'est l'objet de
     :class:`PasDeCatchAllTest`, qui installe une route APRÈS le montage statique
     et vérifie qu'elle répond encore.
  2. **La page est publique, l'API non.** Le middleware exige un token partout
     sauf ``/health`` et ``/pair`` ; la page HTML doit pourtant se charger avant
     que son JavaScript ait pu s'appairer. L'exemption est donc élargie aux
     fichiers statiques — et à eux seuls. :class:`SurfacePubliqueTest` affirme
     les deux moitiés : ce qui s'ouvre, et ce qui ne s'ouvre surtout pas.
  3. **Le service est éteint sans ``index.html``.** C'est le mode développement,
     et c'est aussi ce qui rend la suite déterministe : ``_test_env`` pose
     ``EPURE_WEB_DIR`` sur un temporaire vide, sinon le comportement des tests
     dépendrait de la présence d'un ``npm run build`` sur le poste.

Ces tests n'importent PAS ``main.app`` pour le montage : ils fabriquent une app
neuve par test. ``_register_web`` prend l'app en paramètre exactement pour ça.
En revanche ``_WEB_EXEMPT_PATHS`` est un état de module partagé avec le vrai
middleware : il est sauvegardé et restauré autour de chaque test, sans quoi ce
fichier ouvrirait des chemins publics dans les tests qui le suivent (l'ordre de
``unittest discover`` mettrait `test_zz_` après, mais pas `test_workshop_paths`).

Usage :
    python test_web_statique.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


def _fabriquer_dist(racine: Path) -> Path:
    """Fabrique un ``dist/`` minimal mais réaliste (structure d'un build vite).

    Les noms d'assets portent un hash comme ceux de vite : c'est ce qui rend
    l'exemption par préfixe nécessaire côté ``/_assets/`` — on ne peut pas les
    énumérer à l'avance dans le code, seulement sur le disque.
    """
    dist = racine / "dist"
    (dist / "_assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><head><script type="module" crossorigin '
        'src="/_assets/index-ABC123.js"></script></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (dist / "icons.svg").write_text("<svg id='icons'/>", encoding="utf-8")
    (dist / "_assets" / "index-ABC123.js").write_text(
        "console.log('epure')", encoding="utf-8"
    )
    (dist / "_assets" / "index-DEF456.css").write_text(":root{}", encoding="utf-8")
    return dist


class _BaseWeb(unittest.TestCase):
    """Isole l'état de module partagé et le dossier web, par test."""

    def setUp(self):
        self._exempt_original = set(main._WEB_EXEMPT_PATHS)
        self._web_original = os.environ.get("EPURE_WEB_DIR")
        self._tmp = tempfile.TemporaryDirectory(prefix="epure-test-dist-")
        self.racine = Path(self._tmp.name)

    def tearDown(self):
        main._WEB_EXEMPT_PATHS.clear()
        main._WEB_EXEMPT_PATHS.update(self._exempt_original)
        if self._web_original is None:
            os.environ.pop("EPURE_WEB_DIR", None)
        else:
            os.environ["EPURE_WEB_DIR"] = self._web_original
        self._tmp.cleanup()

    def _monter(self, dist: Path) -> tuple[FastAPI, dict]:
        os.environ["EPURE_WEB_DIR"] = str(dist)
        app = FastAPI()
        infos = main._register_web(app)
        return app, infos


class ServiceStatiqueTest(_BaseWeb):
    def test_eteint_sans_index_html(self):
        """Dossier absent ou sans index.html → rien n'est monté, rien ne casse.

        C'est le mode développement (Vite sert l'interface) et le défaut de la
        suite de tests. Un échec ici signifierait que le backend d'Ilyann s'est
        mis à servir un frontend construit à son insu.
        """
        vide = self.racine / "dist-absent"
        app, infos = self._monter(vide)
        self.assertFalse(infos["servi"])
        self.assertEqual(infos["routes"], [])
        self.assertEqual(main._WEB_EXEMPT_PATHS, self._exempt_original)
        with TestClient(app) as c:
            self.assertEqual(c.get("/").status_code, 404)

    def test_sert_index_a_la_racine(self):
        dist = _fabriquer_dist(self.racine)
        app, infos = self._monter(dist)
        self.assertTrue(infos["servi"])
        with TestClient(app) as c:
            r = c.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("<div id=\"root\">", r.text)
            self.assertIn("text/html", r.headers["content-type"])

    def test_sert_les_fichiers_racine_et_les_assets(self):
        dist = _fabriquer_dist(self.racine)
        app, _ = self._monter(dist)
        with TestClient(app) as c:
            self.assertEqual(c.get("/index.html").status_code, 200)
            self.assertEqual(c.get("/favicon.svg").text, "<svg/>")
            self.assertEqual(c.get("/icons.svg").text, "<svg id='icons'/>")
            js = c.get("/_assets/index-ABC123.js")
            self.assertEqual(js.status_code, 200)
            self.assertEqual(js.text, "console.log('epure')")
            self.assertEqual(c.get("/_assets/index-DEF456.css").status_code, 200)

    def test_un_chemin_inconnu_ne_renvoie_pas_index_html(self):
        """L'absence de catch-all, vue depuis le client.

        Un mount sur ``/`` ferait répondre 200 + index.html à n'importe quoi, et
        une route d'API mal orthographiée deviendrait indétectable côté frontend
        (il recevrait du HTML là où il attend du JSON).
        """
        dist = _fabriquer_dist(self.racine)
        app, _ = self._monter(dist)
        with TestClient(app) as c:
            for chemin in ("/inconnu", "/modeles", "/_assets/absent.js", "/a/b/c"):
                with self.subTest(chemin=chemin):
                    self.assertEqual(c.get(chemin).status_code, 404)


class PasDeCatchAllTest(_BaseWeb):
    """Le piège que ce montage devait éviter : un module installé APRÈS.

    ``include_router`` ajoute en fin de liste et Starlette sert la première route
    qui correspond. Ce test reproduit la séquence réelle d'une installation
    depuis le catalogue (montage statique au démarrage, puis ``_remount``) et
    échoue si le service statique passe devant.
    """

    def test_une_route_ajoutee_apres_le_montage_repond_encore(self):
        dist = _fabriquer_dist(self.racine)
        app, _ = self._monter(dist)

        router = APIRouter()

        @router.get("/nouveau/ping")
        async def _ping():
            return {"ok": True}

        app.include_router(router, prefix="")  # comme module_workshop._remount

        with TestClient(app) as c:
            r = c.get("/nouveau/ping")
            self.assertEqual(r.status_code, 200, "le service statique masque le module")
            self.assertEqual(r.json(), {"ok": True})
            # …et la racine sert toujours l'interface.
            self.assertEqual(c.get("/").status_code, 200)


class SurfacePubliqueTest(_BaseWeb):
    """`main._est_public` : ce qui passe sans token, et ce qui ne passe jamais."""

    def test_exemptions_historiques_et_preflight(self):
        self.assertTrue(main._est_public("/health", "GET"))
        self.assertTrue(main._est_public("/pair", "GET"))
        self.assertTrue(main._est_public("/models", "OPTIONS"))

    def test_api_jamais_publique_service_statique_monte_ou_non(self):
        chemins = ("/models", "/modules", "/instance/config", "/workshop/generate",
                   "/settings/api-keys", "/history")
        for chemin in chemins:
            with self.subTest(chemin=chemin, monte=False):
                self.assertFalse(main._est_public(chemin, "GET"))

        dist = _fabriquer_dist(self.racine)
        self._monter(dist)
        for chemin in chemins:
            with self.subTest(chemin=chemin, monte=True):
                self.assertFalse(main._est_public(chemin, "GET"))

    def test_statique_public_seulement_quand_monte(self):
        for chemin in ("/", "/index.html", "/favicon.svg", "/_assets/index-ABC123.js"):
            with self.subTest(chemin=chemin):
                self.assertFalse(
                    main._est_public(chemin, "GET"),
                    "un chemin statique est public alors que rien n'est monté",
                )

        dist = _fabriquer_dist(self.racine)
        self._monter(dist)
        for chemin in ("/", "/index.html", "/favicon.svg", "/icons.svg",
                       "/_assets/index-ABC123.js", "/_assets/index-DEF456.css"):
            with self.subTest(chemin=chemin):
                self.assertTrue(main._est_public(chemin, "GET"))

    def test_un_chemin_avec_point_point_n_est_jamais_public(self):
        """Starlette ne normalise pas le chemin ASGI.

        ``/_assets/../models`` satisfait un test de préfixe naïf. Le routeur ne
        servirait pas ``/models`` pour autant, mais une décision
        d'authentification ne doit pas dépendre de ce raisonnement-là.
        """
        dist = _fabriquer_dist(self.racine)
        self._monter(dist)
        for chemin in ("/_assets/../models", "/../pair", "/_assets/..%2fmodels",
                       "/./../instance/config"):
            with self.subTest(chemin=chemin):
                self.assertFalse(main._est_public(chemin, "GET"))

    def test_le_prefixe_assets_ne_peut_pas_etre_un_id_de_module(self):
        """Pourquoi `_assets` et non `assets` (vite.config.ts).

        Le préfixe des assets est exempté d'auth par préfixe. Si un module
        pouvait s'appeler comme lui, ses routes le seraient aussi — un module
        monté sur le préfixe vide écrit ``@router.get("/<id>/…")`` à la main.
        L'underscore initial rend la collision impossible : c'est l'expression
        régulière des ids qui le garantit, donc c'est elle qu'on interroge.
        """
        from core.module_workshop import _ID_RE

        segment = main._WEB_ASSETS_PREFIX.strip("/")
        self.assertIsNone(
            _ID_RE.match(segment),
            f"{segment!r} est un id de module valide : un module pourrait poser "
            f"des routes sous un préfixe exempté d'authentification",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
