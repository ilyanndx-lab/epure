"""Routeur du module Réglages / plateforme. Monté avec prefix "" : il regroupe
les endpoints transverses qui sous-tendent l'UI Réglages et les services
partagés (voix, fichiers, mémoire, contexte, quotas, clés API, presets
orchestrateur). Les chemins sont conservés tels quels (aucun changement d'API).

Moteurs partagés injectés via core.runtime.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from typing import Optional

import pypdf
from dotenv import load_dotenv, set_key as dotenv_set_key
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import catalogue as _catalogue
from core.codeagent import SecurityError
from core.embedding_install import declencher_installation, etat_installation
from core.instance import fiches_root, instance_config, modele_local_defaut
from core.paths import PathOutsideDataError, resolve_user_path, safe_upload_name
from core.rag import SUPPORTED_EXTENSIONS, RAGEngine
from core.runtime import (
    API_KEY_NAMES,
    PIPER_VOICE,
    SSE_HEADERS,
    consolidation_engine,
    llm,
    memory,
    models_registry,
    orchestrator,
    piper,
    rag,
    usage_tracker,
    whisper,
)
from core.voice import VoiceModelUnavailable, capacites_vocales, etat_modele_vocal

logger = logging.getLogger(__name__)

router = APIRouter()

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"
#: Extensions acceptées à l'upload. **Importées, plus recopiées** : c'était une
#: seconde liste pour la même notion, et elle avait déjà divergé de rien — mais
#: l'ajout de `.pptx`/`.xlsx` demandait de penser à deux endroits, et un fichier
#: accepté ici que `_extract_text_from_path` ne sait pas lire s'indexe à vide, en
#: silence. Une seule liste ne peut pas diverger d'elle-même.
_SUPPORTED_EXT = SUPPORTED_EXTENSIONS


# ── Voice ────────────────────────────────────────────────────────────────────

class SynthesizeRequest(BaseModel):
    text: str
    voice: str = "fr_FR-upmc-medium"


@router.get("/voice/capabilities")
async def voice_capabilities():
    """Disponibilité de la transcription et de la synthèse, avant tout clic.

    Même rôle que `key_ok` pour les fournisseurs cloud dans `main.py` : l'interface
    doit pouvoir MASQUER un contrôle plutôt que l'afficher pour qu'il échoue. Sur
    Windows ARM64 la voix est déclarée indisponible (décision du 2026-08-22,
    `docs/remplacement-vectoriel.md`) — les paquets ne sont pas installés, et un
    micro affiché n'y rend qu'un 503 à chaque appui.

    Endpoint distinct de `/models` — où vit la disponibilité des fournisseurs
    cloud — et pas par goût de la symétrie : `/models` interroge quatre API
    distantes (`core/models.py`, timeout 4 s chacune). Faire dépendre l'affichage
    d'un bouton micro d'un aller-retour réseau serait payer une question de
    réseau pour une réponse qui est sur le disque local.

    Distinct aussi de `/voice/model`, qui répond d'une autre question : celui-là
    dit si le modèle de 76 Mo est là, celui-ci si le code capable de le lire
    existe. Un paquet absent ne s'installe pas en cliquant ; un modèle manquant,
    si.
    """
    return capacites_vocales()


@router.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    loop = asyncio.get_running_loop()
    try:
        # lambda : le 1er appel construit le modèle (lazy) DANS l'executor, sans
        # bloquer la boucle d'événements.
        text = await loop.run_in_executor(None, lambda: whisper.transcribe(audio_bytes))
    except VoiceModelUnavailable as exc:
        # Même traitement que /voice/synthesize, et pour la même raison : dans une
        # app local-first la voix est optionnelle, son indisponibilité est un état
        # prévu, pas une panne. Le message part tel quel — il dit si le paquet
        # faster-whisper manque ou si le modèle n'a pas pu être récupéré, et on ne
        # peut rien faire d'un « Erreur transcription » nu.
        #
        # `logger.warning` et non `logger.exception` : une dépendance absente
        # n'est pas un incident à tracer sur une pile complète. C'est ce que
        # faisait la branche générique ci-dessous, faute que cette branche existe.
        logger.warning("Transcription vocale indisponible : %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        logger.exception("Erreur transcription /voice/transcribe")
        raise HTTPException(status_code=500, detail="Erreur transcription")
    return {"text": text}


@router.get("/voice/model")
async def voice_model():
    """État du modèle de synthèse — sert à prévenir AVANT 77 Mo de téléchargement.

    Le frontend l'interroge avant la première synthèse : le modèle n'est plus
    versionné, il arrive au premier usage. Demander l'état au moteur lui-même
    serait contradictoire — le construire déclenche le téléchargement qu'on veut
    annoncer.
    """
    return etat_modele_vocal(PIPER_VOICE)


@router.post("/voice/synthesize")
async def voice_synthesize(req: SynthesizeRequest):
    loop = asyncio.get_running_loop()
    try:
        wav_bytes = await loop.run_in_executor(None, piper.synthesize, req.text)
    except VoiceModelUnavailable as exc:
        # 503 et pas 500 : dans une app local-first la voix est optionnelle, son
        # indisponibilité est un état prévu. Le message part tel quel — il dit
        # s'il manque le réseau, si l'empreinte a divergé ou si piper-tts est
        # absent, et on ne peut rien faire d'un « Erreur synthèse vocale » nu.
        logger.warning("Synthèse vocale indisponible : %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        logger.exception("Erreur synthèse /voice/synthesize")
        raise HTTPException(status_code=500, detail="Erreur synthèse vocale")
    return StreamingResponse(io.BytesIO(wav_bytes), media_type="audio/wav")


# ── Quota / Usage ────────────────────────────────────────────────────────────

@router.get("/quota/usage")
async def quota_usage():
    return usage_tracker.get_usage()


@router.post("/quota/reset/{provider}")
async def quota_reset(provider: str):
    ok = usage_tracker.reset(provider)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Provider inconnu : {provider}")
    return {"ok": True}


@router.get("/quota/deepseek-balance")
def deepseek_balance():
    """Crédit DeepSeek en temps réel via l'API officielle (GET /user/balance).

    DeepSeek est une API payante : on suit le solde restant (pas des tokens/req).
    Réponse : {ok, is_available, balances:[{currency,total_balance,...}], raison?}.
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return {"ok": False, "raison": "DEEPSEEK_API_KEY non configurée dans les Réglages."}
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) epure/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "ok": True,
            "is_available": bool(data.get("is_available", False)),
            "balances": data.get("balance_infos", []),
        }
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "raison": f"Clé DeepSeek refusée (HTTP {exc.code})."}
        return {"ok": False, "raison": f"Erreur DeepSeek (HTTP {exc.code})."}
    except Exception as exc:
        logger.warning("DeepSeek balance: %s", exc)
        return {"ok": False, "raison": f"Solde DeepSeek indisponible : {exc}"}


# ── Settings — API keys ──────────────────────────────────────────────────────

class ApiKeysRequest(BaseModel):
    GEMINI_API_KEY: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None
    CEREBRAS_API_KEY: Optional[str] = None
    MISTRAL_API_KEY: Optional[str] = None
    NVIDIA_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None


@router.get("/settings/api-keys")
async def api_keys_get():
    return {k: bool(os.environ.get(k, "").strip()) for k in API_KEY_NAMES}


@router.put("/settings/api-keys")
async def api_keys_put(req: ApiKeysRequest):
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _ENV_FILE.exists():
        _ENV_FILE.write_text("", encoding="utf-8")
    updated = False
    for k in API_KEY_NAMES:
        val = getattr(req, k, None)
        if val is not None:
            dotenv_set_key(str(_ENV_FILE), k, val)
            updated = True
    if updated:
        load_dotenv(str(_ENV_FILE), override=True)
        llm.reload_dotenv()
        models_registry.invalidate()
    return {"ok": True}


# ── Memory ───────────────────────────────────────────────────────────────────

@router.get("/memory/profile")
async def memory_profile_get():
    return memory.load_profile()


@router.put("/memory/profile")
async def memory_profile_put(request: Request):
    data = await request.json()
    memory.save_profile(data)
    return {"ok": True}


@router.get("/memory/sessions")
async def memory_sessions_get():
    return {"sessions": memory.get_all_sessions()}


class ArchiveRequest(BaseModel):
    dates: list[str]


@router.post("/memory/sessions/archive")
async def memory_sessions_archive(req: ArchiveRequest):
    memory.archive_sessions(req.dates)
    return {"ok": True}


class AddSessionRequest(BaseModel):
    matiere: str
    fichier: str = ""
    erreurs: list = []
    reussies: int = 0
    ratees: int = 0


@router.post("/memory/sessions")
async def memory_session_add(req: AddSessionRequest):
    memory.add_session(req.matiere, req.fichier, req.erreurs, req.reussies, req.ratees)
    return {"ok": True}


@router.get("/memory/context")
async def memory_context_get():
    loop = asyncio.get_running_loop()
    profile = await loop.run_in_executor(None, memory.load_profile)
    sessions = await loop.run_in_executor(None, memory.get_all_sessions)
    forces = profile.get("forces", [])
    lacunes = profile.get("lacunes_confirmées", [])
    style = profile.get("préférences_interaction", {}).get("style", "")
    consol_log = await loop.run_in_executor(None, consolidation_engine.get_log, 1)
    last_consol = consol_log[0]["date"][:10] if consol_log else "jamais"
    lines = ["📊 Profil apprenant :"]
    lines.append(f"Forces : {', '.join(forces[:5])}" if forces else "Forces : (aucune enregistrée)")
    lines.append(f"Lacunes confirmées : {', '.join(lacunes[:5])}" if lacunes else "Lacunes : (aucune confirmée)")
    if style:
        lines.append(f"Style : {style}")
    lines.append(f"Dernière consolidation : {last_consol}")
    lines.append(f"Sessions totales : {len(sessions)}")
    return {"context": "\n".join(lines)}


@router.get("/memory/lacunes")
async def memory_lacunes_get():
    loop = asyncio.get_running_loop()
    profile = await loop.run_in_executor(None, memory.load_profile)
    sessions = await loop.run_in_executor(None, memory.get_sessions, 7)
    lacunes = profile.get("lacunes_confirmées", [])
    errors: list[dict] = []
    for s in sessions[-20:]:
        for e in s.get("erreurs", []):
            errors.append({"date": s.get("date", ""), "erreur": e})
    return {"lacunes": lacunes, "erreurs_recentes": errors}


# ── Context / Settings ───────────────────────────────────────────────────────

@router.get("/context")
async def context_get():
    return memory.get_context()


@router.patch("/context/settings")
async def context_settings(request: Request):
    body = await request.json()
    allowed = {"modèle_actif", "strict_mode", "session_instruction", "consolidation_cloud",
               "orchestrateur_actif", "raisonnement"}
    filtered = {k: v for k, v in body.items() if k in allowed}
    memory.update_context(**filtered)
    return {"ok": True}


# ── Files ────────────────────────────────────────────────────────────────────

async def _stream_load_sse(paths: list[str]):
    """Async generator: index files, stream summary tokens as SSE, send done event."""
    loop = asyncio.get_running_loop()
    total_pages = 0
    text_parts: list[str] = []
    indexed_paths: list[str] = []

    for path in paths:
        ext = Path(path).suffix.lower()
        if ext not in _SUPPORTED_EXT:
            logger.warning("Extension non supportée : %s", path)
            continue
        if not os.path.exists(path):
            logger.warning("Fichier non trouvé : %s", path)
            continue
        try:
            await loop.run_in_executor(None, rag.index_file, path)
            text = await loop.run_in_executor(None, RAGEngine.read_file_text, path)
            text_parts.append(text[:3000])
            if ext == '.pdf':
                reader = pypdf.PdfReader(path)
                total_pages += len(reader.pages)
            indexed_paths.append(path)
        except Exception:
            logger.exception("Erreur chargement fichier %s", path)

    memory.update_context(fichiers_actifs=indexed_paths, résumé_contexte="")

    accumulated = ""
    if text_parts:
        combined = "\n\n---\n\n".join(text_parts)[:12000]
        prompt = (
            "Résume en 100-150 mots maximum ces documents de cours. "
            "Indique les sujets principaux et les notions clés. Sois factuel.\n\n"
            f"Contenu :\n{combined}"
        )
        # TOUJOURS LOCAL, et sans option cloud — décision explicite.
        #
        # Ce résumé n'est pas demandé : il part automatiquement à l'import d'un
        # fichier, dans le même flux SSE que l'indexation. Il tombe donc
        # directement sous la règle « pas de cloud sans choix explicite pour cette
        # tâche précise » : il n'y a pas de choix à faire, donc pas de cloud, donc
        # pas de paramètre `use_cloud` à offrir. Un drapeau ici serait une option
        # que rien ne peut poser, sur le seul chemin où l'utilisateur n'a rien
        # décidé.
        #
        # Ce qu'il envoyait avant : le CONTENU des fichiers qu'on vient d'importer,
        # au fournisseur cloud actif dans le chat.
        model_override = modele_local_defaut()
        queue: asyncio.Queue = asyncio.Queue()

        def _worker(msgs, q, lp, model):
            try:
                for token in llm.stream(msgs, model=model):
                    # Résumé fichiers : texte uniquement. Écarte les sentinelles
                    # dict (__stats__, __reasoning__) — sinon `accumulated += item`
                    # plus bas lèverait un TypeError.
                    if not isinstance(token, str):
                        continue
                    asyncio.run_coroutine_threadsafe(q.put(token), lp)
            except Exception as exc:
                logger.exception("Erreur streaming résumé fichiers")
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
                break
            accumulated += item
            yield f"data: {json.dumps({'type': 'token', 'content': item}, ensure_ascii=False)}\n\n"

    memory.update_context(résumé_contexte=accumulated)

    chunks_count = 0
    if indexed_paths:
        try:
            result = rag._col.get(
                where={"source": {"$in": indexed_paths}}, include=[]
            )
            chunks_count = len(result.get("ids", []))
        except Exception:
            logger.exception("Erreur comptage chunks")

    yield f"data: {json.dumps({'type': 'done', 'pages': total_pages, 'chunks': chunks_count})}\n\n"


class LoadFilesRequest(BaseModel):
    paths: list[str]


@router.post("/files/load")
async def files_load(req: LoadFilesRequest):
    """Indexe des fichiers déjà sur le disque, par chemin.

    Second point d'entrée du même motif que /files/upload, en lecture : les
    chemins viennent du client et `.json` est un type supporté, donc
    ``paths=["…/backend/memory/instance_config.json"]`` faisait entrer le token
    d'API dans le RAG *et* dans le résumé renvoyé en SSE. Confinement AVANT
    d'ouvrir le flux — le refus doit être un code HTTP, pas un événement perdu
    au milieu d'un stream déjà commencé.
    """
    try:
        paths = [str(resolve_user_path(p)) for p in req.paths]
    except PathOutsideDataError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return StreamingResponse(
        _stream_load_sse(paths), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/files/upload")
async def files_upload(files: list[UploadFile] = File(...)):
    """Dépose des fiches dans la racine des fiches, puis les indexe.

    ``upload.filename`` vient du client. ``_fiches_dir / filename`` acceptait
    donc une traversée, et comme ``.json`` est un type supporté (légitimement,
    pour les fiches), un envoi nommé ``../../backend/memory/instance_config.json``
    réécrivait la configuration d'instance — **donc le token d'API**, avec une
    valeur choisie par l'attaquant. D'où ``safe_upload_name`` puis la
    re-vérification du chemin résolu.
    """
    _fiches_dir = fiches_root()
    _fiches_dir.mkdir(parents=True, exist_ok=True)
    racine = _fiches_dir.resolve()
    saved_paths: list[str] = []
    for upload in files:
        try:
            filename = safe_upload_name(upload.filename, "upload.bin")
        except PathOutsideDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        ext = Path(filename).suffix.lower()
        if ext not in _SUPPORTED_EXT:
            continue
        dest = (_fiches_dir / filename).resolve()
        if not dest.is_relative_to(racine):
            raise HTTPException(status_code=400, detail="Nom de fichier invalide")
        content = await upload.read()
        dest.write_bytes(content)
        saved_paths.append(str(dest))
    if not saved_paths:
        raise HTTPException(
            status_code=400,
            # Dérivé de la liste, jamais réécrit : ce message énumérait les types
            # à la main et devenait faux au premier ajout — en promettant moins
            # que ce que le code accepte, ce qui se lit comme un refus légitime.
            detail=("Types supportés : "
                    + ", ".join(sorted(e.lstrip(".").upper() for e in _SUPPORTED_EXT))),
        )
    return StreamingResponse(
        _stream_load_sse(saved_paths), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/files/active")
async def files_active():
    return memory.get_context()


@router.delete("/files/active")
async def files_active_delete():
    memory.update_context(fichiers_actifs=[], résumé_contexte="")
    return {"ok": True}


# ── RAG ──────────────────────────────────────────────────────────────────────

@router.get("/rag/capabilities")
async def rag_capabilities():
    """La recherche documentaire est-elle prête, et sinon où en est-elle ?

    Même rôle que `/voice/capabilities` : répondre AVANT que l'utilisateur
    clique, pour que l'interface explique au lieu d'afficher une erreur. La
    différence est la seule qui compte — un paquet vocal absent ne s'installe pas
    en cliquant (aucune wheel `win_arm64`), la pile d'embedding si. D'où un état
    à quatre valeurs (`absent` / `en_cours` / `prêt` / `échec`) et non un booléen,
    et une `cause` qui distingue « pas de réseau » d'un vrai échec de `pip` —
    parce que ce n'est pas la même chose à dire à quelqu'un.

    **Ne déclenche rien.** C'est une lecture (`find_spec` + un fichier d'état),
    et c'est délibéré : le frontend interroge cette route en boucle pendant
    l'installation, elle ne doit surtout pas en lancer une seconde. Le
    déclenchement appartient à `VectorStore.__init__`, donc aux routes qui ont
    réellement besoin du moteur.
    """
    return etat_installation()


@router.post("/rag/install")
async def rag_install():
    """Relance explicitement la préparation du moteur, après un échec.

    Une tentative automatique par process seulement (cf.
    `core/embedding_install.py`) : sans ça, chaque appel concurrent relancerait
    un `pip install torch`. La conséquence est qu'un échec reste affiché jusqu'à
    ce qu'on redemande — et la cause la plus probable, une connexion absente, se
    corrige en dehors de l'application. L'utilisateur est donc le seul à savoir
    quand réessayer, d'où ce bouton plutôt qu'une boucle de réessais.

    Ne relance jamais par-dessus une installation en cours : `explicite=True`
    lève la garde du « déjà tenté », pas celle du « déjà en train ».
    """
    return declencher_installation(explicite=True)


@router.get("/rag/files")
async def rag_files():
    """Fichiers indexés. Peut répondre 503 — c'est un état, pas une panne.

    `rag` est un `_LazyEngine` : ce premier accès construit `RAGEngine`, donc un
    `VectorStore`, qui a besoin de `sentence_transformers`. Absent (le cas de tout
    paquet livré), il lève `EmbeddingIndisponible` — traduite en 503 avec l'état
    d'avancement par le gestionnaire de `main.py`, et non plus en 500
    « ImportError » dont le corps n'a même pas de champ `files`.
    """
    loop = asyncio.get_running_loop()
    files = await loop.run_in_executor(None, rag.get_indexed_files)
    return {"files": files}


# ── Orchestrator presets ─────────────────────────────────────────────────────

class PresetCreateRequest(BaseModel):
    nom: str
    effort: str
    steps: list[dict]


@router.get("/orchestrator/presets")
async def orchestrator_presets_list():
    loop = asyncio.get_running_loop()
    presets = await loop.run_in_executor(None, orchestrator.get_presets)
    return {"presets": presets}


@router.post("/orchestrator/presets")
async def orchestrator_presets_create(req: PresetCreateRequest):
    loop = asyncio.get_running_loop()
    preset = await loop.run_in_executor(None, orchestrator.create_preset, req.nom, req.effort, req.steps)
    return preset


@router.delete("/orchestrator/presets/{preset_id}")
async def orchestrator_presets_delete(preset_id: str):
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, orchestrator.delete_preset, preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Preset introuvable ou preset par défaut")
    return {"ok": True}


@router.post("/memory/consolidate")
async def memory_consolidate(request: Request):
    body = await request.json()
    use_cloud = bool(body.get("use_cloud", False))
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, consolidation_engine.consolidate_all, use_cloud)
    return result


@router.get("/memory/consolidation-log")
async def memory_consolidation_log():
    loop = asyncio.get_running_loop()
    log = await loop.run_in_executor(None, consolidation_engine.get_log)
    return {"log": log}


# ── Atelier : tests de connectivité des moteurs ───────────────────────────────

@router.post("/settings/test/aider")
def test_aider():
    """Teste que le binaire `aider` (atelier.aider_path) répond à --version."""
    atelier = instance_config.get().get("atelier") or {}
    ap = (atelier.get("aider_path") or "aider").strip()
    bin_path = shutil.which(ap) or shutil.which(ap + ".cmd") or (ap if os.path.exists(ap) else None)
    if not bin_path:
        return {"ok": False, "version": "", "raison": f"Binaire '{ap}' introuvable dans le PATH."}
    try:
        r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0
        version = (r.stdout or r.stderr or "").strip().splitlines()[0] if ok else ""
        return {"ok": ok, "version": version, "raison": "" if ok else r.stderr.strip()[:200]}
    except Exception as exc:
        return {"ok": False, "version": "", "raison": str(exc)}


@router.post("/settings/test/gateway")
def test_gateway():
    """Teste que la passerelle claude_gateway est joignable."""
    from core.module_workshop import gateway_reachable, _gateway_cfg

    gw = _gateway_cfg()
    url = gw["base_url"]
    ok = gateway_reachable(url)
    return {"ok": ok, "url": url, "raison": "" if ok else f"Passerelle injoignable : {url}"}


@router.post("/settings/gateway/start")
def gateway_start():
    """Démarre la passerelle via atelier.gateway.start_command (process détaché)."""
    from core.module_workshop import start_gateway

    return start_gateway()


# Providers cloud → clé API qui les active (réutilise les listes statiques de
# core.models comme source des modèles, plutôt que de les redéfinir).
_PROVIDER_KEY = {
    "nvidia": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Fallback Gemini si core.models ne l'expose pas (ne devrait pas arriver).
_GEMINI_FALLBACK = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro"]


@router.get("/settings/provider-models")
def provider_models():
    """Modèles disponibles par provider cloud, limités aux providers dont la clé
    API est définie dans l'environnement. Les IDs suivent la convention Épure
    `provider:model_id` (ex. "gemini:gemini-2.0-flash")."""
    from core import models as _models
    from core.models import (
        _NVIDIA_STATIC, _GROQ_STATIC, _CEREBRAS_STATIC, _MISTRAL_STATIC, _DEEPSEEK_STATIC,
    )

    provider_static = {
        "nvidia": _NVIDIA_STATIC,
        "groq": _GROQ_STATIC,
        "cerebras": _CEREBRAS_STATIC,
        "mistral": _MISTRAL_STATIC,
        "gemini": getattr(_models, "_GEMINI_STATIC", None) or _GEMINI_FALLBACK,
        "deepseek": _DEEPSEEK_STATIC,
    }
    result: dict[str, list[str]] = {}
    for provider, models in provider_static.items():
        key_name = _PROVIDER_KEY.get(provider, "")
        if os.environ.get(key_name, "").strip():
            result[provider] = [f"{provider}:{mid}" for mid in models]
    return {"providers": result}


# ── Catalogue de modules installables ────────────────────────────────────────
# La logique vit dans core.catalogue, qui réutilise les helpers éprouvés de
# module_workshop (confinement d'id, sauvegarde horodatée, (dé)montage à chaud).
# Ici : seulement le contrat HTTP et la traduction des refus en 400/404.
#
# `request.app` et non un import de `main` : le routeur est monté SUR l'app, il
# ne doit pas la connaître par en haut — un import de main depuis un module
# créerait un cycle et casserait le montage isolé visé par le chantier §7.


@router.get("/settings/catalogue")
async def catalogue_list():
    """Manifestes de modules-catalogue/, chacun avec `installé: bool`. Lecture seule."""
    return {"modules": _catalogue.list_catalogue()}


@router.post("/settings/catalogue/{module_id}/install")
async def catalogue_install(module_id: str, request: Request):
    """Installe un module du catalogue : copie, activation, montage à chaud."""
    try:
        return _catalogue.install(module_id, app=request.app)
    except SecurityError as exc:
        logger.warning("SECURITY: installation refusée — %r", module_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _catalogue.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/settings/modules/{module_id}")
async def module_delete(module_id: str, request: Request):
    """Supprime un module installé. Destructif : sauvegarde AVANT effacement.

    Refuse un id malformé (SecurityError → 400), un module du cœur ou non
    supprimable, et un id inconnu.
    """
    try:
        return _catalogue.uninstall(module_id, app=request.app)
    except SecurityError as exc:
        logger.warning("SECURITY: suppression refusée — %r", module_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _catalogue.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
