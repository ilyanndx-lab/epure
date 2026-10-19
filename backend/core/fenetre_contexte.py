"""Fenêtre de contexte d'un modèle : ce que le fournisseur en dit, ou rien.

Ce module existe pour une seule question — « combien de tokens ce modèle
accepte-t-il ? » — et sa règle tient en une phrase : **il rend ``None`` plutôt
qu'un défaut**. C'est la contrainte qui a dicté tout le reste.

Pourquoi insister : la valeur rendue ici sert de DÉNOMINATEUR à un indicateur
affiché à l'utilisateur (« contexte restant dans la conversation »). Un
dénominateur inventé ne produit pas une imprécision, il produit un **mensonge
crédible** — un fil à moitié plein affiché comme presque vide, ou l'inverse, au
moment précis où l'utilisateur décide s'il peut encore poser sa question. Le
repli évident — `n_ctx: 4096` de `config.yaml` — a été écarté pour cette raison
et pour une seconde, plus décisive : **aucun code ne lit ce réglage** (vérifié
par grep, il n'apparaît que dans des commentaires) et il n'est pas passé à
Ollama. Afficher 4096 reviendrait à afficher un chiffre que rien n'applique.

── TABLE MESURÉE le 2026-09-20 (ce poste) ──────────────────────────────────

Chaque entrée est une mesure, pas une lecture de documentation. Ce qui n'a pas
pu être mesuré n'est pas dans la table, et la conséquence est assumée : pas de
camembert pour ce fournisseur-là.

    ollama    GET /api/ps        context_length          mesuré (cf. plus bas)
    lmstudio  GET /api/v1/models loaded_instances[].config.context_length
                                                         mesuré le 2026-10-18 (cf. plus bas)
    gemini    get_model()        input_token_limit       1 048 576 (2.5-flash)
    mistral   GET /v1/models     max_context_length      262 144 (small-latest)
    cerebras  GET /v1/models     —   clés de l'item : created, id, object, owned_by
    nvidia    GET /v1/models     —   clés de l'item : created, id, object, owned_by
    deepseek  GET /v1/models     —   clés de l'item : id, object, owned_by

Les valeurs entre parenthèses sont celles d'UN modèle, données à titre
d'illustration — **elles ne sont pas générales** : la même liste Mistral sert 46
modèles dont les fenêtres diffèrent (le premier de la liste annonçait 256 000,
`mistral-small-latest` en annonce 262 144). Ce qui est mesuré ici, c'est le NOM
DU CHAMP, pas un nombre : le nombre se lit par modèle, à l'exécution.

**Groq n'est pas mesuré** : sa clé de `backend/.env` répond **401
``expired_api_key``** (constaté le 2026-09-20). Ne rien y mettre plutôt que d'y
deviner un nom de champ — la documentation de Groq annonce un ``context_window``,
mais une doc n'est pas une mesure, et c'est exactement la distinction que ce
dépôt applique partout ailleurs. À compléter le jour où la clé sera renouvelée.

**FLM** n'est pas mesuré non plus, son serveur étant éteint sur ce poste au
moment de la mesure : à mesurer avant de le brancher, pas à supposer.

── LM Studio : le contexte ALLOUÉ, pas ``max_context_length`` ──────────────

Mesuré le 2026-10-18 (LM Studio, port 1234), le 9B chargé par
``lms load … --context-length 8192`` : la même réponse de ``GET
/api/v1/models`` porte ``max_context_length: 262144`` (ce que le modèle SAIT
tenir) et ``loaded_instances[0].config.context_length: 8192`` (ce que LM Studio
a ALLOUÉ). ``/api/v0/models`` dit la même chose en ``loaded_context_length:
8192``. C'est le pendant exact du couple ``/api/ps`` / ``/api/show`` d'Ollama,
et la même règle s'applique : le maximum est la source écartée. Un modèle sans
instance chargée n'a pas de fenêtre connue → ``None``.

``loaded_instances`` se remplit dès le DÉBUT d'un chargement
(`core.models.etat_modele_lmstudio`) ; ça ne gêne pas ici, la configuration de
l'instance (dont ``context_length``) est fixée avant que les poids arrivent.

── Ollama : ``/api/ps``, et pourquoi pas ``/api/show`` ─────────────────────

``/api/ps`` porte ``context_length`` par modèle CHARGÉ (mesuré, fixture réelle
dans `test_ollama_memoire.py:85`). C'est la fenêtre **RUNTIME** : celle qu'Ollama
a réellement allouée, ``num_ctx`` et ``OLLAMA_CONTEXT_LENGTH`` déjà appliqués.

``POST /api/show`` rendrait ``<arch>.context_length``, la fenêtre **MAXIMALE**
que le modèle sait tenir. Les deux diffèrent — Ollama choisit au chargement une
fenêtre plus petite sur une machine contrainte — et se tromper de l'une à
l'autre afficherait un contexte plus vide qu'il ne l'est. C'est la source
écartée, pas la source oubliée.

Conséquence assumée de ce choix : **un modèle Ollama pas encore chargé n'a pas de
fenêtre connue** (``/api/ps`` ne liste que les résidents). On rend ``None``, et
l'indicateur réapparaît au tour suivant — quand le modèle est chargé et que la
question se pose vraiment.

── Le cloud n'est pas appelé à l'aveugle ───────────────────────────────────

Seul le modèle que l'utilisateur a **explicitement choisi** dans le chat est
sondé, jamais un balayage de la surface curatée. Un `GET /v1/models` est gratuit
et non génératif, mais multiplier les appels sortants vers un fournisseur pour
remplir un cache que personne ne lira serait exactement le travers que
`core/instance.py` interdit par ailleurs (« le cloud ne part jamais sans qu'on
l'ait demandé »). D'où le mémo ci-dessous : une sonde par modèle et par session.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from core.llm import lmstudio_host as _lmstudio_host
from core.models import _http_json
from core.ollama_memoire import lister_charges

logger = logging.getLogger(__name__)

#: Préfixes d'identifiant de modèle connus du dépôt (`core/models.py:_make_entry`,
#: `main.py:list_models`). Sert uniquement à DÉCOUPER l'identifiant composite
#: ``<provider>:<modele>`` — pas à décider d'une disponibilité.
#:
#: Nécessaire parce qu'un nom de modèle Ollama contient lui-même un deux-points
#: (`qwen2.5:7b`) : couper sur le premier `:` sans vérifier le préfixe rendrait
#: `qwen2.5` comme nom de fournisseur. Un identifiant sans préfixe connu est un
#: modèle Ollama, ce que `main.py:324` fait déjà pour la même raison.
_PREFIXES_CONNUS = frozenset({
    "ollama", "lmstudio", "flm",
    "gemini", "groq", "cerebras", "mistral", "nvidia", "deepseek",
})

#: Mémo des fenêtres cloud, par ``(provider, modele)``. **Un succès seulement y
#: entre** : mémoriser un échec réseau figerait pour toute la session une absence
#: qui n'était que passagère. Un modèle cloud ne change pas de fenêtre en cours
#: de session, donc un succès est bon jusqu'au redémarrage.
_MEMO: dict[tuple[str, str], int] = {}


def _decouper(modele_id: str) -> tuple[str, str]:
    """``"mistral:mistral-small-latest"`` → ``("mistral", "mistral-small-latest")``.

    Un identifiant sans préfixe connu est rendu tel quel comme modèle Ollama —
    ``("qwen2.5:7b")`` → ``("ollama", "qwen2.5:7b")``.
    """
    if ":" in modele_id:
        prefixe, reste = modele_id.split(":", 1)
        if prefixe in _PREFIXES_CONNUS:
            return prefixe, reste
    return "ollama", modele_id


def _fenetre_ollama(modele_id: str) -> Optional[int]:
    """Fenêtre runtime du modèle s'il est CHARGÉ. ``None`` sinon.

    Aucun cache ici, et c'est délibéré : ``context_length`` décrit l'état du
    serveur Ollama à cet instant. Éjecter puis recharger un modèle avec un autre
    ``num_ctx`` change la valeur — une mémo de session afficherait l'ancienne.
    L'appel est local et borné (`_TIMEOUT_LECTURE_S`), donc le relire ne coûte
    rien de mesurable.
    """
    for entree in (lister_charges() or []):
        if entree.get("id") == modele_id:
            valeur = entree.get("fenetre_contexte")
            return valeur if isinstance(valeur, int) and valeur > 0 else None
    return None


#: Délai de la sonde LM Studio : serveur LOCAL, même borne que
#: `core.models._TIMEOUT_SONDE_LMSTUDIO_S`.
_TIMEOUT_LMSTUDIO_S = 2


def _modeles_lmstudio() -> Optional[dict]:
    """``GET /api/v1/models`` brut, ou ``None`` si LM Studio ne répond pas."""
    try:
        req = urllib.request.Request(f"{_lmstudio_host}/api/v1/models")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_LMSTUDIO_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _fenetre_lmstudio(modele_id: str) -> Optional[int]:
    """Contexte ALLOUÉ à l'instance chargée de ce modèle. ``None`` sinon.

    Pas de mémo, même raison qu'Ollama (`_fenetre_ollama`) : recharger le
    modèle avec un autre contexte change la valeur. Le modèle est reconnu
    par sa clé, une de ses variantes (``qwen/qwen3.8-27b@q4_k_m``) ou l'id
    d'une de ses instances.

    Toutes les entrées sont parcourues, pas seulement la première qui porte
    ce nom : la même clé peut apparaître deux fois (catalogue de ce poste et
    celui d'un appareil relié, mesuré le 2026-10-19), et l'entrée sans
    instance peut venir d'abord.
    """
    for item in ((_modeles_lmstudio() or {}).get("models") or []):
        instances = item.get("loaded_instances") or []
        noms = {item.get("key"), *(item.get("variants") or []), *(i.get("id") for i in instances)}
        if modele_id not in noms:
            continue
        for instance in instances:
            valeur = (instance.get("config") or {}).get("context_length")
            if isinstance(valeur, int) and valeur > 0:
                return valeur
    return None


def _fenetre_mistral(modele_id: str) -> Optional[int]:
    """``GET /v1/models`` → ``max_context_length`` du modèle demandé."""
    token = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not token:
        return None
    reponse = _http_json("https://api.mistral.ai/v1/models", token)
    for item in (reponse.get("data") or []):
        if item.get("id") != modele_id:
            continue
        valeur = item.get("max_context_length")
        return valeur if isinstance(valeur, int) and valeur > 0 else None
    return None


def _fenetre_gemini(modele_id: str) -> Optional[int]:
    """``genai.get_model()`` → ``input_token_limit``.

    L'import du SDK est DANS la fonction, jamais au niveau module : c'est la
    règle posée pour `core/hmer.py` (un import lourd en tête de module fait
    échouer la collecte de tout test qui importe `main`). ``google-generativeai``
    est le SDK déjà utilisé par `core/llm.py` — on ne lui en ajoute pas un second.
    """
    token = os.environ.get("GEMINI_API_KEY", "").strip()
    if not token:
        return None
    import google.generativeai as genai  # noqa: PLC0415 — cf. docstring

    genai.configure(api_key=token)
    # `genai.get_model` attend le préfixe `models/` ; les identifiants de la
    # surface curatée n'en portent pas (`gemini-2.5-flash`).
    modele = genai.get_model(f"models/{modele_id}")
    valeur = getattr(modele, "input_token_limit", None)
    return valeur if isinstance(valeur, int) and valeur > 0 else None


#: Sonde et LIBELLÉ DE SOURCE, par fournisseur. Le libellé n'est pas déduit du
#: nom du fournisseur : Gemini ne passe pas par `/v1/models` mais par le SDK
#: (`genai.get_model`), et annoncer la mauvaise origine dans l'infobulle serait
#: aussi faux qu'annoncer la mauvaise valeur.
#:
#: **L'absence d'une clé ici est une décision, pas un oubli** : Cerebras, NVIDIA
#: et DeepSeek ont été mesurés et n'exposent aucun champ de fenêtre (cf. table de
#: l'en-tête). Leur rendre ``None`` sans même les appeler évite une requête
#: sortante qui ne peut rien rapporter.
_SONDES = {
    "mistral": (_fenetre_mistral, "mistral /v1/models"),
    "gemini": (_fenetre_gemini, "gemini get_model"),
}


def fenetre_de(modele_id: str, provider: Optional[str] = None) -> tuple[Optional[int], Optional[str]]:
    """``(fenetre, source)`` pour ce modèle. ``(None, None)`` si inconnue.

    ``source`` dit **d'où vient le chiffre** (« mistral /v1/models »). Elle n'est
    pas décorative : elle alimente l'infobulle de l'indicateur, qui doit pouvoir
    nommer sa source plutôt que de présenter un nombre sans origine.

    ``provider`` est facultatif — l'identifiant composite le porte déjà. Il est
    accepté pour que l'appelant qui l'a sous la main n'ait pas à le recomposer.

    **N'échoue jamais** : une sonde qui lève (réseau, SDK absent, clé refusée)
    est journalisée et rend ``(None, None)``. Un indicateur qui disparaît est un
    désagrément ; un indicateur qui empêche le chat de répondre serait un bug.
    """
    prefixe_decoupe, nom = _decouper(modele_id)
    prefixe = provider or prefixe_decoupe

    locales = {
        "ollama": (_fenetre_ollama, "ollama /api/ps"),
        "lmstudio": (_fenetre_lmstudio, "lmstudio /api/v1/models"),
    }
    if prefixe in locales:
        sonde, libelle = locales[prefixe]
        try:
            valeur = sonde(nom)
        except Exception:
            logger.exception("Fenêtre de contexte illisible pour %s (%s)", modele_id, prefixe)
            return None, None
        return (valeur, libelle) if valeur else (None, None)

    entree = _SONDES.get(prefixe)
    if entree is None:
        # Fournisseur mesuré comme ne partageant rien, ou fournisseur non mesuré
        # (Groq, FLM) : les deux cas rendent la même chose, et c'est
        # voulu — dans les deux, on ne SAIT pas, donc on n'affiche pas.
        return None, None

    sonde, libelle = entree
    cle = (prefixe, nom)
    memoise = _MEMO.get(cle)
    if memoise is not None:
        return memoise, libelle

    try:
        valeur = sonde(nom)
    except Exception:
        logger.exception("Fenêtre de contexte illisible pour %s (%s)", modele_id, prefixe)
        return None, None

    if valeur:
        _MEMO[cle] = valeur
        return valeur, libelle
    return None, None


# ── Contexte chargé gardé en cache pour le budget de lecture d'URL ──────────
#
# `fenetre_de` resonde à chaque appel, et c'est voulu pour l'indicateur de
# contexte (il décrit un ÉTAT). Le budget de lecture d'URL (`core.webcontent.
# budget_lecture`) la lisait pourtant à CHAQUE tour d'URL, au prix d'un
# aller-retour vers LM Studio — ~2 s mesurées par `localhost` (#80). Ici, la
# valeur est gardée pour le modèle actif et relue seulement :
#   * quand le modèle actif change (autre clé) ;
#   * après une requête de génération en échec (`oublier_contexte_charge`,
#     appelé par le chat) — un débordement de contexte en est le cas typique ;
#   * tant qu'elle est inconnue : un modèle pas encore chargé rend `None`, et
#     le garder figerait le plancher alors qu'il sera chargé au tour suivant.
# Limite assumée : recharger le MÊME modèle avec un autre contexte sans erreur
# entre-temps garde l'ancienne valeur jusqu'au changement de modèle.

_CONTEXTE_CHARGE: dict[str, tuple[int, str]] = {}


def contexte_charge(modele_id: str) -> tuple[Optional[int], Optional[str]]:
    """``(contexte, source)`` du modèle actif, depuis le cache si possible."""
    if modele_id in _CONTEXTE_CHARGE:
        return _CONTEXTE_CHARGE[modele_id]
    valeur, source = fenetre_de(modele_id)
    if valeur:
        # Une seule entrée : un autre modèle actif invalide la précédente.
        _CONTEXTE_CHARGE.clear()
        _CONTEXTE_CHARGE[modele_id] = (valeur, source)
    return valeur, source


def oublier_contexte_charge() -> None:
    """Force la relecture au prochain appel (requête en échec, tests)."""
    _CONTEXTE_CHARGE.clear()
