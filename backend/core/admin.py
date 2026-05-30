import json
import logging
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import ollama
import pypdf

logger = logging.getLogger(__name__)

_FICHES_DIR = Path(r"C:\Users\Ilyan\Fiches")
_MATIERES = ["Maths", "Physique-Chimie", "SI"]
_LOG_PATH = Path(__file__).parent.parent / "memory" / "admin_log.json"
_CACHE_PATH = Path(__file__).parent.parent / "memory" / "admin_cache.json"


class AdminEngine:
    def __init__(self, llm, rag):
        self._llm = llm
        self._rag = rag
        self._classify_model = self._pick_classify_model()
        self._cache = self._load_cache()

    def _pick_classify_model(self) -> str | None:
        try:
            models = [m.model for m in ollama.list().models]
            for m in models:
                if m.startswith("phi4-mini"):
                    return m
        except Exception:
            pass
        return None

    def _load_cache(self) -> dict:
        if not _CACHE_PATH.exists():
            return {}
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("Erreur lecture admin_cache.json")
            return {}

    def _save_cache(self) -> None:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Erreur sauvegarde admin_cache.json")

    def analyze_file(self, path: str) -> dict:
        filename = Path(path).name

        # Cache hit check
        try:
            mtime = Path(path).stat().st_mtime
            cached = self._cache.get(path)
            if cached and cached.get("mtime") == mtime:
                return {
                    "matière": cached["matière"],
                    "nom_suggéré": cached["nom_suggéré"],
                    "confiance": cached["confiance"],
                }
        except Exception:
            mtime = None

        # Read PDF excerpt (800 chars — title + beginning is enough for classification)
        try:
            reader = pypdf.PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            excerpt = text[:800]
        except Exception:
            logger.exception("Erreur lecture PDF %s", path)
            excerpt = ""

        prompt = (
            "Tu analyses un fichier PDF de cours de classe préparatoire scientifique (PTSI/MP).\n"
            f"Nom actuel : {filename}\n"
            f"Début du contenu :\n{excerpt}\n\n"
            "Réponds UNIQUEMENT avec ce JSON valide, sans texte avant ou après :\n"
            '{"matière": "Maths", "nom_suggéré": "Maths_Suites_S24.pdf", "confiance": 0.9}\n\n'
            "Règles :\n"
            '- matière : exactement "Maths", "Physique-Chimie", "SI", ou "Inconnu"\n'
            "- nom_suggéré : Format Matière_Sujet_Semestre.pdf, underscores, pas d'espaces\n"
            "- confiance : 0.0 à 1.0\n"
            "- Si incertain, garde le nom actuel et mets confiance < 0.5"
        )

        model = self._classify_model or self._llm._model
        result = {"matière": "Inconnu", "nom_suggéré": filename, "confiance": 0.0}
        try:
            response = ollama.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                options={"temperature": 0.1, "num_predict": 200},
            )
            raw = response["message"]["content"]
            match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
                matiere = data.get("matière", "Inconnu")
                if matiere not in _MATIERES:
                    matiere = "Inconnu"
                nom = data.get("nom_suggéré", filename) or filename
                if not nom.endswith(".pdf"):
                    nom = filename
                result = {
                    "matière": matiere,
                    "nom_suggéré": nom,
                    "confiance": min(1.0, max(0.0, float(data.get("confiance", 0.0)))),
                }
        except Exception:
            logger.exception("Erreur LLM analyze_file %s", path)

        # Save to cache
        if mtime is not None:
            self._cache[path] = {"mtime": mtime, **result}
            self._save_cache()

        return result

    def scan_all(self):
        """Generator: yields (result_dict, index, total) for each PDF."""
        pdfs = list(_FICHES_DIR.rglob("*.pdf"))
        total = len(pdfs)
        for i, pdf_path in enumerate(pdfs):
            path_str = str(pdf_path)
            nom_actuel = pdf_path.name
            try:
                rel = pdf_path.relative_to(_FICHES_DIR)
                dossier_actuel = rel.parts[0] if len(rel.parts) > 1 else "Racine"
            except ValueError:
                dossier_actuel = "Racine"

            analysis = self.analyze_file(path_str)
            matiere = analysis["matière"]
            nom_suggere = analysis["nom_suggéré"]
            confiance = analysis["confiance"]

            yield {
                "path": path_str,
                "nom_actuel": nom_actuel,
                "dossier_actuel": dossier_actuel,
                "matière_détectée": matiere,
                "nom_suggéré": nom_suggere,
                "confiance": confiance,
                "action_tri": dossier_actuel != matiere and matiere != "Inconnu",
                "action_renommage": nom_actuel != nom_suggere,
            }, i, total

    def find_duplicates(self) -> list:
        try:
            import numpy as np
        except ImportError:
            logger.error("numpy requis pour find_duplicates")
            return []
        try:
            result = self._rag._col.get(include=["embeddings", "metadatas"])
            if not result or not result.get("ids"):
                return []

            file_embs: dict = {}
            for meta, emb in zip(result["metadatas"], result["embeddings"]):
                src = (meta or {}).get("source", "")
                if src and emb is not None:
                    file_embs.setdefault(src, []).append(emb)

            files = list(file_embs.keys())
            if len(files) < 2:
                return []

            means = {f: np.mean(np.array(embs), axis=0) for f, embs in file_embs.items()}

            def cos_sim(a, b):
                d = np.linalg.norm(a) * np.linalg.norm(b)
                return float(np.dot(a, b) / d) if d else 0.0

            groups = []
            visited: set = set()
            for i, f1 in enumerate(files):
                if f1 in visited:
                    continue
                group = [f1]
                sims = []
                for j, f2 in enumerate(files):
                    if i == j or f2 in visited:
                        continue
                    s = cos_sim(means[f1], means[f2])
                    if s > 0.92:
                        group.append(f2)
                        sims.append(s)
                        visited.add(f2)
                if len(group) > 1:
                    visited.add(f1)
                    groups.append({
                        "groupe": group,
                        "similarité": round(sum(sims) / len(sims), 4),
                    })
            return groups
        except Exception:
            logger.exception("Erreur find_duplicates")
            return []

    def execute_actions(self, actions: list) -> list:
        results = []
        for action in actions:
            source = action.get("source", "")
            destination = action.get("destination", "")
            action_type = action.get("type", "action")
            try:
                src = Path(source)
                dst = Path(destination)
                if not src.exists():
                    results.append({"path": source, "succès": False, "erreur": "Fichier source introuvable"})
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                # Invalidate cache for moved file
                self._cache.pop(source, None)
                self._save_cache()
                try:
                    self._rag._col.delete(where={"source": source})
                    self._rag.index_pdf(str(dst))
                except Exception:
                    logger.exception("Erreur màj ChromaDB %s → %s", source, destination)
                self._append_log(action_type, source, destination)
                results.append({"path": destination, "succès": True, "erreur": None})
            except Exception as exc:
                logger.exception("Erreur action %s %s → %s", action_type, source, destination)
                results.append({"path": source, "succès": False, "erreur": str(exc)})
        return results

    def _load_log(self) -> list:
        if not _LOG_PATH.exists():
            return []
        try:
            with open(_LOG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("Erreur lecture admin_log.json")
            return []

    def _save_log(self, log: list) -> None:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)

    def _append_log(self, action_type: str, source: str, destination: str) -> None:
        log = self._load_log()
        log.append({
            "id": f"{len(log)}_{int(time.time())}",
            "date": datetime.now().isoformat(),
            "type": action_type,
            "source": source,
            "destination": destination,
            "annulé": False,
        })
        self._save_log(log)

    def get_log(self) -> list:
        return self._load_log()

    def undo_action(self, action_id: str) -> dict:
        log = self._load_log()
        action = next((a for a in log if a["id"] == action_id), None)
        if not action:
            return {"succès": False, "erreur": "Action introuvable"}
        if action.get("annulé"):
            return {"succès": False, "erreur": "Action déjà annulée"}
        try:
            d = datetime.fromisoformat(action["date"])
            if datetime.now() - d > timedelta(hours=24):
                return {"succès": False, "erreur": "Action trop ancienne (> 24h)"}
        except Exception:
            logger.exception("Erreur parsing date action %s", action_id)

        src = Path(action["destination"])
        dst = Path(action["source"])
        try:
            if not src.exists():
                return {"succès": False, "erreur": f"Fichier {src.name} introuvable pour annulation"}
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            self._cache.pop(action["destination"], None)
            self._save_cache()
            try:
                self._rag._col.delete(where={"source": action["destination"]})
                self._rag.index_pdf(str(dst))
            except Exception:
                logger.exception("Erreur màj ChromaDB lors annulation")
            for a in log:
                if a["id"] == action_id:
                    a["annulé"] = True
            self._save_log(log)
            return {"succès": True, "erreur": None}
        except Exception as exc:
            logger.exception("Erreur annulation %s", action_id)
            return {"succès": False, "erreur": str(exc)}
