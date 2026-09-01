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
   `test_ingestion_documents.py`).

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

import core.models as core_models  # noqa: E402
from core import llm as module_llm  # noqa: E402
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
            moteur.index_file(str(self.image))
        m.assert_called_once_with(str(self.image))
        self.assertEqual(moteur._col.documents, ["Description vision indexée."])

    def test_un_placeholder_vide_n_indexe_rien(self):
        """Le cas dégradé au bout de la chaîne : `full_text` vide → sortie
        anticipée, comme pour n'importe quel autre format (cf.
        `test_ingestion_documents.py`).
        """
        moteur = self._moteur()
        with patch.object(RAGEngine, "_texte_image", return_value="   "):
            moteur.index_file(str(self.image))
        self.assertIsNone(moteur._col.documents)


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

        original = module_llm.ollama_client.chat
        module_llm.ollama_client.chat = faux_chat
        try:
            moteur = LLMEngine(config_path=_CONFIG)
            resultat = moteur.describe_image(r"C:\cours\schema.png", "moondream")
        finally:
            module_llm.ollama_client.chat = original

        self.assertEqual(resultat, "Un schéma de Thalès.")
        self.assertEqual(appels["model"], "moondream")
        message = appels["messages"][0]
        self.assertEqual(message["role"], "user")
        # Le chemin TEL QUEL, aucun encodage manuel — `ollama._types.Image` lit
        # et encode lui-même le fichier (vérifié : accepte str/bytes/Path).
        self.assertEqual(message["images"], [r"C:\cours\schema.png"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
