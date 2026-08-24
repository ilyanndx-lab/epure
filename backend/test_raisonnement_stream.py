#!/usr/bin/env python3
"""Le raisonnement d'Ollama ne doit plus être jeté — et rien d'autre ne doit bouger.

**CE QUI A ÉTÉ MESURÉ**, et que ces tests figent (Ollama 0.32.15, client python
0.6.2, `qwen3:8b`, sur ce poste) :

  * le schéma réel de `chunk.message` est
    `role, content, thinking, images, tool_name, tool_calls` — le raisonnement
    arrive dans un champ SÉPARÉ, il n'y a aucune balise `<think>` à parser ;
  * il arrive **sans qu'on le demande** : aucun argument `think` n'est passé à
    `ollama_client.chat`, et sur une question d'arithmétique **298 chunks sur
    299 portaient un `thinking` non vide avec un `content` vide** ;
  * `_stream_ollama` faisait `if content: yield content`, donc ces 298 chunks ne
    yieldaient RIEN. Sur le chemin réel (`LLMEngine.stream`, `max_tokens=2048`) :
    **584 tokens générés en 78 s, premier caractère visible à 76,5 s**, pour
    `17 x 23 = 391.` — 14 caractères. 76 secondes de silence ;
  * la séquence est `thinking×N → content×N`, vérifiée sur trois formes de prompt
    (arithmétique, deux questions, auto-correction) : jamais de chunk portant les
    deux, jamais de retour en arrière. **Ces tests ne s'appuient pas dessus** —
    trois prompts sur un modèle ne prouvent pas le cas général — et l'un d'eux
    éprouve exprès un chunk qui porte les deux ;
  * `qwen2.5:7b`, le modèle par défaut de `config.yaml` : `thinking: null`,
    4 chunks. Il doit rester **strictement** inchangé, c'est la moitié du
    contrat.

**Les doubles utilisent les VRAIES classes d'Ollama** (`ollama.ChatResponse`,
`ollama.Message`) et non des dictionnaires maison. C'est délibéré : le code lit
`message.get("thinking")`, et `Message` est un `SubscriptableBaseModel` dont
l'indexation d'une clé absente ne se comporte pas comme celle d'un `dict`. Un
double maison validerait un accès que le vrai type refuserait peut-être.

Usage :
    python test_raisonnement_stream.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.*

# Lues à l'import de `main` : les figer rend le test indépendant du poste.
os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import ollama  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402  — monte l'app entière ; cf. test_auth_surface.py
import modules.chat.router as routeur_chat  # noqa: E402
from core import llm as module_llm  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.llm import LLMEngine  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

#: URL WebSocket ABSOLUE, et c'est obligatoire — pas une preference de style.
#: `TestClient(base_url="http://localhost")` ne reporte pas son hote sur les
#: connexions WebSocket : un `websocket_connect("/ws/chat?token=...")` relatif
#: part avec `Host: testserver` et `TrustedHostMiddleware` repond
#: `400 Invalid host header` AVANT le controle de token — un echec qui ressemble
#: a un probleme d'authentification et n'en est pas. Meme piege que celui que
#: `test_auth_surface._client` documente pour HTTP, qui revient ici sur WS.
_WS = "ws://localhost/ws/chat?token={t}"


def _chunk(content="", thinking=None, done=False):
    """Un chunk de streaming Ollama, avec les vraies classes du client."""
    return ollama.ChatResponse(
        model="qwen3:8b",
        created_at="2026-08-24T12:00:00Z",
        done=done,
        done_reason="stop" if done else None,
        message=ollama.Message(role="assistant", content=content, thinking=thinking),
        prompt_eval_count=28 if done else None,
        eval_count=584 if done else None,
        eval_duration=77_600_000_000 if done else None,
        prompt_eval_duration=120_000_000 if done else None,
    )


def _rejouer(chunks, **stream_kwargs):
    """Remplace `ollama_client.chat` et rend (yields, kwargs de l'appel)."""
    appels = {}

    def faux_chat(**kwargs):
        appels.update(kwargs)
        return iter(chunks)

    original = module_llm.ollama_client.chat
    module_llm.ollama_client.chat = faux_chat
    try:
        moteur = LLMEngine(config_path=_CONFIG)
        sortie = list(moteur.stream([{"role": "user", "content": "17 x 23 ?"}],
                                    model="qwen3:8b", **stream_kwargs))
    finally:
        module_llm.ollama_client.chat = original
    return sortie, appels


def _raisonnements(sortie):
    return [p["content"] for p in sortie
            if isinstance(p, dict) and p.get("__reasoning__")]


def _textes(sortie):
    return [p for p in sortie if isinstance(p, str)]


class StreamOllamaTest(unittest.TestCase):
    """`_stream_ollama` : ce qui sort du générateur, et dans quel ordre."""

    #: La forme mesurée : du raisonnement, puis du contenu, puis le chunk `done`.
    MESURE = [
        _chunk(thinking="Okay"),
        _chunk(thinking=", the user asks 17"),
        _chunk(thinking=" x 23. Let me compute."),
        _chunk(content="17 x 23"),
        _chunk(content=" = 391."),
        _chunk(done=True),
    ]

    def test_le_raisonnement_n_est_plus_jete(self):
        """Le cœur du correctif. Avant, les trois premiers chunks ne yieldaient rien."""
        sortie, _ = _rejouer(self.MESURE)
        self.assertEqual(_raisonnements(sortie),
                         ["Okay", ", the user asks 17", " x 23. Let me compute."])
        # Et le contenu final n'a pas bougé d'un caractère.
        self.assertEqual("".join(_textes(sortie)), "17 x 23 = 391.")

    def test_l_ordre_est_respecte(self):
        """Tout le raisonnement AVANT tout le contenu, sans entrelacement.

        L'ordre est le sujet : l'interface referme le bloc de raisonnement au
        premier caractère de réponse. Un raisonnement qui arriverait après du
        contenu s'afficherait dans un bloc déjà replié.
        """
        sortie, _ = _rejouer(self.MESURE)
        genres = ["raisonnement" if isinstance(p, dict) and p.get("__reasoning__")
                  else "stats" if isinstance(p, dict)
                  else "texte"
                  for p in sortie]
        self.assertEqual(genres, ["raisonnement"] * 3 + ["texte"] * 2 + ["stats"])

    def test_le_raisonnement_n_est_pas_une_chaine(self):
        """Il doit être une sentinelle dict, pas du `str`.

        C'est ce qui protège les onze autres consommateurs de `stream()` : ils
        filtrent tous par `isinstance(item, str)` avant de concaténer. Si le
        raisonnement sortait en `str`, il se retrouverait collé dans les résumés
        de documents, les modules générés par l'Atelier et les étapes du
        pipeline, sans qu'aucun d'eux ne l'ait demandé.
        """
        sortie, _ = _rejouer(self.MESURE)
        for piece in sortie:
            if isinstance(piece, dict) and piece.get("__reasoning__"):
                self.assertNotIsInstance(piece, str)
                self.assertIn("content", piece)
        self.assertEqual(len(_textes(sortie)), 2, "le texte a changé de nature")

    def test_les_stats_survivent(self):
        """La sentinelle `__stats__` existait avant : elle doit encore passer."""
        sortie, _ = _rejouer(self.MESURE)
        stats = [p for p in sortie if isinstance(p, dict) and p.get("__stats__")]
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["output_tokens"], 584)
        self.assertEqual(stats[0]["prompt_tokens"], 28)

    def test_un_chunk_portant_les_deux_rend_le_raisonnement_d_abord(self):
        """Cas JAMAIS observé, éprouvé quand même.

        Mesuré sur qwen3:8b : aucun chunk ne porte `thinking` et `content` à la
        fois, sur trois formes de prompt. Mais trois prompts sur un modèle ne
        sont pas une preuve, et rien dans l'API d'Ollama ne l'interdit. On fixe
        donc le comportement au lieu de le laisser au hasard de l'ordre des
        lignes : le raisonnement sort avant le contenu du même chunk.
        """
        sortie, _ = _rejouer([_chunk(content="391", thinking="donc 391"), _chunk(done=True)])
        self.assertEqual(sortie[0], {"__reasoning__": True, "content": "donc 391"})
        self.assertEqual(sortie[1], "391")


class ModeleSansRaisonnementTest(unittest.TestCase):
    """`qwen2.5:7b` : `thinking: null`, 4 chunks. Strictement inchangé."""

    #: Mesuré sur qwen2.5:7b — le modèle par défaut de `config.yaml`.
    MESURE = [
        _chunk(content="3"),
        _chunk(content="9"),
        _chunk(content="1"),
        _chunk(done=True),
    ]

    def test_aucune_sentinelle_de_raisonnement(self):
        sortie, _ = _rejouer(self.MESURE)
        self.assertEqual(_raisonnements(sortie), [])
        self.assertEqual(_textes(sortie), ["3", "9", "1"])

    def test_la_sortie_est_exactement_celle_d_avant(self):
        """Non-régression au sens fort : la liste entière, pas seulement le texte."""
        sortie, _ = _rejouer(self.MESURE)
        self.assertEqual(sortie[:3], ["3", "9", "1"])
        self.assertEqual(len(sortie), 4)
        self.assertTrue(sortie[3].get("__stats__"))

    def test_thinking_vide_n_est_pas_un_raisonnement(self):
        """`thinking=""` est falsy comme `None` : rien ne doit être yieldé.

        Distinction qui compte : le chunk `done` d'Ollama a `content == ""` ET
        `thinking == None`. Yielder sur la simple PRÉSENCE de la clé (elle est
        toujours là, à `None`) produirait une sentinelle vide par chunk.
        """
        sortie, _ = _rejouer([_chunk(content="ok", thinking=""), _chunk(done=True)])
        self.assertEqual(_raisonnements(sortie), [])

    def test_aucun_argument_think_n_est_passe_a_ollama(self):
        """L'appel lui-même ne change pas — c'est ce qui garantit l'invariance.

        Mesuré : le raisonnement arrive par défaut, donc `think=True` n'apporte
        rien aux modèles qui pensent. Le passer modifierait en revanche l'appel
        pour ceux qui ne pensent pas, et `think=False` couperait le raisonnement
        qu'on veut justement afficher. La bonne valeur est donc « pas d'argument ».
        """
        _, appels = _rejouer(self.MESURE)
        self.assertNotIn("think", appels)
        # Les options historiques sont intactes.
        self.assertEqual(appels["options"]["num_thread"], 8)
        self.assertTrue(appels["stream"])


class ProtocoleWebSocketTest(unittest.TestCase):
    """Vu depuis `/ws/chat` : les deux canaux arrivent, typés et dans l'ordre."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def setUp(self):
        self._stream_original = routeur_chat.llm.stream
        #: Les `messages` reçus à chaque appel — sert à prouver que le
        #: raisonnement ne repart PAS dans le prompt du tour suivant.
        self.prompts = []

    def tearDown(self):
        routeur_chat.llm.stream = self._stream_original

    def _poser_flux(self, pieces):
        def faux_stream(messages, model=None, max_tokens=None, raisonnement=True):
            self.prompts.append([dict(m) for m in messages])
            return iter(list(pieces))
        # Remplace la méthode sur le singleton partagé de `core.runtime` — le
        # routeur a lié le NOM `llm` à l'import, donc remplacer l'objet entier
        # ne suffirait pas ; remplacer sa méthode, si.
        routeur_chat.llm.stream = faux_stream

    def _echanger(self, ws, texte="17 x 23 ?"):
        """Envoie un message et collecte les trames jusqu'au `done`."""
        ws.send_text(json.dumps({"role": "user", "content": texte, "direct": True}))
        trames = []
        while True:
            trame = json.loads(ws.receive_text())
            trames.append(trame)
            if trame["type"] in ("done", "error"):
                return trames

    def test_raisonnement_puis_token_dans_le_bon_ordre_et_le_bon_type(self):
        self._poser_flux([
            {"__reasoning__": True, "content": "Okay, 17 x 23"},
            {"__reasoning__": True, "content": " = 391."},
            "17 x 23",
            " = 391.",
            {"__stats__": True, "prompt_tokens": 28, "output_tokens": 584,
             "eval_duration_ns": 77_600_000_000, "prompt_duration_ns": 120_000_000},
        ])
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames = self._echanger(ws)

        types = [t["type"] for t in trames]
        self.assertEqual(types, ["reasoning", "reasoning", "token", "token",
                                 "stats", "done"], types)
        # Le type suit la convention de `{"type": "token", "content": …}` : même
        # forme, canal distinct. Un format ad hoc aurait demandé un second
        # aiguillage côté frontend pour la même chose.
        raisonnement = "".join(t["content"] for t in trames if t["type"] == "reasoning")
        reponse = "".join(t["content"] for t in trames if t["type"] == "token")
        self.assertEqual(raisonnement, "Okay, 17 x 23 = 391.")
        self.assertEqual(reponse, "17 x 23 = 391.")

    def test_le_raisonnement_ne_repart_pas_dans_le_prompt_suivant(self):
        """Il ne doit pas entrer dans `history`, et c'est le point le plus discret.

        `history` alimente le prompt du tour suivant. Y verser le raisonnement le
        ferait relire par le modèle comme s'il l'avait dit à l'utilisateur, et
        gonflerait le contexte de plusieurs centaines de tokens par tour — 584
        générés pour 14 caractères de réponse, mesuré.
        """
        secret = "RAISONNEMENT-QUI-NE-DOIT-PAS-REVENIR"
        self._poser_flux([
            {"__reasoning__": True, "content": secret},
            "391",
            {"__stats__": True, "prompt_tokens": 1, "output_tokens": 2,
             "eval_duration_ns": 1, "prompt_duration_ns": 1},
        ])
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._echanger(ws, "premier tour")
            self._echanger(ws, "second tour")

        self.assertEqual(len(self.prompts), 2, "le second tour n'a pas eu lieu")
        second = json.dumps(self.prompts[1], ensure_ascii=False)
        self.assertNotIn(secret, second)
        # Contre-épreuve : le CONTENU, lui, doit bien y être — sinon ce test
        # passerait aussi sur un historique cassé qui ne garde rien.
        self.assertIn("391", second)
        self.assertIn("premier tour", second)

    def test_un_flux_sans_raisonnement_ne_change_pas_le_protocole(self):
        """Modèle classique : exactement les trames d'avant, dans le même ordre."""
        self._poser_flux([
            "3", "9", "1",
            {"__stats__": True, "prompt_tokens": 10, "output_tokens": 3,
             "eval_duration_ns": 1_000_000_000, "prompt_duration_ns": 1_000_000},
        ])
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames = self._echanger(ws)

        self.assertEqual([t["type"] for t in trames],
                         ["token", "token", "token", "stats", "done"])
        self.assertNotIn("reasoning", [t["type"] for t in trames])


class ResumeSseTest(unittest.TestCase):
    """`/skills/résumé` : les sentinelles ne doivent plus partir comme du texte.

    Bug **préexistant**, découvert en ajoutant la seconde sentinelle : ce flux
    sérialisait tout item non-`error` en `{"type": "token", "content": item}`,
    y compris le dict `__stats__`. Le consommateur fait `last.content +
    ev.content`, donc un « [object Object] » se collait à la fin de chaque
    résumé, avec n'importe quel modèle. Le raisonnement n'aurait fait qu'en
    ajouter un second, plus gros.
    """

    def setUp(self):
        self.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54321))
        self.token = get_api_token()
        self._original = routeur_chat.llm.stream
        # Le flux refuse de commencer sans fichier actif LISIBLE : il lit
        # vraiment le disque (`RAGEngine.read_file_text`) avant d'appeler le
        # modele. Un fichier temporaire est donc le seul moyen d'atteindre la
        # boucle de streaming, qui est le sujet du test.
        self._dossier = tempfile.mkdtemp(prefix="epure-test-resume-")
        fichier = os.path.join(self._dossier, "cours.txt")
        with open(fichier, "w", encoding="utf-8") as sortie:
            sortie.write("Contenu de cours pour le resume.")
        self._contexte = routeur_chat.memory.get_context().get("fichiers_actifs", [])
        routeur_chat.memory.update_context(fichiers_actifs=[fichier])

    def tearDown(self):
        routeur_chat.llm.stream = self._original
        routeur_chat.memory.update_context(fichiers_actifs=self._contexte)
        shutil.rmtree(self._dossier, ignore_errors=True)

    def test_seul_le_texte_part_en_token(self):
        def faux_stream(messages, model=None, max_tokens=None, raisonnement=True):
            return iter([
                {"__reasoning__": True, "content": "je réfléchis"},
                "Résumé.",
                {"__stats__": True, "prompt_tokens": 1, "output_tokens": 1,
                 "eval_duration_ns": 1, "prompt_duration_ns": 1},
            ])
        routeur_chat.llm.stream = faux_stream

        reponse = self.client.post("/skills/résumé",
                                   headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(reponse.status_code, 200, reponse.text)
        evenements = [json.loads(ligne[6:]) for ligne in reponse.text.splitlines()
                      if ligne.startswith("data: ")]
        self.assertEqual(evenements, [{"type": "token", "content": "Résumé."}])
        # Ni le raisonnement, ni les stats, ni un « [object Object] » en devenir.
        self.assertNotIn("__stats__", reponse.text)
        self.assertNotIn("__reasoning__", reponse.text)


class BasculeOllamaTest(unittest.TestCase):
    """La bascule `raisonnement` — et son asymétrie, qui est mesurée.

    Ce qui a été mesuré sur ce poste, et qui dicte la forme du paramètre :

    ============================  ======================================
    appel sur `qwen2.5:7b`        résultat
    ============================  ======================================
    aucun argument `think`        200, 4 chunks, `391`
    `think=False`                 200, 4 chunks, `391` — ignoré proprement
    `think=True`                  **400** `"qwen2.5:7b" does not support
                                  thinking`
    ============================  ======================================

    Même 400 sur `qwen2.5-coder:7b`. Donc « activé » ne peut PAS vouloir dire
    `think=True` : ça casserait le chat sur le modèle par défaut de
    `config.yaml`. « Activé » veut dire « ne rien passer », ce qui est exactement
    l'état d'avant ce réglage.
    """

    def test_desactive_pose_think_false(self):
        _, appels = _rejouer([_chunk(content="391"), _chunk(done=True)],
                             raisonnement=False)
        self.assertIs(appels["think"], False)

    def test_active_ne_pose_aucun_think(self):
        """L'invariant qui protège les modèles sans raisonnement.

        Poser `think=True` ici ferait répondre 400 à Ollama sur `qwen2.5:7b` —
        mesuré, pas supposé. Ce test échoue si quelqu'un « complète » la symétrie.
        """
        _, appels = _rejouer([_chunk(content="391"), _chunk(done=True)],
                             raisonnement=True)
        self.assertNotIn("think", appels)

    def test_le_defaut_est_active_et_identique_a_avant(self):
        """Sans argument : exactement l'appel d'avant l'existence du paramètre.

        C'est ce qui garantit que les onze autres appelants de `stream()`
        (résumés de documents, agent de code, Atelier, pipeline) ne voient rien
        changer.
        """
        _, appels = _rejouer([_chunk(content="391"), _chunk(done=True)])
        self.assertNotIn("think", appels)
        self.assertEqual(appels["options"]["num_thread"], 8)
        self.assertTrue(appels["stream"])

    def test_desactive_ne_touche_pas_les_options_historiques(self):
        """La bascule ajoute une clé, elle n'en modifie aucune."""
        _, avec = _rejouer([_chunk(content="391"), _chunk(done=True)],
                           raisonnement=False)
        _, sans = _rejouer([_chunk(content="391"), _chunk(done=True)])
        self.assertEqual(avec["options"], sans["options"])
        self.assertEqual(avec["model"], sans["model"])
        self.assertEqual(set(avec) - set(sans), {"think"})

    def test_un_modele_sans_raisonnement_traverse_les_deux_valeurs(self):
        """Le flux d'un modèle sans raisonnement est le même dans les deux sens.

        Mesuré : `think=False` est ignoré par `qwen2.5:7b`, qui rend ses 4 chunks
        habituels. Le générateur ne doit donc rien inventer non plus — ni
        sentinelle de raisonnement, ni texte modifié.
        """
        mesure = [_chunk(content="3"), _chunk(content="9"), _chunk(content="1"),
                  _chunk(done=True)]
        for valeur in (True, False):
            sortie, _ = _rejouer(mesure, raisonnement=valeur)
            self.assertEqual(_textes(sortie), ["3", "9", "1"], "raisonnement=%s" % valeur)
            self.assertEqual(_raisonnements(sortie), [], "raisonnement=%s" % valeur)


class BasculeFlmTest(unittest.TestCase):
    """FastFlowLM : la bascule existe, elle ne marche PAS comme celle d'Ollama.

    **Ce qui a été cherché puis mesuré**, la consigne étant « ne suppose pas que
    ça marche comme Ollama » :

    * la CLI `flm --help` n'expose AUCUNE option de raisonnement (vérifié : ni
      `--think`, ni `--no-think`, rien dans les 20 options listées) ;
    * `fastflowlm.com/docs/models/qwen` dit « Type `/think` to toggle on/off
      interactively » et, en mode serveur, « Set the `"think"` flag in the request
      payload » — **sans dire sur quel endpoint, ni donner un seul exemple de
      corps**. La page OpenAI-compat, elle, ne liste aucun paramètre de
      raisonnement ;
    * donc mesuré sur le serveur réel (FLM 0.9.43, `qwen3:4b`, dont
      `GET /api/ps` annonce `think_toggleable: true`) :

      ==========================  ======  =============  ==================
      corps de requête            durée   raisonnement   contenu
      ==========================  ======  =============  ==================
      `think: true`                20 s   733 car.       `17 x 23 = 391.`
      `think: false`                4 s   aucun          `17 x 23 = 391`
      ==========================  ======  =============  ==================

    Trois écarts avec Ollama, tous vérifiés, et chacun change le code :

    1. **`think=True` est SÛR sur FLM.** `lfm2:1.2b` (`think: false`,
       `think_toggleable: false` dans `/api/ps`) répond 200 en 2,8 s avec les
       deux valeurs, le flag ignoré. Pas de 400 — l'asymétrie du chemin Ollama
       n'a donc pas lieu d'être ici, et la reproduire aurait rendu la bascule à
       moitié muette.
    2. **Le flag est COLLANT quand on l'omet.** Séquence mesurée :
       `think=false` → 4 s ; *rien* → 4 s ; `think=true` → 18,5 s ; *rien* →
       27 s avec raisonnement. Son absence ne veut pas dire « défaut du modèle »
       mais « garde la valeur du dernier appel ». D'où : toujours le passer, dans
       les deux sens.
    3. **`extra_body` et non un kwarg** : le SDK `openai` lève sur un paramètre
       inconnu. Vérifié à travers le SDK, pas seulement en HTTP brut.

    Ce que ces tests NE prouvent pas : que FLM honore le flag. Ça, seule la
    mesure ci-dessus le dit — ici on vérifie que le corps de requête part avec la
    bonne valeur, ce qu'aucune relecture ne garantit puisque `extra_body` est
    fusionné silencieusement par le SDK.
    """

    def _capturer(self, provider, model_id, **kwargs):
        """Rend le kwargs passé à `client.chat.completions.create`."""
        vus = {}

        class _Completions:
            def create(_self, **kw):
                vus.update(kw)
                return iter([])

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        moteur = LLMEngine(config_path=_CONFIG)
        list(moteur._stream_openai([{"role": "user", "content": "17 x 23 ?"}],
                                   model_id, _Client(), provider, 300, **kwargs))
        return vus

    def test_flm_recoit_le_flag_dans_les_deux_sens(self):
        """Les deux valeurs partent explicitement — cf. point 2 du docstring.

        Ne poser le flag que pour couper laisserait le serveur en mode
        non-pensant pour tous les appels suivants, mesuré.
        """
        self.assertEqual(self._capturer("flm", "qwen3:4b", raisonnement=False)["extra_body"],
                         {"think": False})
        self.assertEqual(self._capturer("flm", "qwen3:4b", raisonnement=True)["extra_body"],
                         {"think": True})

    def test_flm_passe_par_extra_body_et_non_par_un_kwarg(self):
        """`create(think=...)` lèverait : le SDK refuse les paramètres inconnus."""
        vus = self._capturer("flm", "qwen3:4b", raisonnement=False)
        self.assertNotIn("think", vus)
        self.assertIn("extra_body", vus)

    def test_les_fournisseurs_cloud_ne_recoivent_rien_de_neuf(self):
        """Aucun `extra_body` vers groq/cerebras/mistral/nvidia/deepseek.

        Leur bascule n'a pas été mesurée, et `extra_body` part vers une API
        distante qui peut refuser un champ inconnu. On ne devine pas sur du
        réseau facturé — et un 400 chez un fournisseur cloud se lirait comme une
        clé invalide, pas comme ce paramètre.
        """
        for provider in ("groq", "cerebras", "mistral", "nvidia", "deepseek"):
            for valeur in (True, False):
                vus = self._capturer(provider, "un-modele", raisonnement=valeur)
                self.assertNotIn("extra_body", vus, "%s / %s" % (provider, valeur))

    def test_le_defaut_flm_est_le_raisonnement_actif(self):
        """Sans argument : `think=True`, donc le comportement d'avant.

        Nuance propre à FLM : « avant », c'était l'absence de flag, donc la valeur
        collante du dernier appel. Poser `True` explicitement est un CHANGEMENT
        assumé — il rend le comportement déterministe au lieu de dépendre de ce
        qui s'est passé avant, y compris depuis un autre écran.
        """
        self.assertEqual(self._capturer("flm", "qwen3:4b")["extra_body"], {"think": True})


class ReglageDeSessionTest(unittest.TestCase):
    """Le réglage lui-même : liste blanche, persistance, et effet sur le chat."""

    def setUp(self):
        self.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54321))
        self.token = get_api_token()
        self.entetes = {"Authorization": "Bearer " + self.token}
        self._avant = routeur_chat.memory.get_context().get("raisonnement", True)
        self._stream_original = routeur_chat.llm.stream

    def tearDown(self):
        routeur_chat.memory.update_context(raisonnement=self._avant)
        routeur_chat.llm.stream = self._stream_original

    def test_la_cle_est_acceptee_par_l_endpoint_de_reglages(self):
        """`PATCH /context/settings` a une liste blanche : sans l'entrée, le
        réglage serait ignoré en silence et le toggle reviendrait tout seul à sa
        position d'avant au rechargement suivant.
        """
        r = self.client.patch("/context/settings", headers=self.entetes,
                              json={"raisonnement": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(routeur_chat.memory.get_context()["raisonnement"], False)
        self.assertIs(self.client.get("/context", headers=self.entetes)
                      .json()["raisonnement"], False)

    def test_une_cle_inconnue_reste_refusee(self):
        """La liste blanche s'est élargie d'une entrée, pas ouverte."""
        self.client.patch("/context/settings", headers=self.entetes,
                          json={"nimporte_quoi": 1})
        self.assertNotIn("nimporte_quoi", routeur_chat.memory.get_context())

    def _envoyer_et_lire_le_flag(self):
        """Envoie un message par le WS et rend la valeur de `raisonnement` reçue."""
        recu = {}

        def faux_stream(messages, model=None, max_tokens=None, raisonnement=True):
            recu["raisonnement"] = raisonnement
            return iter(["ok"])

        routeur_chat.llm.stream = faux_stream
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps({"role": "user", "content": "test", "direct": True}))
            while json.loads(ws.receive_text())["type"] != "done":
                pass
        return recu.get("raisonnement")

    def test_le_reglage_atteint_le_flux_du_chat(self):
        """Le bout du fil : la valeur du contexte de session arrive jusqu'à
        `llm.stream`. Sans ce test, le toggle pourrait être purement décoratif —
        il écrirait un JSON que personne ne lit.
        """
        routeur_chat.memory.update_context(raisonnement=False)
        self.assertIs(self._envoyer_et_lire_le_flag(), False)
        routeur_chat.memory.update_context(raisonnement=True)
        self.assertIs(self._envoyer_et_lire_le_flag(), True)

    def test_une_cle_absente_vaut_active(self):
        """Un `context_session.json` écrit avant ce réglage n'a pas la clé.

        `get_context` rend le fichier tel quel sans fusionner les défauts : lire
        par indexation lèverait, et lire avec un défaut `False` couperait la
        réflexion chez tous ceux qui ont déjà un fichier sur le disque, sans que
        rien ne l'explique.
        """
        contexte = routeur_chat.memory.get_context()
        contexte.pop("raisonnement", None)
        # Réécrit le fichier SANS la clé, comme un fichier d'avant ce réglage.
        routeur_chat.memory._write(routeur_chat.memory._context_path, contexte)
        self.assertNotIn("raisonnement", routeur_chat.memory.get_context())
        self.assertIs(self._envoyer_et_lire_le_flag(), True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
