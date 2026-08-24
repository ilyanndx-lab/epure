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
from threading import Thread
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from core.auth import ws_require_token
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

async def _stream_résumé_sse():
    ctx = memory.get_context()
    active_files = ctx.get("fichiers_actifs", [])
    if not active_files:
        yield (
            f"data: {json.dumps({'type': 'error', 'content': 'Aucun fichier actif. Chargez des fichiers via le panneau 📎.'})}\n\n"
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
    model_override = ctx.get("modèle_actif") or None
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


@router.post("/skills/résumé")
async def skills_résumé():
    return StreamingResponse(
        _stream_résumé_sse(), media_type="text/event-stream", headers=SSE_HEADERS
    )


# ── WebSocket de chat ────────────────────────────────────────────────────────

@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    if not await ws_require_token(websocket):
        return
    await websocket.accept()
    history: list[dict] = []
    loop = asyncio.get_running_loop()
    _last_model: list[str] = [llm._model]

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            history.append({"role": msg["role"], "content": msg["content"]})

            rag_override: str | None = msg.get("rag_override")
            strict_override: bool = bool(msg.get("strict_override", False))
            web_search_override: bool = bool(msg.get("web_search_override", False))

            ctx = memory.get_context()
            active_files = ctx.get("fichiers_actifs", [])
            model_override = ctx.get("modèle_actif") or None
            _last_model[0] = model_override or llm._model

            _req_start = time.time()
            user_text = msg["content"]

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
                history[-1]["content"] = user_text or msg["content"]

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

            messages = list(history)
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
                    async for _event in orchestrator.run_pipeline(_pipeline, user_text, messages, loop):
                        if _event.get("type") == "pipeline_done":
                            _final = _event.get("final_output", "")
                        await websocket.send_text(json.dumps(_event))
                    if _final:
                        history.append({"role": "assistant", "content": _final})
                    await websocket.send_text(json.dumps({"type": "done"}))
                    continue
                elif not _direct_mode:
                    _direct_mode = True  # empty pipeline → fall through to direct
            # ─────────────────────────────────────────────────────────────────

            queue: asyncio.Queue = asyncio.Queue()

            def _stream(msgs, q, lp, model):
                try:
                    for token in llm.stream(msgs, model=model):
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

            if accumulated:
                history.append({"role": "assistant", "content": accumulated})
            await websocket.send_text(json.dumps({"type": "done"}))

    except WebSocketDisconnect:
        if len(history) >= 3:
            model = _last_model[0]
            msgs = list(history)
            use_cloud = memory.get_context().get("consolidation_cloud", False)

            def _save_and_consolidate():
                conv_id = history_engine.save_conversation(msgs, model, ["chat"])
                if len(msgs) >= 10:
                    consolidation_engine.consolidate_history(conv_id, use_cloud)

            Thread(target=_save_and_consolidate, daemon=True).start()
