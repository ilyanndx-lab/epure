import base64
import logging
import os
import time
from pathlib import Path
from typing import Generator, Optional

import httpx
import ollama
import yaml
from dotenv import load_dotenv

_ENV_FILE = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_FILE)

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"

# OLLAMA_HOST=0.0.0.0 is a server *listen* address — the client can't connect
# to it on Windows. Normalize to localhost for all client calls.
ollama_host = os.environ.get("OLLAMA_HOST", "").strip() or "http://localhost:11434"
if "0.0.0.0" in ollama_host:
    ollama_host = ollama_host.replace("0.0.0.0", "localhost")
if not ollama_host.startswith("http"):
    ollama_host = f"http://{ollama_host}:11434"


def _ollama_timeout_s() -> float:
    """``model.timeout_s`` de config.yaml (défaut 60 s).

    Lu au niveau module et pas dans ``LLMEngine.__init__`` : le client est un
    singleton partagé (admin l'utilise aussi) construit avant qu'un moteur
    existe. Chemin absolu — LLMEngine reçoit ``"config.yaml"`` en relatif, ce qui
    dépend du répertoire courant.
    """
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return float((cfg.get("model") or {}).get("timeout_s") or 60)
    except Exception:
        logger.warning("model.timeout_s illisible dans config.yaml — défaut 60 s")
        return 60.0


#: Client Ollama UNIQUE du backend (host normalisé + timeout). Tout appel au
#: client python Ollama passe par lui : `core/admin.py` construisait ses requêtes
#: avec le module `ollama` brut, donc sans normalisation du host (OLLAMA_HOST=0.0.0.0
#: le faisait échouer) et sans timeout.
#:
#: Le timeout est éclaté volontairement : `connect` court, parce qu'un Ollama
#: arrêté doit être détecté tout de suite ; `read` long, parce que c'est le délai
#: d'attente ENTRE deux paquets — un modèle de 7B qui se charge à froid ne renvoie
#: rien pendant des dizaines de secondes (incident connu : la webview coupait le
#: flux SSE muet pendant le cold-load), et un read trop court avorterait un
#: démarrage parfaitement normal.
ollama_client = ollama.Client(
    host=ollama_host,
    timeout=httpx.Timeout(_ollama_timeout_s(), connect=5.0),
)

#: Timeout dédié à `describe_image`, INDÉPENDANT de `model.timeout_s`
#: (300 s par défaut — pensé pour le chargement à froid d'un modèle de CHAT).
#: `describe_image` tourne en SYNCHRONE dans le chargement d'un fichier
#: (`_stream_load_sse`, pas une conversation active) : elle doit échouer vite
#: et retomber sur le placeholder (`RAGEngine._texte_image` filtre déjà toute
#: exception) plutôt que bloquer jusqu'au défaut du SDK openai (600 s) ou du
#: timeout de lecture d'`ollama_client` (300 s).
#: 60 s = pire cas mesuré (26 s, `flm:qwen3vl-it:4b` sur ce poste) + marge.
_VISION_TIMEOUT_S = 60.0

#: Client Ollama DÉDIÉ à `describe_image`, distinct de `ollama_client`.
#: `Client.chat()` n'accepte aucun paramètre `timeout` par appel — vérifié sur
#: sa signature réelle, contrairement à `client.chat.completions.create()` du
#: SDK openai, qui lui l'expose. Un second client, construit une fois avec le
#: timeout court, est le seul moyen propre de borner CETTE méthode sans changer
#: le timeout du reste (chat, résumés, agent de code…), qui a besoin des 300 s
#: pour couvrir un chargement à froid de modèle de chat.
_vision_ollama_client = ollama.Client(
    host=ollama_host,
    timeout=httpx.Timeout(_VISION_TIMEOUT_S, connect=5.0),
)

# OpenAI-compatible providers: name → (base_url, env_key | None)
# env_key=None means no API key required (local server)
_OPENAI_COMPAT: dict[str, tuple[str, str | None]] = {
    "groq":     ("https://api.groq.com/openai/v1",      "GROQ_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1",          "CEREBRAS_API_KEY"),
    "mistral":  ("https://api.mistral.ai/v1",           "MISTRAL_API_KEY"),
    "nvidia":   ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1",         "DEEPSEEK_API_KEY"),
    "flm":      ("http://localhost:11435/v1",           None),
}


#: Prompt de description d'image, partagé entre les deux chemins de
#: `describe_image`. Volontairement COURT — mesuré sur `moondream` (le repli
#: Ollama, cf. `core/models.py:_ollama_vision_model`) : une formulation plus
#: longue et détaillée, demandant explicitement titres/légendes/formules/
#: annotations entre parenthèses, fait dégénérer ce modèle — soit une réponse
#: VIDE (`eval_count: 1`, arrêt immédiat), soit une boucle de répétition
#: (mesuré : 1265 tokens de charabia thaï en 65 s pour la même image). La forme
#: courte ci-dessous a été rejouée quatre fois sur `moondream` sans variation
#: (~2 s, description correcte, transcription exacte) et vérifiée aussi sur
#: `qwen3vl-it:4b` (8,8 s, transcription exacte) — donc commune aux deux
#: providers plutôt que deux prompts à maintenir.
_VISION_PROMPT = "Décris cette image et transcris tout texte visible."


def _provider_error_message(provider: str, model_id: str, exc: Exception) -> str:
    """Transforme une exception provider en message clair et actionnable.

    Récupère le code HTTP et le message renvoyé par l'API (OpenAI SDK) pour
    expliquer POURQUOI ça échoue (modèle inexistant, clé refusée, quota…) au lieu
    d'un générique « [Erreur nvidia] ».
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    # Message renvoyé par l'API si disponible (body JSON {'message': ...}).
    detail = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("message") or body.get("detail") or ""
    if not detail:
        detail = getattr(exc, "message", "") or str(exc)
    detail = (detail or "").strip()

    label = f"{provider}:{model_id}"
    if status in (401, 403):
        return (f"[{label}] clé API refusée (HTTP {status}). "
                f"Vérifiez {provider.upper()}_API_KEY dans les Réglages. {detail}").strip()
    if status == 404:
        return (f"[{label}] modèle introuvable chez {provider} (HTTP 404). "
                f"Cet identifiant n'existe pas/plus dans le catalogue. {detail}").strip()
    if status == 429:
        return f"[{label}] quota/débit dépassé (HTTP 429). Réessayez plus tard. {detail}".strip()
    if status == 400:
        return f"[{label}] requête refusée (HTTP 400). {detail}".strip()
    if status:
        return f"[{label}] erreur HTTP {status}. {detail}".strip()
    return f"[{label}] échec d'appel : {detail or type(exc).__name__}".strip()


def _gemini_contents(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("content", "")
        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "(vide)"}]}]
    return "\n\n".join(system_parts), contents


class LLMEngine:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        self._model = cfg["model"]["name"]
        self._gen = cfg["generation"]

    def _budget(self, max_tokens: Optional[int], raisonnement: bool) -> int:
        """Plafond de génération pour cet appel.

        Un ``max_tokens`` explicite l'emporte toujours : les appelants qui en
        passent un (résumés, agent de code, étapes du pipeline) l'ont
        dimensionné pour leur tâche, et le raisonnement n'y change rien.

        Sinon, ``max_tokens_raisonnement`` quand la réflexion est active. Les
        deux API ne connaissent qu'un plafond UNIQUE — la réflexion et la
        réponse y puisent au même endroit — donc le seul levier disponible est
        de le relever quand on sait qu'il devra couvrir les deux.

        ``.get()`` avec repli sur ``max_tokens`` : un ``config.yaml`` écrit avant
        ce réglage ne doit pas faire échouer le démarrage, il retombe simplement
        sur l'ancien comportement.
        """
        if max_tokens:
            return max_tokens
        if raisonnement:
            return self._gen.get("max_tokens_raisonnement") or self._gen["max_tokens"]
        return self._gen["max_tokens"]

    @staticmethod
    def _parse_model(model: str) -> tuple[str, str]:
        """'provider:model_id' → (provider, model_id).  Falls back to ('ollama', model)."""
        if ":" in model:
            prefix, rest = model.split(":", 1)
            if prefix in _OPENAI_COMPAT or prefix == "gemini":
                return prefix, rest
        return "ollama", model

    def _openai_client(self, provider: str):
        base_url, key_name = _OPENAI_COMPAT[provider]
        if key_name is None:
            api_key = "not-needed"  # local server, no auth required
        else:
            api_key = os.environ.get(key_name, "").strip()
            if not api_key:
                raise ValueError(f"{key_name} non configurée — ajoutez-la dans Settings")
        try:
            from openai import OpenAI
            return OpenAI(base_url=base_url, api_key=api_key)
        except ImportError:
            raise RuntimeError("Package 'openai' non installé — pip install openai")

    # ── Public API ───────────────────────────────────────────────────────────

    def stream(self, messages: list[dict], model: Optional[str] = None,
               max_tokens: Optional[int] = None, raisonnement: bool = True) -> Generator:
        """Flux de génération. ``raisonnement=False`` coupe la réflexion du modèle.

        **Le défaut est ``True``, et c'est le comportement historique** : les
        modèles qui pensent pensent, ceux qui ne pensent pas ne changent pas.
        Les onze autres appelants de cette méthode (résumés de documents, agent de
        code, Atelier…) n'ont donc rien à passer et rien à voir changer.

        **Asymétrie imposée par la mesure, pas par le goût** — cf.
        :meth:`_stream_ollama` : couper se dit ``think=False``, mais *allumer* ne
        se dit PAS ``think=True``, qui fait répondre 400 à Ollama sur un modèle
        sans capacité de raisonnement. « Allumer » veut donc dire « ne rien
        passer ». Le paramètre de cette méthode est booléen quand même : c'est aux
        chemins de flux de traduire, pas à leurs appelants de connaître ce piège.
        """
        m = model or self._model
        provider, model_id = self._parse_model(m)
        # `max_tokens` est passé TEL QUEL aux deux chemins qui gèrent le
        # raisonnement : c'est `_budget` qui tranche chez eux, et il a besoin de
        # savoir si l'appelant en a fourni un ou non. Le résoudre ici — ce que
        # faisait `mt = max_tokens or self._gen["max_tokens"]` — le rendait
        # toujours non nul, donc rendait `_budget` inopérant.
        if provider == "gemini":
            # Pas de bascule ici : `google-generativeai` n'expose rien
            # d'équivalent sur ce chemin, et rien n'a été mesuré. Ne pas inventer
            # — y compris pour le budget : relever le plafond de Gemini parce que
            # `raisonnement` est vrai par défaut changerait un chemin dont on ne
            # sait rien.
            yield from self._stream_gemini(messages, m, max_tokens or self._gen["max_tokens"])
        elif provider in _OPENAI_COMPAT:
            client = self._openai_client(provider)  # raises if key missing
            yield from self._stream_openai(messages, model_id, client, provider, max_tokens,
                                          raisonnement=raisonnement)
        else:
            yield from self._stream_ollama(messages, m, max_tokens, raisonnement=raisonnement)

    def generate(self, messages: list[dict], model: Optional[str] = None) -> str:
        m = model or self._model
        provider, model_id = self._parse_model(m)
        if provider == "gemini":
            return self._generate_gemini(messages, m)
        elif provider in _OPENAI_COMPAT:
            client = self._openai_client(provider)
            return self._generate_openai(messages, model_id, client, provider)
        return self._generate_ollama(messages, m)

    def reload_dotenv(self) -> None:
        load_dotenv(_ENV_FILE, override=True)

    def describe_image(self, path: str, model: str) -> str:
        """Décrit une image et transcrit son texte visible, via un modèle vision.

        Dispatch par provider — même principe que :meth:`stream` (``_parse_model``
        décide) — parce que le format du message est **radicalement différent**
        d'un chemin à l'autre, vérifié par appel réel et non lu dans une doc :

        * **Ollama** accepte le CHEMIN du fichier tel quel dans
          ``images=[...]`` : ``ollama._types.Image`` le lit et l'encode lui-même.
          Rien à préparer ici.
        * **flm** (OpenAI-compatible) exige le bloc ``image_url`` en base64
          (``data:image/<ext>;base64,...``) — mesuré sur ``qwen3vl-it:4b`` :
          7,5 s, transcription exacte d'un texte photographié.

        Seuls ces deux providers sont câblés, et c'est délibéré :
        ``core.models.premier_modele_vision_disponible()`` ne rend jamais que
        ``flm:...`` ou un nom Ollama nu — deviner un troisième format non mesuré
        serait exactement l'erreur que ce fichier évite ailleurs (cf. la bascule
        ``raisonnement``, réservée à ``flm`` pour la même raison).

        **Timeout court et dédié sur les deux chemins** (``_VISION_TIMEOUT_S``,
        60 s) — jamais ``model.timeout_s`` : cette méthode tourne en synchrone
        dans le chargement d'un fichier, pas dans une conversation active, et
        doit échouer vite plutôt que bloquer jusqu'aux défauts globaux (600 s
        SDK openai, 300 s lecture ``ollama_client``). Un timeout qui expire lève
        (``httpx.TimeoutException`` côté Ollama, ``openai.APITimeoutError`` côté
        flm) : c'est `RAGEngine._texte_image` qui l'attrape et retombe sur le
        placeholder, pas cette méthode.

        Ni l'un ni l'autre n'accepte de le poser PROPREMENT de la même façon :
        ``ollama.Client.chat()`` n'a pas de paramètre ``timeout`` par appel
        (vérifié sur sa signature réelle) — d'où le second client dédié,
        ``_vision_ollama_client``, construit une fois avec ce timeout. Le SDK
        openai, lui, l'expose sur ``create()``, mais **la retry policy par
        défaut (2 essais) MULTIPLIE l'attente au lieu de la borner** — mesuré :
        un ``timeout=0.5`` seul relève à 5,4 s avant de lever, contre 1,9 s avec
        ``max_retries=0``. Sans ce réglage, ``_VISION_TIMEOUT_S`` ne bornerait
        rien — le pire cas réel serait ~3x plus long que la valeur affichée.
        """
        provider, model_id = self._parse_model(model)
        if provider == "ollama":
            response = _vision_ollama_client.chat(
                model=model_id,
                messages=[{"role": "user", "content": _VISION_PROMPT, "images": [str(path)]}],
            )
            content = response["message"]["content"] or ""
            if not content:
                # Diagnostic AVANT de rendre la chaîne vide : `_texte_image`
                # (core/rag.py) ne voit plus que le résultat, pas la réponse
                # brute. `done_reason` distingue une génération COUPÉE
                # (`"length"`, le piège déjà documenté pour le chat — la
                # réflexion épuise le budget avant la réponse) d'un modèle qui
                # a réellement décidé de ne rien dire (`"stop"`, 0 token émis).
                logger.warning(
                    "describe_image (ollama:%s) : content vide — done_reason=%r, "
                    "eval_count=%r",
                    model_id, response.get("done_reason"), response.get("eval_count"),
                )
            return content
        if provider in _OPENAI_COMPAT:
            # `max_retries=0` : mesuré, le comportement par défaut du SDK
            # (2 essais) MULTIPLIE l'attente sur un timeout au lieu de la
            # borner — un `timeout=0.5` seul relève à 5,4 s avant de lever ;
            # avec `max_retries=0`, 1,9 s. Sans ce réglage, `_VISION_TIMEOUT_S`
            # ne borne rien : le pire cas réel serait ~3x plus long.
            client = self._openai_client(provider).with_options(
                timeout=_VISION_TIMEOUT_S, max_retries=0)
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            ext = Path(path).suffix.lstrip(".").lower()
            mime = "jpeg" if ext == "jpg" else (ext or "png")
            response = client.chat.completions.create(
                model=model_id,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VISION_PROMPT},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                    ],
                }],
            )
            message = response.choices[0].message
            content = message.content or ""
            if not content:
                # Même diagnostic côté openai-compat : `finish_reason` (`stop`
                # vs `length` vs `content_filter`), `refusal` (schéma OpenAI
                # pour un refus de contenu — jamais vu sur flm, mais le champ
                # existe et coûte rien à logger), et `model_extra` (un champ
                # non modélisé par le SDK, comme `reasoning_content` sur le
                # chemin streaming — cf. `_stream_openai` — se retrouve là).
                logger.warning(
                    "describe_image (%s:%s) : content vide — finish_reason=%r, "
                    "refusal=%r, extra=%r, usage=%r",
                    provider, model_id, response.choices[0].finish_reason,
                    getattr(message, "refusal", None), message.model_extra,
                    response.usage,
                )
            return content
        raise ValueError(f"describe_image : provider '{provider}' non pris en charge pour la vision")

    # ── Ollama ───────────────────────────────────────────────────────────────

    def _stream_ollama(self, messages: list[dict], model: str, max_tokens: Optional[int] = None,
                       raisonnement: bool = True) -> Generator:
        """Flux Ollama : texte (``str``), raisonnement et stats (dicts sentinelles).

        **Le raisonnement arrive dans un champ SÉPARÉ, et il était jeté.** Mesuré
        sur ce dépôt (Ollama 0.32.15, client python 0.6.2, ``qwen3:8b``) : le
        schéma réel de ``chunk.message`` est
        ``role, content, thinking, images, tool_name, tool_calls``, et sur une
        question d'arithmétique **298 chunks sur 299 portaient un ``thinking``
        non vide avec un ``content`` VIDE**. Aucune balise ``<think>`` à parser —
        Ollama sépare lui-même, et il le fait **sans qu'on le demande** : aucun
        argument ``think`` n'est passé ci-dessous, et le raisonnement arrive
        quand même. C'est pour ça qu'il n'y en a pas : le demander ne changerait
        rien pour les modèles qui pensent, et modifierait l'appel pour ceux qui
        ne pensent pas (``qwen2.5:7b`` : ``thinking: null``, 4 chunks), qui
        doivent rester strictement inchangés.

        L'ancien code lisait ``chunk["message"]["content"]`` et faisait
        ``if content: yield content``. Un chunk de raisonnement a
        ``content == ""``, donc falsy, donc **rien n'était yieldé** : le
        raisonnement était jeté ici, chunk par chunk, avant que quoi que ce soit
        en aval puisse le voir. Coût mesuré sur le chemin réel
        (``LLMEngine.stream``, ``max_tokens=2048``) : **584 tokens générés en
        78 s, premier caractère visible à 76,5 s**, pour ``17 x 23 = 391.`` —
        14 caractères. 76 secondes de silence total dans le chat, et le budget
        ``num_predict`` consommé de façon invisible.

        La forme du yield suit la convention déjà en place dans ce générateur —
        du ``str`` pour le texte, un dict sentinelle pour le reste (``__stats__``
        existait déjà). Le nom ``__reasoning__`` n'est pas choisi ici : un
        commentaire de ``modules/settings/router.py`` le nommait **avant** que ce
        code existe, en anticipation. Autant tenir la promesse qui était écrite.

        **Tous les consommateurs de ``stream()`` filtrent déjà par
        ``isinstance(item, str)``** avant de concaténer (vérifié un par un :
        ``codeagent`` ×4, ``docanalysis`` ×4, ``module_workshop``,
        ``orchestrator``, les deux du chat, ``settings``), donc cette sentinelle
        supplémentaire ne peut pas se retrouver concaténée à du texte par
        accident. Le seul qui ne filtrait pas — ``_stream_résumé_sse`` — sérialisait
        déjà ``__stats__`` comme un token ; il est corrigé dans le même lot.

        **``raisonnement=False`` → ``think=False``. Mais l'inverse n'est PAS
        ``think=True``, et cette asymétrie est mesurée, pas prudentielle :**

        ============================  ==========================================
        appel sur ``qwen2.5:7b``      résultat
        ============================  ==========================================
        aucun argument ``think``      200, 4 chunks, ``391``
        ``think=False``               200, 4 chunks, ``391`` — ignoré proprement
        ``think=True``                **400** ``"qwen2.5:7b" does not support
                                      thinking``
        ============================  ==========================================

        Vérifié aussi sur ``qwen2.5-coder:7b`` : même 400. Donc un réglage
        « raisonnement activé » qui poserait ``think=True`` casserait le chat sur
        le modèle par défaut de ``config.yaml``, et sur tous les modèles non
        pensants — un réglage censé n'ajouter qu'un affichage. « Activé » veut
        dire **ne rien passer**, ce qui est exactement l'état d'avant ce
        paramètre. ``think=False``, lui, est sûr partout : mesuré ignoré sur les
        deux modèles sans raisonnement, et il évite les ~570 tokens que le modèle
        produirait pour rien.
        """
        appel: dict = {
            "model": model, "messages": messages, "stream": True,
            "options": {
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": self._budget(max_tokens, raisonnement),
                "num_thread": 8,
            },
        }
        if not raisonnement:
            appel["think"] = False
        for chunk in ollama_client.chat(**appel):
            message = chunk["message"]
            # `.get()` et non `message["thinking"]` : `Message` est un
            # `SubscriptableBaseModel` d'Ollama, dont l'indexation d'une clé
            # absente lève. Vérifié plutôt que supposé — `get()` existe bien et
            # rend `None` sur un modèle qui ne pense pas.
            reasoning = message.get("thinking")
            if reasoning:
                # Avant le contenu du même chunk : mesuré, aucun chunk ne porte
                # les deux à la fois (3 formes de prompt sur qwen3:8b, séquence
                # toujours `thinking×N → content×N`), mais l'ordre correct ne
                # coûte rien et vaut mieux qu'une hypothèse.
                yield {"__reasoning__": True, "content": reasoning}
            content = message["content"]
            if content:
                yield content
            try:
                if chunk["done"]:
                    # `done_reason == "length"` = le plafond `num_predict` a été
                    # atteint, donc la génération est COUPÉE, pas terminée.
                    #
                    # Il était ignoré, et c'est ce qui rendait le bug muet : sur
                    # un modèle qui pense, la réflexion peut consommer tout le
                    # budget et la réponse n'être jamais produite. Le chat
                    # affichait alors une bulle vide, indiscernable d'un modèle
                    # qui n'aurait rien à dire.
                    #
                    # `.get()` sur un `SubscriptableBaseModel` : le champ existe
                    # dans le schéma, mais un serveur plus ancien peut le laisser
                    # à `None` — auquel cas on ne prétend pas savoir.
                    yield {
                        "__stats__": True,
                        "prompt_tokens": chunk["prompt_eval_count"] or 0,
                        "output_tokens": chunk["eval_count"] or 0,
                        "eval_duration_ns": chunk["eval_duration"] or 0,
                        "prompt_duration_ns": chunk["prompt_eval_duration"] or 0,
                        "tronqué": chunk.get("done_reason") == "length",
                    }
            except Exception:
                pass

    def _generate_ollama(self, messages: list[dict], model: str) -> str:
        response = ollama_client.chat(
            model=model, messages=messages, stream=False,
            options={
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": self._gen["max_tokens"],
                "num_thread": 8,
            },
        )
        return response["message"]["content"]

    # ── Gemini ───────────────────────────────────────────────────────────────

    def _stream_gemini(self, messages: list[dict], model: str, max_tokens: Optional[int] = None) -> Generator:
        try:
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError("Package 'google-generativeai' non installé")

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("GEMINI_API_KEY non configurée — ajoutez-la dans Settings")

        genai.configure(api_key=api_key)
        model_name = model.split("gemini:", 1)[1]
        sys_instr, contents = _gemini_contents(messages)

        kwargs: dict = {}
        if sys_instr:
            kwargs["system_instruction"] = sys_instr
        gen_model = genai.GenerativeModel(model_name, **kwargs)
        response = gen_model.generate_content(
            contents, stream=True,
            generation_config=genai.types.GenerationConfig(
                temperature=self._gen["temperature"],
                max_output_tokens=max_tokens or self._gen["max_tokens"],
            ),
        )

        stream_start = time.time()
        for chunk in response:
            if chunk.text:
                yield chunk.text
        stream_end = time.time()  # capture après la boucle complète

        try:
            meta = response.usage_metadata
            yield {
                "__stats__": True,
                "prompt_tokens": meta.prompt_token_count or 0,
                "output_tokens": meta.candidates_token_count or 0,
                "eval_duration_ns": int((stream_end - stream_start) * 1e9),
                "prompt_duration_ns": 0,
            }
        except Exception:
            pass

    def _generate_gemini(self, messages: list[dict], model: str) -> str:
        try:
            import google.generativeai as genai
        except ImportError:
            return "[Erreur: google-generativeai non installé]"
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return "[Erreur: GEMINI_API_KEY non configurée]"
        try:
            genai.configure(api_key=api_key)
            model_name = model.split("gemini:", 1)[1]
            sys_instr, contents = _gemini_contents(messages)
            kwargs: dict = {}
            if sys_instr:
                kwargs["system_instruction"] = sys_instr
            gen_model = genai.GenerativeModel(model_name, **kwargs)
            response = gen_model.generate_content(
                contents,
                generation_config=genai.types.GenerationConfig(
                    temperature=self._gen["temperature"],
                    max_output_tokens=self._gen["max_tokens"],
                ),
            )
            return response.text
        except Exception as exc:
            logger.warning("Generate Gemini (%s) échec : %s", model, exc)
            return f"[gemini:{model.split('gemini:', 1)[-1]}] échec : {exc}"

    # ── OpenAI-compatible providers ──────────────────────────────────────────

    def _stream_openai(self, messages: list[dict], model_id: str, client, provider: str = "",
                       max_tokens: Optional[int] = None, raisonnement: bool = True) -> Generator:
        """Flux OpenAI-compatible. ``raisonnement`` n'agit que sur ``flm``.

        **FastFlowLM (le NPU) a une bascule, et elle ne marche pas comme celle
        d'Ollama.** Mesuré sur ce poste (FLM 0.9.43, ``qwen3:4b``, dont
        ``GET /api/ps`` annonce ``think_toggleable: true``) :

        ==========================  ======  =============  ====================
        corps de requête            durée   raisonnement   contenu
        ==========================  ======  =============  ====================
        ``think: true``              20 s   733 car.       ``17 x 23 = 391.``
        ``think: false``              4 s   aucun          ``17 x 23 = 391``
        ==========================  ======  =============  ====================

        Trois différences avec Ollama, toutes vérifiées :

        1. **``think=True`` est SÛR ici.** Sur ``lfm2:1.2b``
           (``think: false, think_toggleable: false`` dans ``/api/ps``), les deux
           valeurs répondent 200 en 2,8 s et le flag est ignoré. Pas de 400,
           contrairement à Ollama — donc pas besoin de l'asymétrie du chemin
           Ollama.
        2. **Le flag doit être passé À CHAQUE APPEL, dans les deux sens.** Son
           absence n'est pas « valeur par défaut du modèle » mais **« garde la
           valeur du dernier appel »** : séquence mesurée —
           ``think=false`` → 4 s ; ``rien`` → 4 s ; ``think=true`` → 18,5 s ;
           ``rien`` → 27 s avec raisonnement. L'état est collant côté serveur.
           Ne poser le flag que pour couper laisserait donc le chat en mode
           non-pensant après le premier envoi, sans que rien ne l'explique.
        3. **``extra_body`` et non un kwarg.** Le SDK ``openai`` lève sur un
           paramètre inconnu ; ``extra_body`` le fusionne dans le corps JSON.
           Vérifié à travers le SDK, pas seulement en HTTP brut.

        Les autres fournisseurs OpenAI-compatibles (groq, cerebras, mistral,
        nvidia, deepseek) ne reçoivent **rien de nouveau** : leur bascule n'a pas
        été mesurée, et ``extra_body`` part vers une API distante qui pourrait
        refuser un champ inconnu. On ne devine pas sur du réseau facturé.

        **Le raisonnement de FLM est remonté depuis le 2026-08-24**, comme celui
        d'Ollama et sous la même sentinelle ``__reasoning__`` — donc le même
        ``{"type": "reasoning"}`` sur ``/ws/chat`` et le même bloc repliable, sans
        une ligne de frontend à ajouter. Ce paragraphe disait le contraire la
        veille (« non traité dans ce lot »), et disait aussi, l'avant-veille, que
        FLM ne séparait pas le raisonnement du contenu : **c'était une mesure trop
        étroite.** Vrai de ``qwen3.5:4b``, faux de ``qwen3:4b``, qui envoie bien
        un champ à part. Un seul modèle sondé ne dit rien de la famille.

        Ce qui a été mesuré avant d'écrire (FLM 0.9.43, ``qwen3:4b``,
        ``think: true``, ``max_tokens`` = 2048 du ``config.yaml``) :

        * **le nom exact du champ est ``reasoning_content``** — pas ``reasoning``.
          Lire le mauvais aurait donné un flux vide sans la moindre erreur ;
        * il est atteignable **en attribut** (``delta.reasoning_content``) bien
          qu'il ne soit pas modélisé par le SDK : ``ChoiceDelta`` accepte les
          champs extra et les expose. ``getattr(..., None)`` plutôt qu'un accès
          direct, pour que la disparition de cette tolérance donne « pas de
          raisonnement » et non une exception ;
        * **le premier chunk le porte VIDE** (``reasoning_content: ""``), d'où le
          test de vérité et non de présence — sinon une sentinelle vide part à
          chaque flux ;
        * séquence ``raisonnement×1419 → contenu×290``, aucun chunk portant les
          deux, aucun retour en arrière — même forme qu'Ollama ;
        * **premier contenu à 91,8 s**, premier raisonnement à 4,2 s. Le silence
          était donc ici plus long que sur Ollama (76 s), sur le chemin NPU qui
          est censé être le rapide.

        **Réservé à ``flm``, délibérément.** Les autres fournisseurs
        OpenAI-compatibles n'ont pas été mesurés — ``deepseek`` en particulier
        publie un ``reasoning_content`` sur son modèle de raisonnement, et le
        remonter serait probablement juste. Mais « probablement » n'est pas une
        mesure, et la vérifier veut dire appeler une API **payante**. Lever la
        garde quand ce sera mesuré tient en un mot : retirer le test sur
        ``provider``. Le reste du code n'a pas à changer.
        """
        oai = [{"role": m["role"], "content": m["content"]} for m in messages]
        stream_start = time.time()
        prompt_tokens = 0
        output_tokens = 0
        tronque = False
        mt = self._budget(max_tokens, raisonnement)

        def _create(with_usage: bool):
            kwargs = dict(
                model=model_id, messages=oai, stream=True,
                temperature=self._gen["temperature"], max_tokens=mt,
            )
            if provider == "flm":
                kwargs["extra_body"] = {"think": bool(raisonnement)}
            if with_usage:
                kwargs["stream_options"] = {"include_usage": True}
            return client.chat.completions.create(**kwargs)

        try:
            stream = _create(with_usage=True)
        except Exception:
            # Le provider peut ne pas gérer stream_options : on retente sans.
            # Si ça échoue encore, c'est une vraie erreur API → message explicite.
            try:
                stream = _create(with_usage=False)
            except Exception as exc:
                logger.warning("Stream %s (%s) refusé : %s", provider, model_id, exc)
                raise RuntimeError(_provider_error_message(provider, model_id, exc)) from exc

        try:
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is not None:
                    if provider == "flm":
                        # Champ extra, non modélisé par le SDK : `getattr` et non
                        # `delta.reasoning_content`, pour que la fin de cette
                        # tolérance rende None au lieu de lever. Test de VÉRITÉ et
                        # non de présence — le premier chunk le porte vide.
                        reasoning = getattr(delta, "reasoning_content", None)
                        if reasoning:
                            # Avant le contenu du même chunk, comme sur le chemin
                            # Ollama. Mesuré : aucun chunk ne porte les deux, mais
                            # l'ordre correct ne coûte rien.
                            yield {"__reasoning__": True, "content": reasoning}
                    if delta.content:
                        yield delta.content
                # `finish_reason == "length"` : le plafond `max_tokens` a été
                # atteint, donc la génération est COUPÉE. Pendant OpenAI du
                # `done_reason` d'Ollama, et même conséquence — sur FLM, dont le
                # raisonnement puise dans le même budget, la réponse peut ne
                # jamais être produite.
                #
                # `getattr` en cascade : `choices` peut être vide sur le chunk
                # d'usage final, et `finish_reason` absent selon le fournisseur.
                if chunk.choices:
                    if getattr(chunk.choices[0], "finish_reason", None) == "length":
                        tronque = True
                if getattr(chunk, "usage", None):
                    prompt_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0
        except Exception as exc:
            # Erreur survenue en cours de streaming (coupure, refus serveur…).
            logger.warning("Stream %s (%s) interrompu : %s", provider, model_id, exc)
            raise RuntimeError(_provider_error_message(provider, model_id, exc)) from exc

        yield {
            "__stats__": True,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "eval_duration_ns": int((time.time() - stream_start) * 1e9),
            "prompt_duration_ns": 0,
            "tronqué": tronque,
        }

    def _generate_openai(self, messages: list[dict], model_id: str, client, provider: str = "") -> str:
        oai = [{"role": m["role"], "content": m["content"]} for m in messages]
        try:
            response = client.chat.completions.create(
                model=model_id, messages=oai, stream=False,
                temperature=self._gen["temperature"],
                max_tokens=self._gen["max_tokens"],
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("Generate %s (%s) refusé : %s", provider, model_id, exc)
            return _provider_error_message(provider, model_id, exc)
