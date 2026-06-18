"""
Routeur du module « rangement » — analyse et organisation intelligente de fichiers.

Utilise le LLM (local Ollama, NPU/FLM, ou cloud) pour catégoriser, résumer et
détecter les doublons dans un lot de fichiers. Fallback heuristique si le LLM
est indisponible ou si la réponse n'est pas exploitable.

Nouveautés v3 :
- Sélecteur de modèles (Ollama local + NPU/FLM)
- Streaming SSE en temps réel pour voir la réponse du LLM token par token

Contraintes respectées : seulement `from fastapi import APIRouter` + `router = APIRouter()`,
aucun subprocess/socket/importlib/os.system/eval/exec, aucun accès aux clés API.
Le backend.prefix vaut "" (router monté à la racine) → chaque route est préfixée
par /rangement pour éviter les collisions entre modules.
"""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Dict, List, Optional
from collections import defaultdict
from pathlib import Path
import asyncio
import json
import datetime
import re
import logging

router = APIRouter()

logger = logging.getLogger(__name__)

# ── Import LLM (via l'infrastructure standard d'Épure) ─────────────────────────
try:
    from core.runtime import llm
except Exception:
    llm = None

try:
    from core.runtime import SSE_HEADERS
except Exception:
    SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ── Modèles Pydantic ───────────────────────────────────────────────────────────

class FileInfo(BaseModel):
    name: str
    size: int
    relativePath: str
    extension: str


class AnalyzeRequest(BaseModel):
    files: List[FileInfo]
    theme: str = ""
    model: Optional[str] = None  # ex: "qwen2.5-coder:7b" (ollama) ou "flm:qwen3:4b" (NPU)


class AnalyzeResponse(BaseModel):
    plan: Dict[str, List[str]]
    summary: Dict[str, str]
    duplicates: Dict[str, List[str]]
    powershell_script: str


# ── Résolution du dossier backend (fonctionne en staging ET après activation) ──

def _backend_root() -> Path:
    """Remonte jusqu'au dossier contenant main.py (backend/)."""
    d = Path(__file__).resolve().parent
    for _ in range(10):
        if (d / "main.py").is_file():
            return d
        d = d.parent
    # Fallback : 3 niveaux au-dessus (modules/_staging/rangement → backend)
    return Path(__file__).resolve().parents[3]


_HISTORY_FILE = _backend_root() / "memory" / "rangement_history.json"


# ── Historique (persistance fichier JSON, 100 dernières entrées) ───────────────

def _load_history() -> list:
    try:
        if _HISTORY_FILE.is_file():
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_history(history: list) -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if len(history) > 100:
            history = history[-100:]
        _HISTORY_FILE.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _append_history(req: AnalyzeRequest, result: dict, model_used: str = "") -> None:
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "files_count": len(req.files),
        "theme": req.theme,
        "plan": result.get("plan", {}),
        "powershell_script": result.get("powershell_script", ""),
        "model_used": model_used or (req.model or "fallback"),
    }
    hist = _load_history()
    hist.append(entry)
    _save_history(hist)


# ── Heuristique de fallback (classification par extension) ─────────────────────

def _categorize(ext: str) -> str:
    ext = ext.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".svg", ".ico", ".raw"}:
        return "Images"
    if ext in {".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".odt", ".csv", ".xlsx", ".xls", ".pptx", ".ppt"}:
        return "Documents"
    if ext in {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}:
        return "Videos"
    if ext in {".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma"}:
        return "Audio"
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}:
        return "Archives"
    if ext in {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
               ".java", ".cpp", ".c", ".h", ".cs", ".rb", ".go", ".rs", ".swift",
               ".kt", ".php", ".sql", ".sh", ".bat", ".ps1"}:
        return "Code"
    return "Autres"


def _summarize(filename: str) -> str:
    if "." in filename:
        base = filename.rsplit(".", 1)[0]
    else:
        base = filename
    return base if len(base) <= 20 else base[:17] + "..."


def _find_duplicates(files: List[FileInfo]) -> Dict[str, List[str]]:
    """Détecte les doublons par nom + taille identiques."""
    groups: Dict[tuple, List[FileInfo]] = defaultdict(list)
    for f in files:
        key = (f.name.lower(), f.size)
        groups[key].append(f)
    dedup: Dict[str, List[str]] = {}
    for lst in groups.values():
        if len(lst) > 1:
            keep = lst[0]
            dedup[keep.relativePath] = [f.relativePath for f in lst[1:]]
    return dedup


def _build_powershell_script(
    plan: Dict[str, List[str]], duplicates: Dict[str, List[str]]
) -> str:
    """Génère un script PowerShell pour exécuter le plan de rangement."""
    lines = [
        "$source = Read-Host 'Chemin du dossier racine'",
        "if (-not (Test-Path $source)) { Write-Error 'Dossier introuvable' ; exit 1 }",
        "Set-Location $source",
        "",
        "# ---- Création des dossiers ----",
    ]
    for folder in plan:
        safe = folder.replace("'", "''")
        lines.append(f"New-Item -ItemType Directory -Force -Path '{safe}' | Out-Null")
    lines.append("")
    lines.append("# ---- Déplacement des fichiers ----")
    for folder, paths in plan.items():
        safe_folder = folder.replace("'", "''")
        for rel in paths:
            safe_rel = rel.replace("'", "''")
            lines.append(f"if (Test-Path '{safe_rel}') {{ Move-Item -Path '{safe_rel}' -Destination '{safe_folder}/' -Force }}")
    lines.append("")
    lines.append("# ---- Suppression des doublons ----")
    for keep_rel, dup_list in duplicates.items():
        for dup in dup_list:
            safe_dup = dup.replace("'", "''")
            lines.append(f"if (Test-Path '{safe_dup}') {{ Remove-Item -Path '{safe_dup}' -Force }}")
    lines.append("")
    lines.append("Write-Host 'Rangement terminé.'")
    return "\n".join(lines)


def _fallback_analysis(files: List[FileInfo]) -> dict:
    """Classification heuristique (extension) + résumés + doublons."""
    plan: Dict[str, List[str]] = {}
    summary: Dict[str, str] = {}
    for f in files:
        folder = _categorize(f.extension)
        plan.setdefault(folder, []).append(f.relativePath)
        summary[f.relativePath] = _summarize(f.name)
    duplicates = _find_duplicates(files)
    script = _build_powershell_script(plan, duplicates)
    return {
        "plan": plan,
        "summary": summary,
        "duplicates": duplicates,
        "powershell_script": script,
    }


# ── Analyse par LLM ────────────────────────────────────────────────────────────

def _build_llm_messages(files: List[FileInfo], theme: str) -> list:
    """Construit les messages system / user pour le LLM local."""
    desc_lines = []
    for f in files:
        desc_lines.append(
            f"- {f.relativePath}  (extension: {f.extension}, taille: {f.size} octets)"
        )
    desc = "\n".join(desc_lines)
    theme_line = f"\n\nThème souhaité par l'utilisateur : {theme}" if theme.strip() else ""

    system = (
        "Tu es un assistant expert en organisation de fichiers. Ta tâche est d'analyser "
        "une liste de fichiers et de proposer un plan de rangement intelligent. "
        "Tu réponds UNIQUEMENT par un objet JSON valide, sans aucun texte avant ni après. "
        "Le JSON doit avoir exactement ce format :\n"
        '{"plan": {"Dossier1": ["chemin/vers/fichier1", ...], ...}, '
        '"summary": {"chemin/vers/fichier1": "resume court", ...}, '
        '"duplicates": {"chemin/conserve": ["chemin/doublon1", ...]}}\n\n'
        "Règles :\n"
        "- plan : organiser les fichiers dans des dossiers logiques (Images, Documents, "
        "Videos, Audio, Archives, Code, Autres, ou des sous-dossiers plus spécifiques).\n"
        "- summary : pour chaque chemin, un résumé de 20 caractères max (sans l'extension).\n"
        "- duplicates : détecter les fichiers ayant le MÊME nom ET la MÊME taille. "
        "Pour chaque groupe, désigner un conservé (le premier) et lister les autres à supprimer.\n"
        f"- IMPORTANT : le tableau 'files' contient {len(files)} fichiers. "
        "CHAQUE fichier doit apparaître exactement une fois dans 'plan'."
    )

    user = f"Liste des fichiers à organiser :\n{desc}{theme_line}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_llm_response(raw: str, files: List[FileInfo]) -> Optional[dict]:
    """Parse la réponse JSON du LLM. Retourne None si invalide ou incomplète."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    # Retirer les éventuels marqueurs de bloc de code ```json ... ```
    if text.startswith("```"):
        start = text.find("\n")
        end = text.rfind("```")
        if start != -1 and end != -1:
            text = text[start:end].strip()
        elif end != -1:
            text = text[:end].strip()
        if text.startswith("```"):
            text = text[3:].strip()

    # Première tentative : parser tel quel
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Seconde tentative : extraire le premier objet JSON dans le texte
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        else:
            return None

    if not isinstance(data, dict):
        return None

    plan = data.get("plan", {})
    summary = data.get("summary", {})
    duplicates = data.get("duplicates", {})

    if not isinstance(plan, dict) or not isinstance(summary, dict) or not isinstance(duplicates, dict):
        return None

    # Vérification sommaire : le plan doit contenir des fichiers
    total_in_plan = sum(len(v) for v in plan.values() if isinstance(v, list))
    if total_in_plan == 0 and len(files) > 0:
        # Le LLM n'a rien classé → fallback
        return None

    script = _build_powershell_script(plan, duplicates)
    return {
        "plan": plan,
        "summary": summary,
        "duplicates": duplicates,
        "powershell_script": script,
    }


# ── Modèles disponibles (Ollama local + NPU/FLM) ───────────────────────────────

def _get_available_models() -> list:
    """Liste tous les modèles disponibles : Ollama local + FLM/NPU."""
    models: list = []

    # ── Ollama local ──────────────────────────────────────────────────────────
    try:
        from core.models import get_ollama_installed
        ollama_models = get_ollama_installed()
        if ollama_models:
            for name in ollama_models:
                models.append({
                    "id": name,
                    "nom": name,
                    "provider": "ollama",
                    "type": "local",
                    "gratuit": True,
                    "description": "Modèle local Ollama (CPU/GPU)",
                    "available": True,
                })
    except Exception:
        logger.warning("Impossible de lister les modèles Ollama", exc_info=True)

    # ── FLM / NPU ────────────────────────────────────────────────────────────
    try:
        from core.models import get_flm_installed, check_flm, FLM_MODELS_STATIC
        if check_flm():
            installed = get_flm_installed()
            for meta in FLM_MODELS_STATIC:
                model_id_short = meta["id"].split("flm:", 1)[1] if meta["id"].startswith("flm:") else meta["id"]
                is_available = model_id_short in installed if installed else None
                models.append({
                    "id": meta["id"],
                    "nom": meta["nom"],
                    "provider": "flm",
                    "type": "npu",
                    "gratuit": True,
                    "description": meta.get("description", ""),
                    "available": is_available if is_available is not None else True,
                })
        else:
            # FLM non joignable → on liste les modèles statiques comme indisponibles
            for meta in FLM_MODELS_STATIC:
                models.append({
                    "id": meta["id"],
                    "nom": meta["nom"],
                    "provider": "flm",
                    "type": "npu",
                    "gratuit": True,
                    "description": meta.get("description", ""),
                    "available": False,
                })
    except Exception:
        logger.warning("Impossible de lister les modèles FLM/NPU", exc_info=True)

    return models


# ═══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/rangement/models")
async def list_models():
    """Liste les modèles disponibles pour l'analyse (Ollama local + NPU/FLM)."""
    loop = asyncio.get_running_loop()
    models = await loop.run_in_executor(None, _get_available_models)
    return {"models": models}


@router.post("/rangement/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    """Analyse un lot de fichiers et propose un plan de rangement.

    Utilise le LLM sélectionné (req.model) si disponible, sinon fallback heuristique.
    """
    if not req.files:
        return AnalyzeResponse(
            plan={}, summary={}, duplicates={}, powershell_script=""
        )

    result = None
    model_used = req.model or "défaut"

    if llm is not None:
        try:
            messages = _build_llm_messages(req.files, req.theme)
            loop = asyncio.get_running_loop()

            def _generate():
                return llm.generate(messages, model=req.model)

            raw = await loop.run_in_executor(None, _generate)
            result = _parse_llm_response(raw, req.files)
            if result:
                model_used = req.model or "ollama:défaut"
        except Exception:
            logger.exception("Échec LLM (model=%s), fallback heuristique", req.model)

    if result is None:
        result = _fallback_analysis(req.files)
        model_used = "fallback"

    _append_history(req, result, model_used)
    return AnalyzeResponse(**result)


@router.post("/rangement/analyze-stream")
async def analyze_stream(req: AnalyzeRequest):
    """Analyse un lot de fichiers avec streaming SSE en temps réel.

    Le client reçoit les événements suivants :
    - ``status`` : message d'étape (ex: "Envoi au LLM…", "Analyse terminée")
    - ``token``  : fragment de texte produit par le LLM en temps réel
    - ``result`` : résultat final parsé (plan, summary, duplicates, script)
    - ``error``  : erreur survenue (puis fallback exécuté)
    - ``done``   : signal de fin du flux
    """

    async def _stream():
        def _sse(event: str, data: dict) -> str:
            payload = json.dumps(data, ensure_ascii=False)
            return f"event: {event}\ndata: {payload}\n\n"

        if not req.files:
            yield _sse("result", {"plan": {}, "summary": {}, "duplicates": {}, "powershell_script": ""})
            yield _sse("done", {})
            return

        # ── Si pas de LLM → fallback direct ──────────────────────────────────
        if llm is None:
            yield _sse("status", {"message": "LLM indisponible — classification heuristique"})
            await asyncio.sleep(0)
            result = _fallback_analysis(req.files)
            _append_history(req, result, "fallback")
            yield _sse("result", result)
            yield _sse("done", {})
            return

        # ── Streaming LLM ────────────────────────────────────────────────────
        messages = _build_llm_messages(req.files, req.theme)
        model_label = req.model or "défaut"
        yield _sse("status", {"message": f"Envoi au LLM ({model_label})…"})
        await asyncio.sleep(0)

        full_response: str = ""
        stream_error: Optional[str] = None

        try:
            loop = asyncio.get_running_loop()

            # On lance llm.stream() dans un thread séparé et on récupère les
            # tokens via une queue asyncio pour ne pas bloquer la boucle d'events.
            queue: asyncio.Queue = asyncio.Queue()

            def _run_stream():
                try:
                    for chunk in llm.stream(messages, model=req.model):
                        # Les chunks __stats__ sont des métadonnées, pas du texte
                        if isinstance(chunk, dict) and chunk.get("__stats__"):
                            continue
                        if chunk:
                            # Utilise call_soon_threadsafe pour injecter dans
                            # la boucle asyncio depuis le thread d'exécution.
                            loop.call_soon_threadsafe(queue.put_nowait, ("token", chunk))
                    loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

            # Démarre le stream dans un thread (llm.stream() est bloquant)
            executor_task = asyncio.ensure_future(
                loop.run_in_executor(None, _run_stream)
            )

            # Consomme la queue et yield les événements SSE.
            # HEARTBEAT : tant que le LLM n'a rien produit (chargement du modèle en
            # RAM au 1er appel = plusieurs secondes muettes), on émet un commentaire
            # SSE (`: ...`, ignoré par le client) à intervalle court. Sans ça, la
            # webview ferme une connexion streaming restée silencieuse → « network
            # error » côté frontend. On garde un plafond global d'attente.
            HEARTBEAT_S = 4.0
            MAX_WAIT_S = 300.0
            waited = 0.0
            while True:
                try:
                    msg_type, payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    waited += HEARTBEAT_S
                    if waited >= MAX_WAIT_S:
                        yield _sse("error", {"message": "Timeout : le LLM n'a pas répondu à temps"})
                        break
                    yield ": keep-alive\n\n"  # commentaire SSE — maintient la connexion ouverte
                    yield _sse("status", {"message": "Chargement du modèle / génération en cours…"})
                    continue
                waited = 0.0

                if msg_type == "token":
                    full_response += payload
                    yield _sse("token", {"text": payload})
                elif msg_type == "error":
                    stream_error = payload
                    yield _sse("status", {"message": f"Erreur LLM : {payload}"})
                    break
                elif msg_type == "done":
                    break

            # Nettoie le executor_task s'il est toujours en cours
            if not executor_task.done():
                executor_task.cancel()
                try:
                    await executor_task
                except Exception:
                    pass

        except Exception as exc:
            stream_error = str(exc)
            logger.exception("Erreur streaming LLM")

        # ── Parse la réponse complète ────────────────────────────────────────
        if stream_error:
            yield _sse("status", {"message": "LLM en erreur — fallback heuristique"})
            await asyncio.sleep(0)
            result = _fallback_analysis(req.files)
            _append_history(req, result, "fallback")
            yield _sse("result", result)
            yield _sse("done", {})
            return

        yield _sse("status", {"message": "Analyse du JSON reçu…"})
        await asyncio.sleep(0)

        result = _parse_llm_response(full_response, req.files)

        if result is None:
            yield _sse("status", {"message": "JSON invalide du LLM — fallback heuristique"})
            await asyncio.sleep(0)
            result = _fallback_analysis(req.files)
            model_used = "fallback"
        else:
            model_used = req.model or "défaut"

        _append_history(req, result, model_used)
        yield _sse("result", result)
        yield _sse("done", {})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/rangement/history")
async def get_history():
    """Retourne l'historique des analyses (100 dernières entrées)."""
    hist = _load_history()
    return {"history": hist[-100:]}


@router.post("/rangement/deduplicate")
async def dedup(files: List[FileInfo]):
    """Détecte les doublons sans reclasser (appel rapide, sans LLM)."""
    return {"duplicates": _find_duplicates(files)}
