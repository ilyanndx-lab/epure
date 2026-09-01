#!/usr/bin/env python3
"""Une image du corpus est décrite par un modèle vision — plus un placeholder muet.

**L'ÉTAT MESURÉ AVANT D'ÉCRIRE LE CODE**, le 2026-09-01 :

* `flm` (OpenAI-compatible) accepte le format vision standard — vérifié par
  appel réel à `qwen3vl-it:4b` : bloc `image_url` en base64 (data URI),
  transcription exacte d'un texte photographié (« THALES 42 »), 7,5 s ;
* le client python `ollama` accepte le CHEMIN du fichier directement dans
  `images=[...]` — vérifié par appel réel à `moondream` : pas de base64 à
  préparer, `ollama._types.Image` lit et encode lui-même. ~2 s modèle déjà
  chargé (25 s au premier appel après le pull) ;
* `moondream` existe dans la bibliothèque Ollama, se pull sans erreur, et
  répond dans un délai raisonnable — retenu comme repli configurable
  (`config.yaml:vision.ollama_model`) pour une machine sans FLM (ARM64, pas de
  NPU AMD). **Mais transcrit moins bien que `flm` sur du texte structuré** :
  sur un triangle annoté + une formule, `moondream` décrit la forme sans
  transcrire la formule et répond en anglais à un prompt français, alors que
  `flm:qwen3vl-it:4b` transcrit titre et formule mot pour mot en français —
  cf. CLAUDE.md §3.3 bis pour le détail. Retenu quand même comme repli faute
  d'alternative Ollama vérifiée, PAS parce que sa qualité est jugée suffisante.

Ce que ces tests gardent, dans l'ordre du besoin :

1. **`describe_image` encapsule la différence de format par provider** — même
   principe de dispatch que `LLMEngine.stream` (`_parse_model`), sans qu'un
   appelant ait à connaître la forme du message ;
2. **`RAGEngine` ne connaît aucun nom de modèle en dur** — il appelle
   `core.models.premier_modele_vision_disponible()`, jamais un ID écrit ici ;
3. **la dégradation est totale et silencieuse** : pas de LLM injecté, aucun
   modèle vision disponible, ou l'appel échoue → le placeholder d'avant, jamais
   une exception qui ferait échouer l'indexation d'un fichier par ailleurs
   valide (même convention que les extracteurs `.docx`/`.pptx`/`.xlsx`,
   `test_ingestion_documents.py`) ;
4. **`describe_image` a un timeout COURT et DÉDIÉ (60 s), sur les deux
   chemins** — jamais `model.timeout_s` (300 s, pensé pour le chat) : cette
   méthode tourne en synchrone dans le chargement d'un fichier
   (`_stream_load_sse`), pas une conversation active. Deux mécanismes
   différents, vérifiés par appel réel :
   - Ollama : `Client.chat()` n'a pas de `timeout` par appel → un second
     client dédié, `_vision_ollama_client` ;
   - flm (openai) : `create(timeout=...)` existe, mais la retry policy PAR
     DÉFAUT (2 essais) multiplie l'attente au lieu de la borner — mesuré,
     5,4 s pour lever sur un `timeout=0.5` seul, contre 1,9 s avec
     `max_retries=0`. `describe_image` pose donc les deux via
     `.with_options(timeout=..., max_retries=0)`.

**Bug trouvé après le premier merge, corrigé ici** : `_stream_load_sse`
(`modules/settings/router.py`) construisait le résumé affiché à l'import via
un SECOND appel — `RAGEngine.read_file_text`, une méthode STATIQUE qui appelle
`_extract_text_from_path` directement et ne passe donc jamais par
`_texte_image`/le modèle vision. Résultat : le résumé d'une image importée
disait systématiquement « je n'ai pas accès à l'image », même quand
`rag.index_file` (appelé juste avant, dans la même boucle) avait produit la
vraie description. `index_file` rend maintenant le texte qu'il a réellement
indexé, et `_stream_load_sse` le réutilise au lieu de relire/reparser le
fichier — corrige le résumé ET élimine une double extraction pour tous les
formats (pdf/docx/pptx/xlsx…), pas seulement les images.

Usage :
    python test_vision_images.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import core.models as core_models  # noqa: E402
import main  # noqa: E402  — monte l'app entière ; cf. test_fichiers_par_conversation.py
import modules.settings.router as routeur_reglages  # noqa: E402
from core import llm as module_llm  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.llm import LLMEngine  # noqa: E402
from core.rag import RAGEngine  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


class _Fichiers(unittest.TestCase):
    """Base : un dossier temporaire par classe, une image bidon dedans."""

    @classmethod
    def setUpClass(cls):
        cls.dossier = Path(tempfile.mkdtemp(prefix="epure-test-vision-"))
        cls.image = cls.dossier / "schema.png"
        # Le contenu n'a pas besoin d'être un PNG valide : ni le placeholder
        # (qui ne lit que le nom) ni les doubles de LLM (mockés) ne décodent
        # l'image dans ces tests.
        cls.image.write_bytes(b"\x89PNG\r\n\x1a\nfausse-image-de-test")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.dossier, ignore_errors=True)


class _FakeLLM:
    """Double minimal : capture l'appel, rend une valeur programmée."""

    def __init__(self, retour="Description vision.", erreur=None):
        self._retour = retour
        self._erreur = erreur
        self.appels: list[tuple[str, str]] = []

    def describe_image(self, path, model):
        self.appels.append((path, model))
        if self._erreur is not None:
            raise self._erreur
        return self._retour


def _moteur_sans_init(llm=None) -> RAGEngine:
    """Un `RAGEngine` sans construire de store ni d'embedding — cf.
    `test_index_file_ignore_une_extension_non_supportee` dans
    `test_ingestion_documents.py`, même idiome.
    """
    moteur = object.__new__(RAGEngine)
    moteur._llm = llm
    return moteur


class TexteImageTest(_Fichiers):
    """`RAGEngine._texte_image` : la dégradation et le cas heureux."""

    def test_sans_llm_injecte_retombe_sur_le_placeholder(self):
        """Un moteur construit sans `llm=` (scripts, tests légers) ne doit pas
        planter — comportement identique à avant ce chantier.
        """
        moteur = _moteur_sans_init(llm=None)
        texte = moteur._texte_image(str(self.image))
        self.assertIn("analyse vision non disponible", texte)

    def test_aucun_modele_vision_disponible_retombe_sur_le_placeholder(self):
        faux_llm = _FakeLLM()
        moteur = _moteur_sans_init(llm=faux_llm)
        with patch("core.rag.premier_modele_vision_disponible", return_value=None):
            texte = moteur._texte_image(str(self.image))
        self.assertIn("analyse vision non disponible", texte)
        self.assertEqual(faux_llm.appels, [], "describe_image n'aurait pas dû être appelé")

    def test_succes_cote_flm_remplace_le_placeholder(self):
        faux_llm = _FakeLLM(retour="Un schéma de Thalès : AB/AC = AM/AN.")
        moteur = _moteur_sans_init(llm=faux_llm)
        with patch("core.rag.premier_modele_vision_disponible",
                   return_value="flm:qwen3vl-it:4b"):
            texte = moteur._texte_image(str(self.image))
        self.assertIn("Un schéma de Thalès : AB/AC = AM/AN.", texte)
        self.assertIn("schema.png", texte)
        self.assertNotIn("analyse vision non disponible", texte)
        self.assertEqual(faux_llm.appels, [(str(self.image), "flm:qwen3vl-it:4b")])

    def test_succes_cote_ollama_remplace_le_placeholder(self):
        faux_llm = _FakeLLM(retour="Une photo de cours : dérivée de sin(x).")
        moteur = _moteur_sans_init(llm=faux_llm)
        with patch("core.rag.premier_modele_vision_disponible", return_value="moondream"):
            texte = moteur._texte_image(str(self.image))
        self.assertIn("Une photo de cours : dérivée de sin(x).", texte)
        self.assertEqual(faux_llm.appels, [(str(self.image), "moondream")])

    def test_echec_du_modele_vision_retombe_sur_le_placeholder_sans_lever(self):
        """Timeout, serveur down, réponse invalide : jamais d'exception qui ferait
        échouer l'indexation d'un fichier par ailleurs valide.
        """
        faux_llm = _FakeLLM(erreur=RuntimeError("[flm:qwen3vl-it:4b] serveur injoignable"))
        moteur = _moteur_sans_init(llm=faux_llm)
        with patch("core.rag.premier_modele_vision_disponible",
                   return_value="flm:qwen3vl-it:4b"):
            texte = moteur._texte_image(str(self.image))  # ne doit pas lever
        self.assertIn("analyse vision non disponible", texte)

    def test_une_reponse_vide_retombe_sur_le_placeholder(self):
        """Un modèle vision qui répond une chaîne vide n'est pas un succès."""
        faux_llm = _FakeLLM(retour="   ")
        moteur = _moteur_sans_init(llm=faux_llm)
        with patch("core.rag.premier_modele_vision_disponible", return_value="moondream"):
            texte = moteur._texte_image(str(self.image))
        self.assertIn("analyse vision non disponible", texte)


class _FakeCollection:
    """Double minimal de `core.vector_store.VectorStore.collection(...)`."""

    def __init__(self):
        self.documents = None

    def delete(self, where=None):
        pass

    def upsert(self, documents, ids, metadatas):
        self.documents = documents


class _FauxCache:
    def cache_clear(self):
        pass


class IndexFileImageTest(_Fichiers):
    """`index_file` bascule vers `_texte_image` pour une image, jamais pour
    autre chose — c'est le seul point d'entrée réel, `_texte_image` étant privé.
    """

    def _moteur(self):
        moteur = _moteur_sans_init(llm=_FakeLLM())
        moteur._chunk_size = 500
        moteur._chunk_overlap = 50
        moteur._col = _FakeCollection()
        moteur._query_lru = _FauxCache()
        moteur._query_filtered_lru = _FauxCache()
        return moteur

    def test_une_image_indexee_utilise_texte_image(self):
        moteur = self._moteur()
        with patch.object(RAGEngine, "_texte_image",
                          return_value="Description vision indexée.") as m:
            resultat = moteur.index_file(str(self.image))
        m.assert_called_once_with(str(self.image))
        self.assertEqual(moteur._col.documents, ["Description vision indexée."])
        # Le texte RÉELLEMENT indexé est rendu à l'appelant — c'est ce que
        # `_stream_load_sse` (modules/settings/router.py) réutilise pour le
        # résumé affiché à l'import, au lieu d'un second appel à
        # `read_file_text` qui retombait sur le placeholder pour une image.
        self.assertEqual(resultat, "Description vision indexée.")

    def test_un_placeholder_vide_n_indexe_rien(self):
        """Le cas dégradé au bout de la chaîne : `full_text` vide → sortie
        anticipée, comme pour n'importe quel autre format (cf.
        `test_ingestion_documents.py`).
        """
        moteur = self._moteur()
        with patch.object(RAGEngine, "_texte_image", return_value="   "):
            resultat = moteur.index_file(str(self.image))
        self.assertIsNone(moteur._col.documents)
        self.assertIsNone(resultat)


class DescribeImageDispatchTest(unittest.TestCase):
    """`LLMEngine.describe_image` : le format du message par provider.

    Les deux tests rejouent EXACTEMENT ce qui a été mesuré (cf. docstring du
    module) — pas une lecture de doc.
    """

    def test_ollama_recoit_le_chemin_du_fichier_dans_images(self):
        appels = {}

        def faux_chat(**kwargs):
            appels.update(kwargs)
            return {"message": {"content": "Un schéma de Thalès."}}

        # Le client DÉDIÉ à la vision, pas `ollama_client` (le partagé, dont le
        # timeout est `model.timeout_s` — 300 s, pensé pour un chargement à
        # froid de modèle de chat, pas pour cette méthode synchrone).
        original = module_llm._vision_ollama_client.chat
        module_llm._vision_ollama_client.chat = faux_chat
        try:
            moteur = LLMEngine(config_path=_CONFIG)
            resultat = moteur.describe_image(r"C:\cours\schema.png", "moondream")
        finally:
            module_llm._vision_ollama_client.chat = original

        self.assertEqual(resultat, "Un schéma de Thalès.")
        self.assertEqual(appels["model"], "moondream")
        message = appels["messages"][0]
        self.assertEqual(message["role"], "user")
        # Le chemin TEL QUEL, aucun encodage manuel — `ollama._types.Image` lit
        # et encode lui-même le fichier (vérifié : accepte str/bytes/Path).
        self.assertEqual(message["images"], [r"C:\cours\schema.png"])

    def test_le_client_ollama_dedie_a_un_timeout_court_et_independant(self):
        """`_vision_ollama_client` n'est PAS `ollama_client` : il a son propre
        timeout, court, sans toucher à celui du chat (`model.timeout_s`).

        Vérifié par appel réel (cf. docstring du module) : un timeout de
        lecture de 0,3 s sur un client dédié lève en 0,35 s sur un vrai appel
        à `moondream`, indépendamment du client partagé. Ici on fige la
        VALEUR configurée plutôt que de rejouer l'appel réseau à chaque run.
        """
        self.assertIsNot(module_llm._vision_ollama_client, module_llm.ollama_client)
        self.assertEqual(module_llm._vision_ollama_client._client.timeout.read,
                         module_llm._VISION_TIMEOUT_S)
        self.assertEqual(module_llm._VISION_TIMEOUT_S, 60.0)

    def test_flm_envoie_le_bloc_image_url_en_base64(self):
        vus = {}

        class _Message:
            content = "Une inscription : THALES 42"

        class _Choice:
            message = _Message()

        class _Reponse:
            choices = [_Choice()]

        class _Completions:
            def create(_self, **kw):
                vus.update(kw)
                return _Reponse()

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

            def with_options(_self, **kw):
                # `.with_options(timeout=..., max_retries=0)` rend le client
                # LUI-MÊME dans le vrai SDK — cf. le test dédié ci-dessous pour
                # la vérification des valeurs passées.
                return _self

        dossier = Path(tempfile.mkdtemp(prefix="epure-test-vision-flm-"))
        try:
            chemin = dossier / "photo.png"
            chemin.write_bytes(b"contenu-binaire-quelconque")

            moteur = LLMEngine(config_path=_CONFIG)
            moteur._openai_client = lambda provider: _Client()
            resultat = moteur.describe_image(str(chemin), "flm:qwen3vl-it:4b")

            self.assertEqual(resultat, "Une inscription : THALES 42")
            self.assertEqual(vus["model"], "qwen3vl-it:4b")
            message = vus["messages"][0]
            self.assertEqual(message["role"], "user")
            blocs = message["content"]
            self.assertEqual(blocs[0], {"type": "text", "text": module_llm._VISION_PROMPT})
            self.assertEqual(blocs[1]["type"], "image_url")

            import base64
            b64_attendu = base64.b64encode(chemin.read_bytes()).decode("ascii")
            self.assertEqual(blocs[1]["image_url"]["url"],
                             f"data:image/png;base64,{b64_attendu}")
        finally:
            import shutil
            shutil.rmtree(dossier, ignore_errors=True)

    def test_flm_utilise_un_timeout_court_et_desactive_les_relances(self):
        """`max_retries=0` est le point qui aurait pu passer inaperçu.

        Mesuré (cf. docstring du module) : la politique de relance PAR DÉFAUT
        du SDK openai (2 essais) MULTIPLIE l'attente sur un timeout au lieu de
        la borner — 5,4 s pour un `timeout=0.5` seul, 1,9 s avec
        `max_retries=0`. Sans ce réglage, `_VISION_TIMEOUT_S` ne bornerait
        rien : le pire cas réel serait environ 3x la valeur affichée.
        """
        options_vus = {}

        class _Completions:
            def create(_self, **kw):
                return type("R", (), {"choices": [
                    type("C", (), {"message": type("M", (), {"content": "ok"})()})()
                ]})()

        class _Chat:
            completions = _Completions()

        class _ClientAvecOptions:
            chat = _Chat()

            def with_options(_self, **kw):
                options_vus.update(kw)
                return _self

        dossier = Path(tempfile.mkdtemp(prefix="epure-test-vision-timeout-"))
        try:
            chemin = dossier / "photo.png"
            chemin.write_bytes(b"x")
            moteur = LLMEngine(config_path=_CONFIG)
            moteur._openai_client = lambda provider: _ClientAvecOptions()
            moteur.describe_image(str(chemin), "flm:qwen3vl-it:4b")
        finally:
            import shutil
            shutil.rmtree(dossier, ignore_errors=True)

        self.assertEqual(options_vus, {"timeout": module_llm._VISION_TIMEOUT_S,
                                       "max_retries": 0})

    def test_un_provider_non_vision_leve_clairement(self):
        """Aucun des deux chemins câblés : pas de tentative silencieuse dans un
        mauvais format. `premier_modele_vision_disponible()` ne rend jamais un
        tel modèle — cette garde ne protège que contre un appel direct erroné.
        """
        moteur = LLMEngine(config_path=_CONFIG)
        with self.assertRaises(ValueError):
            moteur.describe_image("photo.png", "gemini:gemini-2.5-flash")


class PremierModeleVisionDisponibleTest(unittest.TestCase):
    """`core.models.premier_modele_vision_disponible` : FLM d'abord, Ollama en
    repli, `None` si rien — la seule fonction qui a le droit de connaître des
    noms de modèles vision.
    """

    #: Nom délibérément différent de « moondream » (le défaut réel de
    #: config.yaml) : ces tests éprouvent la LOGIQUE de repli, pas le défaut
    #: shipé. Sans ce découplage, changer `vision.ollama_model` un jour ferait
    #: échouer ces tests pour une raison qui n'a rien à voir avec eux.
    _MODELE_OLLAMA_TEST = "vision-test-model"

    def setUp(self):
        self._originaux = {
            nom: getattr(core_models, nom)
            for nom in ("check_flm", "get_flm_installed", "flm_model_ids",
                       "get_ollama_installed", "_ollama_vision_model")
        }
        core_models._ollama_vision_model = lambda: self._MODELE_OLLAMA_TEST

    def tearDown(self):
        for nom, valeur in self._originaux.items():
            setattr(core_models, nom, valeur)

    def test_flm_prioritaire_quand_installe_et_joignable(self):
        core_models.check_flm = lambda: True
        core_models.get_flm_installed = lambda: {"qwen3vl-it:4b"}
        core_models.flm_model_ids = lambda: {"qwen3vl-it:4b"}
        core_models.get_ollama_installed = lambda: [self._MODELE_OLLAMA_TEST]
        self.assertEqual(core_models.premier_modele_vision_disponible(),
                         "flm:qwen3vl-it:4b")

    def test_repli_sur_ollama_si_flm_injoignable(self):
        core_models.check_flm = lambda: False
        core_models.get_ollama_installed = lambda: ["qwen2.5:7b", self._MODELE_OLLAMA_TEST]
        self.assertEqual(core_models.premier_modele_vision_disponible(),
                         self._MODELE_OLLAMA_TEST)

    def test_repli_sur_ollama_si_flm_joignable_mais_modele_pas_installe(self):
        core_models.check_flm = lambda: True
        core_models.get_flm_installed = lambda: set()
        core_models.flm_model_ids = lambda: set()
        core_models.get_ollama_installed = lambda: [self._MODELE_OLLAMA_TEST]
        self.assertEqual(core_models.premier_modele_vision_disponible(),
                         self._MODELE_OLLAMA_TEST)

    def test_aucun_modele_vision_nulle_part_rend_none(self):
        core_models.check_flm = lambda: False
        core_models.get_ollama_installed = lambda: ["qwen2.5:7b"]
        self.assertIsNone(core_models.premier_modele_vision_disponible())

    def test_ollama_injoignable_ne_leve_pas(self):
        """`get_ollama_installed()` rend `None` (serveur injoignable, cf. son
        docstring) — pas d'exception sur un `in` contre `None`.
        """
        core_models.check_flm = lambda: False
        core_models.get_ollama_installed = lambda: None
        self.assertIsNone(core_models.premier_modele_vision_disponible())


class OllamaVisionModelConfigTest(unittest.TestCase):
    """`_ollama_vision_model()` lit bien `config.yaml`, pas une valeur figée.

    Séparé de `PremierModeleVisionDisponibleTest` ci-dessus, qui la mocke
    justement pour ne PAS dépendre de ce défaut.
    """

    def test_le_defaut_shippe_est_moondream(self):
        """Le défaut vérifié empiriquement le 2026-09-01 (cf. docstring du
        module) — si ce test casse, c'est que `config.yaml` a changé, pas un
        hasard : la valeur qui a été mesurée doit être citée explicitement.
        """
        self.assertEqual(core_models._ollama_vision_model(), "moondream")

    def test_une_config_yaml_sans_section_vision_retombe_sur_moondream(self):
        """Un `config.yaml` écrit avant ce réglage n'a pas la clé — comme pour
        `model.timeout_s` (§8 CLAUDE.md), l'absence ne doit pas faire échouer.
        """
        dossier = Path(tempfile.mkdtemp(prefix="epure-test-vision-cfg-"))
        try:
            chemin = dossier / "config.yaml"
            chemin.write_text("model:\n  name: qwen2.5:7b\n", encoding="utf-8")
            with patch.object(core_models, "_CONFIG_FILE", chemin):
                self.assertEqual(core_models._ollama_vision_model(), "moondream")
        finally:
            import shutil
            shutil.rmtree(dossier, ignore_errors=True)


class _RagPourResume:
    """Simule `RAGEngine` APRÈS le correctif : `index_file` rend le texte
    qu'il a indexé — une description vision pour une image, comme le vrai
    moteur depuis ce fix. `_col.get` est nécessaire : `files_load` compte les
    chunks indexés après le flux.
    """

    class _Col:
        def get(self, where=None, include=None):
            return {"ids": []}

    def __init__(self, textes: dict[str, str]):
        self._textes = textes
        self._col = self._Col()
        self.appels_index: list[str] = []

    def index_file(self, path):
        self.appels_index.append(path)
        return self._textes.get(path)


class ResumeImportUtiliseLeTexteIndexeTest(unittest.TestCase):
    """Le bug trouvé après le premier merge (PR #16) : le résumé affiché à
    l'import d'une image disait systématiquement « je n'ai pas accès à
    l'image », même quand `rag.index_file` — appelé juste avant, dans la même
    boucle de `_stream_load_sse` — avait produit la vraie description. Cause :
    le résumé était construit par un SECOND appel, `RAGEngine.read_file_text`
    (statique), qui ne passe jamais par `_texte_image`/le modèle vision.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def setUp(self):
        self.auth = {"Authorization": f"Bearer {self.token}"}
        self.dossier = Path(tempfile.mkdtemp(prefix="epure-test-resume-image-"))

        from core import paths as core_paths
        original_roots = core_paths.user_data_roots
        core_paths.user_data_roots = lambda: [self.dossier.resolve()]
        self.addCleanup(setattr, core_paths, "user_data_roots", original_roots)

        original_rag = routeur_reglages.rag
        self.addCleanup(setattr, routeur_reglages, "rag", original_rag)

        original_stream = routeur_reglages.llm.stream
        self.addCleanup(setattr, routeur_reglages.llm, "stream", original_stream)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dossier, ignore_errors=True)

    def _charger(self, chemin):
        r = self.client.post("/files/load", json={"paths": [str(chemin)]},
                             headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)

    def test_le_resume_utilise_la_description_vision_pas_le_placeholder(self):
        image = self.dossier / "schema.png"
        image.write_bytes(b"\x89PNG fausse-image")
        description_vision = "Un schéma de Thalès : AB/AC = AM/AN = 3/5."

        routeur_reglages.rag = _RagPourResume({str(image.resolve()): description_vision})

        prompts_recus = []

        def faux_stream(messages, model=None, max_tokens=None, raisonnement=True):
            prompts_recus.append(messages)
            return iter(["Résumé du schéma."])

        routeur_reglages.llm.stream = faux_stream

        self._charger(image)

        self.assertEqual(len(prompts_recus), 1, "le résumé n'a pas été demandé")
        contenu_prompt = prompts_recus[0][0]["content"]
        # LE cœur du correctif : la vraie description est dans le prompt du
        # résumé, sans second appel qui l'aurait remplacée par le placeholder.
        self.assertIn(description_vision, contenu_prompt)
        self.assertNotIn("analyse vision non disponible", contenu_prompt)
        self.assertNotIn("je n'ai pas accès", contenu_prompt.lower())

    def test_un_texte_indexe_vide_ne_casse_pas_et_marque_quand_meme_charge(self):
        """`index_file` peut rendre `None` (rien indexé). `(text or "")[:3000]`
        ne doit pas lever, et le fichier reste attaché — comportement identique
        à avant ce correctif, où `read_file_text` pouvait aussi rendre une
        chaîne vide sans empêcher l'attachement.
        """
        fichier = self.dossier / "vide.txt"
        fichier.write_text("", encoding="utf-8")
        routeur_reglages.rag = _RagPourResume({})  # index_file rend None pour tout

        conv = routeur_reglages.history_engine.create_conversation()
        self.addCleanup(routeur_reglages.history_engine.delete_conversation, conv["id"])
        r = self.client.post(
            "/files/load",
            json={"paths": [str(fichier)], "conversation_id": conv["id"]},
            headers=self.auth,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            routeur_reglages.history_engine.get_conversation(conv["id"])["fichiers_attachés"],
            [str(fichier.resolve())],
        )

    def test_rag_index_file_reste_le_seul_appel_utilise(self):
        """Contre-épreuve structurelle : la double extraction est ÉLIMINÉE,
        pas seulement contournée pour les images — `RAGEngine` n'a plus besoin
        d'être importé dans ce routeur du tout (pas de nom à grep : un
        commentaire expliquant CE fix contient forcément le mot ``read_file_
        text``, cf. `test_taches_locales.ResumeImportTest._code_seul`).
        """
        self.assertFalse(hasattr(routeur_reglages, "RAGEngine"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
