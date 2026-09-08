#!/usr/bin/env python3
"""Le mode "maths"/"lettres" à la frontière HTTP du module `encre`.

`test_encre_store.py` couvre déjà `core.encre.EncreEngine` en détail (normalisation
du champ, rétrocompatibilité, aller-retour). Ce fichier couvre ce que ce
niveau-là ne peut pas voir : **le refus de transcription est posé par le
routeur, pas par une case à cocher côté frontend.**

`POST /encre/pages/{id}/transcrire` doit répondre 400 sur une page en mode
"lettres" — `pix2text-mfr` est un reconnaisseur de formules mathématiques
(sortie LaTeX), pas un OCR généraliste ; lui donner du texte manuscrit normal
produit des hallucinations de syntaxe math, pas une transcription dégradée.
Un bouton désactivé côté `Component.tsx` ne protège personne d'un appel direct
à l'API (curl, une extension navigateur, une future version du frontend qui
oublierait la garde) : le vrai refus doit être ici.

**Aucun de ces tests ne charge `pix2text-mfr`.** Le refus de mode est vérifié
AVANT que le routeur touche à `hmer_engine` (cf. `modules/encre/router.py`),
donc la page en mode "lettres" n'a pas besoin d'être transcriptible pour que
son 400 soit correct — même philosophie d'isolation que `test_hmer.py`.

Usage :
    python test_encre_router.py
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

#: Un trait non vide, pour ne pas confondre le refus de MODE avec le refus de
#: PAGE VIDE — les deux sont des 400, et le test de précédence en a besoin.
TRAIT = {
    "couleur": "#111827",
    "taille": 3,
    "points": [{"x": 1.0, "y": 2.0, "pression": 0.5, "t": 0}],
}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54322))
        cls.token = get_api_token()

    def setUp(self):
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def creer_page(self, mode=None, strokes=None):
        corps = {"strokes": strokes if strokes is not None else []}
        if mode is not None:
            corps["mode"] = mode
        r = self.client.post("/encre/pages", json=corps, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class CreationEtModeTest(_Base):
    """`POST /encre/pages` et `PUT /encre/pages/{id}` acceptent `mode`."""

    def test_creation_sans_mode_rend_maths(self):
        page = self.creer_page()
        self.assertEqual(page["mode"], "maths")

    def test_creation_avec_mode_lettres(self):
        page = self.creer_page(mode="lettres")
        self.assertEqual(page["mode"], "lettres")

    def test_lecture_d_une_page_rend_son_mode(self):
        page = self.creer_page(mode="lettres")
        r = self.client.get(f"/encre/pages/{page['id']}", headers=self.auth)
        self.assertEqual(r.json()["mode"], "lettres")

    def test_aller_retour_du_mode_via_put(self):
        """Le geste exact du toggle frontend : bascule, relit, rebascule."""
        page = self.creer_page()
        r = self.client.put(f"/encre/pages/{page['id']}",
                             json={"mode": "lettres"}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "lettres")
        self.assertEqual(
            self.client.get(f"/encre/pages/{page['id']}",
                             headers=self.auth).json()["mode"],
            "lettres",
        )
        r = self.client.put(f"/encre/pages/{page['id']}",
                             json={"mode": "maths"}, headers=self.auth)
        self.assertEqual(r.json()["mode"], "maths")
        self.assertEqual(
            self.client.get(f"/encre/pages/{page['id']}",
                             headers=self.auth).json()["mode"],
            "maths",
        )

    def test_mise_a_jour_du_titre_seul_ne_touche_pas_au_mode(self):
        """``mode`` absent du corps du PUT = inchangé, comme ``titre``/``strokes``."""
        page = self.creer_page(mode="lettres")
        r = self.client.put(f"/encre/pages/{page['id']}",
                             json={"titre": "renommée"}, headers=self.auth)
        self.assertEqual(r.json()["mode"], "lettres")


class RefusDeTranscriptionTest(_Base):
    """`POST /encre/pages/{id}/transcrire` refuse une page en mode "lettres"."""

    def test_page_lettres_avec_encre_refusee_en_400(self):
        page = self.creer_page(mode="lettres", strokes=[TRAIT])
        r = self.client.post(f"/encre/pages/{page['id']}/transcrire",
                              headers=self.auth)
        self.assertEqual(r.status_code, 400, r.text)
        detail = r.json()["detail"]
        self.assertIn("lettres", detail)

    def test_le_message_explique_pourquoi_pas_seulement_qu_il_est_refuse(self):
        """Pas un simple « refusé » : le message doit dire à l'utilisateur
        pourquoi, sur le même modèle que les autres 400 de ce routeur."""
        page = self.creer_page(mode="lettres", strokes=[TRAIT])
        r = self.client.post(f"/encre/pages/{page['id']}/transcrire",
                              headers=self.auth)
        detail = r.json()["detail"].lower()
        self.assertIn("pix2text-mfr", detail)
        self.assertTrue("mathémat" in detail or "formule" in detail)

    def test_page_lettres_sans_aucun_trait_est_aussi_refusee_pour_le_mode(self):
        """Précédence : le refus de MODE passe avant le refus de PAGE VIDE.

        Les deux sont des 400 — sans cette vérification, un correctif futur qui
        inverserait l'ordre des deux contrôles resterait vert sur
        ``test_page_lettres_avec_encre_refusee_en_400`` tout en cassant ce
        cas-ci, où seul l'ordre change le message reçu par l'utilisateur.
        """
        page = self.creer_page(mode="lettres", strokes=[])
        r = self.client.post(f"/encre/pages/{page['id']}/transcrire",
                              headers=self.auth)
        self.assertEqual(r.status_code, 400)
        self.assertIn("lettres", r.json()["detail"])

    def test_page_maths_sans_trait_garde_son_message_de_page_vide(self):
        """Contrôle du contrôle : le nouveau refus ne doit pas masquer l'ancien
        message pour le cas qu'il couvrait déjà."""
        page = self.creer_page(mode="maths", strokes=[])
        r = self.client.post(f"/encre/pages/{page['id']}/transcrire",
                              headers=self.auth)
        self.assertEqual(r.status_code, 400)
        self.assertIn("aucun tracé", r.json()["detail"])

    def test_page_absente_reste_un_404_avant_toute_verification_de_mode(self):
        r = self.client.post("/encre/pages/" + "0" * 32 + "/transcrire",
                              headers=self.auth)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
