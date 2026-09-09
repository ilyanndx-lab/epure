#!/usr/bin/env python3
"""Les quatre routes de la phase 3 du module `encre`, à la frontière HTTP.

`test_encre_exemples.py` couvre déjà `ExemplesEncreEngine` et
`core.banque_dictee_encre` en détail — non-répétition comprise. Ce fichier
couvre ce que ce niveau-là ne peut pas voir : le CÂBLAGE des routes (codes de
statut, ordre des refus, forme des réponses).

**Aucun de ces tests n'appelle `pix2text-mfr`.** Une page « déjà transcrite »
est fabriquée en appelant directement `encre_engine.set_transcription` — comme
le ferait `POST /encre/pages/{id}/transcrire` en usage réel, sans construire
`hmer_engine` (coupé par `_test_env`, cf. `test_hmer.py`).

⚠️ **Les tests de dictée qui VALIDENT une expression touchent l'état de
progression PARTAGÉ** (`core.runtime.encre_exemples_engine`, un singleton pour
tout le process de découverte). C'est sans conséquence ici — aucun test
n'attend une banque « fraîche » — mais c'est pour ça que le drainage complet de
la banque (la vraie preuve de non-répétition) vit dans `test_encre_exemples.py`,
sur un moteur isolé, et pas ici.

Usage :
    python test_encre_entrainement.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les dossiers AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.runtime import encre_engine, encre_exemples_engine  # noqa: E402

TRAIT = {
    "couleur": "#111827",
    "taille": 3,
    "points": [{"x": 1.0, "y": 2.0, "pression": 0.5, "t": 0},
               {"x": 5.0, "y": 6.0, "pression": 0.6, "t": 8}],
}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54323))
        cls.token = get_api_token()

    def setUp(self):
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def creer_page(self, mode=None, strokes=None):
        corps = {"strokes": strokes if strokes is not None else [TRAIT]}
        if mode is not None:
            corps["mode"] = mode
        r = self.client.post("/encre/pages", json=corps, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def creer_page_transcrite(self, texte="x^{2}", mode="maths", strokes=None):
        """Une page « déjà transcrite », sans construire `hmer_engine`.

        Appelle directement le moteur — exactement ce que fait
        `POST /pages/{id}/transcrire` une fois le modèle exécuté — pour isoler
        le test des routes de phase 3 de celui, déjà couvert ailleurs, du
        déclenchement de la transcription elle-même.
        """
        page = self.creer_page(mode=mode, strokes=strokes)
        encre_engine.set_transcription(page["id"], texte, "pix2text-mfr", "v1")
        return page


class ACorrigerTest(_Base):
    """`GET /encre/entrainement/a_corriger`."""

    def test_liste_une_page_maths_transcrite(self):
        page = self.creer_page_transcrite(texte="x^{2}")
        r = self.client.get("/encre/entrainement/a_corriger", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        ids = [p["id"] for p in r.json()["pages"]]
        self.assertIn(page["id"], ids)
        entree = next(p for p in r.json()["pages"] if p["id"] == page["id"])
        self.assertEqual("x^{2}", entree["transcription"]["texte"])
        self.assertNotIn("strokes", entree)

    def test_exclut_une_page_non_transcrite(self):
        page = self.creer_page()  # pas de transcription
        r = self.client.get("/encre/entrainement/a_corriger", headers=self.auth)
        ids = [p["id"] for p in r.json()["pages"]]
        self.assertNotIn(page["id"], ids)

    def test_exclut_une_page_en_mode_lettres(self):
        page = self.creer_page_transcrite(mode="lettres")
        r = self.client.get("/encre/entrainement/a_corriger", headers=self.auth)
        ids = [p["id"] for p in r.json()["pages"]]
        self.assertNotIn(page["id"], ids)


class ValiderTranscriptionTest(_Base):
    """`POST /encre/pages/{page_id}/transcription/valider`."""

    def valider(self, page_id, texte=None):
        corps = {} if texte is None else {"texte": texte}
        return self.client.post(f"/encre/pages/{page_id}/transcription/valider",
                                 json=corps, headers=self.auth)

    def test_valider_tel_quel_sans_corps_reprend_le_texte_modele(self):
        page = self.creer_page_transcrite(texte="x^{2}")
        r = self.valider(page["id"])
        self.assertEqual(r.status_code, 200, r.text)
        corps = r.json()
        self.assertTrue(corps["exemple_id"])
        self.assertTrue(corps["page"]["transcription"]["validee_le"])

        exemple = encre_exemples_engine.get_exemple(corps["exemple_id"])
        self.assertEqual("x^{2}", exemple["texte_verite"])
        self.assertEqual("x^{2}", exemple["texte_modele"])
        self.assertEqual("correction", exemple["source"])

    def test_valider_avec_correction_garde_le_texte_modele_original(self):
        page = self.creer_page_transcrite(texte="x2 + l")
        r = self.valider(page["id"], texte="x^2 + 1")
        self.assertEqual(r.status_code, 200, r.text)
        exemple = encre_exemples_engine.get_exemple(r.json()["exemple_id"])
        self.assertEqual("x^2 + 1", exemple["texte_verite"])
        self.assertEqual("x2 + l", exemple["texte_modele"])

    def test_ne_bloque_jamais_une_re_correction(self):
        page = self.creer_page_transcrite(texte="x")
        premier = self.valider(page["id"])
        second = self.valider(page["id"], texte="x corrigé")
        self.assertEqual(200, premier.status_code)
        self.assertEqual(200, second.status_code)
        self.assertNotEqual(premier.json()["exemple_id"], second.json()["exemple_id"])

    def test_page_sans_transcription_est_refusee_en_400(self):
        page = self.creer_page()
        r = self.valider(page["id"])
        self.assertEqual(400, r.status_code)

    def test_page_videe_apres_transcription_est_refusee_en_400(self):
        page = self.creer_page_transcrite(texte="x", strokes=[TRAIT])
        self.client.put(f"/encre/pages/{page['id']}", json={"strokes": []},
                         headers=self.auth)
        r = self.valider(page["id"])
        self.assertEqual(400, r.status_code)
        self.assertIn("tracé", r.json()["detail"])

    def test_page_absente_404(self):
        r = self.valider("0" * 32)
        self.assertEqual(404, r.status_code)


class DicteeInverseeRouterTest(_Base):
    """`GET /encre/entrainement/dictee/expression` et sa validation."""

    def test_expression_a_une_forme_stable(self):
        r = self.client.get("/encre/entrainement/dictee/expression", headers=self.auth)
        self.assertEqual(200, r.status_code, r.text)
        corps = r.json()
        self.assertIn("id", corps)
        self.assertIn("latex", corps)
        self.assertTrue(corps["latex"])

    def test_valider_cree_un_exemple_source_dictee(self):
        expr = self.client.get(
            "/encre/entrainement/dictee/expression", headers=self.auth).json()
        r = self.client.post(
            "/encre/entrainement/dictee/valider",
            json={"expression_id": expr["id"], "strokes": [TRAIT]},
            headers=self.auth)
        self.assertEqual(200, r.status_code, r.text)
        exemple = encre_exemples_engine.get_exemple(r.json()["exemple_id"])
        self.assertEqual("dictee_inversee", exemple["source"])
        self.assertEqual(expr["latex"], exemple["texte_verite"])
        self.assertIsNone(exemple["texte_modele"])

    def test_expression_inconnue_est_refusee_en_400(self):
        r = self.client.post(
            "/encre/entrainement/dictee/valider",
            json={"expression_id": "id-invente", "strokes": [TRAIT]},
            headers=self.auth)
        self.assertEqual(400, r.status_code)

    def test_trace_vide_est_refuse_en_400(self):
        expr = self.client.get(
            "/encre/entrainement/dictee/expression", headers=self.auth).json()
        r = self.client.post(
            "/encre/entrainement/dictee/valider",
            json={"expression_id": expr["id"], "strokes": []},
            headers=self.auth)
        self.assertEqual(400, r.status_code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
