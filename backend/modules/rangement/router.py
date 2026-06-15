from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
from collections import defaultdict
import os
import json
import asyncio
import re

router = APIRouter()

# ---------- Modèles ----------
class FileInfo(BaseModel):
    name: str
    size: int
    relativePath: str
    extension: str

class AnalyzeRequest(BaseModel):
    files: List[FileInfo]
    theme: str = ""
    model: Optional[str] = None         # nom du fichier modèle sélectionné

class AnalyzeResponse(BaseModel):
    plan: Dict[str, List[str]]
    summary: Dict[str, str]
    duplicates: Dict[str, List[str]]
    powershellScript: str
    error: Optional[str] = None

class DedupResponse(BaseModel):
    duplicates: Dict[str, List[str]]

# ---------- Constantes ----------
MODELS_DIR = r"C:\Users\Ilyan\.flm\models"

# ---------- LLM NPU optionnel ----------
try:
    import flm
except ImportError:
    flm = None

# ---------- Utilitaires (mêmes que le code d'origine) ----------
def _categorize(ext: str) -> str:
    ext = ext.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}:
        return "Images"
    if ext in {".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".odt"}:
        return "Documents"
    if ext in {".mp4", ".avi", ".mov", ".mkv"}:
        return "Videos"
    if ext in {".mp3", ".wav", ".ogg"}:
        return "Audio"
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        return "Archives"
    if ext in {".py", ".js", ".ts", ".html", ".css", ".java", ".cpp", ".cs", ".rb"}:
        return "Code"
    return "Autres"

def _summarize(filename: str) -> str:
    if "." in filename:
        base = filename.rsplit(".", 1)[0]
    else:
        base = filename
    return base if len(base) <= 20 else base[:17] + "..."

def _find_duplicates(files: List[FileInfo]) -> Dict[str, List[str]]:
    groups = defaultdict(list)
    for f in files:
        key = (f.name.lower(), f.size)
        groups[key].append(f)
    dedup = {}
    for lst in groups.values():
        if len(lst) > 1:
            keep = lst[0]
            dedup[keep.relativePath] = [f.relativePath for f in lst[1:]]
    return dedup

def _build_powershell_script(plan: Dict[str, List[str]], duplicates: Dict[str, List[str]]) -> str:
    lines = [
        "$source = Read-Host 'Enter the root folder path'",
        "Set-Location $source",
        "# ---- Création des dossiers ----",
    ]
    for folder in plan.keys():
        lines.append(f"New-Item -ItemType Directory -Force -Path '{folder}'")
    lines.append("\n# ---- Déplacement des fichiers ----")
    for folder, paths in plan.items():
        for rel in paths:
            lines.append(f"Move-Item -Path '{rel}' -Destination '{folder}/'")
    lines.append("\n# ---- Suppression des doublons ----")
    for keep_rel, dup_list in duplicates.items():
        for dup in dup_list:
            lines.append(f"Remove-Item -Path '{dup}' -Force")
    return "\n".join(lines)

def _fallback_analysis(files: List[FileInfo]) -> dict:
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
        "powershellScript": script,
        "error": None,
    }

def _build_llm_prompt(files: List[FileInfo], theme: str) -> str:
    desc = []
    for f in files:
        desc.append(f"- {f.relativePath} ({f.extension}, {f.size} octets)")
    prompt = (
        "Tu es un assistant de rangement intelligent. Classe les fichiers suivants selon leur type "
        "(dossier Images, Documents, Vidéos, Audio, Archives, Code, Autres). "
        "Pour chaque dossier, propose une liste de chemins relatifs. Résume chaque nom de fichier "
        "en quelques mots (20 caractères maxi, retire l'extension). Détecte les doublons "
        "(même nom ET même taille) et propose un conservé et les autres à supprimer.\n\n"
        f"Liste des fichiers :\n{chr(10).join(desc)}\n"
        "Réponds EXACTEMENT par un objet JSON contenant les clés : "
        "plan (objet dossier -> liste de chemins), summary (objet chemin -> résumé), "
        "duplicates (objet chemin conservé -> liste à supprimer). N'ajoute AUCUN autre texte."
    )
    return prompt

def _run_llm_blocking(files: List[FileInfo], theme: str, model_path: str) -> dict:
    prompt = _build_llm_prompt(files, theme)
    try:
        model = flm.load(model_path)
        raw_output = model.generate(prompt)
    except Exception:
        return _fallback_analysis(files)

    try:
        data = json.loads(raw_output.strip())
        plan_valid = isinstance(data.get("plan"), dict)
        summary_valid = isinstance(data.get("summary"), dict)
        dup_valid = isinstance(data.get("duplicates"), dict)
        if plan_valid and summary_valid and dup_valid:
            plan = data["plan"]
            summary = data["summary"]
            duplicates = data["duplicates"]
            script = _build_powershell_script(plan, duplicates)
            return {
                "plan": plan,
                "summary": summary,
                "duplicates": duplicates,
                "powershellScript": script,
                "error": None,
            }
    except Exception:
        pass

    return _fallback_analysis(files)

# ---------- Endpoints ----------
@router.get("/models")
async def list_models():
    try:
        if not os.path.isdir(MODELS_DIR):
            return {"models": []}
        models = [f for f in os.listdir(MODELS_DIR) if f.endswith(".gguf")]
    except Exception:
        models = []
    return {"models": models}

@router.post("/analyze")
async def analyze(req: AnalyzeRequest):
    if not req.model or flm is None:
        result = _fallback_analysis(req.files)
        return AnalyzeResponse(**result)

    model_path = os.path.join(MODELS_DIR, req.model)
    if not os.path.isfile(model_path):
        result = _fallback_analysis(req.files)
        return AnalyzeResponse(**result)

    return StreamingResponse(_analysis_stream(req, model_path, req.model),
                             media_type="text/event-stream")

async def _analysis_stream(req: AnalyzeRequest, model_path: str, model_name: str):
    def _sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    yield _sse({"type": "status", "message": f"Chargement du modèle {model_name}..."})
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run_llm_blocking,
                                        req.files, req.theme, model_path)

    yield _sse({"type": "status", "message": "Analyse terminée, construction du résultat..."})
    yield _sse({"type": "result", "result": result})

@router.post("/deduplicate", response_model=DedupResponse)
async def dedup(files: List[FileInfo]):
    return DedupResponse(duplicates=_find_duplicates(files))
