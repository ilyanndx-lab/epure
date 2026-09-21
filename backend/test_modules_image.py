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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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


class GenererImageLoraTest(unittest.TestCase):
    """`use_lora=True` — insertion de `LoraLoader` et rebranchement
    MODEL/CLIP, sur txt2img ET img2img ; `use_lora=False` (ou absent) reste
    un test de non-régression EXPLICITE (cf. rapport de tâche, §Tests)."""

    def _mock_post_txt2img(self, url, **kwargs):
        if url.endswith("/prompt"):
            self._payload_envoye = kwargs["json"]
            return _FakeHttpxResponse({"prompt_id": "abc123"})
        raise AssertionError(f"POST inattendu : {url}")

    def _mock_post_img2img(self, url, **kwargs):
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

    def test_use_lora_false_ne_change_rien_au_graphe_txt2img(self):
        """Non-régression explicite : payload identique, aucun LoraLoader."""
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post_txt2img), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", seed=42)

        workflow = self._payload_envoye["prompt"]
        self.assertNotIn(image_router._NODE_LORA, workflow)
        self.assertEqual(
            workflow[image_router._NODE_KSAMPLER]["inputs"]["model"], [image_router._NODE_CHECKPOINT, 0]
        )
        self.assertEqual(
            workflow[image_router._NODE_POSITIVE]["inputs"]["clip"], [image_router._NODE_CHECKPOINT, 1]
        )

    def test_use_lora_false_ne_change_rien_au_graphe_img2img(self):
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post_img2img), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", image_source=b"...")

        workflow = self._payload_envoye["prompt"]
        self.assertNotIn(image_router._NODE_LORA, workflow)

    def test_node_lora_absent_du_workflow_de_base(self):
        """`_NODE_LORA` ("34") est un ID choisi à la main comme libre —
        garde-fou si un futur export du gabarit de base venait à l'occuper
        pour autre chose (cf. revue de la tâche, §LoRA)."""
        self.assertNotIn(image_router._NODE_LORA, image_router._charger_workflow())

    def test_use_lora_true_insere_le_node_et_rebranche_txt2img(self):
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post_txt2img), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", use_lora=True, lora_strength=0.55)

        workflow = self._payload_envoye["prompt"]
        self.assertIn(image_router._NODE_LORA, workflow)
        lora = workflow[image_router._NODE_LORA]["inputs"]
        self.assertEqual(lora["model"], [image_router._NODE_CHECKPOINT, 0])
        self.assertEqual(lora["clip"], [image_router._NODE_CHECKPOINT, 1])
        self.assertEqual(lora["lora_name"], image_router._LORA_NAME)
        self.assertEqual(lora["strength_model"], 0.55)
        self.assertEqual(lora["strength_clip"], 0.55)

        # KSampler.model et les deux CLIPTextEncode.clip pointent maintenant
        # vers le LoraLoader, pas directement vers le checkpoint (30).
        self.assertEqual(
            workflow[image_router._NODE_KSAMPLER]["inputs"]["model"], [image_router._NODE_LORA, 0]
        )
        self.assertEqual(
            workflow[image_router._NODE_POSITIVE]["inputs"]["clip"], [image_router._NODE_LORA, 1]
        )
        self.assertEqual(
            workflow[image_router._NODE_NEGATIVE]["inputs"]["clip"], [image_router._NODE_LORA, 1]
        )

    def test_use_lora_true_insere_le_node_et_rebranche_img2img(self):
        """Même rebranchement MODEL/CLIP en img2img — indépendant de
        LoadImage/ImageScale/VAEEncode, qui ne touchent pas MODEL/CLIP."""
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post_img2img), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", image_source=b"...", denoise=0.5, use_lora=True)

        workflow = self._payload_envoye["prompt"]
        self.assertIn(image_router._NODE_LORA, workflow)
        self.assertEqual(
            workflow[image_router._NODE_KSAMPLER]["inputs"]["model"], [image_router._NODE_LORA, 0]
        )
        self.assertEqual(
            workflow[image_router._NODE_POSITIVE]["inputs"]["clip"], [image_router._NODE_LORA, 1]
        )
        self.assertEqual(
            workflow[image_router._NODE_NEGATIVE]["inputs"]["clip"], [image_router._NODE_LORA, 1]
        )
        # La branche img2img reste inchangée par ailleurs : VAEEncode.vae
        # vient toujours du checkpoint (30), pas du LoraLoader (sortie
        # MODEL/CLIP uniquement, pas de VAE).
        self.assertEqual(
            workflow[image_router._NODE_VAE_ENCODE]["inputs"]["vae"], [image_router._NODE_CHECKPOINT, 2]
        )

    def test_use_lora_true_sans_strength_applique_le_placeholder_defaut(self):
        with mock.patch.object(image_router.httpx, "post", side_effect=self._mock_post_txt2img), \
             mock.patch.object(image_router.httpx, "get", side_effect=self._mock_get), \
             mock.patch.object(image_router.time, "sleep"):
            image_router.generer_image("prompt", use_lora=True)

        lora = self._payload_envoye["prompt"][image_router._NODE_LORA]["inputs"]
        self.assertEqual(lora["strength_model"], image_router._LORA_STRENGTH_DEFAUT)
        self.assertEqual(lora["strength_clip"], image_router._LORA_STRENGTH_DEFAUT)


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


class ImageFileRouteTest(unittest.TestCase):
    """`GET /image/file/{nom}` — sert le PNG écrit par `/generate`.

    Sur une app minimale (ce routeur seul), pas `main.app` : le montage réel
    dépend de `modules_activés` (décision utilisateur, cf. manifest.json —
    le module est installé mais pas encore activé), une question sans
    rapport avec ce que cette route doit faire une fois montée."""

    def setUp(self):
        app = FastAPI()
        app.include_router(image_router.router, prefix="/image")
        self.client = TestClient(app)

    def test_fichier_existant_sert_le_png(self):
        chemin = image_router._images_dir() / "test.png"
        chemin.write_bytes(b"PNGDATA")
        self.addCleanup(chemin.unlink)

        r = self.client.get("/image/file/test.png")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"PNGDATA")
        self.assertEqual(r.headers["content-type"], "image/png")

    def test_fichier_absent_rend_404(self):
        r = self.client.get("/image/file/jamais-genere.png")
        self.assertEqual(r.status_code, 404)

    def test_traversee_de_chemin_rejetee(self):
        """Un antislash EST un séparateur sous Windows (plateforme primaire)
        même à l'intérieur d'un segment d'URL que Starlette ne découpe que
        sur `/` — cf. docstring de `image_file`. `..` seul est aussi rejeté,
        avant même la résolution."""
        for nom in ("..\\..\\secret.txt", ".."):
            with self.subTest(nom=nom):
                r = self.client.get(f"/image/file/{nom}")
                self.assertEqual(r.status_code, 404)


class ImageGenerateEndpointTest(unittest.TestCase):
    """`POST /image/generate` — les nouveaux champs `use_lora`/`lora_strength`
    atteignent bien `generer_image()`, sans changer le contrat existant
    (`generer_image` mocké : ce test ne parle jamais à un vrai ComfyUI)."""

    def setUp(self):
        app = FastAPI()
        app.include_router(image_router.router, prefix="/image")
        self.client = TestClient(app)

    def test_champs_use_lora_et_lora_strength_transmis(self):
        appels = {}

        def _generer_image_mock(prompt, seed, image_source, denoise, use_lora, lora_strength):
            appels["args"] = (prompt, seed, image_source, denoise, use_lora, lora_strength)
            return b"PNGDATA"

        with mock.patch.object(image_router, "generer_image", side_effect=_generer_image_mock):
            r = self.client.post(
                "/image/generate",
                data={"prompt": "un chat", "use_lora": "true", "lora_strength": "0.55"},
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(appels["args"], ("un chat", None, None, None, True, 0.55))

    def test_use_lora_absent_reste_false_non_regression(self):
        appels = {}

        def _generer_image_mock(prompt, seed, image_source, denoise, use_lora, lora_strength):
            appels["args"] = (prompt, seed, image_source, denoise, use_lora, lora_strength)
            return b"PNGDATA"

        with mock.patch.object(image_router, "generer_image", side_effect=_generer_image_mock):
            r = self.client.post("/image/generate", data={"prompt": "un chat"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(appels["args"], ("un chat", None, None, None, False, None))


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

    def test_generation_txt2img_reelle_avec_lora(self):
        """`use_lora=True` contre un VRAI ComfyUI — seule façon de confirmer
        que le graphe rebranché (LoraLoader entre le checkpoint et
        KSampler/CLIPTextEncode) est accepté tel quel, pas seulement câblé
        correctement côté payload (cf. `GenererImageLoraTest`, qui ne vérifie
        que la forme). Force modérée (0.55, milieu de la fourchette 0.5-0.6
        discutée) — pas d'assertion qualitative sur l'image elle-même, hors
        de portée d'un test automatisé ; compatibilité Dev/Schnell de ce LoRA
        précis non garantie côté source (cf. docstring module), donc un
        succès HTTP ici ne dit rien de plus que « ComfyUI a accepté et
        produit une image ». Un succès HTTP seul ne distingue PAS une
        application réelle du LoRA d'un no-op silencieux (clés du LoRA sans
        correspondance sur un checkpoint Schnell) — ne pas sur-interpréter ce
        test seul, cf. la note manuelle ci-dessous qui tranche cette question
        séparément.

        Vérifié manuellement hors suite (2026-09-21, prompt "test épure,
        portrait réaliste, gros plan visage", seed fixe, pas rejoué ici pour
        ne pas doubler la fragilité réseau déjà documentée d'un test gated) :
        la sortie SANS LoRA fait 1 417 615 octets et AVEC LoRA (force 0.9,
        `strength_clip=0.9` compris) fait 1 203 756 octets, contenus
        différents. `strength_clip` modifie directement le conditionnement
        CLIP envoyé au sampler — un LoRA réellement no-op (clés sans
        correspondance sur ce checkpoint Schnell) ne pourrait pas produire un
        écart de 214 Ko sur le PNG encodé à seed identique ; l'écart observé
        est donc attribuable au LoRA, pas au hasard. Limite assumée : le
        témoin de déterminisme (même appel SANS LoRA rejoué deux fois) n'a
        PAS pu être vérifié séparément — le ComfyUI distant est devenu
        injoignable pendant cette tentative (cf. `check_comfyui()` skip plus
        bas), donc cette conclusion repose sur une seule paire, pas sur un
        témoin confirmé. Ça ne dit toujours rien de la qualité visuelle
        (jamais jugée automatiquement).
        """
        image_bytes = image_router.generer_image(
            "test épure, portrait réaliste", use_lora=True, lora_strength=0.55
        )
        self.assertGreater(len(image_bytes), 0)

        nom = f"{image_router.uuid.uuid4().hex}.png"
        chemin = image_router._images_dir() / nom
        chemin.write_bytes(image_bytes)
        self.assertTrue(chemin.is_file())
        self.assertGreater(chemin.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
