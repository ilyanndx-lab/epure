"""Routeur du module Chat. Monté avec prefix "" : WebSocket /ws/chat, skill
SSE /skills/résumé, et la recherche web @web (perform_web_search).

Moteurs partagés (llm, memory, rag, orchestrator, history_engine,
consolidation_engine, usage_tracker, provider_of) injectés via core.runtime.
"""

import asyncio
import html as _htmllib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from threading import Thread
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import ws_require_token
from core.embedding_install import EmbeddingIndisponible
from core.instance import modele_local_defaut
from core.paths import PathOutsideDataError, cle_chemin, resolve_user_path
from core.rag import RAGEngine
from core.runtime import (
    SSE_HEADERS,
    consolidation_engine,
    history_engine,
    llm,
    memory,
    orchestrator,
    provider_of as _provider_of,
    rag,
    usage_tracker,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Recherche web (@web) ─────────────────────────────────────────────────────

_WEB_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_WEB_SEARCH_TIMEOUT = 8.0
# User-Agent alternatifs essayés en cas de blocage (403 Cloudflare, etc.)
_WEB_SEARCH_USER_AGENTS = [
    _WEB_SEARCH_UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

# Cache mémoire LRU avec TTL court : évite de re-frapper DuckDuckGo pour une
# même requête (utile quand l'utilisateur reformule peu ou relance @web).
_WEB_SEARCH_CACHE_TTL = 300.0  # secondes
_WEB_SEARCH_CACHE_MAX = 64
_web_search_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()


def _web_search_cache_get(key: str) -> Optional[str]:
    """Retourne la valeur en cache si présente et non expirée, sinon None."""
    entry = _web_search_cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if (time.time() - ts) > _WEB_SEARCH_CACHE_TTL:
        _web_search_cache.pop(key, None)
        return None
    _web_search_cache.move_to_end(key)  # marque comme récemment utilisé
    return value


def _web_search_cache_set(key: str, value: str) -> None:
    """Insère/rafraîchit une entrée et évince les plus anciennes (LRU)."""
    _web_search_cache[key] = (time.time(), value)
    _web_search_cache.move_to_end(key)
    while len(_web_search_cache) > _WEB_SEARCH_CACHE_MAX:
        _web_search_cache.popitem(last=False)


def _web_search_fetch(url: str, accept: str) -> tuple[Optional[str], Optional[str]]:
    """Récupère une URL en essayant plusieurs User-Agent.

    Retourne ``(texte, None)`` en cas de succès, ``(None, erreur)`` sinon.
    """
    last_exc: Optional[str] = None
    for ua in _WEB_SEARCH_USER_AGENTS:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=_WEB_SEARCH_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning("Web search HTTP %s pour %s (UA: %s)", resp.status, url, ua)
                    last_exc = f"HTTP {resp.status}"
                    continue  # essayer prochain UA
                return resp.read().decode("utf-8", errors="replace"), None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            logger.warning("Web search impossible pour %s (UA: %s) : %s", url, ua, exc)
            last_exc = str(exc)
            continue
        except Exception as exc:  # pragma: no cover - imprévisible
            logger.exception("Web search erreur inattendue pour %s (UA: %s)", url, ua)
            last_exc = str(exc)
            continue
    return None, (last_exc or "erreur inconnue")


def _web_search_instant(q: str) -> tuple[list[str], list[str], Optional[str]]:
    """Stratégie 1 : API DuckDuckGo Instant Answer (JSON).

    Retourne ``(parties, lignes_source, erreur)``. ``parties`` est vide quand
    l'API ne renvoie rien d'exploitable (cas qui déclenche le fallback HTML).
    """
    params = {
        "q": q,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
        "t": "epure",
    }
    url = "https://api.duckduckgo.com/" + urllib.parse.urlencode(params)
    raw, err = _web_search_fetch(url, "application/json")
    if raw is None:
        return [], [], err

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Web search JSON invalide pour %s", q)
        return [], [], "réponse JSON invalide"

    abstract = (data.get("Abstract") or "").strip()
    abstract_source = (data.get("AbstractSource") or "").strip()
    abstract_url = (data.get("AbstractURL") or "").strip()
    definition = (data.get("Definition") or "").strip()
    definition_source = (data.get("DefinitionSource") or "").strip()
    definition_url = (data.get("DefinitionURL") or "").strip()
    answer = (data.get("Answer") or "").strip()
    answer_type = (data.get("AnswerType") or "").strip()

    related: list[str] = []
    for item in data.get("RelatedTopics", []) or []:
        if not isinstance(item, dict):
            continue
        # Les RelatedTopics imbriqués (sous "Topics") sont groupés par sujet
        if "Topics" in item and isinstance(item["Topics"], list):
            for sub in item["Topics"]:
                text = (sub.get("Text") or "").strip()
                if text:
                    related.append(text)
        else:
            text = (item.get("Text") or "").strip()
            if text:
                related.append(text)

    parts: list[str] = []
    if abstract:
        parts.append(abstract)
    if definition and definition != abstract:
        parts.append(f"Définition : {definition}")
    if answer:
        prefix = f"Réponse ({answer_type})" if answer_type else "Réponse"
        parts.append(f"{prefix} : {answer}")
    for r in related[:5]:
        parts.append(f"- {r}")

    source_lines: list[str] = []
    if abstract and abstract_source:
        source_lines.append(f"Source abstract : {abstract_source}" + (f" ({abstract_url})" if abstract_url else ""))
    if definition and definition_source:
        source_lines.append(f"Source définition : {definition_source}" + (f" ({definition_url})" if definition_url else ""))
    return parts, source_lines, None


_HTML_RESULT_RE = re.compile(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_HTML_SNIPPET_RE = re.compile(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(fragment: str) -> str:
    """Retire les balises et déséchappe les entités d'un fragment HTML."""
    return _htmllib.unescape(_HTML_TAG_RE.sub("", fragment)).strip()


def _web_search_html(q: str) -> tuple[list[str], Optional[str]]:
    """Stratégie 2 (fallback) : endpoint HTML html.duckduckgo.com.

    Retourne ``(parties, erreur)``. Parse les résultats (titre + extrait) par
    expression régulière — pas de dépendance HTML externe.
    """
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
    raw, err = _web_search_fetch(url, "text/html")
    if raw is None:
        return [], err

    titles = [_strip_html(m) for m in _HTML_RESULT_RE.findall(raw)]
    snippets = [_strip_html(m) for m in _HTML_SNIPPET_RE.findall(raw)]

    parts: list[str] = []
    for i in range(max(len(titles), len(snippets))):
        title = titles[i] if i < len(titles) else ""
        snippet = snippets[i] if i < len(snippets) else ""
        line = " — ".join(p for p in (title, snippet) if p)
        if line:
            parts.append(f"- {line}")
        if len(parts) >= 5:
            break
    return parts, None


def perform_web_search(query: str) -> str:
    """Recherche web via DuckDuckGo, avec fallback HTML et cache LRU.

    Stratégie : (1) API Instant Answer (JSON) ; (2) si rien d'exploitable,
    fallback sur l'endpoint HTML html.duckduckgo.com. Le résultat formaté est
    mis en cache (TTL court). En cas d'échec réseau total, retourne un message
    d'erreur court pour informer l'utilisateur.
    """
    if not query or not query.strip():
        return ""
    q = query.strip()

    cached = _web_search_cache_get(q)
    if cached is not None:
        logger.info("Web search « %s » : %d caractères servis depuis le cache", q, len(cached))
        return cached

    # Stratégie 1 : Instant Answer
    parts, source_lines, err = _web_search_instant(q)
    source = "DuckDuckGo Instant Answer"

    # Stratégie 2 : fallback HTML si l'Instant Answer ne donne rien
    if not parts:
        html_parts, html_err = _web_search_html(q)
        if html_parts:
            parts = html_parts
            source_lines = []
            source = "DuckDuckGo HTML"
        elif err and html_err:
            # Les deux stratégies ont échoué au niveau réseau
            logger.error("Web search échoué pour « %s » : instant=%s ; html=%s", q, err, html_err)
            return f"Erreur de recherche web : {err}"

    if not parts:
        # Aucun résultat exploitable, mais pas d'erreur réseau
        logger.info("Web search « %s » : 0 résultat (source: %s)", q, source)
        return ""

    sources_block = ("\n\n" + "\n".join(source_lines)) if source_lines else ""
    result = (
        f"Résultats de recherche web pour « {q} » ({source}) :\n"
        + "\n".join(parts)
        + sources_block
    )

    excerpt = result[:160].replace("\n", " ")
    logger.info("Web search « %s » : %d résultat(s) via %s — extrait : %s", q, len(parts), source, excerpt)

    _web_search_cache_set(q, result)
    return result


# ── Skill /résumé (SSE) ──────────────────────────────────────────────────────

async def _stream_résumé_sse(conversation_id: str = ""):
    """Résumé des fichiers ATTACHÉS À UNE CONVERSATION.

    Lisait `ctx["fichiers_actifs"]`, la liste globale retirée le 2026-08-27 :
    demander un résumé depuis un fil résumait donc les fichiers du dernier import,
    quel que soit le fil. L'identifiant est requis — sans lui il n'y a plus de
    notion de « fichiers actifs » à laquelle se rattacher, et deviner serait
    reproduire exactement le défaut qu'on retire.
    """
    conv = history_engine.get_conversation(conversation_id) if conversation_id else None
    if conv is None:
        yield (
            f"data: {json.dumps({'type': 'error', 'content': 'Conversation inconnue : impossible de savoir quels fichiers résumer.'}, ensure_ascii=False)}\n\n"
        )
        return
    active_files = conv.get("fichiers_attachés", [])
    if not active_files:
        yield (
            f"data: {json.dumps({'type': 'error', 'content': 'Aucun fichier attaché à cette conversation. Ajoutez-en via le panneau 📎.'}, ensure_ascii=False)}\n\n"
        )
        return

    loop = asyncio.get_running_loop()
    text_parts: list[str] = []
    for path in active_files:
        try:
            text = await loop.run_in_executor(None, RAGEngine.read_file_text, path)
            text_parts.append(text[:3000])
        except Exception:
            logger.exception("Erreur lecture fichier %s pour /résumé", path)

    if not text_parts:
        yield f"data: {json.dumps({'type': 'error', 'content': 'Impossible de lire les fichiers actifs.'})}\n\n"
        return

    combined = "\n\n---\n\n".join(text_parts)[:12000]
    prompt = (
        "Résume en 100-150 mots maximum ces documents de cours. "
        "Indique les sujets principaux et les notions clés. Sois factuel.\n\n"
        f"Contenu :\n{combined}"
    )
    # LOCAL, jamais le modèle du chat. Ce flux héritait de `ctx["modèle_actif"]`
    # sans le moindre garde-fou : demander un résumé de ses fiches envoyait leur
    # CONTENU (jusqu'à 12 000 caractères, cf. `combined` ci-dessus) au fournisseur
    # cloud choisi pour discuter — sans que rien ne le dise, et sans qu'aucun
    # `use_cloud` n'existe pour le refuser. C'était le site le plus exposé du lot.
    #
    # Pas de paramètre `use_cloud` ici, et c'est délibéré : `/skills/résumé` est
    # déclenché par un bouton unique, sans écran de choix. Ajouter le drapeau sans
    # interface pour le poser produirait une option que personne ne peut atteindre.
    # Le jour où ce choix existe côté client, ce site prendra `modele_pour_tache`
    # comme les autres.
    model_override = modele_local_defaut()
    queue: asyncio.Queue = asyncio.Queue()

    def _worker(msgs, q, lp, model):
        try:
            for token in llm.stream(msgs, model=model):
                asyncio.run_coroutine_threadsafe(q.put(token), lp)
        except Exception as exc:
            logger.exception("Erreur streaming /skills/résumé")
            asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
        finally:
            asyncio.run_coroutine_threadsafe(q.put(None), lp)

    Thread(
        target=_worker,
        args=([{"role": "user", "content": prompt}], queue, loop, model_override),
        daemon=True,
    ).start()

    while True:
        item = await queue.get()
        if item is None:
            break
        if isinstance(item, dict) and "error" in item:
            yield f"data: {json.dumps({'type': 'error', 'content': item['error']})}\n\n"
            return
        if not isinstance(item, str):
            # Sentinelles du générateur (`__stats__`, `__reasoning__`) : ce flux-ci
            # ne sert qu'à afficher un résumé, il n'a rien à en faire.
            #
            # Bug PRÉEXISTANT, pas une précaution ajoutée pour le raisonnement :
            # `__stats__` existe depuis longtemps et était sérialisé tel quel en
            # `{"type": "token", "content": {"__stats__": true, …}}`. Le
            # consommateur fait `last.content + ev.content`, donc un
            # « [object Object] » se collait à la fin de chaque résumé, avec
            # n'importe quel modèle. Le raisonnement n'aurait fait qu'en ajouter
            # un second, plus gros.
            continue
        yield f"data: {json.dumps({'type': 'token', 'content': item}, ensure_ascii=False)}\n\n"


class ResumeRequest(BaseModel):
    conversation_id: str = ""


@router.post("/skills/résumé")
async def skills_résumé(req: ResumeRequest | None = None):
    """Corps optionnel pour rester compatible avec un client qui n'en envoie pas.

    Sans `conversation_id`, le flux rend une erreur explicite plutôt qu'un 422 :
    c'est un flux SSE, et une erreur de validation FastAPI n'y serait pas lisible
    par le consommateur, qui n'écoute que des événements `data:`.
    """
    return StreamingResponse(
        _stream_résumé_sse(req.conversation_id if req else ""),
        media_type="text/event-stream", headers=SSE_HEADERS,
    )


# ── Conversations ────────────────────────────────────────────────────────────
#
# Ces routes vivent dans le module CHAT et non dans le module Historique, qui
# manipule pourtant le même magasin. La raison est son manifeste :
# `history` porte `removable: true`. Y loger le cycle de vie des conversations
# voudrait dire que désactiver l'Historique depuis les Réglages décapite le chat.
# `/history*` reste la VUE de parcours et de recherche sémantique ; le cycle de
# vie est ici. Un seul moteur derrière les deux (`core.runtime.history_engine`),
# donc pas de second magasin — cf. docs/conversations-persistees.md §3.
#
# ⚠️ Le module chat est monté avec `prefix: ""` (son manifeste), donc chaque
# route s'écrit préfixée À LA MAIN. Sans le `/chat/`, collision silencieuse avec
# une route du cœur (CLAUDE.md §3.3).


class ConversationCreate(BaseModel):
    titre: str = ""
    fichiers: list[str] | None = None
    #: Messages préexistants — UN seul appelant : la reprise de ce qui était à
    #: l'écran au moment de la mise à jour (étape 7 du chantier). Les
    #: conversations naissent normalement vides et se remplissent tour par tour.
    #:
    #: `list` NUE et non `list[dict]`, délibérément. Le contenu vient d'un
    #: `localStorage` écrit par une version ANTÉRIEURE du frontend : sa forme
    #: n'est pas garantie, et `list[dict]` fait répondre 422 à Pydantic dès
    #: qu'une seule entrée n'est pas un objet — donc **perd toute la
    #: conversation** pour un message abîmé. Le moteur réduit chaque entrée à
    #: `{role, content}` et écarte le reste (`_messages_propres`) : filtrer est
    #: ici la bonne réponse, refuser ne l'est pas. Mesuré en écrivant le test :
    #: une chaîne dans la liste suffisait à faire échouer la reprise entière.
    messages: list | None = None


class ConversationPatch(BaseModel):
    titre: str


class FichiersRequest(BaseModel):
    paths: list[str]


def _corpus_ou_inconnu() -> Optional[list]:
    """Chemins indexés, ou ``None`` si le corpus n'est pas interrogeable.

    **Pour la LECTURE seulement.** Ouvrir une conversation ne doit jamais
    dépendre de la pile d'embedding : dans un paquet fraîchement installé les
    90 Mo du modèle ne sont pas encore là, et `rag` est un `_LazyEngine` dont le
    premier accès lève `EmbeddingIndisponible` — que `main.py` traduit en 503 par
    un gestionnaire GLOBAL. Sans cette rattrapage, `GET /chat/conversations/{id}`
    répondrait 503 et l'utilisateur ne pourrait plus lire ses conversations parce
    que la recherche documentaire n'est pas prête. Ce serait l'incident du §8
    (« la recherche documentaire répond 500 dans un paquet livré ») déplacé d'un
    cran, sur une fonction qui n'a rien à voir.

    `None` n'est pas une liste vide : il se propage en `présent: None` jusqu'au
    client (cf. `core.history.croiser_fichiers`), c'est-à-dire « on ne sait pas »
    et non « ce fichier a disparu ».
    """
    try:
        return rag.get_indexed_files()
    except EmbeddingIndisponible:
        return None
    except Exception:
        logger.exception("Corpus indexé illisible — fichiers marqués « inconnu »")
        return None


def _valider_fichiers(paths: list) -> list[str]:
    """Confine les chemins puis exige leur présence dans le corpus indexé.

    Deux refus distincts, et les confondre serait une faute :

    * hors des dossiers de données → **403**, c'est une tentative de sortie
      (`resolve_user_path`, CLAUDE.md §6). On renvoie le chemin RÉSOLU, jamais la
      chaîne d'origine, sinon la vérification ne porte pas sur ce qui sera lu ;
    * dans les dossiers mais absent du corpus → **400**, c'est une demande
      absurde : attacher un fichier que le moteur n'a pas indexé produirait une
      conversation dont le contexte ne contient rien, sans que rien ne le dise —
      exactement le symptôme « indexé à zéro chunk, en silence » (§3.3 bis).

    ⚠️ Contrairement à :func:`_corpus_ou_inconnu`, on ne rattrape PAS
    `EmbeddingIndisponible` : elle remonte au gestionnaire global, qui rend un 503
    porteur de l'état d'installation. C'est volontaire et c'est l'asymétrie du
    chantier — **lire une conversation ne doit jamais échouer, y attacher un
    fichier peut attendre que le corpus existe.** Le second est une action
    explicite de l'utilisateur, à qui l'on doit une explication, pas un silence.
    """
    resolus: list[str] = []
    for p in paths:
        try:
            resolus.append(str(resolve_user_path(p)))
        except PathOutsideDataError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    connus = {cle_chemin(str(s)) for s in rag.get_indexed_files()}
    inconnus = [p for p in resolus if cle_chemin(p) not in connus]
    if inconnus:
        raise HTTPException(
            status_code=400,
            detail=("Fichiers non indexés (les importer d'abord) : "
                    + ", ".join(Path(p).name for p in inconnus)),
        )
    return resolus


@router.post("/chat/conversations")
async def conversation_create(req: ConversationCreate | None = None):
    loop = asyncio.get_running_loop()
    req = req or ConversationCreate()
    fichiers = _valider_fichiers(req.fichiers) if req.fichiers else []
    conv = await loop.run_in_executor(
        None, lambda: history_engine.create_conversation(
            titre=req.titre, fichiers=fichiers, messages=req.messages)
    )
    return {"id": conv["id"], "titre": conv["titre"], "créée": conv["créée"],
            "modifiée": conv["modifiée"], "fichiers_attachés": conv["fichiers_attachés"],
            "messages": conv["messages"], "n_messages": conv["n_messages"]}


@router.get("/chat/conversations")
async def conversation_list(days: int = 0, limit: int = 100, offset: int = 0):
    """Liste l'INDEX — jamais les messages, qui pèsent ~6,7 Ko par conversation.

    ``days=0`` par défaut, c'est-à-dire tout : une liste de conversations sert à
    retrouver un fil ancien, la borner à 30 jours en cacherait la moitié. Le
    module Historique garde son défaut à 30, il répond à un autre besoin.
    """
    loop = asyncio.get_running_loop()
    toutes = await loop.run_in_executor(None, history_engine.list_conversations, days)
    debut = max(0, offset)
    fin = debut + max(0, min(limit, 500))
    return {"conversations": toutes[debut:fin], "total": len(toutes)}


@router.get("/chat/conversations/{conv_id}")
async def conversation_get(conv_id: str):
    """La conversation entière, fichiers attachés marqués ``présent``.

    Ne 503 jamais : cf. :func:`_corpus_ou_inconnu`. ``corpus_interrogeable`` dit
    au client si les ``présent`` valent quelque chose, pour qu'il puisse afficher
    « état inconnu » plutôt qu'une croix mensongère.
    """
    loop = asyncio.get_running_loop()
    corpus = await loop.run_in_executor(None, _corpus_ou_inconnu)
    vue = await loop.run_in_executor(
        None, history_engine.conversation_view, conv_id, corpus
    )
    if vue is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    vue["corpus_interrogeable"] = corpus is not None
    return vue


@router.patch("/chat/conversations/{conv_id}")
async def conversation_patch(conv_id: str, req: ConversationPatch):
    titre = req.titre.strip()
    if not titre:
        raise HTTPException(status_code=400, detail="Titre vide")
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, history_engine.rename_conversation, conv_id, titre
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return {"ok": True, "titre": titre[:80]}


@router.delete("/chat/conversations/{conv_id}")
async def conversation_delete(conv_id: str):
    """404 sur une conversation absente, plutôt qu'un ``{"ok": true}`` menteur.

    `delete_conversation` est idempotent et rend `True` même sur un identifiant
    inconnu (il nettoie l'index et le vecteur au cas où) ; l'existence est donc
    vérifiée ici. Un client qui supprime deux fois doit voir la différence,
    sinon un bug d'identifiant passe pour un succès.
    """
    loop = asyncio.get_running_loop()
    if await loop.run_in_executor(None, history_engine.get_conversation, conv_id) is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    await loop.run_in_executor(None, history_engine.delete_conversation, conv_id)
    return {"ok": True}


@router.put("/chat/conversations/{conv_id}/fichiers")
async def conversation_fichiers(conv_id: str, req: FichiersRequest):
    """Remplace l'ENSEMBLE des fichiers attachés.

    Un `PUT` de l'ensemble et non un `POST`/`DELETE` par fichier : l'interface est
    une liste à cocher, donc c'est la forme de l'interaction réelle, et ça
    supprime toute question d'ordre entre deux requêtes concurrentes.
    """
    resolus = _valider_fichiers(req.paths)
    loop = asyncio.get_running_loop()
    rendus = await loop.run_in_executor(
        None, history_engine.set_conversation_files, conv_id, resolus
    )
    if rendus is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return {"fichiers_attachés": [{"chemin": p, "présent": True} for p in rendus]}


# ── WebSocket de chat ────────────────────────────────────────────────────────

#: Nombre de messages entre deux consolidations d'une même conversation.
#: Ancien déclencheur : `len(history) >= 10` à la déconnexion, donc AU PLUS UNE
#: fois par connexion, et jamais pour qui laisse son onglet ouvert des heures.
_SEUIL_CONSOLIDATION = 10


def _travaux_apres_tour(conv_id: str, use_cloud: bool, annoncer_titre) -> None:
    """Titrage, consolidation, indexation vectorielle — **hors** du chemin du message.

    Les trois étaient accrochés à ``WebSocketDisconnect``, qui n'est plus une
    frontière signifiante : la conversation est désormais écrite à chaque tour,
    et une déconnexion n'est qu'un onglet qu'on ferme. Ils sont donc rejoués ici,
    dans un thread, APRÈS que la réponse est partie.

    ⚠️ **L'indexation vectorielle n'est pas par tour, et c'est le point.**
    ``_indexer_vectoriel`` calcule un embedding sur 8 000 caractères ; l'appeler
    à chaque message mettrait un modèle sur le trajet de la réponse, ce que
    CLAUDE.md §8 interdit explicitement (« ne rien mettre de bloquant sur ce
    chemin »). Elle suit donc la cadence de la consolidation. Conséquence
    assumée : ``@historique`` ne retrouve une conversation qu'à partir de son
    dixième message, plus le rattrapage de la déconnexion.

    Tout est best-effort : ce thread ne doit jamais faire tomber une réponse déjà
    livrée à l'utilisateur.
    """
    try:
        titre = history_engine.generer_titre(conv_id)
        if titre:
            annoncer_titre(titre)
    except Exception:
        logger.exception("Titrage automatique impossible (%s)", conv_id)

    try:
        conv = history_engine.get_conversation(conv_id)
        if conv is None:
            return
        n = len(conv.get("messages", []))
        if n < _SEUIL_CONSOLIDATION:
            return
        if (n - conv.get("dernière_consolidation", 0)) < _SEUIL_CONSOLIDATION:
            return
        # Marquer AVANT de travailler : si la consolidation échoue, on ne veut pas
        # la relancer à chaque message suivant. C'est une tâche d'agrément, pas
        # une transaction dont il faudrait garantir l'exécution.
        history_engine.marquer_consolidation(conv_id, n)
        history_engine.indexer_vectoriel(conv_id)
        consolidation_engine.consolidate_history(conv_id, use_cloud)
    except Exception:
        logger.exception("Travaux de fin de tour impossibles (%s)", conv_id)


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    if not await ws_require_token(websocket):
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()
    _last_model: list[str] = [llm._model]
    #: Dernière conversation touchée sur CETTE connexion — sert au rattrapage de
    #: la déconnexion. L'identifiant de travail, lui, est relu à CHAQUE message :
    #: le client peut changer de conversation sans rouvrir le socket, et une
    #: variable de connexion figerait la première ouverte.
    _derniere_conv: list[Optional[str]] = [None]

    def _annoncer_titre_depuis_thread(conv_id: str):
        """Renvoie une closure qui pousse `{"type": "titre"}` vers ce socket.

        Passer par ``run_coroutine_threadsafe`` : l'appelant est un ``Thread``,
        et toucher le WebSocket depuis un autre fil sans repasser par la boucle
        corromprait le protocole. L'échec est avalé — le socket peut avoir été
        fermé entre-temps, ce qui est normal et sans conséquence : le titre est
        déjà sur le disque, la liste le montrera au prochain chargement.
        """
        def _pousser(titre: str) -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    websocket.send_text(json.dumps(
                        {"type": "titre", "id": conv_id, "titre": titre},
                        ensure_ascii=False,
                    )),
                    loop,
                )
            except Exception:
                logger.debug("Titre non annoncé (socket fermé ?) pour %s", conv_id)
        return _pousser

    async def _enregistrer_reponse(conv_id: str, texte: str) -> dict:
        """Seconde transaction du tour : la réponse, puis les travaux de fond.

        Séparée de l'ajout du message utilisateur, et pas par commodité : entre
        les deux il y a le streaming, c'est-à-dire des secondes. Une transaction
        unique qui enjamberait le flux garderait le fichier verrouillé pendant
        toute la génération, et un envoi concurrent dans la même conversation
        attendrait la fin de la réponse précédente.
        """
        conv = await loop.run_in_executor(
            None, lambda: history_engine.append_messages(
                conv_id, [{"role": "assistant", "content": texte}],
                model=_last_model[0],
            )
        )
        use_cloud = bool(memory.get_context().get("consolidation_cloud", False))
        Thread(
            target=_travaux_apres_tour,
            args=(conv_id, use_cloud, _annoncer_titre_depuis_thread(conv_id)),
            daemon=True,
        ).start()
        # Les métadonnées du message qu'on vient d'écrire, pour que le client les
        # affiche sans relire la conversation entière après chaque tour. C'est le
        # SERVEUR qui fait foi : il vient de les poser sur le disque, et une
        # horloge de navigateur qui diverge donnerait deux heures différentes pour
        # le même message selon qu'on le regarde avant ou après un rechargement.
        dernier = (conv or {}).get("messages", [])
        return dict(dernier[-1]) if dernier else {}

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            rag_override: str | None = msg.get("rag_override")
            strict_override: bool = bool(msg.get("strict_override", False))
            web_search_override: bool = bool(msg.get("web_search_override", False))

            ctx = memory.get_context()
            model_override = ctx.get("modèle_actif") or None
            # Réglage de session, comme `strict_mode` : lu ici plutôt que reçu
            # dans le message. Le client n'a donc rien à envoyer, et le réglage
            # vaut pour tous les chemins de ce tour (direct ET pipeline).
            # `.get(..., True)` : un `context_session.json` déjà sur le disque
            # n'a pas la clé, et son absence doit valoir « activé » — le
            # comportement d'avant ce réglage.
            raisonnement = bool(ctx.get("raisonnement", True))
            _last_model[0] = model_override or llm._model

            _req_start = time.time()
            user_text = msg["content"]

            # ── Quelle conversation ? ─────────────────────────────────────────
            #
            # Relu à CHAQUE message et non une fois par connexion : le client
            # change de conversation sans rouvrir le socket. Un seul `/ws/chat`
            # subsiste donc, ce qui évite de reconnecter à chaque bascule — la
            # logique de reconnexion du frontend est déjà délicate.
            #
            # CRÉATION PARESSEUSE : sans identifiant, la conversation naît ici,
            # au premier message, et jamais à l'ouverture d'un onglet vide. Sinon
            # la liste se remplit de coquilles sans contenu, que l'utilisateur
            # devrait nettoyer à la main.
            conv_id = (msg.get("conversation_id") or "").strip()
            if not conv_id:
                # Pas d'identifiant → on CONTINUE la conversation de cette
                # connexion, on n'en ouvre pas une seconde.
                #
                # Sans cette ligne, un client qui n'envoie pas d'identifiant
                # obtient une conversation NEUVE à chaque message : le modèle perd
                # tout le contexte au deuxième tour, et la liste se remplit d'un
                # fil par message. Mesuré — c'est ce qu'ont attrapé les tests de
                # protocole, dont le prompt du second tour ne contenait plus la
                # réponse du premier.
                #
                # « Pas d'identifiant » veut dire « poursuis », jamais
                # « recommence » : pour ouvrir une nouvelle conversation, le
                # client appelle `POST /chat/conversations` et envoie l'id rendu.
                # C'est aussi ce qui préserve à l'identique le comportement des
                # clients d'avant ce chantier — une conversation par connexion.
                conv_id = _derniere_conv[0] or ""
            if conv_id:
                existe = await loop.run_in_executor(
                    None, history_engine.get_conversation, conv_id
                )
                if existe is None:
                    # Identifiant inconnu : supprimée ailleurs, ou client
                    # désynchronisé. On n'échoue PAS — le message vient d'être
                    # tapé, le perdre serait le pire des comportements. On repart
                    # sur une conversation neuve, et on l'ANNONCE pour que le
                    # client se recale au lieu d'écrire dans le vide.
                    logger.warning("Conversation inconnue %r — création d'une neuve", conv_id)
                    conv_id = ""
            if not conv_id:
                neuve = await loop.run_in_executor(
                    None, lambda: history_engine.create_conversation(model=_last_model[0])
                )
                conv_id = neuve["id"]
                await websocket.send_text(json.dumps({
                    "type": "conversation", "id": conv_id,
                }))
            _derniere_conv[0] = conv_id

            # @historique skill
            hist_ctx = ""
            if "@historique" in user_text:
                hist_query = user_text.replace("@historique", "").strip() or user_text
                hist_results = await loop.run_in_executor(
                    None, history_engine.search_history, hist_query
                )
                if hist_results:
                    extraits = "\n\n".join(
                        f"— {r['titre']} ({r['date']}) :\n{r['extrait']}"
                        for r in hist_results
                    )
                    hist_ctx = f"Extraits de conversations précédentes pertinentes :\n{extraits}"
                user_text = hist_query if hist_query != user_text else user_text.replace("@historique", "").strip()
                user_text = user_text or msg["content"]

            # Le message de l'utilisateur entre sur le DISQUE ici, et pas plus
            # tôt : son texte a pu être réécrit juste au-dessus (`@historique`
            # retire sa balise), et c'est le texte réellement soumis au modèle qui
            # doit être conservé — sinon le tour suivant relit une question que
            # personne n'a posée.
            #
            # Cet ajout rend la conversation à jour, donc l'historique du prompt
            # avec : une seule lecture-écriture au lieu d'un `append` puis d'un
            # `get`. Le tour d'assistant sera une SECONDE transaction, plus bas —
            # deux écritures distinctes, jamais une seule qui les enjamberait
            # (docs/conversations-persistees.md §6).
            conv = await loop.run_in_executor(
                None, lambda: history_engine.append_messages(
                    conv_id, [{"role": msg.get("role", "user"), "content": user_text}],
                    model=_last_model[0],
                )
            )
            if conv is None:
                # La conversation a disparu entre sa résolution et cet ajout
                # (suppression depuis un autre onglet). Rare, mais le message de
                # l'utilisateur ne doit pas être perdu en silence.
                logger.warning("Conversation %s disparue en cours de tour", conv_id)
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "content": "Cette conversation n'existe plus. Ouvrez-en une nouvelle.",
                }))
                continue


            # @web skill
            web_ctx = ""
            if web_search_override:
                _t_web = time.time()
                web_query = user_text.strip()
                web_results = await loop.run_in_executor(None, perform_web_search, web_query)
                logger.info("TTFT Web: %.3fs (query=%r, len=%d)", time.time() - _t_web, web_query[:80], len(web_results))
                if web_results:
                    web_ctx = (
                        "Résultats de recherche web récents (peuvent compléter tes connaissances) :\n"
                        f"{web_results}\n\n"
                        "Si pertinent, intègre ces informations dans ta réponse et cite la source."
                    )
                else:
                    web_ctx = (
                        "Recherche web : aucun résultat exploitable trouvé pour cette requête. "
                        "Réponds à partir de tes connaissances en le signalant."
                    )

            # Les fichiers viennent de LA CONVERSATION, plus d'un `fichiers_actifs`
            # global (retiré le 2026-08-27). `conv` a été relu juste au-dessus par
            # l'ajout du message, donc l'attachement est frais sans lecture
            # supplémentaire.
            #
            # Les trois modes restent ceux d'avant, seule la provenance de la
            # liste change : « corpus entier » (`rag_override == "all"`) reste
            # orthogonal à l'attachement — c'est « cherche partout », pas
            # « attache tout ».
            active_files = conv.get("fichiers_attachés", [])
            _t = time.time()
            if rag_override == "all":
                chunks = await loop.run_in_executor(None, rag.query, user_text)
            elif active_files:
                chunks = await loop.run_in_executor(
                    None, rag.query_filtered, user_text, active_files
                )
            else:
                chunks = ""
            logger.info("TTFT RAG: %.3fs", time.time() - _t)

            sys_parts: list[str] = []
            if strict_override:
                sys_parts.append(
                    "Réponds de façon maximalement concise. "
                    "Pas d'introduction, pas de reformulation."
                )
            _t = time.time()
            mem_ctx = await loop.run_in_executor(None, memory.build_system_context, user_text)
            logger.info("TTFT Memory: %.3fs", time.time() - _t)
            if mem_ctx:
                sys_parts.append(mem_ctx)
            # [CONTEXTE ACTIF] : le résumé des fichiers de CETTE conversation.
            # Il était injecté par `memory.build_system_context`, qui n'a aucun
            # moyen de savoir de quelle conversation il s'agit — tolérable tant
            # que le résumé était global, faux dès qu'il y en a un par fil. Il est
            # donc ajouté ici, où la conversation est connue.
            resume_conv = (conv.get("résumé_contexte") or "").strip()
            if resume_conv:
                sys_parts.append(f"[CONTEXTE ACTIF]\n{resume_conv}")
            if hist_ctx:
                sys_parts.append(hist_ctx)
            if web_ctx:
                sys_parts.append(web_ctx)
            if chunks:
                sys_parts.append(
                    "Contexte extrait de tes fiches de révision :\n"
                    f"{chunks}\n\n"
                    "Réponds à la question en te basant sur ce contexte si pertinent."
                )

            # Horodatage du message qu'on vient d'écrire, renvoyé au client.
            #
            # Le client l'a affiché optimistement dès la frappe, sans heure. Il
            # pourrait en poser une lui-même — mais alors l'heure affichée avant
            # un rechargement viendrait de l'horloge du navigateur et celle
            # d'après du disque, donc deux valeurs pour le même message dès que
            # les deux horloges divergent. Le serveur fait foi, ici comme dans
            # l'événement `done`.
            _meta_user = (conv["messages"] or [{}])[-1]
            await websocket.send_text(json.dumps({
                "type": "meta_message", "role": "user",
                "horodatage": _meta_user.get("horodatage", ""),
                # Le modèle À QUI la question est posée. Utile surtout dans un
                # fil où l'on change de modèle en cours de route : sans lui, on
                # ne peut plus savoir laquelle des questions est partie où.
                "modèle": _meta_user.get("modèle", ""),
            }, ensure_ascii=False))

            messages = list(conv["messages"])
            if sys_parts:
                messages = [{"role": "system", "content": "\n\n".join(sys_parts)}] + messages

            # ── Orchestrator ──────────────────────────────────────────────────
            _effort = msg.get("effort", "direct")
            _client_steps = msg.get("steps", [])  # [{"role": "...", "model": "..."}]
            _direct_mode = bool(msg.get("direct", False)) or _effort == "direct" or not _effort

            if not _direct_mode:
                _pipeline: list[dict] = []

                if _effort == "adaptive":
                    try:
                        _classification = await asyncio.wait_for(
                            loop.run_in_executor(None, orchestrator.classify_task, user_text, ctx),
                            timeout=3.0,
                        )
                    except Exception:
                        _classification = {"complexity": "simple"}
                    _complexity = _classification.get("complexity", "simple")
                    if _complexity == "simple":
                        _direct_mode = True
                    else:
                        _eff = "medium" if _complexity == "moderate" else "high"
                        _pipeline = orchestrator.build_steps(_eff, [], ctx)
                elif _effort in ("low", "medium", "high"):
                    _pipeline = orchestrator.build_steps(_effort, _client_steps, ctx)

                if not _direct_mode and _pipeline:
                    await websocket.send_text(json.dumps({
                        "type": "pipeline_info",
                        "effort": _effort,
                        "steps": [{"role": s["role"], "label": s.get("label", s["role"]), "model": s["model"]} for s in _pipeline],
                    }))
                    _final = ""
                    async for _event in orchestrator.run_pipeline(
                            _pipeline, user_text, messages, loop,
                            raisonnement=raisonnement):
                        if _event.get("type") == "pipeline_done":
                            _final = _event.get("final_output", "")
                        await websocket.send_text(json.dumps(_event))
                    _meta = await _enregistrer_reponse(conv_id, _final) if _final else {}
                    await websocket.send_text(json.dumps({
                        "type": "done",
                        "horodatage": _meta.get("horodatage", ""),
                        "modèle": _meta.get("modèle", ""),
                    }, ensure_ascii=False))
                    continue
                elif not _direct_mode:
                    _direct_mode = True  # empty pipeline → fall through to direct
            # ─────────────────────────────────────────────────────────────────

            queue: asyncio.Queue = asyncio.Queue()

            def _stream(msgs, q, lp, model):
                try:
                    for token in llm.stream(msgs, model=model, raisonnement=raisonnement):
                        asyncio.run_coroutine_threadsafe(q.put(token), lp)
                except Exception as exc:
                    logger.exception("Erreur streaming chat")
                    asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(None), lp)

            Thread(
                target=_stream, args=(messages, queue, loop, model_override), daemon=True
            ).start()

            accumulated = ""
            _first_token = True
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, dict) and "error" in item:
                    await websocket.send_text(
                        json.dumps({"type": "error", "content": item["error"]})
                    )
                    break
                if isinstance(item, dict) and item.get("__reasoning__"):
                    # Raisonnement du modèle, canal distinct du contenu final.
                    # `{"type": "reasoning"}` suit la forme de `{"type": "token"}`
                    # juste en dessous plutôt qu'un format à part : le frontend a
                    # déjà un aiguillage sur `data.type`, et un second format
                    # aurait demandé un second aiguillage pour la même chose.
                    #
                    # N'entre PAS dans `accumulated`, et c'est le point : c'est
                    # `accumulated` qui part dans `history` puis dans le prompt du
                    # tour suivant. Y verser le raisonnement le ferait relire par
                    # le modèle comme s'il l'avait dit à l'utilisateur, et
                    # gonflerait le contexte de plusieurs centaines de tokens par
                    # tour (584 générés pour 14 caractères de réponse, mesuré).
                    await websocket.send_text(json.dumps({
                        "type": "reasoning", "content": item["content"],
                    }, ensure_ascii=False))
                    continue
                if isinstance(item, dict) and "__stats__" in item:
                    usage_tracker.track(
                        _provider_of(_last_model[0]),
                        item.get("prompt_tokens", 0),
                        item.get("output_tokens", 0),
                    )
                    await websocket.send_text(json.dumps({
                        "type": "stats",
                        "prompt_tokens": item.get("prompt_tokens", 0),
                        "output_tokens": item.get("output_tokens", 0),
                        "eval_duration_ms": (item.get("eval_duration_ns", 0) or 0) // 1_000_000,
                        "prompt_duration_ms": (item.get("prompt_duration_ns", 0) or 0) // 1_000_000,
                    }))
                    continue
                if _first_token:
                    logger.info("TTFT total: %.3fs", time.time() - _req_start)
                    _first_token = False
                accumulated += item
                await websocket.send_text(json.dumps({"type": "token", "content": item}))

            _meta = await _enregistrer_reponse(conv_id, accumulated) if accumulated else {}
            await websocket.send_text(json.dumps({
                "type": "done",
                "horodatage": _meta.get("horodatage", ""),
                "modèle": _meta.get("modèle", ""),
            }, ensure_ascii=False))

    except WebSocketDisconnect:
        # RATTRAPAGE, plus une sauvegarde. Tout est déjà sur le disque : la
        # déconnexion ne sert qu'à rendre la conversation trouvable par
        # `@historique` sans attendre le dixième message, puisque l'indexation
        # vectorielle suit la cadence de la consolidation (cf.
        # `_travaux_apres_tour`).
        #
        # L'ancien bloc faisait tout ici — `save_conversation` de l'historique
        # accumulé en mémoire, puis consolidation. C'était le SEUL moment où quoi
        # que ce soit était écrit : fermer l'onglet autrement qu'en se
        # déconnectant proprement, ou laisser le processus mourir, perdait la
        # conversation entière.
        conv_id = _derniere_conv[0]
        if conv_id:
            Thread(
                target=history_engine.indexer_vectoriel, args=(conv_id,), daemon=True
            ).start()
