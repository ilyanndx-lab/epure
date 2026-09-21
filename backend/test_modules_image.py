#!/usr/bin/env python3
"""Module Image (génération via ComfyUI, réseau local) — cf. modules/image/router.py.

ComfyUI mocké dans tous les tests unitaires (`httpx`/`urllib` remplacés, pas de
réseau) : `check_comfyui()`, la construction du payload `/prompt` (override
des bons node IDs, cf. Étape 0 du rapport du 2026-09-21), et le parsing d'un
`/history` simulé. Un test d'intégration GATED clôt le fichier, sauté si aucun
ComfyUI n'est réellement joignable — même patron que
`test_codeagent_plots.py::AvecMatplotlibTest` pour matplotlib.

Usage :
    python test_modules_image.py
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

import modules.image.router as image_router  # noqa: E402
import core.paths as core_paths  # noqa: E402


class _FakeUrlopenResponse:
    """Réponse `urllib.request.urlopen` minimale — même patron que
    `test_models_lmstudio.py::_FakeResponse`."""

    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeHttpxResponse:
    def __init__(self, json_data=None, content: bytes = b"", status_code: int = 200):
        self._json = json_data or {}
        self.content = content
        self.status_code = status_code
        self.text = json.dumps(self._json)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise image_router.httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self
            )


class CheckComfyuiTest(unittest.TestCase):
    """Même contrat que `check_lmstudio()` — booléen, jamais d'exception."""

    def test_true_si_le_serveur_repond(self):
        with mock.patch.object(
            image_router.urllib.request, "urlopen",
            return_value=_FakeUrlopenResponse(200),
        ):
            self.assertTrue(image_router.check_comfyui())

    def test_false_si_injoignable(self):
        with mock.patch.object(
            image_router.urllib.request, "urlopen",
            side_effect=image_router.urllib.error.URLError("injoignable"),
        ):
            self.assertFalse(image_router.check_comfyui())


class GenererImageTxt2ImgTest(unittest.TestCase):
    """`generer_image()` sans `image_source` — branche txt2img, celle lue
    depuis le fichier fourni (pas de nœud construit)."""

    def _mock_post(self, url, **kwargs):
        if url.endswith("/prompt"):
            self._payload_envoye = kwargs["json"]
            return _FakeHttpxResponse({"prompt_id": "abc123"})
        raise AssertionError(f"POST inattendu : {url}")

    def _mock_get(self, url, **kwargs):
        if url.endswith("/history/abc123"):
            return _FakeHttpxResponse({
                "abc123": {
                    "outputs": {
                        image_router._NODE_SAVE: {
                            "images": [{
                                "filename": "ComfyUI_00001_.png",
                                "subfolder": "",
                                "type": "output",
                            }]
                        }
                    }
                }
            })
        if url.endswith("/view"):
            return _FakeHttpxResponse(content=b"PNGDATA")
        raise AssertionError(f"GET inattendu : {url}")

    def test_override_prompt_et_seed_sur_les_bons_noeuds(self):
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            resultat = image_router.generer_image("un chat en aquarelle", seed=42)

        self.assertEqual(resultat, b"PNGDATA")
        workflow_envoye = self._payload_envoye["prompt"]
        self.assertEqual(
            workflow_envoye[image_router._NODE_POSITIVE]["inputs"]["text"],
            "un chat en aquarelle",
        )
        self.assertEqual(
            workflow_envoye[image_router._NODE_KSAMPLER]["inputs"]["seed"], 42
        )
        # txt2img : pas de branche construite, latent vide inchangé.
        self.assertEqual(
            workflow_envoye[image_router._NODE_KSAMPLER]["inputs"]["latent_image"],
            [image_router._NODE_LATENT_EMPTY, 0],
        )
        self.assertNotIn(image_router._NODE_LOAD_IMAGE, workflow_envoye)

    def test_seed_absent_genere_une_valeur_aleatoire(self):
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt")
        seed = self._payload_envoye["prompt"][image_router._NODE_KSAMPLER]["inputs"]["seed"]
        self.assertIsInstance(seed, int)

    def test_rejet_http_400_leve_comfyui_error_avec_le_detail(self):
        """Forme réelle de ComfyUI : /prompt répond 400, le détail (node_errors)
        est dans le CORPS — pas un `node_errors` dans une réponse 200."""
        def _post_refuse(url, **kwargs):
            return _FakeHttpxResponse(
                {"error": {"message": "invalide"}, "node_errors": {"6": "erreur"}},
                status_code=400,
            )

        with mock.patch.object(image_router.httpx, "post", side_effect=_post_refuse):
            with self.assertRaises(image_router.ComfyUIError) as ctx:
                image_router.generer_image("prompt")
        self.assertIn("node_errors", str(ctx.exception))

    def test_timeout_leve_comfyui_error(self):
        """Aucune sortie dans /history avant l'échéance → erreur claire, pas
        de blocage indéfini (cf. docstring de tête du module)."""
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(
                 image_router.httpx, "get",
                 return_value=_FakeHttpxResponse({"abc123": {"outputs": {}}}),
             ), \
             mock.patch.object(image_router.time, "sleep"), \
             mock.patch.object(image_router, "_TIMEOUT_S", 0.0):
            with self.assertRaises(image_router.ComfyUIError):
                image_router.generer_image("prompt")

    def test_erreur_execution_comfyui_leve_sans_attendre_l_echeance(self):
        """ComfyUI a terminé (OOM par ex.) sans produire d'image au nœud
        SaveImage → erreur immédiate, pas un sondage jusqu'à _TIMEOUT_S."""
        def _mock_get_erreur(url, **kwargs):
            if url.endswith("/history/abc123"):
                return _FakeHttpxResponse({
                    "abc123": {
                        "outputs": {},
                        "status": {
                            "completed": True,
                            "status_str": "error",
                            "messages": [["execution_error", {"exception_message": "OOM"}]],
                        },
                    }
                })
            raise AssertionError(f"GET inattendu : {url}")

        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(image_router.httpx, "get", side_effect=_mock_get_erreur), \
             mock.patch.object(image_router.time, "sleep") as sleep_mock, \
             mock.patch.object(image_router, "_TIMEOUT_S", 180.0):
            with self.assertRaises(image_router.ComfyUIError) as ctx:
                image_router.generer_image("prompt")
        self.assertIn("OOM", str(ctx.exception))
        sleep_mock.assert_not_called()  # pas de sondage supplémentaire une fois l'échec vu

    def test_panne_wifi_transitoire_pendant_le_sondage_ne_fait_pas_abandonner(self):
        """Un GET /history raté au milieu du sondage doit réessayer, pas
        abandonner une génération que ComfyUI mène peut-être encore (wifi
        partagé Yoga-Acer "pas garanti à 100 %", cf. docstring de tête)."""
        appels = {"n": 0}

        def _mock_get_flaky(url, **kwargs):
            appels["n"] += 1
            if appels["n"] == 1:
                raise image_router.httpx.ConnectError("wifi coupé")
            return self._mock_get(url, **kwargs)

        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(image_router.httpx, "get", side_effect=_mock_get_flaky), \
             mock.patch.object(image_router.time, "sleep"):
            resultat = image_router.generer_image("prompt")
        self.assertEqual(resultat, b"PNGDATA")
        self.assertGreaterEqual(appels["n"], 2)


class GenererImageImg2ImgTest(unittest.TestCase):
    """Branche LoadImage(38) → ImageScale(40) → VAEEncode(39), IDs vérifiés
    contre un export réel (cf. docstring module, §2026-09-21) — vérifie le
    câblage, pas une vraie réponse ComfyUI."""

    def _mock_post(self, url, **kwargs):
        if url.endswith("/upload/image"):
            return _FakeHttpxResponse({"name": "src.png", "subfolder": "", "type": "input"})
        if url.endswith("/prompt"):
            self._payload_envoye = kwargs["json"]
            return _FakeHttpxResponse({"prompt_id": "abc123"})
        raise AssertionError(f"POST inattendu : {url}")

    def _mock_get(self, url, **kwargs):
        if url.endswith("/history/abc123"):
            return _FakeHttpxResponse({
                "abc123": {"outputs": {image_router._NODE_SAVE: {
                    "images": [{"filename": "f.png", "subfolder": "", "type": "output"}]
                }}}
            })
        return _FakeHttpxResponse(content=b"PNGDATA")

    def test_branche_rebranche_le_ksampler_via_load_scale_encode(self):
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", image_source=b"...", denoise=0.5)

        workflow = self._payload_envoye["prompt"]
        self.assertIn(image_router._NODE_LOAD_IMAGE, workflow)
        self.assertIn(image_router._NODE_IMAGE_SCALE, workflow)
        self.assertIn(image_router._NODE_VAE_ENCODE, workflow)

        # Chaîne réelle : LoadImage -> ImageScale -> VAEEncode -> KSampler,
        # pas LoadImage -> VAEEncode direct (hypothèse initiale, fausse).
        self.assertEqual(
            workflow[image_router._NODE_IMAGE_SCALE]["inputs"]["image"],
            [image_router._NODE_LOAD_IMAGE, 0],
        )
        self.assertEqual(
            workflow[image_router._NODE_VAE_ENCODE]["inputs"]["pixels"],
            [image_router._NODE_IMAGE_SCALE, 0],
        )
        self.assertEqual(
            workflow[image_router._NODE_KSAMPLER]["inputs"]["latent_image"],
            [image_router._NODE_VAE_ENCODE, 0],
        )
        self.assertEqual(workflow[image_router._NODE_KSAMPLER]["inputs"]["denoise"], 0.5)
        self.assertEqual(workflow[image_router._NODE_LOAD_IMAGE]["inputs"]["image"], "src.png")
        # Pas de clé "upload" : absente du format API réel (cf. docstring module).
        self.assertNotIn("upload", workflow[image_router._NODE_LOAD_IMAGE]["inputs"])

    def test_resolution_image_scale_reprise_du_latent_vide(self):
        """Pas de second 1024×1024 en dur : ImageScale reprend la résolution
        de l'EmptySD3LatentImage du graphe de base (décision documentée dans
        le module, cf. §2026-09-21)."""
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", image_source=b"...")

        workflow = self._payload_envoye["prompt"]
        latent_vide = workflow[image_router._NODE_LATENT_EMPTY]["inputs"]
        scale = workflow[image_router._NODE_IMAGE_SCALE]["inputs"]
        self.assertEqual(scale["width"], latent_vide["width"])
        self.assertEqual(scale["height"], latent_vide["height"])

    def test_denoise_defaut_reste_0_75_pas_le_residu_de_session_0_8(self):
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", image_source=b"...")

        workflow = self._payload_envoye["prompt"]
        self.assertEqual(workflow[image_router._NODE_KSAMPLER]["inputs"]["denoise"], 0.75)


class ImagesDirDefautTest(unittest.TestCase):
    """`_images_dir()` sans `EPURE_DATA_DIR` — jamais exercé jusqu'ici (les
    deux sessions précédentes ne passaient que par un `EPURE_DATA_DIR` de
    test, via `_test_env`, donc n'atteignaient jamais le vrai défaut).

    `Path.mkdir` neutralisé : seule la RÉSOLUTION du chemin est exercée. Créer
    pour de vrai `backend/memory/generated_images/` pendant la suite violerait
    l'invariant verrouillé par `test_zz_donnees_reelles.py` (rien n'est écrit
    sous le vrai `backend/memory/` pendant les tests) — cf. le rapport du
    2026-09-21 qui a écarté cette option pour cette raison précise.
    """

    def setUp(self):
        self._epure_data_dir_avant = os.environ.pop("EPURE_DATA_DIR", None)
        self.addCleanup(self._restaurer)

    def _restaurer(self):
        if self._epure_data_dir_avant is not None:
            os.environ["EPURE_DATA_DIR"] = self._epure_data_dir_avant
        else:
            os.environ.pop("EPURE_DATA_DIR", None)

    def test_chemin_par_defaut_pointe_sous_backend_memory(self):
        with mock.patch("pathlib.Path.mkdir"):
            chemin = image_router._images_dir()

        # Composition réelle du code (router.py) : resolve_data_dir() + le
        # sous-dossier "generated_images" — pas une valeur réimplémentée.
        self.assertEqual(chemin, image_router.resolve_data_dir() / "generated_images")

        # Valeur concrète attendue, calculée indépendamment — même patron que
        # test_data_dir.py::ResolutionTest.test_defaut_sans_variable. Ancrée
        # sur core.paths.__file__, PAS image_router.__file__ : `_test_env`
        # redirige `modules/` vers une copie temporaire (EPURE_MODULES_DIR),
        # donc `image_router.__file__` ne vit plus sous le vrai `backend/` —
        # `core/` n'est jamais copié, lui reste fiable comme ancrage.
        attendu = (
            Path(core_paths.__file__).resolve().parent.parent
            / "memory" / "generated_images"
        ).resolve()
        self.assertEqual(chemin, attendu)


@unittest.skipUnless(image_router.check_comfyui(), "aucun ComfyUI joignable sur ce poste")
class ComfyUIIntegrationTest(unittest.TestCase):
    """Bout en bout sur un VRAI ComfyUI — sauté sans réseau/serveur."""

    def test_generation_txt2img_reelle(self):
        image_bytes = image_router.generer_image("test épure, formes simples")
        self.assertGreater(len(image_bytes), 0)

    def test_generation_img2img_reelle_et_ecriture_sur_disque(self):
        """IDs 38/40/39 (LoadImage/ImageScale/VAEEncode) contre un vrai
        ComfyUI — jamais exercés avant le 2026-09-21. Reproduit aussi
        l'écriture de fichier faite par l'endpoint (`_images_dir()` +
        `write_bytes`), sous l'`EPURE_DATA_DIR` isolé de `_test_env` — pas le
        vrai `backend/memory/generated_images/` (cf. `ImagesDirDefautTest`,
        qui couvre séparément la résolution du chemin par défaut)."""
        from io import BytesIO

        from PIL import Image

        tampon = BytesIO()
        Image.new("RGB", (64, 64), (200, 50, 50)).save(tampon, format="PNG")
        image_bytes = image_router.generer_image(
            "test épure, formes simples",
            image_source=tampon.getvalue(),
            denoise=0.5,
        )
        self.assertGreater(len(image_bytes), 0)

        nom = f"{image_router.uuid.uuid4().hex}.png"
        chemin = image_router._images_dir() / nom
        chemin.write_bytes(image_bytes)
        self.assertTrue(chemin.is_file())
        self.assertGreater(chemin.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
