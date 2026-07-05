import functools
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
import pypdf
import yaml
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.txt', '.md', '.csv', '.json', '.png', '.jpg', '.jpeg', '.webp'}


class RAGEngine:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        rag_cfg = cfg.get("rag", {})
        self._chunk_size = rag_cfg.get("chunk_size", 500)
        self._chunk_overlap = rag_cfg.get("chunk_overlap", 50)
        self._n_results = rag_cfg.get("n_results", 3)

        db_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "chroma_db")
        self._client = chromadb.PersistentClient(path=db_path)
        self._ef = SentenceTransformerEmbeddingFunction(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )
        self._col = self._client.get_or_create_collection(
            "fiches", embedding_function=self._ef
        )

        # Per-instance LRU caches — cleared on index_file to avoid stale results
        self._query_lru = functools.lru_cache(maxsize=50)(self._do_query)
        self._query_filtered_lru = functools.lru_cache(maxsize=50)(self._do_query_filtered)

    @staticmethod
    def _extract_text_from_path(path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext == '.pdf':
            reader = pypdf.PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        elif ext == '.docx':
            try:
                from docx import Document  # python-docx
                doc = Document(str(path))
                return "\n".join(para.text for para in doc.paragraphs)
            except ImportError:
                logger.warning("python-docx non installé — pip install python-docx")
                return ""
        elif ext in ('.txt', '.md'):
            return Path(path).read_text(encoding='utf-8', errors='ignore')
        elif ext == '.csv':
            try:
                import pandas as pd
                df = pd.read_csv(path, nrows=500)
                return df.to_string(index=False)
            except Exception:
                return Path(path).read_text(encoding='utf-8', errors='ignore')
        elif ext == '.json':
            try:
                # utf-8-sig : les JSON fournis par l'utilisateur portent souvent un BOM
                data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
                return json.dumps(data, ensure_ascii=False, indent=2)[:50000]
            except Exception:
                return Path(path).read_text(encoding='utf-8', errors='ignore')
        elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
            name = Path(path).name
            return f"Image : {name} (analyse vision non disponible sans modèle vision)"
        return ""

    def index_file(self, path: str) -> None:
        ext = Path(path).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return

        full_text = self._extract_text_from_path(path)
        if not full_text.strip():
            return

        # Remove stale chunks before re-indexing to avoid duplicates
        try:
            self._col.delete(where={"source": str(path)})
        except Exception:
            logger.exception("Erreur suppression chunks existants pour %s", path)

        chunk_chars = self._chunk_size * 4
        overlap_chars = self._chunk_overlap * 4
        step = max(1, chunk_chars - overlap_chars)

        chunks = []
        start = 0
        while start < len(full_text):
            chunks.append(full_text[start: start + chunk_chars])
            start += step

        # mtime stocké pour la re-indexation incrémentale au démarrage (cf. watch) :
        # on saute le ré-embedding des fichiers inchangés.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0

        base_id = str(path).replace("\\", "/")
        ids = [f"{base_id}::{i}" for i in range(len(chunks))]
        self._col.upsert(
            documents=chunks,
            ids=ids,
            metadatas=[{"source": str(path), "chunk": i, "mtime": mtime} for i in range(len(chunks))],
        )

        # Invalidate query caches since the index has changed
        self._query_lru.cache_clear()
        self._query_filtered_lru.cache_clear()

    def index_pdf(self, path: str) -> None:
        """Backward-compat alias for index_file."""
        self.index_file(path)

    def _do_query(self, text: str, n: int) -> str:
        count = self._col.count()
        if count == 0:
            return ""
        results = self._col.query(query_texts=[text], n_results=min(n, count))
        docs = results.get("documents", [[]])[0]
        return "\n\n---\n\n".join(d for d in docs if d)

    def _do_query_filtered(self, text: str, paths_key: tuple, n: int) -> str:
        paths = list(paths_key)
        if not paths:
            return ""
        try:
            existing = self._col.get(
                where={"source": {"$in": paths}}, include=[]
            )
            count = len(existing.get("ids", []))
            if count == 0:
                return ""
            results = self._col.query(
                query_texts=[text],
                n_results=min(n, count),
                where={"source": {"$in": paths}},
            )
            docs = results.get("documents", [[]])[0]
            return "\n\n---\n\n".join(d for d in docs if d)
        except Exception:
            logger.exception("Erreur query_filtered")
            return ""

    def query(self, text: str, n_results: Optional[int] = None) -> str:
        n = n_results if n_results is not None else self._n_results
        return self._query_lru(text, n)

    def query_filtered(self, text: str, paths: list, n_results: Optional[int] = None) -> str:
        if not paths:
            return ""
        n = n_results if n_results is not None else self._n_results
        return self._query_filtered_lru(text, tuple(sorted(paths)), n)

    def get_indexed_files(self) -> list:
        result = self._col.get(include=["metadatas"])
        sources = {m["source"] for m in result["metadatas"] if m and "source" in m}
        return sorted(sources)

    @staticmethod
    def read_file_text(path: str) -> str:
        return RAGEngine._extract_text_from_path(path)

    @staticmethod
    def read_pdf_text(path: str) -> str:
        """Backward-compat alias for read_file_text."""
        return RAGEngine._extract_text_from_path(path)

    def _indexed_mtimes(self) -> dict:
        """source → mtime déjà indexé (pour sauter les fichiers inchangés)."""
        try:
            result = self._col.get(include=["metadatas"])
        except Exception:
            logger.exception("Erreur lecture index pour scan incrémental")
            return {}
        out: dict[str, float] = {}
        for m in result.get("metadatas", []) or []:
            if m and "source" in m and "mtime" in m:
                out[m["source"]] = m["mtime"]
        return out

    def _initial_scan(self, folder: str) -> None:
        """Scan initial d'un dossier surveillé : (ré)indexe les fichiers nouveaux
        ou modifiés depuis la dernière fois. Tourne en tâche de fond (cf. watch).
        """
        indexed = self._indexed_mtimes()
        scanned = reindexed = 0
        for ext in SUPPORTED_EXTENSIONS:
            for file_path in Path(folder).rglob(f"*{ext}"):
                scanned += 1
                sp = str(file_path)
                try:
                    mtime = os.path.getmtime(sp)
                except OSError:
                    continue
                # Inchangé depuis la dernière indexation → pas de ré-embedding.
                if abs(indexed.get(sp, -1.0) - mtime) < 1e-6:
                    continue
                try:
                    self.index_file(sp)
                    reindexed += 1
                except Exception:
                    logger.exception("Erreur lors de l'indexation de %s", file_path)
        if reindexed:
            logger.info(
                "RAG scan initial %s : %d/%d fichier(s) (ré)indexé(s)",
                folder, reindexed, scanned,
            )

    def watch(self, folder: str) -> None:
        folder = str(folder)
        if not os.path.isdir(folder):
            return

        # L'observer démarre tout de suite (léger) : modifs/créations captées sans
        # attendre le scan initial.
        handler = _FileHandler(self)
        observer = Observer()
        observer.schedule(handler, folder, recursive=True)
        observer.daemon = True
        observer.start()

        # Scan initial en tâche de fond : il pouvait ré-embedder des dizaines de
        # fichiers (plusieurs minutes). Synchrone ici, il bloquait l'import de
        # core.runtime → uvicorn ne répondait pas et l'app restait figée sur
        # « Chargement… » au démarrage.
        threading.Thread(
            target=self._initial_scan, args=(folder,), daemon=True,
            name=f"rag-scan-{Path(folder).name}",
        ).start()


class _FileHandler(FileSystemEventHandler):
    def __init__(self, engine: RAGEngine):
        self._engine = engine

    def on_created(self, event):
        if not event.is_directory:
            ext = Path(event.src_path).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                try:
                    self._engine.index_file(event.src_path)
                except Exception:
                    logger.exception("Erreur lors de l'indexation de %s", event.src_path)
