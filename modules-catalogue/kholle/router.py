"""Routeur du module Kholle. Monté avec prefix "" : REST /kholle/start ET le
WebSocket /ws/kholle.

Moteurs partagés (llm, memory, consolidation_engine, usage_tracker, provider_of)
injectés via core.runtime.
"""

import asyncio
import json
import logging
import re
from threading import Thread
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from core.auth import ws_require_token
from core.instance import modele_local_defaut, modele_pour_tache
from core.rag import RAGEngine
from core.runtime import (
    consolidation_engine,
    llm,
    memory,
    provider_of as _provider_of,
    usage_tracker,
)

#: Cible cloud de ce module, nommee POUR LA TACHE (cf. `core/instance.py`) :
#: `use_cloud=True` ne veut pas dire « le modele actif du chat ».
_MODELE_CLOUD = "groq:openai/gpt-oss-120b"
_CLE_CLOUD = "GROQ_API_KEY"

logger = logging.getLogger(__name__)

router = APIRouter()

_KHOLLE_SYSTEM = (
    "Tu es un professeur de kholle de classe préparatoire scientifique (MPSI/MP). "
    "Tu poses une question à la fois, tu écoutes la réponse de l'élève, tu la corriges "
    "avec rigueur en pointant les erreurs exactes et les imprécisions, tu donnes la réponse "
    "attendue si nécessaire, puis tu passes à la question suivante. Sois exigeant mais "
    "pédagogue. Ne pose jamais deux questions en même temps."
)


class KholleStartRequest(BaseModel):
    mode: str
    source_files: Optional[list] = None
    questions: Optional[list] = None


def _generate_questions(source_files: list) -> list:
    parts = []
    for path in source_files:
        try:
            text = RAGEngine.read_pdf_text(path)
            parts.append(text[:6000])
        except Exception:
            logger.exception("Erreur lecture PDF %s", path)
    if not parts:
        raise ValueError("Aucun contenu extrait des fichiers sélectionnés")

    content = "\n\n---\n\n".join(parts)[:14000]
    prompt = (
        "Tu es un professeur de kholle de classe préparatoire scientifique (MPSI/MP).\n"
        "À partir du contenu de cours suivant, génère 10 questions de kholle adaptées au niveau prépa.\n"
        "Les questions doivent être précises, demander des définitions rigoureuses ou des démonstrations, "
        "et couvrir les notions importantes du cours.\n\n"
        f"Contenu :\n{content}\n\n"
        "Réponds UNIQUEMENT avec un JSON valide, sans texte avant ou après :\n"
        '{"questions": ["question1", "question2", ..., "question10"]}'
    )
    # LOCAL. Aucun modele n'etait passe, donc `config.yaml` : local de fait, mais
    # hors du reglage, donc impossible a changer depuis l'interface. Generer des
    # questions a partir d'un cours est une tache de fond — l'utilisateur a clique
    # sur « demarrer une kholle », pas choisi un modele pour cette etape.
    raw = llm.generate([{"role": "user", "content": prompt}],
                       model=modele_local_defaut())
    return _parse_questions_json(raw)


def _parse_questions_json(raw: str) -> list:
    try:
        return json.loads(raw)["questions"]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    match = re.search(r'\{.*?"questions"\s*:\s*(\[[^\]]*\])', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    lines = [l.strip().strip('"').rstrip('",') for l in raw.splitlines()]
    questions = [l for l in lines if len(l) > 15]
    if questions:
        return questions[:10]
    raise ValueError(f"Impossible de parser les questions générées : {raw[:200]}")


def _extract_errors(correction: str, question: str, answer: str) -> list:
    try:
        prompt = (
            f"Analyse cette correction de kholle.\n\n"
            f"Question : {question}\n"
            f"Réponse élève : {answer}\n"
            f"Correction : {correction}\n\n"
            "Liste uniquement les erreurs ou imprécisions de la réponse de l'élève.\n"
            'Réponds UNIQUEMENT avec ce JSON valide : {"errors": ["erreur1", "erreur2"]}\n'
            'Si aucune erreur : {"errors": []}'
        )
        # LOCAL, meme raison que `_generate_questions` : analyse automatique
        # apres la correction, jamais demandee explicitement.
        raw = llm.generate([{"role": "user", "content": prompt}],
                           model=modele_local_defaut())
        match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group()).get("errors", [])
    except Exception:
        logger.exception("Erreur lors de l'extraction des erreurs kholle")
    return []


@router.post("/kholle/start")
async def kholle_start(req: KholleStartRequest):
    loop = asyncio.get_running_loop()
    if req.mode == "generate":
        if not req.source_files:
            raise HTTPException(status_code=400, detail="source_files requis pour le mode generate")
        questions = await loop.run_in_executor(None, _generate_questions, req.source_files)
    elif req.mode == "list":
        if not req.questions:
            raise HTTPException(status_code=400, detail="questions requises pour le mode list")
        questions = [q.strip() for q in req.questions if q.strip()]
    else:
        raise HTTPException(status_code=400, detail="mode invalide, valeurs acceptées : generate, list")
    return {"questions": questions}


@router.websocket("/ws/kholle")
async def ws_kholle(websocket: WebSocket):
    if not await ws_require_token(websocket):
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()

    questions: list = []
    current_index = 0
    session_errors: list = []
    answers: list = []

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")

            if msg_type == "start":
                questions = msg["questions"]
                current_index = 0
                session_errors = []
                answers = []
                await websocket.send_text(json.dumps({
                    "type": "question",
                    "content": questions[0],
                    "index": 0,
                    "total": len(questions),
                }))

            elif msg_type == "answer":
                answer = msg["content"]
                question = questions[current_index]
                answers.append(answer)

                # LOCAL par defaut, cloud sur demande EXPLICITE du client.
                #
                # Ce site est le plus discutable des trois, et vaut d'etre lu : la
                # correction repond a ce que l'eleve vient d'ecrire, donc elle
                # ressemble a un tour de chat. Mais elle n'est pas *le* chat — le
                # modele actif y a ete choisi pour discuter, et il partait ici
                # avec la question, la reponse de l'eleve ET son contexte memoire
                # (profil, lacunes) sans que personne ne l'ait demande.
                #
                # `use_cloud` dans le message WS plutot que rien : contrairement au
                # resume d'import et au plan de revision, ce flux a un client qui
                # envoie du JSON, donc un endroit ou le choix peut reellement se
                # poser. Absent du message → local.
                model_override = modele_pour_tache(
                    bool(msg.get("use_cloud", False)), _MODELE_CLOUD, _CLE_CLOUD)
                mem_ctx = await loop.run_in_executor(None, memory.build_system_context, question)

                system_content = _KHOLLE_SYSTEM
                if mem_ctx:
                    system_content = mem_ctx + "\n\n" + system_content

                correction_msgs = [
                    {"role": "system", "content": system_content},
                    {
                        "role": "user",
                        "content": f"Question posée : {question}\nRéponse de l'élève : {answer}",
                    },
                ]

                queue: asyncio.Queue = asyncio.Queue()

                def _stream_correction(msgs, q, lp, model):
                    try:
                        for token in llm.stream(msgs, model=model):
                            asyncio.run_coroutine_threadsafe(q.put(token), lp)
                    except Exception as exc:
                        logger.exception("Erreur streaming correction kholle")
                        asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                    finally:
                        asyncio.run_coroutine_threadsafe(q.put(None), lp)

                Thread(
                    target=_stream_correction,
                    args=(correction_msgs, queue, loop, model_override),
                    daemon=True,
                ).start()

                accumulated = ""
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, dict) and "error" in item:
                        await websocket.send_text(
                            json.dumps({"type": "error", "content": item["error"]})
                        )
                        break
                    if isinstance(item, dict) and "__stats__" in item:
                        usage_tracker.track(
                            _provider_of(model_override or llm._model),
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
                    accumulated += item
                    await websocket.send_text(json.dumps({"type": "token", "content": item}))

                errors = await loop.run_in_executor(
                    None, _extract_errors, accumulated, question, answer
                )
                if errors:
                    session_errors.append({"question": question, "errors": errors})

                await websocket.send_text(json.dumps({"type": "done"}))

            elif msg_type == "next":
                current_index += 1
                if current_index >= len(questions):
                    flat = []
                    for item in session_errors:
                        q_short = item["question"][:60].rstrip()
                        for err in item["errors"]:
                            flat.append(f"[{q_short}…] {err}")
                    await websocket.send_text(
                        json.dumps({"type": "session_end", "errors": flat})
                    )

                    # Persist session to memory
                    try:
                        ctx = memory.get_context()
                        active_files = ctx.get("fichiers_actifs", [])
                        fichier = active_files[0] if active_files else ""
                        all_errors = []
                        for item in session_errors:
                            all_errors.extend(item["errors"])
                        réussies = len(questions) - len(session_errors)
                        await loop.run_in_executor(
                            None,
                            memory.add_session,
                            "kholle",
                            fichier,
                            all_errors,
                            réussies,
                            len(session_errors),
                        )
                        await loop.run_in_executor(None, memory.promote_lacunes)
                        # Non-blocking consolidation after kholle session
                        consol_data = {
                            "matière": "kholle",
                            "erreurs": all_errors,
                            "réussies": réussies,
                            "ratées": len(session_errors),
                        }
                        _use_cloud = memory.get_context().get("consolidation_cloud", False)
                        Thread(
                            target=lambda: consolidation_engine.consolidate_session(consol_data, _use_cloud),
                            daemon=True,
                        ).start()
                    except Exception:
                        logger.exception("Erreur sauvegarde session kholle en mémoire")
                else:
                    await websocket.send_text(json.dumps({
                        "type": "question",
                        "content": questions[current_index],
                        "index": current_index,
                        "total": len(questions),
                    }))

    except WebSocketDisconnect:
        pass
