#!/usr/bin/env python3
"""Un fournisseur cloud sans clé ne rend aucun modèle — il n'en rend pas de grisés.

Ce que ça corrige, côté usage : le sélecteur de modèles du chat s'ouvrait sur
six catégories de modèles cloud tous marqués « indisponible », au-dessus des
modèles locaux qui, eux, marchent. Une liste où presque tout est barré ne
renseigne pas : elle cache ce qui fonctionne. Même parti pris que le catalogue
de modules non livré dans un paquet (``GET /settings/catalogue`` renvoie une
liste vide, le bouton n'apparaît pas) — l'incapacité est silencieuse plutôt
qu'affichée.

Ce qui reste listé et grisé, et qu'il ne faut pas confondre avec le cas
ci-dessus : un modèle dont la clé **est** posée mais que le ``/v1/models`` du
fournisseur ne connaît plus (``_disponible is False``). Celui-là est un
diagnostic — un ID retiré du catalogue amont — et il doit rester visible.
Autrement dit ``disponible: False`` sur un modèle cloud n'a plus qu'une seule
cause, et c'est ce fichier qui le tient.

``fournisseurs`` est ajouté à la réponse pour une raison précise : les
recommandations curées du frontend (``ModuleBar.MODULE_RECOMMENDATIONS``)
nomment des IDs en dur. Sans cette carte, un modèle absent de ``cloud`` faute de
clé serait indistinguable d'un modèle absent du catalogue amont, et l'interface
le proposerait comme « inconnu, tentons » — un clic vers une erreur. Six
booléens, jamais les clés : c'est aussi vérifié ici.

Attention : importer ``main`` démarre une vraie instance (cf. l'en-tête de
test_auth_surface.py). Aucun réseau n'est touché — les sondes Ollama/FLM et le
catalogue cloud sont neutralisés dans ``setUpModule``.

Usage :
    python test_models_cloud_sans_cle.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.* / main

# Lues à l'import de main : les figer rend le test indépendant du poste.
os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ["EPURE_CORS_ORIGINS"] = "http://localhost:5173"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core.auth import get_api_token  # noqa: E402

#: Les six clés que /models consulte. Recopiées ici plutôt qu'importées de
#: core.runtime.API_KEY_NAMES : ce test doit échouer si l'endpoint cesse de
#: regarder l'une d'elles, ce qu'une liste partagée masquerait.
CLES = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

#: Catalogue de test. `_disponible` reprend la convention de core/models.py :
#: None = pas de verdict live (la clé décide), False = le fournisseur ne connaît
#: plus cet ID.
CATALOGUE = {
    "rapide": [
        {"id": "gemini:essai-flash", "nom": "Essai Flash", "provider": "gemini",
         "_disponible": None, "_usages": []},
        {"id": "groq:essai-8b", "nom": "Essai 8B", "provider": "groq",
         "_disponible": None, "_usages": []},
    ],
    "puissant": [
        {"id": "groq:essai-retire", "nom": "Essai retiré", "provider": "groq",
         "_disponible": False, "_usages": []},
    ],
    "long_contexte": [],
}

_ORIGINAUX: dict = {}


def setUpModule():
    async def _catalogue():
        # Copie profonde à la main : l'endpoint fait `m.items()` sans muter, mais
        # `_place` de core/models.py fait un `pop` — mieux vaut ne pas parier sur
        # l'implémentation d'en face pour un objet réutilisé entre les tests.
        return {cat: [dict(m) for m in models] for cat, models in CATALOGUE.items()}

    _ORIGINAUX["get_ollama_installed"] = main.get_ollama_installed
    _ORIGINAUX["check_flm"] = main.check_flm
    _ORIGINAUX["get_catalog"] = main.models_registry.get_catalog
    main.get_ollama_installed = lambda: None
    main.check_flm = lambda: False
    main.models_registry.get_catalog = _catalogue


def tearDownModule():
    main.get_ollama_installed = _ORIGINAUX["get_ollama_installed"]
    main.check_flm = _ORIGINAUX["check_flm"]
    main.models_registry.get_catalog = _ORIGINAUX["get_catalog"]


class ModelesCloudTest(unittest.TestCase):
    """`GET /models` vu depuis le sélecteur du chat."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(
            main.app, base_url="http://localhost", client=("127.0.0.1", 54321)
        )
        cls.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        # Le poste d'Ilyann a de vraies clés dans backend/.env, chargées dans
        # os.environ à l'import : sans ce nettoyage le test mesurerait la
        # configuration du poste au lieu du comportement de l'endpoint.
        self._sauvegarde = {nom: os.environ.get(nom) for nom in CLES.values()}
        for nom in CLES.values():
            os.environ.pop(nom, None)

    def tearDown(self):
        for nom, valeur in self._sauvegarde.items():
            if valeur is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = valeur

    def _cloud(self) -> dict:
        r = self.client.get("/models", headers=self.entetes)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _tous_les_ids(self, reponse: dict) -> set[str]:
        return {
            m["id"] for models in reponse["cloud"].values() for m in models
        }

    # ── Aucune clé : rien du tout ────────────────────────────────────────────

    def test_sans_aucune_cle_le_cloud_est_vide(self):
        d = self._cloud()
        self.assertEqual(
            self._tous_les_ids(d),
            set(),
            "des modèles cloud sont listés sans qu'aucune clé soit posée — ils "
            "s'afficheraient grisés dans le sélecteur du chat",
        )
        for cat in ("rapide", "puissant", "long_contexte"):
            self.assertIn(cat, d["cloud"], "les catégories restent présentes, vides")

    def test_sans_aucune_cle_les_modeles_locaux_restent(self):
        """Le filtre ne doit toucher que le cloud : c'est tout ce qui reste."""
        d = self._cloud()
        self.assertTrue(d["local"], "plus aucun modèle local listé")

    # ── Une clé : ce fournisseur seulement ───────────────────────────────────

    def test_une_seule_cle_ne_montre_que_ce_fournisseur(self):
        os.environ["GEMINI_API_KEY"] = "clé-de-test"
        d = self._cloud()
        self.assertEqual(self._tous_les_ids(d), {"gemini:essai-flash"})
        (modele,) = d["cloud"]["rapide"]
        self.assertTrue(modele["disponible"])

    def test_un_modele_retire_du_catalogue_amont_reste_visible_et_grise(self):
        """L'autre cause de `disponible: False`, celle qui doit rester affichée.

        C'est un diagnostic — la clé est bonne, l'ID a disparu chez le
        fournisseur. Le masquer effacerait l'information au lieu de la donner.
        """
        os.environ["GROQ_API_KEY"] = "clé-de-test"
        d = self._cloud()
        self.assertEqual(
            self._tous_les_ids(d), {"groq:essai-8b", "groq:essai-retire"}
        )
        retire = {m["id"]: m for m in d["cloud"]["puissant"]}["groq:essai-retire"]
        self.assertFalse(retire["disponible"])

    # ── La carte des fournisseurs ────────────────────────────────────────────

    def test_fournisseurs_couvre_les_six_et_ne_porte_que_des_booleens(self):
        os.environ["MISTRAL_API_KEY"] = "clé-de-test"
        d = self._cloud()
        fournisseurs = d["fournisseurs"]
        self.assertEqual(set(fournisseurs), set(CLES))
        for nom, valeur in fournisseurs.items():
            with self.subTest(fournisseur=nom):
                self.assertIsInstance(
                    valeur, bool, "la carte des fournisseurs doit rester booléenne"
                )
        self.assertTrue(fournisseurs["mistral"])
        self.assertFalse(fournisseurs["gemini"])

    def test_fournisseurs_ne_laisse_pas_fuir_la_valeur_de_la_cle(self):
        """Même garde-fou que pour le token d'API (CLAUDE.md §6), au cas où.

        La carte est construite par `bool(...)`, donc le secret ne peut pas y
        entrer aujourd'hui — l'affirmer ici est ce qui empêche un futur
        « tant qu'on y est, renvoyons les quatre derniers caractères ».
        """
        secret = "sk-un-secret-qui-ne-doit-pas-sortir"
        os.environ["NVIDIA_API_KEY"] = secret
        r = self.client.get("/models", headers=self.entetes)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(secret, r.text)

    # ── Une clé absente ne doit pas non plus être recommandée ────────────────

    def test_aucune_recommandation_vers_un_fournisseur_sans_cle(self):
        d = self._cloud()
        self.assertEqual(
            d["recommandations"],
            {},
            "des usages sont recommandés alors qu'aucune clé n'est posée",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
