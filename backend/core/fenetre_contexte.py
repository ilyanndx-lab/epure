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

**FLM et LM Studio** ne sont pas mesurés non plus, leurs serveurs étant éteints
sur ce poste au moment de la mesure (LM Studio porte pourtant un
``max_context_length`` sur ``/api/v0/models``, cf. `core/materiel.py:791`) : à
mesurer avant de les brancher, pas à supposer.

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

import logging
import os
import urllib.error
from typing import Optional

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

    if prefixe == "ollama":
        try:
            valeur = _fenetre_ollama(nom)
        except Exception:
            logger.exception("Fenêtre de contexte illisible pour %s (Ollama)", modele_id)
            return None, None
        return (valeur, "ollama /api/ps") if valeur else (None, None)

    entree = _SONDES.get(prefixe)
    if entree is None:
        # Fournisseur mesuré comme ne partageant rien, ou fournisseur non mesuré
        # (Groq, FLM, LM Studio) : les deux cas rendent la même chose, et c'est
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
