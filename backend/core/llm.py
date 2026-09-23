import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Generator, Optional

import httpx
import ollama
import yaml
from dotenv import load_dotenv

from core.paths import BACKEND_DIR

_ENV_FILE = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_FILE)

logger = logging.getLogger(__name__)

_CONFIG_FILE = BACKEND_DIR / "config.yaml"

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
    existe. Chemin absolu, comme le défaut de ``LLMEngine`` : un ``"config.yaml"``
    relatif dépendrait du répertoire courant (``test_config_hors_cwd.py``).
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

#: LM Studio expose une API compatible OpenAI en local, sans authentification —
#: même famille que FLM, mais avec un port par défaut différent (1234) et,
#: contrairement à FLM, CONFIGURABLE : c'est une application de bureau qui
#: laisse son port se changer dans ses réglages, à la différence du service FLM
#: (port fixe 11435, jamais lu depuis l'environnement ci-dessous). `LMSTUDIO_HOST`
#: reprend donc la convention d'`OLLAMA_HOST` — URL complète, `http://` et le
#: port ajoutés si absents — plutôt qu'un port en dur.
#:
#: Définie ICI et pas dans `core/models.py` (qui la consomme pour ses sondes de
#: détection, `check_lmstudio`/`get_lmstudio_installed`) : une même variable
#: d'environnement parsée indépendamment aux deux endroits pourrait diverger
#: silencieusement si l'une des deux normalisations est oubliée un jour — même
#: raison que `core.models.ollama_host` est déjà importé d'ici plutôt que
#: reparsé.
lmstudio_host = os.environ.get("LMSTUDIO_HOST", "").strip() or "http://localhost:1234"
if not lmstudio_host.startswith("http"):
    lmstudio_host = f"http://{lmstudio_host}:1234"

# OpenAI-compatible providers: name → (base_url, env_key | None)
# env_key=None means no API key required (local server)
_OPENAI_COMPAT: dict[str, tuple[str, str | None]] = {
    "groq":     ("https://api.groq.com/openai/v1",      "GROQ_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1",          "CEREBRAS_API_KEY"),
    "mistral":  ("https://api.mistral.ai/v1",           "MISTRAL_API_KEY"),
    "nvidia":   ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1",         "DEEPSEEK_API_KEY"),
    "flm":      ("http://localhost:11435/v1",           None),
    "lmstudio": (f"{lmstudio_host}/v1",                 None),
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

#: Longueur maximale de la question REPRISE dans le prompt vision ciblé.
#:
#: `_VISION_PROMPT` est court par nécessité MESURÉE (cf. juste au-dessus :
#: `moondream` dégénère sur une formulation longue — réponse vide ou boucle de
#: répétition). Le prompt ciblé, lui, contient un texte que nous n'écrivons
#: pas : la question de l'utilisateur. Elle peut faire trois paragraphes, ce qui
#: replacerait exactement le modèle dans la condition qui le fait dégénérer,
#: sans qu'aucune ligne du dépôt n'ait changé. La borne est donc sur la
#: question, pas sur le gabarit — 500 caractères, l'ordre de grandeur d'une
#: question réellement posée à une image, et un budget qui laisse le gabarit
#: dominer le prompt.
_VISION_QUESTION_MAX = 500

#: Gabarit du prompt vision CIBLÉ, quand un appelant fournit une question.
#:
#: Volontairement aussi court que `_VISION_PROMPT`, et pour la même raison
#: mesurée. La question passe en PREMIER : sur un modèle qui s'arrête tôt
#: (`eval_count` proche de 1, le mode d'échec déjà observé), ce qui a été lu en
#: premier est ce qui a le plus de chances d'avoir orienté la lecture de
#: l'image. La consigne de transcription est conservée — c'est elle qui fait la
#: différence sur un énoncé dense, où une légende ne suffit pas (cf. la mesure
#: `flm` vs `moondream` du §3.3 bis de CLAUDE.md).
_VISION_PROMPT_CIBLE = (
    "Question : {question}\n\n"
    "Regarde l'image et réponds à cette question. "
    "Transcris tout texte, formule ou annotation utile pour y répondre."
)


def prompt_vision(question: Optional[str] = None) -> str:
    """Prompt de `describe_image` : générique sans question, ciblé avec.

    Public (pas de `_`) parce que les tests l'éprouvent et qu'un appelant qui
    veut savoir ce qui part au modèle ne doit pas avoir à lire une constante
    privée. La question est nettoyée et bornée (`_VISION_QUESTION_MAX`) ; vide
    ou blanche, elle rend le prompt générique — un appelant qui passe une
    chaîne vide demande le comportement d'avant, pas un prompt ciblé creux.
    """
    q = (question or "").strip()
    if not q:
        return _VISION_PROMPT
    return _VISION_PROMPT_CIBLE.format(question=q[:_VISION_QUESTION_MAX])


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


# ── Tool-calling natif — registre de skills, Ollama SEUL ─────────────────────
#
# Mécanisme SÉPARÉ et INDÉPENDANT des déclenchements heuristiques/manuels
# (core/websearch.py::detecter_intention_recherche pour @web, l'injection
# eager de `@historique` dans modules/chat/router.py — déclenchés AVANT ou
# INDÉPENDAMMENT du tour de chat, sans que le modèle en décide). Ici c'est le
# MODÈLE, en cours de génération, qui décide d'appeler un outil — les deux
# mécanismes coexistent volontairement pour chaque skill (CLAUDE.md §3.7
# documente déjà cette distinction pour d'autres tâches de fond ; ceci n'y
# déroge pas, un appel déclenché par le modèle reste un choix fait POUR
# répondre au message en cours, jamais une tâche de fond). Rien ici ne touche,
# ne désactive ni ne remplace les chemins manuels/heuristiques existants.
#
# `tools=` est câblé sur `_stream_ollama` ET, depuis le 2026-09-22, sur
# `_stream_openai` pour le SEUL fournisseur `lmstudio` (cf. sa docstring). Les
# six autres fournisseurs de `_stream_openai` et `_gemini_contents` continuent
# de normaliser les messages en `{role, content}` et ne reçoivent aucun outil.
# La boucle de dispatch (budgets, outil inconnu, exécuteur, renumérotation,
# sentinelle `__tool_call__`) est COMMUNE aux deux chemins :
# `_executer_appels_outil` — seuls la forme du message `role="tool"` et le
# décodage des arguments diffèrent, et restent chez chaque appelant.
#
# `_SKILLS` est le registre : chaque entrée porte son schéma exposé au modèle,
# son exécuteur (signature uniforme `(arguments: dict, on_etape=None,
# rang_de_depart=0) -> tuple[str, list]`, pour que la boucle de dispatch
# n'ait jamais besoin de connaître le nom de l'argument métier d'un skill) et
# son plafond d'invocations. Ajouter un skill = ajouter une entrée ; rien
# d'autre dans ce fichier ne nomme "web_search" ou "history_search" en dur
# après ce chantier, hormis la traduction historique
# `tool_call_web_search`/`tool_call_history_search` (émise DANS chaque
# exécuteur, cf. leurs docstrings) — c'est ce nom qui alimente le panneau de
# trace de recherche, pas un canal générique.

#: Cap sur le nombre d'INVOCATIONS de l'outil par tour (pas de rounds
#: `chat()` — un modèle peut demander plusieurs appels dans le même round).
#: Une boucle non bornée sur un outil qui ne renvoie jamais « j'ai assez
#: d'information » consommerait un `num_predict` par round indéfiniment.
_MAX_APPELS_OUTIL_WEB = 2

#: Schéma exposé au modèle. Ollama valide ses propres schémas de `tools` via
#: pydantic (`ollama._types.Tool`) — un dict brut suffit, pas besoin de
#: construire l'objet pydantic à la main.
_OUTIL_WEB_SEARCH: dict = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Recherche sur le web (DuckDuckGo) une information récente, "
            "factuelle, ou que tu ne connais pas avec certitude. Cite "
            "ensuite les résultats par leur numéro entre crochets, ex. [1] — "
            "jamais l'URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requete": {
                    "type": "string",
                    "description": "Termes de recherche concis, en français.",
                },
            },
            "required": ["requete"],
        },
    },
}

#: Borne du texte de résultat renvoyé au modèle (`role="tool"`), au même
#: ordre de grandeur que le budget d'une recherche @web classique
#: (`core/webcontent.py` : jusqu'à 5 pages × 800 caractères). Sans borne, un
#: tour qui combine le contexte @web du classifieur ET un résultat d'outil
#: peut dépasser `n_ctx: 4096` et faire tomber le système prompt du DÉBUT du
#: contexte plutôt que de tronquer proprement ici.
_BUDGET_CARACTERES_OUTIL_WEB = 4000

#: Rafraîchi au plus toutes les `_CAPACITES_TTL_S` : `capacites_installees()`
#: est une requête HTTP (`/api/tags`) par tour sinon, sur le chemin chaud du
#: TTFT (cf. les mesures de `_stream_ollama`). `(horodatage, {id: set|None})`.
_CAPACITES_TTL_S = 30.0
_capacites_cache: tuple[float, dict] = (0.0, {})


def _capacites_ollama_fraiches() -> dict:
    """`{id du modèle: set des capacités déclarées}`, caché `_CAPACITES_TTL_S`.

    Import PARESSEUX : `core.ollama_memoire` importe `core.llm` au niveau
    module (host normalisé, cf. `hote_ollama()`) — un import en tête de
    fichier ici créerait un cycle (`llm` → `ollama_memoire` → `llm`).

    `{}` si Ollama ne répond pas (jamais `None` propagé) : l'appelant ne doit
    jamais planter sur cette sonde, seulement se priver de l'outil — même
    philosophie que le reste de `core/ollama_memoire.py`.
    """
    global _capacites_cache
    horodatage, cache = _capacites_cache
    if time.time() - horodatage < _CAPACITES_TTL_S:
        return cache
    from core.ollama_memoire import capacites_installees
    capacites = capacites_installees() or {}
    _capacites_cache = (time.time(), capacites)
    return capacites


def _capacite_tools_disponible(model: str) -> bool:
    """Le modèle DÉCLARE-t-il la capacité `tools` ? Jamais supposé.

    Nom générique et non `_outil_web_disponible` (son nom avant le registre
    de skills) : Ollama ne déclare qu'UNE capacité `tools` par modèle, pas une
    par outil — cette sonde gate donc le registre `_SKILLS` entier, jamais un
    skill en particulier. Un modèle qui la déclare peut recevoir n'importe
    quel schéma du registre ; un modèle qui ne la déclare pas n'en reçoit
    aucun.

    Même piège que `think=True` sur un modèle sans raisonnement (§3.6
    CLAUDE.md, mesuré : 400 `"...` does not support thinking`"`) : passer
    `tools=` à un modèle qui ne la déclare pas casserait le tour entier au
    lieu d'être ignoré proprement. `None` (Ollama injoignable, ou champ
    absent pour ce modèle) est traité comme « non disponible » — l'absence de
    preuve n'autorise pas l'appel.
    """
    declarees = _capacites_ollama_fraiches().get(model)
    return declarees is not None and "tools" in declarees


def _plafond_rounds_outils(budgets: dict[str, int]) -> int:
    """Nombre MAXIMAL de rounds d'un tour de tool-calling : un par invocation
    budgétée, plus le round de conclusion.

    Les budgets par skill ne suffisent pas à borner la boucle : un appel à un
    outil INCONNU (nom inventé, skill non actif ce tour) reçoit un message
    « Outil inconnu » sans décrémenter aucun budget. Un petit modèle qui
    insiste relancerait donc un round, puis un autre, sans fin — chaque round
    renvoyant le prompt entier et consommant un `num_predict`. Au-delà de
    ce plafond, aucune invocation budgétée ne peut plus avoir eu lieu, donc
    aucun round de plus n'est justifié.
    """
    return sum(budgets.values()) + 1


def _schemas_du_round(
    registre: dict[str, dict], budgets: dict[str, int], round_courant: int,
    plafond: int, on_etape: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    """Schémas `tools` à exposer au round `round_courant` (compté depuis 0).

    Ceux dont le budget est encore positif — sauf au DERNIER round permis par
    `plafond` (`_plafond_rounds_outils`), qui conclut sans outil. Si ce
    dernier round retire des outils que les budgets autorisaient encore, c'est
    le plafond qui a tranché, pas l'épuisement normal : étape de trace
    `tool_call_plafond_atteint`, pour que l'arrêt se voie dans le panneau au
    lieu d'être un silence. Un épuisement normal des budgets n'émet rien.
    """
    schemas = [registre[n]["schema"] for n in budgets if budgets[n] > 0]
    if schemas and round_courant >= plafond - 1:
        # `==` et non `>=` pour l'étape : un round de conclusion SUPPLÉMENTAIRE
        # (cf. `conclusion_forcee` dans `_stream_ollama`) ne la réémet pas.
        if round_courant == plafond - 1:
            logger.info("Tool-calling : plafond de %d rounds atteint, conclusion sans outil", plafond)
            if on_etape:
                on_etape({"etape": "tool_call_plafond_atteint", "rounds": round_courant})
        return []
    return schemas


#: Pendant LM Studio de `_capacites_cache`, même TTL et même raison : sans
#: cache, une requête HTTP (`/api/v1/models`) de plus par tour de chat.
_capacites_lmstudio_cache: tuple[float, dict] = (0.0, {})


def _capacites_lmstudio_fraiches() -> dict:
    """`{clé du modèle: bool}`, caché `_CAPACITES_TTL_S` — cf.
    `core.models.capacites_outils_lmstudio` pour la source et sa mesure.

    Import PARESSEUX, même raison que `_capacites_ollama_fraiches` :
    `core.models` importe `core.llm` au niveau module (`lmstudio_host`,
    `LLMEngine`), un import en tête de fichier ici créerait un cycle.

    `{}` si LM Studio ne répond pas — l'appelant se prive de l'outil, ne
    plante jamais.
    """
    global _capacites_lmstudio_cache
    horodatage, cache = _capacites_lmstudio_cache
    if time.time() - horodatage < _CAPACITES_TTL_S:
        return cache
    from core.models import capacites_outils_lmstudio
    capacites = capacites_outils_lmstudio() or {}
    _capacites_lmstudio_cache = (time.time(), capacites)
    return capacites


#: Consigne ajoutée au prompt système quand LM Studio CONTINUE un message
#: assistant après une tentative d'outil abandonnée (`_continuer_sans_outil`).
#: Mesuré sans elle : le modèle suit parfois sa propre annonce et écrit
#: « *(Je vais chercher les informations actualisées)* » — une recherche
#: promise qui n'aura jamais lieu. Avec elle, il répond et signale que ses
#: données peuvent être datées.
_CONSIGNE_SANS_OUTIL = (
    "La recherche web n'a pas pu aboutir pour ce message et aucun outil n'est "
    "disponible : termine ta réponse directement avec tes connaissances, sans "
    "annoncer ni simuler de recherche, et signale qu'elles peuvent être datées."
)


def _continuer_sans_outil(oai: list[dict], texte_emis: str) -> None:
    """Prépare `oai` (la copie LOCALE des messages) pour que le round suivant
    CONTINUE la réponse déjà affichée au lieu d'en recommencer une.

    **LM Studio continue un dernier message `assistant` inachevé — mesuré le
    2026-09-22, pas supposé** (`mistralai/ministral-3-3b`) :

    * aucun drapeau n'est nécessaire : un historique qui se termine par un
      message `assistant` est traité comme un préfixe de la réponse
      (« …sont le bleu, » → « le blanc et le rouge… », « 1, 2, 3, 4, » →
      « 5, 6, … 10 », 3/3 à température 0). `continue_final_message` dans
      `extra_body` ne change rien — il n'est donc pas envoyé ;
    * scénario réel (système + question d'actualité + annonce « Je vais
      chercher… », température 0,7 du chat, 3 formulations × 5 tirages) :
      **aucune répétition du préambule, aucun pseudo-appel d'outil** dans le
      texte, avec ou sans consigne. Sans consigne, en revanche, le modèle
      annonce parfois encore une recherche — d'où `_CONSIGNE_SANS_OUTIL` ;
    * sans prompt système du tout et à température 0, le même scénario a
      produit un `{fetch…` en clair : la consigne est donc posée même quand
      l'historique n'avait pas de message système.

    Mute `oai` en place : consigne ajoutée au PREMIER message système (ou
    insérée en tête), puis le texte émis en dernier message `assistant`. Ne
    touche jamais l'historique de l'appelant (`oai` est déjà une copie).
    """
    if oai and oai[0].get("role") == "system":
        oai[0] = {**oai[0], "content": f"{oai[0].get('content') or ''}\n\n{_CONSIGNE_SANS_OUTIL}"}
    else:
        oai.insert(0, {"role": "system", "content": _CONSIGNE_SANS_OUTIL})
    oai.append({"role": "assistant", "content": texte_emis})


def _capacite_tools_lmstudio(model_id: str) -> bool:
    """Le modèle LM Studio est-il ENTRAÎNÉ au tool-calling ? Jamais supposé.

    Même règle que `_capacite_tools_disponible` côté Ollama : absent,
    injoignable ou `False` → aucun outil. `False` couvre aussi le mode d'outils
    « par défaut » de LM Studio (prompt injecté + parsing de marqueurs), filtré
    volontairement — cf. `core.models.capacites_outils_lmstudio`.
    """
    return _capacites_lmstudio_fraiches().get(model_id) is True


def _executer_appels_outil(
    appels: list[tuple[str, dict, object]],
    registre: dict[str, dict],
    budgets: dict[str, int],
    msgs: list[dict],
    message_outil: Callable[[str, object, str], dict],
    on_etape: Optional[Callable[[dict], None]],
    rang_suivant: int,
) -> Generator:
    """Dispatch des appels d'outil d'UN round — commun à `_stream_ollama` et au
    chemin LM Studio de `_stream_openai`, pour qu'il n'existe qu'une seule
    implémentation des budgets, du message « outil inconnu »/« budget
    épuisé », de l'exécuteur, de la renumérotation des rangs et de la
    sentinelle `__tool_call__` (que `modules/chat/router.py` replie dans
    `web_resultats`, validé ensuite par `core.citations` quelle que soit
    l'origine).

    `appels` : `(nom, arguments déjà décodés en dict, corrélation)`. Les deux
    différences entre les chemins restent CHEZ L'APPELANT, jamais ici :
    le décodage des arguments (Ollama les rend déjà en dict ; l'API OpenAI en
    chaîne JSON, accumulée fragment par fragment) et la forme du message
    `role="tool"` (`message_outil(nom, correlation, contenu)` — `tool_name`
    côté Ollama, qui n'a pas d'`id` ; `tool_call_id` côté OpenAI, qui l'exige).

    Générateur : yielde les sentinelles `__tool_call__`, AJOUTE les messages
    `role="tool"` à `msgs` (la copie locale de l'appelant, jamais l'historique)
    et DÉCRÉMENTE `budgets` sur place. Rend (`return`, lu via `yield from`)
    le `rang_suivant` après ce round.
    """
    for nom, arguments, correlation in appels:
        if nom not in budgets:
            # Absent du registre, OU présent mais pas actif pour CE tour (le
            # caller ne l'a pas demandé) : les deux cas se traitent pareil, un
            # modèle ne peut pas invoquer un outil qui ne lui a pas été offert.
            msgs.append(message_outil(nom, correlation, f"Outil inconnu : {nom}"))
            continue
        if budgets[nom] <= 0:
            msgs.append(message_outil(
                nom, correlation,
                "Budget d'appels d'outil épuisé pour ce tour — réponds avec ce que tu as déjà.",
            ))
            continue
        budgets[nom] -= 1
        texte_outil, resultats = registre[nom]["executor"](
            arguments, on_etape=on_etape, rang_de_depart=rang_suivant,
        )
        # `len(resultats)` vaut 0 par construction pour un skill dont les
        # résultats ne sont pas des `ResultatWeb` numérotés (`history_search`
        # en rend toujours `[]`) : la renumérotation ne s'applique donc, de
        # fait, qu'aux skills citables — sans qu'une branche ait à le savoir.
        rang_suivant += len(resultats)
        # Sentinelle distincte de `__reasoning__`/`__stats__` : porte les
        # résultats STRUCTURÉS jusqu'au consommateur async
        # (modules/chat/router.py), seul endroit qui connaît `web_resultats` —
        # ce générateur tourne dans le thread de fond de `_stream`, il ne peut
        # pas l'étendre lui-même. Le champ `outil` permet à l'appelant de
        # n'étendre `web_resultats` qu'avec les skills citables
        # (`skill_citable`).
        yield {
            "__tool_call__": True, "outil": nom,
            "arguments": arguments, "resultats": resultats,
        }
        msgs.append(message_outil(nom, correlation, texte_outil))
    return rang_suivant


def _executer_outil_web_search(
    arguments: dict, on_etape: Optional[Callable[[dict], None]] = None,
    rang_de_depart: int = 0,
) -> tuple[str, list]:
    """Exécute VRAIMENT `web_search` : réutilise `core.websearch.rechercher` +
    `core.webcontent.recuperer_contenu`, sans dupliquer leur logique — mêmes
    fonctions que `modules/chat/router.py:_rechercher_pour_prompt` (chemin du
    classifieur), à une différence assumée près (cf. plus bas).

    Signature `(arguments: dict, ...)` et non `(requete: str, ...)` : c'est le
    format uniforme du registre `_SKILLS`, qui permet à la boucle de dispatch
    (`_stream_ollama`) d'appeler n'importe quel exécuteur sans connaître le nom
    de son paramètre métier. `requete` est donc extraite ICI, à partir de
    `tc["function"]["arguments"]` transmis tel quel — ce que faisait la boucle
    de dispatch avant ce chantier, déplacé sans changer de comportement.

    L'étape `tool_call_web_search` est émise ICI, avant même de vérifier que
    `requete` est non vide : c'est ce que faisait la boucle de dispatch avant
    ce chantier (elle appelait `on_etape_recherche` puis cet exécuteur), donc
    la migrer dans l'exécuteur ne change ni son contenu ni son ordre relatif
    aux étapes `recherche_debut`/`recherche_resultats` plus bas (même callable
    `on_etape`, transmis tel quel à `rechercher()`).

    Import PARESSEUX : `core.websearch` importe `core.runtime`, qui importe
    `core.llm` pour construire le moteur partagé — un import en tête de
    fichier ici créerait un cycle (`llm` → `websearch` → `runtime` → `llm`).

    **Pas de `reformuler_requete` ici**, à la différence de
    `_rechercher_pour_prompt` : le modèle a DÉJÀ réduit sa question à des
    mots-clés pour remplir l'argument `requete` de l'outil — c'est exactement
    ce que la reformulation produirait pour le chemin classifieur. La rejouer
    appellerait un second modèle (`modele_local_defaut()`, pas forcément
    celui qui est en train de générer CE tour) PENDANT le tour en cours — un
    chargement concurrent que rien ne justifie ici.

    `rang_de_depart` renumérote les rangs `[n]` : sans lui, un DEUXIÈME appel
    d'outil dans le même tour (plafond `_MAX_APPELS_OUTIL_WEB`), ou un tour où
    le classifieur heuristique a AUSSI déclenché une recherche avant le tour
    de chat, produirait plusieurs résultats de rang `[1]` —
    `core.citations`/`_sources_citees` (modules/chat/router.py) résolvent une
    citation `[n]` par rang, donc la collision ferait pointer `[1]` vers la
    MAUVAISE recherche selon l'ordre d'apparition dans `web_resultats`.

    La consigne de citation (« cite par [n], jamais l'URL ») est réécrite ICI
    plutôt qu'héritée de `_construire_web_ctx` (modules/chat/router.py) : ce
    texte part dans un message `role="tool"`, pas le system prompt du
    classifieur — deux points d'injection différents, qui doivent chacun
    porter leur propre consigne, la recherche pouvant être déclenchée par
    l'outil SEUL, sans qu'`@web`/le classifieur n'ait rien injecté ce tour.
    """
    from core.websearch import RechercheWebErreur, formater_pour_llm, rechercher, tronquer_champ
    from core.webcontent import recuperer_contenu

    requete = str((arguments or {}).get("requete") or "").strip()
    if on_etape:
        on_etape({"etape": "tool_call_web_search", "requete": requete})
    if not requete:
        return "Requête vide — aucune recherche effectuée.", []
    try:
        resultats = rechercher(requete, on_etape=on_etape)
    except RechercheWebErreur as exc:
        return f"Recherche impossible : {exc}", []
    resultats = recuperer_contenu(resultats, requete, on_etape=on_etape)
    if not resultats:
        return "Aucun résultat exploitable pour cette recherche.", []
    if rang_de_depart:
        for r in resultats:
            r.rang += rang_de_depart
    texte = (
        "Résultats de recherche web (cite-les par leur numéro entre crochets, "
        "ex. [1] — n'écris JAMAIS d'URL, ne cite JAMAIS un numéro absent de "
        "cette liste) :\n" + formater_pour_llm(resultats)
    )
    return tronquer_champ(texte, _BUDGET_CARACTERES_OUTIL_WEB), resultats


#: Même raisonnement que `_MAX_APPELS_OUTIL_WEB` : borne un `while True` qui
#: n'est pas garanti de converger tout seul. Valeur par défaut identique, faute
#: de raison mesurée de faire autrement.
_MAX_APPELS_OUTIL_HISTORY = 2

#: Schéma exposé au modèle — même forme que `_OUTIL_WEB_SEARCH`.
_OUTIL_HISTORY_SEARCH: dict = {
    "type": "function",
    "function": {
        "name": "history_search",
        "description": (
            "Recherche dans les conversations passées de l'utilisateur un "
            "sujet déjà discuté, quand la question actuelle s'y réfère "
            "implicitement ou que tu as besoin de retrouver ce qui a été dit "
            "précédemment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requete": {
                    "type": "string",
                    "description": "Termes de recherche concis, en français.",
                },
            },
            "required": ["requete"],
        },
    },
}


def _executer_outil_history_search(
    arguments: dict, on_etape: Optional[Callable[[dict], None]] = None,
    rang_de_depart: int = 0,
) -> tuple[str, list]:
    """Exécute `history_search` : délègue à `HistoryEngine.search_history`,
    le même moteur que le chemin manuel `@historique`
    (`modules/chat/router.py`), sans dupliquer son formatage.

    Import PARESSEUX de `core.runtime.history_engine`, même raison que
    `_executer_outil_web_search` : `core.runtime` importe `core.llm` pour
    construire `LLMEngine()`, un import en tête de fichier créerait un cycle
    (`llm` → `runtime` → `llm`).

    **Rend TOUJOURS `resultats=[]`, et c'est le point le plus important de
    cette fonction.** Les extraits d'historique ne sont pas des `ResultatWeb`
    citables par `[n]` — ils n'ont ni rang ni URL — et ne doivent JAMAIS
    entrer dans `web_resultats` (modules/chat/router.py) : ce pipeline
    alimente le bloc Sources et la résolution des citations `[n]`, qui
    résout par RANG. Y verser un résultat d'historique casserait cette
    numérotation en silence, pour n'importe quel autre appel de `web_search`
    dans le même tour. `rang_de_depart` est accepté pour la signature uniforme
    du registre mais n'a aucun effet ici — il n'y a rien à renuméroter.

    `on_etape`, s'il est fourni, reçoit `{"etape": "tool_call_history_search",
    "requete": requete}` avant l'appel — même pattern que
    `tool_call_web_search` côté `_executer_outil_web_search`, pour que le
    panneau de trace de recherche affiche aussi ce déclenchement (il ne
    distingue pas encore les deux origines par un rendu dédié — cf. rapport).

    Pas de borne de troncature dédiée (`_BUDGET_CARACTERES_OUTIL_WEB` n'a pas
    d'équivalent ici) : `search_history` est déjà bornée en amont
    (`n_results=3` × extrait de 300 caractères), un ordre de grandeur bien en
    deçà de ce qui justifierait une troncature supplémentaire.
    """
    from core.runtime import history_engine

    requete = str((arguments or {}).get("requete") or "").strip()
    if on_etape:
        on_etape({"etape": "tool_call_history_search", "requete": requete})
    if not requete:
        return "Requête vide — aucune recherche effectuée.", []
    resultats = history_engine.search_history(requete)
    if not resultats:
        return "Aucun résultat trouvé dans l'historique.", []
    extraits = "\n\n".join(
        f"— {r['titre']} ({r['date']}) :\n{r['extrait']}" for r in resultats
    )
    return f"Extraits de conversations précédentes pertinentes :\n{extraits}", []


#: Repli si aucun réglage utilisateur n'existe pour `recherche_approfondie`
#: (le budget RÉEL vient de `tool_calling.skills.recherche_approfondie.budget`,
#: réglage persistant — cf. `core.memory._CONTEXT_DEFAULT` et
#: `budgets_override` plus bas). En pratique jamais consulté : le défaut de
#: `_CONTEXT_DEFAULT` porte déjà la même valeur — verrouillé par
#: `test_tool_calling_reglages.py` pour que les deux ne divergent pas en
#: silence.
_MAX_APPELS_RECHERCHE_APPROFONDIE = 4

#: Même schéma que `_OUTIL_WEB_SEARCH`, description différente : encourage
#: explicitement l'ITÉRATION (plusieurs appels, requête affinée à partir des
#: résultats précédents), pour que le modèle choisisse ce skill plutôt que
#: `web_search` sur une question qui demande de croiser plusieurs sources.
#: Réutilise `_executer_outil_web_search` tel quel (cf. `_SKILLS` plus bas) —
#: seuls le schéma exposé et le budget diffèrent de `web_search`, la mécanique
#: de recherche (DuckDuckGo + récupération de contenu) est identique.
_OUTIL_RECHERCHE_APPROFONDIE: dict = {
    "type": "function",
    "function": {
        "name": "recherche_approfondie",
        "description": (
            "Recherche web approfondie pour une question complexe qui "
            "nécessite de croiser plusieurs sources ou d'affiner ta requête "
            "selon les résultats précédents avant de conclure — "
            "contrairement à web_search, tu es encouragé à appeler cet "
            "outil plusieurs fois de suite en reformulant ta requête à "
            "partir de ce que tu as déjà trouvé. Cite ensuite les résultats "
            "par leur numéro entre crochets, ex. [1] — jamais l'URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requete": {
                    "type": "string",
                    "description": "Termes de recherche concis, en français.",
                },
            },
            "required": ["requete"],
        },
    },
}


def skill_citable(nom: str) -> bool:
    """Les `resultats` de ce skill sont-ils des `ResultatWeb` NUMÉROTÉS, à
    verser dans `web_resultats` (`modules/chat/router.py`) pour la résolution
    des citations `[n]` ?

    Expose la clé `citable` du registre `_SKILLS` — réexportée par
    `core.runtime` — pour que le routeur n'ait jamais à comparer
    `outil == "web_search"` en dur : `recherche_approfondie` réutilise le
    même exécuteur que `web_search` et rend donc les mêmes `ResultatWeb`,
    mais une comparaison figée sur un seul nom les aurait silencieusement
    jetés — numérotés par `rang_suivant` ci-dessous, jamais transmis au
    consommateur. `history_search`, lui, rend toujours `resultats=[]`
    (cf. sa docstring) : `citable=False` documente ce fait plutôt que de
    compter sur une liste vide pour ne jamais rien casser.
    """
    return bool(_SKILLS.get(nom, {}).get("citable"))


def _assainir_nom_skill(nom: str) -> str:
    """`nom` humain (celui que l'utilisateur tape dans Réglages › Préfixes) →
    identifiant Ollama valide pour ``function.name`` : minuscules, underscores,
    rien d'autre. Peut rendre une chaîne VIDE (un `nom` fait uniquement de
    ponctuation/emojis) — à l'appelant de l'écarter, jamais d'exposer un nom
    vide au modèle."""
    return re.sub(r"[^a-z0-9]+", "_", nom.strip().lower()).strip("_")


def construire_executor_skill_personnalise(instruction: str) -> Callable:
    """Fabrique l'exécuteur d'un skill personnalisé AGENTIQUE — signature
    uniforme du registre (``(arguments, on_etape=None, rang_de_depart=0) ->
    tuple[str, list]``), mais qui IGNORE ses arguments : un skill personnalisé
    n'a pas de paramètre métier (son schéma expose ``parameters: {}``, cf.
    `construire_skills_personnalises`).

    Il n'exécute rien — il RÉVÈLE au modèle, en réponse à son propre appel
    d'outil, le texte d'instruction que l'utilisateur a écrit pour ce
    déclenchement précis. C'est l'équivalent AGENTIQUE du déclenchement manuel
    par préfixe (le modèle décide, lui, du moment) : ni recherche, ni accès
    réseau, ni résultat structuré — `resultats` est toujours `[]`, donc ce
    skill ne peut jamais alimenter `web_resultats` (cf. `skill_citable`, qui
    rend `False` pour un nom absent de `_SKILLS` : un skill personnalisé n'y
    est jamais, par construction)."""
    def executor(arguments, on_etape=None, rang_de_depart=0):
        return instruction, []
    return executor


def construire_skills_personnalises(objets: list[dict]) -> dict[str, dict]:
    """Sous-registre DYNAMIQUE, au format de `_SKILLS`, pour les skills
    personnalisés AGENTIQUES actifs de CE tour — à fusionner par-dessus
    `_SKILLS` par l'appelant (`stream`/`_stream_ollama`), jamais à l'intérieur :
    `_SKILLS` reste le registre STATIQUE des 3 skills natifs.

    `objets` : des éléments déjà normalisés de
    `core.memory.normaliser_prefixes(...)["personnalises"]`, filtrés sur
    `agentique` par l'appelant (`modules/chat/router.py`) — cette fonction ne
    connaît pas ce filtre et ne le réapplique pas.

    Collisions de nom assaini, réglées PAR CONSTRUCTION, jamais par un
    remplacement qui masquerait un skill existant :

    - priorité ABSOLUE aux 3 skills NATIFS de `_SKILLS` — un nom personnalisé
      qui s'assainirait en ``"web_search"`` ne doit jamais pouvoir se
      substituer à la vraie recherche web ;
    - entre deux personnalisés, priorité au premier dans l'ORDRE de `objets`
      — même logique que la priorité de trigger documentée dans
      `core.memory.normaliser_prefixes` ;
    - un nom assaini VIDE (`nom` réduit à de la ponctuation/des emojis) est
      écarté : un ``function.name`` vide serait un schéma malformé envoyé au
      modèle.
    """
    registre: dict[str, dict] = {}
    for obj in objets:
        nom_technique = _assainir_nom_skill(obj.get("nom", ""))
        if not nom_technique or nom_technique in registre or nom_technique in _SKILLS:
            continue
        registre[nom_technique] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": nom_technique,
                    "description": obj.get("description", ""),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            "executor": construire_executor_skill_personnalise(obj.get("instruction", "")),
            # `obj["budget"]` est déjà un entier clampé — `normaliser_prefixes`
            # (core/memory.py) le garantit pour tout élément qui survit à la
            # validation. Le défaut ci-dessous ne sert qu'à un appelant qui
            # passerait un dict construit à la main (tests).
            "budget_max": int(obj.get("budget", 4)),
            "citable": False,
        }
    return registre


#: Registre des skills exposables au tool-calling natif. Chaque entrée porte
#: son schéma, son exécuteur (signature uniforme, cf. les fonctions
#: ci-dessus), son plafond d'invocations propre — les budgets sont
#: INDÉPENDANTS entre skills (épuiser `web_search` n'empêche pas
#: `history_search` de continuer à répondre, et réciproquement), cf.
#: `_stream_ollama` — et `citable` (cf. `skill_citable`).
_SKILLS: dict[str, dict] = {
    "web_search": {
        "schema": _OUTIL_WEB_SEARCH,
        "executor": _executer_outil_web_search,
        "budget_max": _MAX_APPELS_OUTIL_WEB,
        "citable": True,
    },
    "history_search": {
        "schema": _OUTIL_HISTORY_SEARCH,
        "executor": _executer_outil_history_search,
        "budget_max": _MAX_APPELS_OUTIL_HISTORY,
        "citable": False,
    },
    "recherche_approfondie": {
        "schema": _OUTIL_RECHERCHE_APPROFONDIE,
        "executor": _executer_outil_web_search,
        "budget_max": _MAX_APPELS_RECHERCHE_APPROFONDIE,
        "citable": True,
    },
}


class LLMEngine:
    def __init__(self, config_path: str | Path | None = None):
        with open(config_path or _CONFIG_FILE) as f:
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
               max_tokens: Optional[int] = None, raisonnement: bool = True,
               outils: Optional[list[str]] = None,
               budgets_override: Optional[dict[str, int]] = None,
               skills_dynamiques: Optional[dict[str, dict]] = None,
               on_etape_recherche: Optional[Callable[[dict], None]] = None,
               rang_web_existant: int = 0) -> Generator:
        """Flux de génération. ``raisonnement=False`` coupe la réflexion du modèle.

        ``outils``/``on_etape_recherche``/``rang_web_existant`` : tool-calling
        natif du registre ``_SKILLS`` (``web_search``, ``history_search``,
        ``recherche_approfondie``), **Ollama et LM Studio seuls** — ignorés
        silencieusement pour gemini et les six autres fournisseurs
        compatibles OpenAI, dont les messages restent `{role, content}` (cf.
        `_stream_ollama`, `_stream_openai`). Les appelants qui ne
        passent rien n'ont rien à changer : ``outils=None`` (défaut) est le
        comportement historique, à l'octet — aucune sonde de capacité, aucun
        `tools=` exposé, une seule boucle. ``outils`` liste les NOMS de skills
        actifs pour CE tour (ex. ``["web_search", "history_search"]``) ; un nom
        absent du registre est ignoré silencieusement (cf. `_stream_ollama`)
        plutôt que de faire échouer le tour — un futur appelant peut lister un
        skill pas encore implémenté sans tout casser.

        ``budgets_override`` : ``{nom du skill: plafond d'invocations}``, pour
        les skills dont le plafond est un RÉGLAGE utilisateur plutôt qu'une
        constante du registre (``recherche_approfondie``,
        `core.memory._CONTEXT_DEFAULT["tool_calling"]`). Ne remplace le
        `budget_max` de `_SKILLS` QUE pour les noms présents dans ce dict — un
        skill absent de `budgets_override` garde son plafond par défaut. C'est
        un override d'APPEL, pas une propriété statique du registre : `_SKILLS`
        lui-même n'en sait rien.

        ``skills_dynamiques`` : sous-registre construit par l'appelant via
        `construire_skills_personnalises` (skills personnalisés AGENTIQUES,
        cf. `core.memory.normaliser_prefixes`) — même format que `_SKILLS`,
        fusionné PAR-DESSUS pour cet appel seulement, `_SKILLS` restant
        prioritaire en cas de collision de nom (cf. `_stream_ollama`).
        ``None`` (le défaut, tous les appelants sauf le chemin direct du chat)
        ne change rien : seuls les 3 skills natifs sont jamais exposés.

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
            # Les paramètres d'outils sont transmis à TOUS les fournisseurs
            # compatibles OpenAI, mais `_stream_openai` ne les lit que pour
            # `lmstudio` — cf. sa docstring.
            yield from self._stream_openai(
                messages, model_id, client, provider, max_tokens,
                raisonnement=raisonnement,
                outils=outils, budgets_override=budgets_override,
                skills_dynamiques=skills_dynamiques,
                on_etape_recherche=on_etape_recherche,
                rang_web_existant=rang_web_existant,
            )
        else:
            yield from self._stream_ollama(
                messages, m, max_tokens, raisonnement=raisonnement,
                outils=outils, budgets_override=budgets_override,
                skills_dynamiques=skills_dynamiques,
                on_etape_recherche=on_etape_recherche,
                rang_web_existant=rang_web_existant,
            )

    def generate(self, messages: list[dict], model: Optional[str] = None) -> str:
        """Réponse complète, non streamée. **Lève en cas d'échec** — comme
        :meth:`stream`, jamais un message d'échec rendu comme texte.

        Le chemin Ollama a toujours levé (l'exception du client remonte
        telle quelle) ; les chemins OpenAI-compatible et Gemini, eux,
        RETOURNAIENT ``"[flm:qwen3:4b] échec d'appel : …"``. Incident du
        2026-09-22 : FLM éteint, ce texte passait les heuristiques de
        validation de ``HistoryEngine._generate_title`` et de
        ``modules/image/router.py::calibrer_prompt`` (court, une ligne, sans
        backtick) et s'affichait comme titre de conversation / prompt
        calibré — leur repli silencieux n'était écrit que dans leur
        ``except``. Même piège, non observé mais présent, chez
        ``websearch.reformuler_requete`` (la requête de recherche devenait
        le message d'erreur) et ``kholle._generate_questions`` (le message
        découpé en « questions »).

        Corrigé à la source plutôt que par détection du motif ``[x:y]
        échec`` chez chaque appelant : tous les appelants avaient déjà un
        ``try/except`` (imposé par le chemin Ollama), et un motif textuel ne
        protège que ceux qui le connaissent. Le message de
        ``_provider_error_message`` n'est pas perdu : il voyage dans
        l'exception (logs, trame d'erreur SSE des modules qui la relaient).
        Verrouillé par ``test_generate_echec_leve.py``.
        """
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

    def describe_image(self, path: str, model: str,
                       question: Optional[str] = None,
                       stats: Optional[dict] = None) -> str:
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

        Seuls ces deux CHEMINS sont câblés, et c'est délibéré — le ``if
        provider in _OPENAI_COMPAT`` ci-dessous couvre aussi ``lmstudio``
        depuis son ajout, mais reste inatteignable en pratique :
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

        ── ``question`` : la même méthode, informée ────────────────────────────

        Sans ``question``, le prompt est ``_VISION_PROMPT`` et le comportement
        est **identique à celui d'avant ce paramètre** — c'est ce que vérifie
        `test_vision_images.py`, inchangé : le chemin de l'IMPORT (`RAGEngine.
        _texte_image`) n'en passe aucune et ne doit rien voir changer.

        Avec une question, `prompt_vision` la reprend dans un gabarit court.
        Ce n'est pas un raffinement cosmétique mais la raison d'être du
        paramètre : mesuré sur un énoncé mathématique dense, la légende
        générique ne permet pas de répondre à une vraie question sur l'image —
        elle décrit, elle ne lit pas ce qu'on lui demande de lire. Le chat
        (`core/vision_chat.py`) passe donc la question de l'utilisateur.

        ── ``stats`` : les tokens, pour la comptabilité des quotas ─────────────

        Dictionnaire REMPLI SUR PLACE (``prompt_tokens`` / ``output_tokens``)
        quand l'appelant en fournit un, et ignoré sinon — donc, là encore, le
        chemin de l'import ne voit rien changer. Un paramètre de sortie plutôt
        qu'un changement de type de retour, parce que ce dernier casserait les
        deux appelants existants pour un besoin qui n'est pas le leur.

        Pourquoi c'est nécessaire et pas confortable : `modele_vision_pour`
        peut rendre un modèle **cloud** en dernier recours, et un appel cloud
        dans un tour de chat qui n'entre pas dans `usage_tracker` est un appel
        payant NON COMPTÉ — le reste du tour, lui, est compté (sentinelle
        ``__stats__`` de :meth:`stream`, tracée par `modules/chat/router.py`).
        Un quota qui sous-compte est pire qu'un quota absent : il donne
        confiance dans un chiffre faux.

        Les deux providers ne nomment pas la même chose pareil — Ollama compte
        en ``prompt_eval_count``/``eval_count``, le SDK openai en
        ``usage.prompt_tokens``/``usage.completion_tokens``. Traduit ici, une
        fois, plutôt que chez chaque appelant.

        ⚠️ **Le timeout ne change pas de valeur, mais change de contexte.**
        Les 60 s ci-dessus étaient justifiées par « hors conversation active ».
        Appelée depuis un tour de chat, cette méthode reste SYNCHRONE : c'est à
        l'appelant de ne pas bloquer la boucle d'événements
        (``loop.run_in_executor``, ce que fait `modules/chat/router.py`). La
        valeur est conservée telle quelle — le pire cas mesuré reste 26 s
        (`flm:qwen3vl-it:4b`, premier appel après chargement) et un timeout plus
        court couperait une analyse parfaitement normale.
        """
        provider, model_id = self._parse_model(model)
        prompt = prompt_vision(question)
        if provider == "ollama":
            response = _vision_ollama_client.chat(
                model=model_id,
                messages=[{"role": "user", "content": prompt, "images": [str(path)]}],
            )
            content = response["message"]["content"] or ""
            if stats is not None:
                stats["prompt_tokens"] = int(response.get("prompt_eval_count") or 0)
                stats["output_tokens"] = int(response.get("eval_count") or 0)
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
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                    ],
                }],
            )
            message = response.choices[0].message
            content = message.content or ""
            if stats is not None:
                # `getattr` en cascade : `usage` peut être absent (un serveur
                # local compatible OpenAI n'est pas obligé de l'émettre), et un
                # `None` ne doit pas faire lever la comptabilité.
                usage = getattr(response, "usage", None)
                stats["prompt_tokens"] = int(getattr(usage, "prompt_tokens", 0) or 0)
                stats["output_tokens"] = int(getattr(usage, "completion_tokens", 0) or 0)
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
                       raisonnement: bool = True, outils: Optional[list[str]] = None,
                       budgets_override: Optional[dict[str, int]] = None,
                       skills_dynamiques: Optional[dict[str, dict]] = None,
                       on_etape_recherche: Optional[Callable[[dict], None]] = None,
                       rang_web_existant: int = 0) -> Generator:
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

        **Tool-calling natif — registre ``_SKILLS``, ajouté SANS changer ce qui
        précède.** ``outils=None`` (défaut de tous les appelants sauf le
        chemin direct du chat) : la boucle ``while`` ci-dessous ne fait qu'un
        seul tour, byte pour byte comme avant ce paramètre — aucune sonde de
        capacité, aucun ``tools=`` posé.

        **``registre`` — fusion locale, jamais une mutation de ``_SKILLS``** :
        ``registre = {**skills_dynamiques, **_SKILLS}`` avant tout le reste de
        cette méthode, qui lit ensuite EXCLUSIVEMENT ``registre`` (plus jamais
        ``_SKILLS`` directement). `_SKILLS` gagne toute collision de nom — un
        skill personnalisé assaini en ``"web_search"`` ne doit jamais pouvoir
        se substituer au vrai (déjà filtré en amont par
        `construire_skills_personnalises`, mais la priorité est reposée ici,
        au point de fusion, plutôt que fiée à un seul appelant). ``registre``
        ne fuit jamais hors de cette méthode : `_SKILLS` reste le seul
        registre STATIQUE, partagé entre tous les appels.

        ``outils=[...]`` filtre d'abord aux noms présents dans ``registre``
        (un nom inconnu est ignoré silencieusement, jamais une erreur), PUIS
        sonde la capacité ``tools`` du modèle (``_capacite_tools_disponible``
        — jamais supposée, même piège que ``think=True`` documenté
        ci-dessus) — dans cet ordre, pour ne JAMAIS interroger ``/api/tags``
        quand la liste filtrée est vide, comme avant ce paramètre.
        Chaque skill actif a son propre budget d'invocations
        (``budgets: dict[nom, int]``, initialisé à ``budget_max`` du
        registre) : épuiser celui de ``web_search`` n'empêche pas
        ``history_search`` de continuer à répondre ce même tour, et
        réciproquement. À chaque round, seuls les schémas des skills dont le
        budget est encore positif sont exposés dans ``tools=`` — budgets tous
        épuisés (ou liste filtrée vide) → pas de clé ``tools`` du tout, le
        modèle conclut avec ce qu'il a. Borne DURE en plus des budgets :
        ``_plafond_rounds_outils`` (un round par invocation budgétée + un de
        conclusion), parce qu'un outil INCONNU ne décrémente aucun budget —
        sans elle, un modèle qui l'appelle en boucle ne s'arrêtait jamais.
        Atteinte, elle émet l'étape de trace ``tool_call_plafond_atteint``.

        Un ``tool_calls`` reçu est dispatché par son ``nom`` : absent de
        ``budgets`` (skill non actif ce tour, ou inventé par le modèle) →
        message « outil inconnu » ; budget à 0 → message « budget épuisé » ;
        sinon décrémenté et exécuté via ``registre[nom]["executor"]``, avec la
        signature uniforme ``(arguments, on_etape, rang_de_depart)``. Le
        résultat est renvoyé en ``role="tool"``, corrélé par ``tool_name`` —
        ``ollama._types.Message.ToolCall`` n'a PAS de champ ``id`` (vérifié sur
        le schéma installé), à la différence du SDK openai ; ne pas
        réintroduire un ``tool_call_id``. Chaque budget plafonne des
        INVOCATIONS, pas des rounds ``chat()`` : un modèle qui demande deux
        appels du même skill dans le même round les épuise d'un coup.

        Un seul ``__stats__`` par tour de ``stream()``, agrégé sur tous les
        rounds plutôt qu'un par appel ``chat()`` : le contrat historique (une
        sentinelle par tour) reste inchangé pour les onze autres appelants, et
        ``usage_tracker.track`` (modules/chat/router.py) ne doit compter
        qu'une fois par réponse. Conséquence acceptée : ``num_predict``
        s'applique PAR round, donc un tour à plusieurs appels d'outil peut
        consommer plusieurs fois le budget habituel.

        Les messages ``role="assistant"``/``role="tool"`` construits pendant
        la boucle ne vivent que dans la copie LOCALE ``msgs`` — jamais dans
        ``messages``, l'objet de l'appelant : ils ne doivent jamais atteindre
        ``accumulated`` ni l'historique persisté (modules/chat/router.py),
        qui rejoue ``messages`` tel quel au tour suivant.

        ── ``contexte_tokens`` : le dernier round, pas la somme ───────────────

        Cette sentinelle porte DEUX mesures de tokens d'entrée, et les
        confondre est l'erreur à ne pas commettre :

        * ``prompt_tokens`` = la **somme** sur tous les rounds. C'est ce que le
          fournisseur a traité, donc ce qui se facture — `usage_tracker` s'en
          sert et doit continuer.
        * ``contexte_tokens`` = le prompt du **dernier** round. C'est la TAILLE
          DU CONTEXTE au moment de conclure, la seule qui décrive où en est la
          conversation dans la fenêtre du modèle.

        L'écart n'est pas théorique : chaque round renvoie le prompt entier
        AUGMENTÉ des résultats d'outil du round précédent, donc un tour à trois
        rounds a une somme proche du triple du contexte réel. Afficher la somme
        comme « contexte utilisé » montrerait une fenêtre saturée sur une
        conversation à moitié pleine.
        """
        # `_SKILLS` gagne toute collision — cf. docstring ci-dessus.
        registre: dict[str, dict] = {**(skills_dynamiques or {}), **_SKILLS}
        actifs = [n for n in (outils or []) if n in registre]
        # `and` court-circuite : `_capacite_tools_disponible` (une requête
        # HTTP potentielle, cf. `_capacites_ollama_fraiches`) n'est JAMAIS
        # appelée quand `actifs` est vide — comportement d'avant ce paramètre.
        capacite_ok = bool(actifs) and _capacite_tools_disponible(model)
        msgs = list(messages)
        overrides = budgets_override or {}
        budgets: dict[str, int] = (
            {n: overrides.get(n, registre[n]["budget_max"]) for n in actifs} if capacite_ok else {}
        )
        rang_suivant = rang_web_existant
        #: Borne DURE de la boucle, cf. `_plafond_rounds_outils` : les budgets
        #: seuls ne la garantissent pas face à un outil inconnu appelé en boucle.
        plafond_rounds = _plafond_rounds_outils(budgets)
        round_courant = 0
        #: Vrai après un round où le modèle a émis un `tool_calls` SANS
        #: qu'aucun outil lui ait été offert (budgets épuisés ou plafond) :
        #: il reçoit ses réponses « budget épuisé »/« outil inconnu » et UN
        #: round pour conclure — comportement d'avant le plafond, conservé —
        #: mais pas un de plus, sinon la boucle ne serait toujours pas bornée.
        conclusion_forcee = False

        total_prompt_tokens = 0
        total_output_tokens = 0
        total_eval_ns = 0
        total_prompt_ns = 0
        #: Prompt du DERNIER round seul, par opposition à `total_prompt_tokens`
        #: qui les ADDITIONNE. Les deux sont nécessaires et ne mesurent pas la
        #: même chose : la somme est ce que le fournisseur facture (elle part
        #: dans `usage_tracker`), le dernier round est la TAILLE DU CONTEXTE au
        #: moment de conclure — un round inclut les résultats d'outil des rounds
        #: précédents, donc la somme sur trois rounds vaut environ le triple du
        #: contexte réel. S'en servir pour un « contexte restant » afficherait
        #: une fenêtre saturée qui ne l'est pas.
        dernier_prompt_tokens = 0
        dernier_tronque = False
        stats_vues = False

        while True:
            appel: dict = {
                "model": model, "messages": msgs, "stream": True,
                "options": {
                    "temperature": self._gen["temperature"],
                    "top_p": self._gen["top_p"],
                    "num_predict": self._budget(max_tokens, raisonnement),
                    "num_thread": 8,
                },
            }
            if not raisonnement:
                appel["think"] = False
            tools_actifs = _schemas_du_round(
                registre, budgets, round_courant, plafond_rounds, on_etape_recherche,
            )
            round_courant += 1
            if tools_actifs:
                appel["tools"] = tools_actifs

            contenu_du_round: list[str] = []
            tool_calls_recus = None
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
                    contenu_du_round.append(content)
                    yield content
                # Mesuré (diagnostic préalable à cette tâche) : le `tool_calls`
                # arrive en UN SEUL chunk, déjà en dict/objet Python parsé —
                # rien à accumuler ni reparser en JSON.
                tc = message.get("tool_calls")
                if tc:
                    tool_calls_recus = tc
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
                        total_prompt_tokens += chunk["prompt_eval_count"] or 0
                        total_output_tokens += chunk["eval_count"] or 0
                        total_eval_ns += chunk["eval_duration"] or 0
                        total_prompt_ns += chunk["prompt_eval_duration"] or 0
                        # AFFECTATION, pas addition — cf. la déclaration plus haut.
                        dernier_prompt_tokens = chunk["prompt_eval_count"] or 0
                        dernier_tronque = chunk.get("done_reason") == "length"
                        stats_vues = True
                except Exception:
                    pass

            if not tool_calls_recus or conclusion_forcee:
                break
            if not tools_actifs:
                conclusion_forcee = True

            msgs.append({
                "role": "assistant", "content": "".join(contenu_du_round),
                "tool_calls": tool_calls_recus,
            })
            # Dispatch commun (`_executer_appels_outil`, partagé avec le chemin
            # LM Studio de `_stream_openai`). Ce qui reste propre à Ollama est
            # ici : les arguments arrivent DÉJÀ en dict, et le message
            # `role="tool"` se corrèle par `tool_name` — pas d'`id` côté
            # Ollama, cf. docstring.
            rang_suivant = yield from _executer_appels_outil(
                [(tc["function"]["name"], tc["function"]["arguments"] or {}, None)
                 for tc in tool_calls_recus],
                registre, budgets, msgs,
                lambda nom, _correlation, contenu: {
                    "role": "tool", "tool_name": nom, "content": contenu,
                },
                on_etape_recherche, rang_suivant,
            )

        if stats_vues:
            yield {
                "__stats__": True,
                "prompt_tokens": total_prompt_tokens,
                "output_tokens": total_output_tokens,
                "eval_duration_ns": total_eval_ns,
                "prompt_duration_ns": total_prompt_ns,
                "tronqué": dernier_tronque,
                # Champ ADDITIF, au même titre que `tronqué` : les onze autres
                # consommateurs de `stream()` filtrent par `isinstance(item, str)`
                # et ne voient rien passer. `usage_tracker` continue de compter
                # `prompt_tokens` (la somme), qui reste la bonne valeur pour une
                # facturation.
                "contexte_tokens": dernier_prompt_tokens,
            }

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
                # Mono-round par construction (pas de boucle d'outils ici) :
                # le contexte EST le prompt. Même nom que côté Ollama pour que
                # le consommateur n'ait pas à savoir d'où vient la trame.
                "contexte_tokens": meta.prompt_token_count or 0,
            }
        except Exception:
            pass

    def _generate_gemini(self, messages: list[dict], model: str) -> str:
        try:
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError("Package 'google-generativeai' non installé")
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("GEMINI_API_KEY non configurée — ajoutez-la dans Settings")
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
            raise RuntimeError(
                f"[gemini:{model.split('gemini:', 1)[-1]}] échec : {exc}"
            ) from exc

    # ── OpenAI-compatible providers ──────────────────────────────────────────

    def _stream_openai(self, messages: list[dict], model_id: str, client, provider: str = "",
                       max_tokens: Optional[int] = None, raisonnement: bool = True,
                       outils: Optional[list[str]] = None,
                       budgets_override: Optional[dict[str, int]] = None,
                       skills_dynamiques: Optional[dict[str, dict]] = None,
                       on_etape_recherche: Optional[Callable[[dict], None]] = None,
                       rang_web_existant: int = 0) -> Generator:
        """Flux OpenAI-compatible. ``raisonnement`` n'agit que sur ``flm`` ; les
        outils (``outils`` & co., mêmes paramètres que `_stream_ollama`) que sur
        ``lmstudio`` — cf. la section « Tool-calling » en fin de docstring.

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

        **``lmstudio`` rejoint ce groupe, délibérément, pour une raison
        différente : il n'existe PAS UN paramètre API stable à poser.** LM
        Studio expose deux mécanismes concurrents selon le modèle/la version —
        ``reasoning_effort`` (un raccourci ajouté après coup) et
        ``chat_template_kwargs: {"enable_thinking": ...}`` (dépendant du gabarit
        de discussion du modèle chargé) — et son propre suivi de bugs rapporte
        ce flag **ignoré** sur des familles récentes (Qwen3.5 : le raisonnement
        continue de consommer tout ``max_tokens`` malgré ``enableThinking:
        false``). Rien de tout ça n'a été rejoué sur ce poste — aucune instance
        LM Studio disponible au moment d'écrire —, donc ni mesuré ni supposé
        fiable : câbler un flag qui peut être silencieusement ignoré serait pire
        que ne rien câbler, ça donnerait l'illusion d'un contrôle qui n'agit pas
        toujours. Revoir cette garde le jour où c'est mesuré sur un vrai serveur
        LM Studio ; jusque-là, ``lmstudio`` suit le chemin ``raisonnement``
        inerte du chat classique — le toggle du frontend le range déjà dans sa
        branche « budget de réflexion seul » (aucun préfixe local reconnu),
        sans qu'il ait fallu y toucher.

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

        **Tool-calling natif — ``lmstudio`` SEUL (2026-09-22).** Même registre
        ``_SKILLS`` (plus les skills dynamiques), mêmes budgets, même dispatch
        (`_executer_appels_outil`, partagé avec `_stream_ollama`), même
        sentinelle ``__tool_call__``, un seul ``__stats__`` agrégé. Ce qui
        diffère d'Ollama, et qui a été MESURÉ sur LM Studio
        (``mistralai/ministral-3-3b``) avant d'écrire :

        * les ``tool_calls`` arrivent en **deltas fragmentés** — l'``id`` et le
          ``name`` dans le premier fragment seulement, les ``arguments`` token
          par token (``""`` → ``"{"`` → ``"\\""`` → ``"ville"``…), la fin
          signalée par ``finish_reason: "tool_calls"``. Accumulation PAR
          ``index`` (l'``id`` manque aux fragments suivants), JSON parsé
          seulement à la fin ;
        * arguments en chaîne JSON, pas en dict : un JSON final invalide (petit
          modèle) ABANDONNE la tentative — étape de trace
          ``tool_call_abandonne`` (badge « recherche abandonnée » dans le
          chat), puis réponse sans outil, jamais un crash. Si une phrase était
          déjà partie, le round suivant la CONTINUE au lieu de la répéter
          (`_continuer_sans_outil`, mesuré) ;
        * le message ``role="tool"`` se corrèle par ``tool_call_id``, que
          l'API exige (Ollama, lui, n'a pas d'``id``).

        Capacité jamais supposée : `_capacite_tools_lmstudio`
        (``trained_for_tool_use`` de ``/api/v1/models``, cf.
        `core.models.capacites_outils_lmstudio` — le mode d'outils « par
        défaut » de LM Studio y est filtré).

        **Les six autres fournisseurs ne voient RIEN de ce chantier** : pas de
        sonde, pas de ``tools``, un seul round, le corps d'avant à l'octet
        (verrouillé par ``test_tool_calling_lmstudio.py``). Les étendre
        demandera de mesurer chacun — format des deltas, acceptation de
        ``tools`` et de ``role="tool"`` — sur des API FACTURÉES : ce n'est pas
        un simple retrait de la garde sur ``provider``.
        """
        # Normalisation HISTORIQUE, inchangée : l'historique d'une conversation
        # reste `{role, content}` pour les sept fournisseurs. Seuls les
        # messages ajoutés PENDANT la boucle d'outils ci-dessous (assistant
        # avec `tool_calls`, `role="tool"` avec `tool_call_id`) portent
        # d'autres clés — et ils ne vivent que dans cette copie locale.
        oai = [{"role": m["role"], "content": m["content"]} for m in messages]
        stream_start = time.time()
        total_prompt_tokens = 0
        total_output_tokens = 0
        #: Prompt du DERNIER round — même distinction que `_stream_ollama`
        #: (`contexte_tokens` ≠ somme facturée), cf. sa docstring.
        dernier_prompt_tokens = 0
        tronque = False
        mt = self._budget(max_tokens, raisonnement)

        # Tool-calling : `lmstudio` SEUL. Pour les six autres fournisseurs,
        # `actifs` est vide, donc `budgets` aussi, donc aucun `tools=` posé,
        # aucune sonde, un seul round — le corps d'avant à l'octet (verrouillé
        # par `test_tool_calling_lmstudio.NonRegressionSixFournisseursTest`).
        # `and` court-circuite la sonde HTTP quand rien n'est actif, comme
        # côté Ollama.
        registre: dict[str, dict] = {**(skills_dynamiques or {}), **_SKILLS}
        actifs = [n for n in (outils or []) if n in registre] if provider == "lmstudio" else []
        capacite_ok = bool(actifs) and _capacite_tools_lmstudio(model_id)
        overrides = budgets_override or {}
        budgets: dict[str, int] = (
            {n: overrides.get(n, registre[n]["budget_max"]) for n in actifs} if capacite_ok else {}
        )
        rang_suivant = rang_web_existant
        #: Même borne dure que `_stream_ollama` (`_plafond_rounds_outils`) : un
        #: outil INCONNU appelé en boucle ne décrémente aucun budget. Pas de
        #: « relance de conclusion » ici, contrairement à Ollama : des fragments
        #: d'outil reçus sans outil offert sont ignorés (cf. plus bas), donc le
        #: round sans outil termine toujours le tour.
        plafond_rounds = _plafond_rounds_outils(budgets)
        round_courant = 0

        def _create(with_usage: bool, tools: list[dict]):
            kwargs = dict(
                model=model_id, messages=oai, stream=True,
                temperature=self._gen["temperature"], max_tokens=mt,
            )
            if provider == "flm":
                kwargs["extra_body"] = {"think": bool(raisonnement)}
            if with_usage:
                kwargs["stream_options"] = {"include_usage": True}
            if tools:
                kwargs["tools"] = tools
            return client.chat.completions.create(**kwargs)

        def _abandonner(raison: str, noms: list[str]) -> None:
            """Tentative d'appel d'outil ABANDONNÉE — pas de crash, une étape
            de trace qui le dit, et le tour continue sans outil. Nom d'étape
            distinct des `tool_call_<nom>` (émis, eux, par les exécuteurs) :
            la trace dit qu'un appel a été TENTÉ puis écarté, pas exécuté."""
            logger.info("Tool-calling %s (%s) abandonné : %s %s", provider, model_id, raison, noms)
            if on_etape_recherche:
                on_etape_recherche({"etape": "tool_call_abandonne", "raison": raison, "outils": noms})
            budgets.clear()

        while True:
            tools_actifs = _schemas_du_round(
                registre, budgets, round_courant, plafond_rounds, on_etape_recherche,
            )
            round_courant += 1
            try:
                stream = _create(with_usage=True, tools=tools_actifs)
            except Exception:
                # Le provider peut ne pas gérer stream_options : on retente sans.
                # Si ça échoue encore, c'est une vraie erreur API → message explicite.
                try:
                    stream = _create(with_usage=False, tools=tools_actifs)
                except Exception as exc:
                    logger.warning("Stream %s (%s) refusé : %s", provider, model_id, exc)
                    raise RuntimeError(_provider_error_message(provider, model_id, exc)) from exc

            contenu_du_round: list[str] = []
            #: Fragments de `tool_calls` accumulés PAR INDEX, jamais par `id` :
            #: mesuré sur LM Studio, l'`id` (et le `name`) n'arrivent que dans
            #: le PREMIER fragment d'un appel, les suivants ne portent que
            #: `index` + un morceau d'`arguments`. Rien n'est parsé avant la
            #: fin du flux.
            fragments: dict[int, dict] = {}
            finish_reason = None
            tronque = False
            round_prompt = 0
            round_output = 0
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
                        # Le texte part au fil de l'eau MÊME s'il précède ou
                        # accompagne un `tool_call` dans le même chunk — comme
                        # côté Ollama, où il est déjà affiché avant l'outil.
                        if delta.content:
                            contenu_du_round.append(delta.content)
                            yield delta.content
                        # Lu SEULEMENT si des outils ont été envoyés ce round :
                        # pour les six autres fournisseurs (et LM Studio sans
                        # capacité), rien de ce qui suit n'existe.
                        if tools_actifs:
                            for frag in getattr(delta, "tool_calls", None) or []:
                                entree = fragments.setdefault(
                                    frag.index, {"id": "", "name": "", "arguments": ""})
                                if frag.id and not entree["id"]:
                                    entree["id"] = frag.id
                                fn = frag.function
                                if fn is not None:
                                    # Concaténés tous les deux (forme de
                                    # référence de l'API) : un serveur peut
                                    # morceler le `name` aussi, pas seulement
                                    # les `arguments`.
                                    entree["name"] += fn.name or ""
                                    entree["arguments"] += fn.arguments or ""
                    # `finish_reason == "length"` : le plafond `max_tokens` a été
                    # atteint, donc la génération est COUPÉE. Pendant OpenAI du
                    # `done_reason` d'Ollama, et même conséquence — sur FLM, dont le
                    # raisonnement puise dans le même budget, la réponse peut ne
                    # jamais être produite.
                    #
                    # `getattr` en cascade : `choices` peut être vide sur le chunk
                    # d'usage final, et `finish_reason` absent selon le fournisseur.
                    if chunk.choices:
                        fr = getattr(chunk.choices[0], "finish_reason", None)
                        if fr:
                            finish_reason = fr
                        if fr == "length":
                            tronque = True
                    if getattr(chunk, "usage", None):
                        # AFFECTATION dans le round, comme avant ce chantier :
                        # un fournisseur qui enverrait l'usage sur plusieurs
                        # chunks (valeurs cumulées) serait compté plusieurs
                        # fois par une addition ici. Le DERNIER fait foi.
                        round_prompt = getattr(chunk.usage, "prompt_tokens", 0) or 0
                        round_output = getattr(chunk.usage, "completion_tokens", 0) or 0
            except Exception as exc:
                # Erreur survenue en cours de streaming (coupure, refus serveur…).
                logger.warning("Stream %s (%s) interrompu : %s", provider, model_id, exc)
                raise RuntimeError(_provider_error_message(provider, model_id, exc)) from exc

            # Entre rounds, en revanche, on ADDITIONNE pour la facturation et on
            # AFFECTE pour le contexte (cf. `dernier_prompt_tokens` plus haut).
            total_prompt_tokens += round_prompt
            total_output_tokens += round_output
            dernier_prompt_tokens = round_prompt

            if not fragments:
                break

            appels = [fragments[i] for i in sorted(fragments)]
            noms = [a["name"] for a in appels]
            if finish_reason != "tool_calls":
                # Flux terminé (`length`, `stop`…) avec des fragments encore en
                # attente : l'appel n'a jamais été complété, ses arguments sont
                # au mieux tronqués. Rien à exécuter.
                _abandonner("flux_interrompu", noms)
            else:
                decodes: list[tuple[str, dict, object]] = []
                for position, a in enumerate(appels):
                    try:
                        arguments = json.loads(a["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError):
                        arguments = None
                    if not isinstance(arguments, dict) or not a["name"]:
                        decodes = []
                        break
                    # `id` réel fourni par LM Studio (mesuré) ; un id
                    # synthétique seulement pour un serveur qui l'omettrait —
                    # l'API exige que chaque `role="tool"` le référence.
                    a["id"] = a["id"] or f"call_{position}"
                    decodes.append((a["name"], arguments, a["id"]))
                if not decodes:
                    # JSON final invalide (petit modèle qui produit des
                    # arguments cassés) : TOUT le round est écarté — l'API
                    # exige une réponse `role="tool"` par `tool_call_id` du
                    # message assistant, donc exécuter les appels valides et
                    # taire les autres produirait un historique refusé.
                    _abandonner("arguments_invalides", noms)
                else:
                    oai.append({
                        "role": "assistant", "content": "".join(contenu_du_round),
                        "tool_calls": [
                            {"id": a["id"], "type": "function",
                             "function": {"name": a["name"], "arguments": a["arguments"]}}
                            for a in appels
                        ],
                    })
                    rang_suivant = yield from _executer_appels_outil(
                        decodes, registre, budgets, oai,
                        lambda _nom, correlation, contenu: {
                            "role": "tool", "tool_call_id": correlation, "content": contenu,
                        },
                        on_etape_recherche, rang_suivant,
                    )
                    continue

            # Tentative abandonnée : un round de plus SANS outil (`budgets` vidé
            # par `_abandonner`), pour ne jamais laisser une bulle vide ni une
            # réponse coupée sur une phrase d'annonce.
            if contenu_du_round:
                if finish_reason == "length":
                    # Plafond `max_tokens` atteint : la réponse est COUPÉE et
                    # signalée comme telle (`tronqué`) — relancer consommerait
                    # un second budget entier pour la même question.
                    break
                # Du texte est déjà parti ce round (« Je vais chercher… »). Le
                # relancer tel quel le DUPLIQUERAIT dans la bulle ; on CONTINUE
                # donc ce message assistant — mesuré sur LM Studio, cf.
                # `_continuer_sans_outil`.
                _continuer_sans_outil(oai, "".join(contenu_du_round))

        yield {
            "__stats__": True,
            "prompt_tokens": total_prompt_tokens,
            "output_tokens": total_output_tokens,
            "eval_duration_ns": int((time.time() - stream_start) * 1e9),
            "prompt_duration_ns": 0,
            # Du DERNIER round : c'est lui qui a produit (ou coupé) la réponse.
            "tronqué": tronque,
            # Mono-round pour les six autres fournisseurs : le contexte EST le
            # prompt du tour, comme avant. Multi-round (LM Studio avec
            # outils) : le dernier round, pas la somme.
            "contexte_tokens": dernier_prompt_tokens,
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
            raise RuntimeError(_provider_error_message(provider, model_id, exc)) from exc
