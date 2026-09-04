#!/usr/bin/env python3
"""Une tâche de fond ne part JAMAIS vers le cloud sans choix explicite.

**LA RÈGLE**, et pourquoi elle a un fichier de test à elle : tout ce qui n'est
pas le tour de chat de l'utilisateur — résumé, titrage, fiches, plan de révision,
classification, réflexion de l'agent de code — tourne sur un modèle local, et ne
part vers un fournisseur distant que sur un choix explicite **pour cette tâche
précise**. Jamais en héritant de ``modèle_actif``, qui est un choix fait pour
répondre à un message.

**CE QUI SE PASSAIT AVANT**, mesuré site par site le 2026-08-24 : six tâches
lisaient ``ctx["modèle_actif"]``. Choisir Groq ou Gemini pour discuter suffisait
donc à envoyer, sans le moindre message :

* le CONTENU des fiches (``/skills/résumé`` : jusqu'à 12 000 caractères) ;
* le contenu des fichiers qu'on vient d'importer (résumé d'import) ;
* 14 000 caractères de cours (flashcards) ;
* les questions de kholle, la réponse de l'élève et son contexte mémoire ;
* le profil de révision — lacunes confirmées, forces, extraits de fiches.

Deux autres partaient vers le cloud **en dur**, sans même hériter : la
classification du palier Adaptatif (avant chaque message) et la réflexion de
l'agent de code (avant chaque demande de code).

**CE QUE CES TESTS GARDENT** : pour chaque site, qu'aucun chemin par défaut ne
peut produire un identifiant cloud. Ils posent exprès un ``modèle_actif`` cloud
ET toutes les clés d'API dans l'environnement — la configuration la plus
favorable à une fuite — puis vérifient ce qui arrive vraiment au moteur.

Usage :
    python test_taches_locales.py
"""

import asyncio
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
import modules.settings.router as routeur_reglages  # noqa: E402
from core import instance as mod_instance  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.instance import (  # noqa: E402
    est_modele_cloud, instance_config, modele_local_defaut, modele_pour_tache,
)

#: Modèle cloud posé comme `modèle_actif` : si un site le laisse fuir, il
#: apparaîtra tel quel dans l'appel capturé.
_ACTIF_CLOUD = "groq:openai/gpt-oss-120b"

#: Toutes les clés présentes = le pire cas. Un site qui teste « ai-je une clé ? »
#: avant de basculer en cloud le fera ici, et nulle part ailleurs dans la suite.
_CLES = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
         "MISTRAL_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"]


class _BaseLocale(unittest.TestCase):
    """Pose le pire cas : modèle de chat cloud + toutes les clés d'API."""

    def setUp(self):
        self._env = {c: os.environ.get(c) for c in _CLES}
        for c in _CLES:
            os.environ[c] = "cle-de-test"
        self._ctx = routeur_chat.memory.get_context().get("modèle_actif")
        routeur_chat.memory.update_context(**{"modèle_actif": _ACTIF_CLOUD})

    def tearDown(self):
        for c, v in self._env.items():
            if v is None:
                os.environ.pop(c, None)
            else:
                os.environ[c] = v
        routeur_chat.memory.update_context(**{"modèle_actif": self._ctx or "qwen2.5:7b"})

    def assertLocal(self, modele, ou=""):
        """Le modèle reçu est local — l'assertion centrale de tout ce fichier."""
        self.assertIsNotNone(modele, f"{ou} : modèle None → LLMEngine retombe sur "
                                     "config.yaml, donc hors du réglage")
        self.assertFalse(est_modele_cloud(modele),
                         f"{ou} : {modele!r} est un modèle CLOUD")
        self.assertNotEqual(modele, _ACTIF_CLOUD,
                            f"{ou} : le modèle du chat a fuité")


class AccesseurTest(_BaseLocale):
    """`modele_local_defaut` / `modele_pour_tache` — le contrat partagé."""

    def test_le_reglage_gagne_sur_config_yaml(self):
        instance_config.update({"providers": {"local": "flm:qwen3:4b"}})
        try:
            self.assertEqual(modele_local_defaut(), "flm:qwen3:4b")
        finally:
            instance_config.update({"providers": {"local": "qwen2.5:7b"}})

    def test_sans_reglage_on_retombe_sur_config_yaml(self):
        """Le comportement d'avant : aucune instance ne change de modèle en
        installant cette version.
        """
        instance_config.update({"providers": {"local": ""}})
        try:
            self.assertEqual(modele_local_defaut(), mod_instance._modele_config_yaml())
        finally:
            instance_config.update({"providers": {"local": "qwen2.5:7b"}})

    def test_un_modele_cloud_dans_le_reglage_est_ignore(self):
        """Vérifié à la LECTURE, pas seulement à l'écriture.

        Le 400 de `PUT /instance/config` couvre l'interface ; un fichier édité à
        la main, non. Un `providers.local` cloud viderait la règle de son sens en
        ayant l'air d'un réglage valide.
        """
        # Écrit directement, en contournant la validation de l'endpoint.
        instance_config.update({"providers": {"local": "x"}})
        cfg = instance_config.get()
        cfg["providers"]["local"] = _ACTIF_CLOUD
        instance_config._save(cfg)          # noqa: SLF001 — simule une édition manuelle
        instance_config._cache = None       # noqa: SLF001
        instance_config._ensure()           # noqa: SLF001
        try:
            self.assertLocal(modele_local_defaut(), "réglage édité à la main")
        finally:
            instance_config.update({"providers": {"local": "qwen2.5:7b"}})

    def test_flm_compte_comme_local(self):
        """Le NPU de la machine n'est pas un service distant. Le confondre avec
        du cloud interdirait le seul moteur local rapide de ce poste.
        """
        self.assertFalse(est_modele_cloud("flm:qwen3:4b"))
        self.assertFalse(est_modele_cloud("qwen2.5:7b"))
        self.assertTrue(est_modele_cloud("groq:openai/gpt-oss-120b"))
        self.assertTrue(est_modele_cloud("gemini:gemini-2.5-flash"))

    def test_lmstudio_compte_comme_local(self):
        """Même raisonnement que `flm` ci-dessus : un serveur qui tourne SUR ce
        poste, sans clé dans `_KEY_TO_PROVIDER` — rien à faire pour l'exclure
        du cloud, mais ça se casserait en silence si quelqu'un l'y ajoutait un
        jour par erreur.
        """
        self.assertFalse(est_modele_cloud("lmstudio:llama-3.1-8b-instruct"))

    def test_use_cloud_faux_rend_du_local(self):
        self.assertLocal(modele_pour_tache(False, _ACTIF_CLOUD, "GROQ_API_KEY"),
                         "use_cloud=False")

    def test_use_cloud_vrai_rend_le_modele_nomme_pour_la_tache(self):
        self.assertEqual(
            modele_pour_tache(True, "groq:openai/gpt-oss-120b", "GROQ_API_KEY"),
            "groq:openai/gpt-oss-120b")

    def test_use_cloud_vrai_sans_cle_retombe_en_local(self):
        """Une clé absente est un état prévu : répondre plus lentement vaut mieux
        qu'échouer.
        """
        os.environ.pop("GROQ_API_KEY", None)
        self.assertLocal(modele_pour_tache(True, "groq:x", "GROQ_API_KEY"),
                         "cloud demandé sans clé")

    def test_les_fournisseurs_cloud_derivent_de_la_table_des_cles(self):
        """Une clé ajoutée à `_KEY_TO_PROVIDER` doit compter comme cloud sans
        qu'on pense à toucher une seconde liste — sinon un modèle de ce
        fournisseur passerait pour local.
        """
        self.assertEqual(mod_instance._FOURNISSEURS_CLOUD,
                         frozenset(mod_instance._KEY_TO_PROVIDER.values()))


class _CaptureLLM:
    """Capture le `model` de chaque appel à `stream`/`generate`."""

    def __init__(self, reponse="ok"):
        self.modeles: list = []
        self._reponse = reponse

    def stream(self, messages, model=None, max_tokens=None, raisonnement=True):
        self.modeles.append(model)
        return iter([self._reponse])

    def generate(self, messages, model=None):
        self.modeles.append(model)
        return self._reponse


class SkillsResumeTest(_BaseLocale):
    """`/skills/résumé` — le site le plus exposé : il envoie le CONTENU des fiches."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54321))
        self.entetes = {"Authorization": f"Bearer {get_api_token()}"}
        self._original = routeur_chat.llm

    def tearDown(self):
        routeur_chat.llm = self._original
        super().tearDown()

    def test_le_resume_ne_part_jamais_vers_le_modele_du_chat(self):
        import tempfile
        capture = _CaptureLLM("Résumé.")
        routeur_chat.llm = capture
        dossier = tempfile.mkdtemp(prefix="epure-test-resume-")
        fichier = os.path.join(dossier, "cours.txt")
        with open(fichier, "w", encoding="utf-8") as f:
            f.write("Contenu de cours.")
        # Les fichiers sont attaches a une CONVERSATION depuis le 2026-08-27
        # (`fichiers_actifs` global retire) ; le pire cas teste par cette classe
        # — modele_actif cloud + toutes les cles presentes — est inchange.
        conv = routeur_chat.history_engine.create_conversation(fichiers=[fichier])
        try:
            r = self.client.post("/skills/résumé",
                                 json={"conversation_id": conv["id"]},
                                 headers=self.entetes)
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            routeur_chat.history_engine.delete_conversation(conv["id"])
        self.assertTrue(capture.modeles, "aucun appel LLM capturé")
        for modele in capture.modeles:
            self.assertLocal(modele, "/skills/résumé")


class ResumeImportTest(_BaseLocale):
    """Résumé automatique à l'import — TOUJOURS local, sans option.

    Décision explicite : ce résumé n'est pas demandé, il part dans le flux
    d'indexation. Pas de choix à faire, donc pas de cloud, donc pas de
    `use_cloud` à offrir — un drapeau ici serait une option que rien ne peut
    poser, sur le seul chemin où l'utilisateur n'a rien décidé.
    """

    @staticmethod
    def _code_seul(source: str) -> str:
        """La source SANS ses commentaires.

        Nécessaire, et déjà appris ailleurs dans ce dépôt
        (`test_paquet.DrapeauxDeBuildTest._code_seul`) : le commentaire de ce site
        EXPLIQUE pourquoi `use_cloud` n'y est pas, donc il contient le mot.
        Chercher dans le fichier entier fait échouer le test sur sa propre
        explication — mesuré, en écrivant ce test.
        """
        return chr(10).join(l.split("#")[0] for l in source.splitlines())

    def test_aucun_parametre_ne_permet_le_cloud(self):
        import inspect
        code = self._code_seul(inspect.getsource(routeur_reglages._stream_load_sse))
        self.assertIn("modele_local_defaut()", code)
        self.assertNotIn('ctx.get("modèle_actif")', code)
        # Et aucun `use_cloud` : l'absence est le sujet, pas un oubli.
        self.assertNotIn("use_cloud", code)


class DocAnalysisTest(_BaseLocale):
    """`summarize_document` / `summarize_section` — le `use_cloud` visait le chat."""

    def _moteur(self, capture):
        from core.docanalysis import DocAnalysisEngine
        moteur = object.__new__(DocAnalysisEngine)
        moteur._llm = capture           # noqa: SLF001
        return moteur

    def test_par_defaut_local(self):
        capture = _CaptureLLM()
        moteur = self._moteur(capture)
        list(moteur.summarize_section(["extrait"]))
        self.assertLocal(capture.modeles[0], "summarize_section par défaut")

    def test_use_cloud_vrai_ne_prend_pas_le_modele_du_chat(self):
        """Le point de la correction : `use_cloud=True` prend le modèle nommé POUR
        LA TÂCHE, pas celui que l'utilisateur a choisi pour discuter.
        """
        capture = _CaptureLLM()
        moteur = self._moteur(capture)
        list(moteur.summarize_section(["extrait"], use_cloud=True))
        recu = capture.modeles[0]
        from core.docanalysis import DocAnalysisEngine
        self.assertEqual(recu, DocAnalysisEngine._MODELE_CLOUD)

    def test_use_cloud_vrai_sans_cle_retombe_en_local(self):
        os.environ.pop("GROQ_API_KEY", None)
        capture = _CaptureLLM()
        moteur = self._moteur(capture)
        list(moteur.summarize_section(["extrait"], use_cloud=True))
        self.assertLocal(capture.modeles[0], "cloud sans clé")

    def test_un_modele_explicite_gagne(self):
        """Un appelant qui NOMME un modèle a fait un choix explicite : la règle
        l'autorise, c'est même sa définition.
        """
        capture = _CaptureLLM()
        moteur = self._moteur(capture)
        list(moteur.summarize_section(["extrait"], model="flm:qwen3:4b"))
        self.assertEqual(capture.modeles[0], "flm:qwen3:4b")

    def test_le_routeur_ne_lit_plus_le_modele_du_chat(self):
        import inspect
        import importlib.util
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "modules-catalogue", "docs", "router.py")
        source = open(os.path.normpath(chemin), encoding="utf-8").read()
        # Les deux endpoints de résumé ne doivent plus toucher `modèle_actif`.
        for bloc in ("docanalysis_deepen", "docanalysis_summarize"):
            debut = source.index(f"async def {bloc}")
            corps = source[debut:debut + 900]
            self.assertNotIn('ctx.get("modèle_actif")', corps, bloc)


class OrchestrateurTest(_BaseLocale):
    """`classify_task` — appel automatique AVANT CHAQUE MESSAGE en Adaptatif."""

    def _moteur(self, capture):
        from core.orchestrator import OrchestratorEngine
        return OrchestratorEngine(capture)

    def test_la_classification_est_locale(self):
        capture = _CaptureLLM("simple")
        moteur = self._moteur(capture)
        moteur.classify_task("une question", {})
        self.assertTrue(capture.modeles)
        self.assertLocal(capture.modeles[0], "classify_task")

    def test_meme_avec_toutes_les_cles_presentes(self):
        """Elle rendait `groq:llama-3.1-8b-instant` dès qu'une clé Groq existait.
        `setUp` en pose une : ce test échouerait sur l'ancien code.
        """
        capture = _CaptureLLM("simple")
        self._moteur(capture).classify_task("x", {})
        self.assertLocal(capture.modeles[0], "classify_task avec clés")

    def test_les_paliers_medium_et_high_gardent_leurs_defauts_cloud(self):
        """**Volontairement INTACTS.** Le cloud est le but assumé de ces paliers,
        et ils sont visibles et modifiables dans l'interface. Les basculer en
        local changerait la qualité de ce que l'utilisateur demande en choisissant
        « High » — ce n'est pas une tâche de fond, c'est son choix.
        """
        moteur = self._moteur(_CaptureLLM())
        for palier in ("medium", "high"):
            modeles = [s["model"] for s in moteur.build_steps(palier, [], {})]
            self.assertTrue(any(est_modele_cloud(m) for m in modeles),
                            f"{palier} : plus aucun modèle cloud — défaut modifié ?")

    def test_le_repli_local_du_palier_low_passe_par_le_reglage(self):
        instance_config.update({"providers": {"local": "flm:qwen3:4b"}})
        try:
            moteur = self._moteur(_CaptureLLM())
            steps = moteur.build_steps("low", [], {})
            roles = {s["role"]: s["model"] for s in steps}
            self.assertEqual(roles["contextualizer"], "flm:qwen3:4b")
        finally:
            instance_config.update({"providers": {"local": "qwen2.5:7b"}})


class ReflexionCodeagentTest(_BaseLocale):
    """`pick_reflection_model` — cloud automatique dès qu'une clé traînait."""

    def test_locale_par_defaut(self):
        from core.runtime import pick_reflection_model
        self.assertLocal(pick_reflection_model(None), "réflexion sans préférence")

    def test_locale_meme_avec_les_cles(self):
        """Elle rendait Gemini, sinon Groq, dès qu'une clé existait. `setUp` les
        pose toutes : ce test échouerait sur l'ancien code.
        """
        from core.runtime import pick_reflection_model
        self.assertLocal(pick_reflection_model(None), "réflexion avec clés")
        self.assertLocal(pick_reflection_model("qwen2.5:7b"), "préférence locale")

    def test_un_modele_cloud_explicitement_demande_est_respecte(self):
        """Le seul cloud restant ici, et c'est un choix : il vient du
        `pipeline.reflection.model` de l'écran Code, donc d'un réglage visible.
        """
        from core.runtime import pick_reflection_model
        self.assertEqual(pick_reflection_model("groq:openai/gpt-oss-120b"),
                         "groq:openai/gpt-oss-120b")

    def test_le_repli_n_est_plus_none(self):
        """`None` DÉSACTIVAIT l'étape (`if ref_enabled and eff_ref_model`) : sur
        une machine sans clé, la réflexion ne tournait jamais — une capacité
        absente en silence, alors qu'un modèle local en est capable.
        """
        from core.runtime import pick_reflection_model
        for c in _CLES:
            os.environ.pop(c, None)
        self.assertIsNotNone(pick_reflection_model(None))


class TitreConversationTest(_BaseLocale):
    """`history._generate_title` — après chaque conversation, sans être demandé."""

    def test_le_titre_est_genere_en_local(self):
        from core.history import HistoryEngine
        capture = _CaptureLLM("Un titre")
        moteur = object.__new__(HistoryEngine)
        moteur._llm = capture            # noqa: SLF001
        titre = HistoryEngine._generate_title(
            moteur, [{"role": "user", "content": "bonjour"}])
        self.assertEqual(titre, "Un titre")
        self.assertLocal(capture.modeles[0], "_generate_title")

    def test_il_passe_par_le_reglage(self):
        from core.history import HistoryEngine
        instance_config.update({"providers": {"local": "flm:qwen3:4b"}})
        try:
            capture = _CaptureLLM("T")
            moteur = object.__new__(HistoryEngine)
            moteur._llm = capture        # noqa: SLF001
            HistoryEngine._generate_title(moteur, [{"role": "user", "content": "x"}])
            self.assertEqual(capture.modeles[0], "flm:qwen3:4b")
        finally:
            instance_config.update({"providers": {"local": "qwen2.5:7b"}})


class CatalogueTest(_BaseLocale):
    """Les modules du catalogue : plus aucune lecture de `modèle_actif`.

    Testé sur la SOURCE et non par appel : ces routeurs ne sont pas montés dans
    l'arbre de test (`modules-catalogue/` est la source des modules installables,
    rien n'y est monté — CLAUDE.md §3.3), et les monter pour vérifier une ligne
    coûterait plus que ce que ça prouve.
    """

    _SITES = {
        "flashcards/router.py": ["modele_pour_tache("],
        "kholle/router.py": ["modele_local_defaut()", "modele_pour_tache("],
        "reviseur/router.py": ["modele_local_defaut()"],
        "docs/router.py": ["req.use_cloud"],
    }

    def _source(self, relatif):
        chemin = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "modules-catalogue", relatif))
        with open(chemin, encoding="utf-8") as f:
            return f.read()

    def test_aucun_module_ne_lit_le_modele_actif_pour_une_tache_de_fond(self):
        for relatif in self._SITES:
            with self.subTest(module=relatif):
                source = self._source(relatif)
                self.assertNotIn('ctx.get("modèle_actif")', source, relatif)
                self.assertNotIn('get_context().get("modèle_actif")', source, relatif)

    def test_chaque_module_passe_par_le_contrat_partage(self):
        for relatif, attendus in self._SITES.items():
            source = self._source(relatif)
            for attendu in attendus:
                with self.subTest(module=relatif, attendu=attendu):
                    self.assertIn(attendu, source)

    def test_les_cibles_cloud_sont_nommees_pour_la_tache(self):
        """`use_cloud=True` doit viser un modèle DÉCIDÉ pour la tâche. Un module
        qui reprendrait `modèle_actif` comme cible cloud aurait un `use_cloud`
        d'apparence correcte et le même bug qu'avant.
        """
        for relatif in ("flashcards/router.py", "kholle/router.py"):
            with self.subTest(module=relatif):
                self.assertIn('_MODELE_CLOUD = "groq:', self._source(relatif))


class EndpointReglageTest(_BaseLocale):
    """`PUT /instance/config` : le réglage refuse un modèle cloud."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54321))
        self.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def test_un_modele_cloud_est_refuse_avec_un_message_utile(self):
        r = self.client.put("/instance/config", headers=self.entetes,
                            json={"providers": {"local": _ACTIF_CLOUD}})
        self.assertEqual(r.status_code, 400, r.text)
        detail = r.json()["detail"]
        self.assertIn(_ACTIF_CLOUD, detail)
        # Le message dit QUOI faire, pas seulement que c'est refusé.
        self.assertIn("local", detail.lower())
        # Et rien n'a été écrit.
        self.assertNotEqual(instance_config.get()["providers"]["local"], _ACTIF_CLOUD)

    def test_un_modele_local_est_accepte(self):
        for local in ("qwen2.5:7b", "flm:qwen3:4b"):
            with self.subTest(local=local):
                r = self.client.put("/instance/config", headers=self.entetes,
                                    json={"providers": {"local": local}})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["providers"]["local"], local)
        instance_config.update({"providers": {"local": "qwen2.5:7b"}})

    def test_le_modele_du_chat_reste_libre(self):
        """`providers.actif` peut être cloud — c'est le sujet même du chat. La
        validation ne doit pas déborder sur lui.
        """
        r = self.client.put("/instance/config", headers=self.entetes,
                            json={"providers": {"actif": _ACTIF_CLOUD}})
        self.assertEqual(r.status_code, 200, r.text)


class AucuneLectureResiduelleTest(unittest.TestCase):
    """Le garde-fou d'ensemble : plus une seule lecture de `_llm._model`.

    Cinq sites la faisaient, donc retombaient sur `config.yaml` — un fichier que
    l'utilisateur n'édite pas depuis l'interface, ce qui rendait le réglage
    contournable sans le savoir. Ce test attrape la sixième, celle qu'on ajoutera
    sans y penser.
    """

    def test_plus_aucun_llm_model_en_dur(self):
        import inspect
        from core import docanalysis, history, orchestrator
        for module in (docanalysis, history, orchestrator):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("self._llm._model", source)
                self.assertNotIn("_llm._model", source)


class CataloguesSansModeleMortTest(unittest.TestCase):
    """Aucun catalogue en dur ne cite un modèle retiré par son fournisseur.

    **Trois entrées Groq étaient mortes**, mesuré le 2026-08-24 par
    `client.models.list()` puis par un appel réel : `llama-3.1-8b-instant`,
    `llama-3.3-70b-versatile` et `deepseek-r1-distill-llama-70b` répondaient tous
    404. Groq n'avait plus aucun modèle Llama de chat à son catalogue.

    Ce que ça coûtait, et c'est ce qui rend ce test utile plutôt que cosmétique :

    * `consolidation._pick_model` visait le second → la branche « cloud » de la
      consolidation échouait, et le `except` de chaque `consolidate_*` la faisait
      retomber en local **sans le dire**. Une option qui avait l'air de marcher ;
    * `orchestrator._CLASSIFY_MODEL_GROQ` visait le premier → le palier Adaptatif
      se comportait comme Direct, son `except` rendant `{"complexity": "simple"}` ;
    * `RECOMMENDATION_OVERRIDES` et les recommandations curées les proposaient à
      l'utilisateur, qui suivait le conseil et tombait sur une erreur sans pouvoir
      la relier au conseil.

    Le test ne peut pas appeler Groq (pas de réseau dans la suite, et une clé n'est
    pas garantie) : il vérifie qu'aucune des trois chaînes ne revient dans un
    catalogue en dur. C'est la régression réelle — ces identifiants ont été copiés
    d'un site à l'autre, et c'est comme ça qu'ils ont survécu à leur retrait.
    """

    #: Retirés de Groq. La liste est là pour être ALLONGÉE quand un fournisseur
    #: retire encore un modèle — c'est un journal, pas une exhaustivité.
    MORTS = (
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "deepseek-r1-distill-llama-70b",
    )

    def _utilise(self, source: str, modele: str) -> bool:
        """L'identifiant est-il UTILISÉ comme valeur, et pas seulement mentionné ?

        Première version de ce test : retirer les commentaires (`#`, `//`) puis
        chercher la chaîne. Insuffisant — l'identifiant apparaît aussi dans des
        **docstrings**, qui ne sont ni l'un ni l'autre. Le docstring de
        `_classify_model` explique justement que ce modèle répond 404, et faisait
        donc échouer le garde-fou sur sa propre explication. Retirer les
        docstrings ligne par ligne demandait de suivre l'état d'ouverture des
        triples guillemets : plus de machinerie que ce que le test vaut.

        DEUX filtres, et chacun couvre l'angle mort de l'autre :

        1. on retire les commentaires de ligne (`#`, `//`) — parce qu'un
           commentaire peut REPRODUIRE la ligne de code supprimée, guillemets
           compris (`# _CLASSIFY_MODEL_GROQ = "groq:…" vivait ici`), ce qu'un
           test de littéral prendrait pour une vraie utilisation ;
        2. dans ce qui reste, on cherche un **littéral de chaîne** — avec ou sans
           préfixe de fournisseur : `"llama-3.3-70b-versatile"` comme entrée de
           table, `'groq:llama-3.1-8b-instant'` comme identifiant complet. Les
           docstrings, eux, citent ces noms entre backticks : ils passent, donc
           une trace écrite du retrait reste possible — ce qui est le but, sinon
           on ne peut pas documenter ce qu'on retire.

        Les deux essais précédents ont chacun échoué sur l'angle mort de l'autre,
        et c'est ce qui a mené à cette forme.
        """
        sans_commentaires = chr(10).join(
            l.split("#")[0].split("//")[0] for l in source.splitlines())
        motif = re.compile(r"""["']([a-z0-9_]+:)?""" + re.escape(modele) + r"""["']""")
        return bool(motif.search(sans_commentaires))

    def test_aucune_table_de_modeles_ne_cite_un_mort(self):
        from core import models
        source = open(models.__file__, encoding="utf-8").read()
        for mort in self.MORTS:
            with self.subTest(modele=mort):
                self.assertFalse(self._utilise(source, mort),
                                 f"{mort} est encore utilisé dans core/models.py")

    def test_l_orchestrateur_ne_cite_plus_un_mort(self):
        """Les paliers et les presets livrés, restés sur l'id mort après c7c95e5.

        Cinq sites : les `recommended` de `medium/analyzer`, `high/analyzer`,
        `high/verifier`, et les étapes `analyzer`/`verifier` du preset livré
        « Kholle maths ». Ils avaient survécu au nettoyage de `core/models.py`
        parce qu'ils vivent dans un AUTRE fichier — c'est exactement comment un
        identifiant copié d'un site à l'autre survit au retrait du modèle.
        """
        from core import orchestrator
        source = open(orchestrator.__file__, encoding="utf-8").read()
        for mort in self.MORTS:
            with self.subTest(modele=mort):
                self.assertFalse(self._utilise(source, mort),
                                 f"{mort} est encore utilisé dans core/orchestrator.py")

    def test_les_paliers_restent_cloud_par_defaut(self):
        """La contre-épreuve du test précédent, et elle compte autant.

        Corriger un identifiant mort ne doit pas devenir « passer les paliers en
        local » : Medium et High sont cloud par choix assumé, et c'est ce que
        l'utilisateur demande en les sélectionnant. Sans cette assertion, un
        remplacement par un modèle local passerait le test ci-dessus en cassant
        la politique.
        """
        from core.orchestrator import EFFORT_PIPELINES
        for palier in ("medium", "high"):
            recommandes = [t.get("recommended") for t in EFFORT_PIPELINES[palier]]
            with self.subTest(palier=palier):
                self.assertTrue(any(r and est_modele_cloud(r) for r in recommandes),
                                f"{palier} n'a plus aucun modèle cloud recommandé")

    def test_les_paliers_du_backend_et_de_l_interface_concordent(self):
        """Le backend et `EFFORT_DEFINITIONS` de `ModuleBar.tsx` doivent nommer le
        MÊME modèle pour un rôle donné.

        C'est la divergence qui a laissé l'id mort survivre : l'interface affichait
        déjà `groq:openai/gpt-oss-120b` quand le backend envoyait le deepseek
        retiré. L'utilisateur lisait un modèle dans le panneau Effort, et un autre
        partait — un écart qu'aucun des deux côtés ne pouvait signaler seul.

        Comparaison limitée aux rôles dont le frontend nomme un modèle : ses
        `recommended: null` veulent dire « le backend décide » (`active`/`local`),
        ce qui n'est pas une divergence.
        """
        chemin = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "frontend", "src", "components", "ModuleBar.tsx"))
        with open(chemin, encoding="utf-8") as f:
            source = f.read()
        bloc = source[source.index("const EFFORT_DEFINITIONS"):
                      source.index("const EFFORT_LABELS")]
        attendu, palier = {}, None
        for ligne in bloc.splitlines():
            m = re.match(r"\s*(low|medium|high):", ligne)
            if m:
                palier = m.group(1)
            m2 = re.search(r"role: '([^']+)'.*recommended: '([^']+)'", ligne)
            if m2 and palier:
                attendu[(palier, m2.group(1))] = m2.group(2)
        self.assertTrue(attendu, "aucune recommandation lue dans ModuleBar.tsx")

        from core.orchestrator import EFFORT_PIPELINES
        for (palier, role), modele_front in attendu.items():
            tpl = next((t for t in EFFORT_PIPELINES[palier] if t["role"] == role), None)
            with self.subTest(palier=palier, role=role):
                self.assertIsNotNone(tpl, f"{palier}/{role} absent du backend")
                self.assertEqual(tpl.get("recommended"), modele_front)

    def test_le_preset_livre_ne_cite_plus_un_mort(self):
        """Les presets LIVRÉS (`_DEFAULT_PRESETS`), lus quand aucun fichier
        n'existe encore — donc sur toute installation neuve.

        À savoir, et hors de portée d'un test : un `orchestrator_presets.json`
        déjà écrit garde ses anciens identifiants. `_DEFAULT_PRESETS` ne s'applique
        qu'à un fichier absent, et ce fichier est une donnée utilisateur qu'on ne
        réécrit pas.
        """
        from core.orchestrator import _DEFAULT_PRESETS
        for preset in _DEFAULT_PRESETS:
            for etape in preset["steps"]:
                with self.subTest(preset=preset["nom"], role=etape["role"]):
                    for mort in self.MORTS:
                        self.assertNotIn(mort, etape["model"])

    def test_la_cible_cloud_de_la_consolidation_est_vivante(self):
        from core.consolidation import _CLOUD_MODEL
        for mort in self.MORTS:
            self.assertNotIn(mort, _CLOUD_MODEL)
        # Et elle reste bien du Groq : la correction ne change pas de fournisseur,
        # seulement d'identifiant.
        self.assertTrue(_CLOUD_MODEL.startswith("groq:"))

    def test_les_recommandations_pointent_des_modeles_du_catalogue(self):
        """Une recommandation doit désigner un modèle que le catalogue propose.

        `RECOMMENDATION_OVERRIDES` visait `deepseek-r1-distill-llama-70b`, absent
        de `_GROQ_STATIC` depuis son retrait : la recommandation survivait à la
        disparition du modèle recommandé.
        """
        from core.models import RECOMMENDATION_OVERRIDES, _GROQ_STATIC
        connus = set(_GROQ_STATIC)
        for usage, ident in RECOMMENDATION_OVERRIDES.items():
            if not ident.startswith("groq:"):
                continue
            with self.subTest(usage=usage):
                self.assertIn(ident.split(":", 1)[1], connus, ident)

    def test_le_frontend_ne_recommande_plus_un_mort(self):
        """`MODULE_RECOMMENDATIONS` de `ModuleBar.tsx` nomme des IDs en dur.

        Ils ne peuvent pas se déduire d'une liste — c'est justement pourquoi ils
        survivent à un retrait côté fournisseur.
        """
        chemin = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "frontend", "src", "components", "ModuleBar.tsx"))
        with open(chemin, encoding="utf-8") as f:
            source = f.read()
        for mort in self.MORTS:
            with self.subTest(modele=mort):
                self.assertFalse(self._utilise(source, mort),
                                 f"{mort} est encore recommandé par ModuleBar.tsx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
