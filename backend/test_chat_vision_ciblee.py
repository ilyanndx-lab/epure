"""Analyse vision CIBLÉE d'une image attachée, dans le chat — chantier 2026-09-07.

Le cas réel qui a lancé ce chantier, et que `TourDeChatTest` rejoue : une image
d'énoncé mathématique attachée à une conversation, une vraie question posée
dessus, et une réponse construite sur la seule LÉGENDE GÉNÉRIQUE produite à
l'import (« Décris cette image et transcris tout texte visible »). Le chat ne
voyait jamais l'image ; la légende ne permettait pas de répondre.

Ce que ce fichier verrouille, dans l'ordre du chantier :

* `core.llm.prompt_vision` — générique SANS question, ciblé AVEC, et le chemin
  d'import inchangé (`test_vision_images.py` reste vrai sans une ligne de
  modification, c'est le contrôle) ;
* `core.models.modele_vision_pour` — le modèle actif s'il déclare la vision,
  sinon le choix de l'import, et le cloud **seulement** sur une absence locale ;
* `core.vision_chat` — la règle de déclenchement (image attachée + pas
  d'analyse), les trois bornes de contexte, et le filtre anti-doublon du RAG ;
* le tour de chat complet : première question → analyse ciblée dans le prompt,
  deuxième question → **aucun** nouvel appel vision, `@image` → réanalyse.

Le piège que ces tests ont attrapé pendant l'écriture : sans le filtre
`chunk_redondant`, le prompt portait les DEUX descriptions de la même image —
l'analyse ciblée et la légende d'import remontée par le RAG, cette dernière
étant la plus longue et la moins utile.

Usage :
    python test_chat_vision_ciblee.py
"""

import json
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
import modules.chat.router as routeur_chat  # noqa: E402
from core import models as core_models  # noqa: E402
from core import vision_chat  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.history import HistoryEngine  # noqa: E402
from core.llm import _VISION_PROMPT, prompt_vision  # noqa: E402
from core.paths import cle_chemin  # noqa: E402
from core.runtime import history_engine  # noqa: E402

_WS = "ws://localhost/ws/chat?token={t}"

#: Ce que le modèle vision est censé rendre quand on lui pose la vraie question,
#: et ce que la légende d'import ne contient pas. Deux marqueurs disjoints : le
#: test ne peut pas confondre « l'analyse ciblée est là » avec « il y a du texte
#: qui parle de l'image ».
_CIBLE = "AB/AC = AM/AN = 3/5, donc (MN) est parallèle à (BC)"
_GENERIQUE = "une figure geometrique avec des lettres et des chiffres"


class PromptVisionTest(unittest.TestCase):
    """§1 du chantier : `describe_image` accepte une question."""

    def test_sans_question_le_prompt_est_celui_de_l_import(self):
        """Le contrôle de non-régression du chemin d'import.

        Si cette assertion tombe, c'est le résumé d'import et l'indexation des
        images qui changent de comportement — pas seulement le chat.
        """
        self.assertEqual(prompt_vision(), _VISION_PROMPT)
        self.assertEqual(prompt_vision(None), _VISION_PROMPT)

    def test_une_question_blanche_ne_fabrique_pas_un_prompt_creux(self):
        self.assertEqual(prompt_vision("   \n "), _VISION_PROMPT)

    def test_la_question_part_dans_le_prompt(self):
        p = prompt_vision("Que vaut le rapport AB/AC ?")
        self.assertIn("Que vaut le rapport AB/AC ?", p)
        self.assertNotEqual(p, _VISION_PROMPT)

    def test_la_question_est_bornee(self):
        """`moondream` dégénère sur un prompt long (mesuré) — et la question est
        du texte que le dépôt n'écrit pas. La borne porte donc sur elle."""
        p = prompt_vision("A" * 5000)
        self.assertLess(len(p), 1000)


class ModeleVisionPourTest(unittest.TestCase):
    """§2 du chantier : quel modèle analyse, et le cloud en dernier recours."""

    def setUp(self):
        self._sondes = {}
        for nom in ("capacites_installees", "premier_modele_vision_disponible",
                    "premier_modele_vision_cloud_disponible", "get_ollama_installed"):
            self._sondes[nom] = getattr(core_models, nom)
            self.addCleanup(setattr, core_models, nom, self._sondes[nom])

    def _poser(self, capacites=None, local=None, cloud=None):
        core_models.capacites_installees = lambda: capacites
        core_models.premier_modele_vision_disponible = lambda: local
        core_models.premier_modele_vision_cloud_disponible = lambda: cloud

    def test_le_modele_actif_est_prefere_s_il_declare_la_vision(self):
        """La « sélection manuelle » du chantier : l'utilisateur voit déjà l'œil
        de `iconesCapacites` dans la liste des modèles. Le choisir pour discuter
        le choisit pour regarder — et évite un second modèle résident."""
        self._poser(capacites={"moondream:latest": {"vision"}}, local="flm:autre")
        self.assertEqual(core_models.modele_vision_pour("moondream"), "moondream")

    def test_un_modele_actif_sans_vision_ne_capte_pas_l_analyse(self):
        self._poser(capacites={"qwen2.5:7b": {"tools"}}, local="flm:qwen3vl-it:4b")
        self.assertEqual(
            core_models.modele_vision_pour("qwen2.5:7b"), "flm:qwen3vl-it:4b")

    def test_une_capacite_INCONNUE_ne_vaut_pas_une_absence(self):
        """`None` ≠ `False` (`decrire_capacites`). Un Ollama qui ne déclare rien
        pour ce modèle ne doit pas faire croire qu'il ne voit pas — mais ne doit
        pas non plus faire envoyer une image à un modèle qui ne la lira pas :
        on ne DÉDUIT rien, on retombe sur le choix mesuré."""
        self._poser(capacites={"mystere:7b": None}, local="flm:qwen3vl-it:4b")
        self.assertEqual(
            core_models.modele_vision_pour("mystere:7b"), "flm:qwen3vl-it:4b")

    def test_ollama_injoignable_retombe_sur_le_choix_de_l_import(self):
        """`capacites_installees()` rend `None` quand Ollama ne répond pas.
        Ce n'est pas « aucune vision disponible » — FLM n'a pas été sondé."""
        self._poser(capacites=None, local="flm:qwen3vl-it:4b")
        self.assertEqual(
            core_models.modele_vision_pour("qwen2.5:7b"), "flm:qwen3vl-it:4b")

    def test_un_modele_actif_CLOUD_ne_devient_pas_le_modele_de_vision(self):
        """CLAUDE.md §3.7 : le cloud ne part pas en héritant de `modèle_actif`.
        Choisir Groq pour discuter n'est pas choisir d'y envoyer ses images."""
        self._poser(capacites={}, local="moondream:latest", cloud="mistral:pixtral")
        self.assertEqual(
            core_models.modele_vision_pour("groq:openai/gpt-oss-120b"), "moondream:latest")

    def test_le_cloud_ne_sort_que_sur_une_absence_locale_constatee(self):
        self._poser(capacites={}, local=None, cloud="mistral:pixtral-12b-2409")
        self.assertEqual(core_models.modele_vision_pour(None), "mistral:pixtral-12b-2409")

    def test_aucun_modele_du_tout_rend_None(self):
        self._poser(capacites={}, local=None, cloud=None)
        self.assertIsNone(core_models.modele_vision_pour("qwen2.5:7b"))

    def test_gemini_reste_hors_de_la_table_cloud(self):
        """`gemini` n'est pas dans `_OPENAI_COMPAT` : `describe_image` LÈVERAIT
        au lieu de dégrader. Un repli qui lève n'est pas un repli."""
        for model_id, _cle in core_models._VISION_CLOUD:
            self.assertFalse(model_id.startswith("gemini"), model_id)

    def test_la_cle_absente_ecarte_le_modele_cloud(self):
        for _model_id, cle in core_models._VISION_CLOUD:
            self.assertTrue(cle.endswith("_API_KEY"), cle)
        anciennes = {cle: os.environ.pop(cle, None)
                     for _m, cle in core_models._VISION_CLOUD}
        for cle, val in anciennes.items():
            if val is not None:
                self.addCleanup(os.environ.__setitem__, cle, val)
        self.assertIsNone(core_models.premier_modele_vision_cloud_disponible())


class DeclenchementTest(unittest.TestCase):
    """§2 et §4 : la règle de déclenchement, et la réanalyse qui ne s'invite pas."""

    def _analyse(self, texte=_CIBLE, question="et alors ?"):
        return {"chemin": "x", "question": question, "texte": texte,
                "modèle": "m", "horodatage": "", "tronquée": False}

    def test_une_image_sans_analyse_declenche(self):
        self.assertEqual(
            vision_chat.images_a_analyser(["/f/enonce.png"], {}), ["/f/enonce.png"])

    def test_un_pdf_ne_declenche_rien(self):
        self.assertEqual(vision_chat.images_a_analyser(["/f/cours.pdf"], {}), [])

    def test_une_image_DEJA_analysee_ne_redeclenche_pas(self):
        """Le cœur de §4 : aucune réanalyse automatique, quelle que soit la
        question suivante. 6 à 26 s par image, et l'utilisateur n'a rien
        demandé."""
        cache = {cle_chemin("/f/enonce.png"): self._analyse()}
        self.assertEqual(vision_chat.images_a_analyser(["/f/enonce.png"], cache), [])

    def test_l_override_redeclenche(self):
        cache = {cle_chemin("/f/enonce.png"): self._analyse()}
        self.assertEqual(
            vision_chat.images_a_analyser(["/f/enonce.png"], cache, force=True),
            ["/f/enonce.png"])

    def test_la_cle_tolere_deux_ecritures_du_meme_chemin(self):
        """Sous Windows, `C:/x/a.png` et `C:\\x\\a.png` sont le même fichier —
        l'analyser deux fois coûterait 26 s pour rien."""
        cache = {cle_chemin("C:/f/enonce.png"): self._analyse()}
        self.assertEqual(
            vision_chat.images_a_analyser(["C:\\f\\enonce.png"], cache), [])

    def test_le_nombre_d_images_par_tour_est_borne(self):
        """Borne de LATENCE : cinq images feraient deux minutes de silence."""
        images = [f"/f/{i}.png" for i in range(5)]
        a_faire = vision_chat.images_a_analyser(images, {})
        self.assertEqual(len(a_faire), vision_chat.MAX_IMAGES_PAR_TOUR)
        self.assertEqual(vision_chat.restantes(images, {}),
                         5 - vision_chat.MAX_IMAGES_PAR_TOUR)

    def test_une_analyse_vide_n_est_pas_conservee(self):
        """Rien d'écrit → l'image reste « à analyser », donc le tour suivant
        réessaie. Une panne de modèle ne condamne pas une image."""
        class _Muet:
            def describe_image(self, *a, **kw):
                return "   "
        self.assertIsNone(vision_chat.analyser(_Muet(), "/f/x.png", "q", "m"))

    def test_une_exception_du_modele_ne_remonte_jamais(self):
        class _Casse:
            def describe_image(self, *a, **kw):
                raise RuntimeError("timeout")
        self.assertIsNone(vision_chat.analyser(_Casse(), "/f/x.png", "q", "m"))

    def test_sans_modele_ni_llm_rien_ne_leve(self):
        self.assertIsNone(vision_chat.analyser(None, "/f/x.png", "q", "m"))

        class _Ok:
            def describe_image(self, *a, **kw):
                raise AssertionError("ne doit pas être appelé sans modèle")
        self.assertIsNone(vision_chat.analyser(_Ok(), "/f/x.png", "q", ""))

    def test_la_question_part_bien_au_modele(self):
        vues = {}

        class _Espion:
            def describe_image(self, path, model, question=None):
                vues["question"] = question
                return _CIBLE
        rec = vision_chat.analyser(_Espion(), "/f/x.png", "Que vaut AB/AC ?", "m")
        self.assertEqual(vues["question"], "Que vaut AB/AC ?")
        self.assertEqual(rec["texte"], _CIBLE)
        self.assertEqual(rec["question"], "Que vaut AB/AC ?")


class ContexteTest(unittest.TestCase):
    """§3 : le cache par FICHIER, injecté, et borné."""

    def _analyse(self, texte, question="q"):
        return {"chemin": "x", "question": question, "texte": texte,
                "modèle": "m", "horodatage": "", "tronquée": False}

    def test_le_bloc_porte_l_analyse_et_la_question_qui_l_a_produite(self):
        cache = {cle_chemin("/f/enonce.png"): self._analyse(_CIBLE, "Que vaut AB/AC ?")}
        bloc = vision_chat.bloc_contexte(["/f/enonce.png"], cache)
        self.assertIn(_CIBLE, bloc)
        self.assertIn("enonce.png", bloc)
        # La question est nommée pour qu'un tour suivant, portant une question
        # sans rapport, ne lise pas l'analyse comme si elle y répondait.
        self.assertIn("Que vaut AB/AC ?", bloc)

    def test_une_image_sans_analyse_n_apparait_pas(self):
        self.assertEqual(vision_chat.bloc_contexte(["/f/enonce.png"], {}), "")

    def test_le_cache_est_par_FICHIER_pas_par_conversation(self):
        """Deux images attachées, une seule analysée : la seconde n'hérite de
        rien. C'est ce qu'un blob par conversation (`résumé_contexte`) ne sait
        pas faire, et la raison d'être de ce stockage."""
        cache = {cle_chemin("/f/a.png"): self._analyse("analyse de A")}
        bloc = vision_chat.bloc_contexte(["/f/a.png", "/f/b.png"], cache)
        self.assertIn("analyse de A", bloc)
        self.assertIn("a.png", bloc)
        self.assertNotIn("b.png", bloc)

    def test_le_texte_conserve_est_tronque_a_l_ECRITURE(self):
        """Tronqué au stockage, pas à l'injection : le disque et le prompt
        doivent dire la même chose."""
        class _Bavard:
            def describe_image(self, *a, **kw):
                return "X" * (vision_chat.MAX_CARACTERES_ANALYSE + 5000)
        rec = vision_chat.analyser(_Bavard(), "/f/x.png", "q", "m")
        self.assertEqual(len(rec["texte"]), vision_chat.MAX_CARACTERES_ANALYSE)
        self.assertTrue(rec["tronquée"])

    def test_le_total_injecte_est_borne_et_l_omission_est_ECRITE(self):
        """Un contexte tronqué en silence est la panne que ce dépôt paie le
        plus cher : le modèle doit savoir qu'il lui manque quelque chose."""
        gros = "Y" * vision_chat.MAX_CARACTERES_ANALYSE
        images = [f"/f/{i}.png" for i in range(6)]
        cache = {cle_chemin(p): self._analyse(gros) for p in images}
        bloc = vision_chat.bloc_contexte(images, cache)
        self.assertLessEqual(
            len(bloc), vision_chat.MAX_CARACTERES_CONTEXTE + 500)
        self.assertIn("non reprise", bloc)

    def test_le_chunk_generique_d_une_image_analysee_est_ecarte(self):
        """Sans ce filtre, le prompt porte les DEUX descriptions de la même
        image : l'analyse ciblée et la légende d'import remontée par le RAG."""
        cache = {cle_chemin("/f/enonce.png"): self._analyse(_CIBLE)}
        chunk = {"texte": f"Image : enonce.png\n\n{_GENERIQUE}",
                 "source": "/f/enonce.png"}
        self.assertTrue(vision_chat.chunk_redondant(chunk, cache))
        # Une image SANS analyse ciblée garde sa légende : c'est mieux que rien.
        self.assertFalse(vision_chat.chunk_redondant(chunk, {}))
        # Un document n'est jamais écarté.
        self.assertFalse(vision_chat.chunk_redondant(
            {"texte": "cours", "source": "/f/cours.pdf"}, cache))


class PersistanceTest(unittest.TestCase):
    """§3 : l'analyse survit sur le disque, par fichier, sans second stockage."""

    def test_une_conversation_ancienne_gagne_la_cle_a_la_LECTURE(self):
        """Même discipline que `fichiers_attachés` : `_normaliser` complète en
        mémoire, sans réécrire un fichier que personne n'a modifié."""
        conv = history_engine.create_conversation(messages=[])
        self.assertEqual(conv["analyses_image"], {})
        chemin = history_engine._conv_path(conv["id"])
        from core.jsonstore import read_json, write_json
        brut = read_json(chemin, {})
        del brut["analyses_image"]
        write_json(chemin, brut)
        relu = history_engine.get_conversation(conv["id"])
        self.assertEqual(relu["analyses_image"], {})

    def test_une_valeur_du_mauvais_TYPE_ne_fait_pas_boucler(self):
        conv = history_engine.create_conversation(messages=[])
        chemin = history_engine._conv_path(conv["id"])
        from core.jsonstore import read_json, write_json
        brut = read_json(chemin, {})
        brut["analyses_image"] = "oups"
        write_json(chemin, brut)
        self.assertEqual(
            history_engine.get_conversation(conv["id"])["analyses_image"], {})

    def test_l_analyse_est_ecrite_sous_la_cle_du_CHEMIN(self):
        conv = history_engine.create_conversation(messages=[])
        ok = history_engine.set_analyse_image(
            conv["id"], "C:/f/enonce.png", {"texte": _CIBLE})
        self.assertTrue(ok)
        relu = history_engine.get_conversation(conv["id"])
        self.assertEqual(list(relu["analyses_image"]), [cle_chemin("C:/f/enonce.png")])

    def test_une_conversation_absente_ne_leve_pas(self):
        self.assertFalse(
            history_engine.set_analyse_image("inexistante", "/f/x.png", {"texte": "t"}))


class TourDeChatTest(unittest.TestCase):
    """Le cas RÉEL, de bout en bout : image attachée, question posée, réponse.

    C'est le seul test du fichier qui traverse le websocket, et donc le seul
    qui prouve que les morceaux sont câblés — les autres prouvent qu'ils sont
    justes, ce qui n'est pas la même chose (leçon de la liste de `run:` de la
    CI, qui laissait quatre fichiers ne jamais tourner).
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def setUp(self):
        self.prompts: list[list[dict]] = []
        # L'image n'a pas besoin d'EXISTER : `describe_image` est doublé, et
        # `core/vision_chat.py` ne lit jamais le fichier lui-même — il ne
        # regarde que son extension. Un chemin qui n'est nulle part est même
        # préférable : aucun test ne doit pouvoir écrire dans les fiches
        # réelles (§3.5, `test_zz_donnees_reelles`).
        self.image = "C:/faux-pour-le-test/enonce.png"

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            self.prompts.append([dict(m) for m in messages])
            return iter(["réponse"])
        self.addCleanup(setattr, routeur_chat.llm, "stream", routeur_chat.llm.stream)
        routeur_chat.llm.stream = faux_stream

        # Appels vision COMPTÉS : c'est la mesure de « ne pas refaire l'analyse ».
        self.appels_vision: list[dict] = []

        def faux_describe(path, model, question=None):
            self.appels_vision.append({"path": path, "model": model, "question": question})
            return _CIBLE
        self.addCleanup(setattr, routeur_chat.llm, "describe_image",
                        routeur_chat.llm.describe_image)
        routeur_chat.llm.describe_image = faux_describe

        # Un modèle vision « disponible », sans sonder ni FLM ni Ollama.
        self.addCleanup(setattr, routeur_chat, "modele_vision_pour",
                        routeur_chat.modele_vision_pour)
        routeur_chat.modele_vision_pour = lambda actif=None: "flm:qwen3vl-it:4b"

        # ── Le RAG, remplacé en BLOC et non par attribut ──────────────────
        #
        # `routeur_chat.rag` est un `_LazyEngine` : LIRE un de ses attributs
        # construit le vrai moteur, donc la pile d'embedding — absente du job
        # backend de la CI (§« Écart de DÉPENDANCES » de CLAUDE.md) et
        # volontairement non téléchargeable pendant la suite
        # (`EPURE_EMBEDDING_AUTOINSTALL=0`). Mesuré en écrivant ce fichier :
        # un `setattr(routeur_chat.rag, …)` lève `EmbeddingIndisponible` avant
        # même le premier test. On remplace donc la variable de MODULE, qui ne
        # touche pas au proxy.
        #
        # Ce double rend la LÉGENDE GÉNÉRIQUE de l'image, comme en vrai : c'est
        # ce qui doit disparaître du prompt une fois l'analyse ciblée faite.
        class _FauxRag:
            def __init__(self, image):
                self._image = image
                self.appels = []

            def _legende(self):
                return [{"texte": "Image : enonce.png" + "\n\n" + _GENERIQUE,
                         "source": self._image}]

            def query_avec_sources(self, texte, *a, **kw):
                self.appels.append(("all", texte))
                return self._legende()

            def query_filtered_avec_sources(self, texte, paths, *a, **kw):
                self.appels.append(("filtered", list(paths)))
                return self._legende()

        self.addCleanup(setattr, routeur_chat, "rag", routeur_chat.rag)
        self.faux_rag = _FauxRag(self.image)
        routeur_chat.rag = self.faux_rag

        # ⚠️ Sur la CLASSE, pas sur le proxy `_LazyEngine` (cf. l'en-tête de
        # `test_chat_ws_conversation.py` : un stub posé sur le proxy est ignoré
        # et un VRAI appel Ollama part).
        self.addCleanup(setattr, HistoryEngine, "_generate_title",
                        HistoryEngine._generate_title)
        HistoryEngine._generate_title = lambda self_, messages: "Titre auto"

        conv = history_engine.create_conversation(messages=[])
        self.conv_id = conv["id"]
        history_engine.set_conversation_files(self.conv_id, [self.image])

    def _envoyer(self, ws, texte, **extra):
        corps = {"role": "user", "content": texte, "direct": True,
                 "conversation_id": self.conv_id, **extra}
        ws.send_text(json.dumps(corps))
        trames = []
        while True:
            t = json.loads(ws.receive_text())
            trames.append(t)
            if t["type"] in ("done", "error"):
                return trames

    def _systeme(self, i=-1):
        msgs = self.prompts[i]
        return msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""

    def test_la_question_declenche_l_analyse_et_elle_entre_dans_le_prompt(self):
        """Le cas réel : la réponse est construite sur l'analyse CIBLÉE, et la
        légende générique ne vient plus la concurrencer dans la même fenêtre."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "Pourquoi (MN) est-elle parallèle à (BC) ?")
        self.assertEqual(len(self.appels_vision), 1)
        self.assertEqual(
            self.appels_vision[0]["question"], "Pourquoi (MN) est-elle parallèle à (BC) ?")
        systeme = self._systeme()
        self.assertIn(_CIBLE, systeme)
        self.assertIn(vision_chat.ETIQUETTE, systeme)
        self.assertNotIn(_GENERIQUE, systeme)

    def test_l_utilisateur_est_averti_pendant_l_analyse(self):
        """6 à 26 s avant le premier token : le silence est un mode d'échec déjà
        payé ici (un flux SSE muet coupé par la webview)."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames = self._envoyer(ws, "Que vaut AB/AC ?")
        etats = [t.get("état") for t in trames if t["type"] == "vision_analyse"]
        self.assertIn("en_cours", etats)
        self.assertIn("terminée", etats)

    def test_l_analyse_est_conservee_par_FICHIER(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "Que vaut AB/AC ?")
        conv = history_engine.get_conversation(self.conv_id)
        self.assertEqual(list(conv["analyses_image"]), [cle_chemin(self.image)])
        self.assertEqual(conv["analyses_image"][cle_chemin(self.image)]["texte"], _CIBLE)

    def test_le_second_tour_ne_REFAIT_pas_l_analyse_mais_la_garde_en_contexte(self):
        """§4 : jamais de réanalyse automatique. Et l'analyse reste injectée —
        la garder sans la réutiliser ne servirait à rien."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "Que vaut AB/AC ?")
            self.assertEqual(len(self.appels_vision), 1)
            self._envoyer(ws, "Et le théorème utilisé, c'est lequel ?")
        self.assertEqual(len(self.appels_vision), 1, "aucun second appel vision")
        self.assertIn(_CIBLE, self._systeme())

    def test_l_override_at_image_redeclenche_une_VRAIE_analyse(self):
        """`@image` est le seul moyen de refaire l'analyse — et il en refait une
        vraie, avec la nouvelle question, pas une relecture du cache."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "Que vaut AB/AC ?")
            self._envoyer(ws, "Quelle est la valeur de AN ?", vision_override=True)
        self.assertEqual(len(self.appels_vision), 2)
        self.assertEqual(self.appels_vision[1]["question"], "Quelle est la valeur de AN ?")

    def test_un_echec_d_analyse_n_empeche_pas_la_reponse(self):
        """Le pire cas sur le chemin d'un message : le modèle vision tombe. La
        réponse part quand même, et l'échec est annoncé."""
        def casse(path, model, question=None):
            raise RuntimeError("timeout vision")
        routeur_chat.llm.describe_image = casse
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames = self._envoyer(ws, "Que vaut AB/AC ?")
        self.assertEqual(trames[-1]["type"], "done")
        self.assertIn("échec", [t.get("état") for t in trames if t["type"] == "vision_analyse"])
        # L'image reste « à analyser » : le tour suivant réessaiera.
        conv = history_engine.get_conversation(self.conv_id)
        self.assertEqual(conv["analyses_image"], {})

    def test_sans_image_attachee_aucun_appel_vision(self):
        history_engine.set_conversation_files(self.conv_id, [])
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "Bonjour")
        self.assertEqual(self.appels_vision, [])

    def test_un_pdf_attache_ne_declenche_rien(self):
        history_engine.set_conversation_files(
            self.conv_id, ["C:/faux-pour-le-test/cours.pdf"])
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "Résume le cours")
        self.assertEqual(self.appels_vision, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
