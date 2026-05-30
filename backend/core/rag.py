import functools
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

        # Per-instance LRU caches — cleared on index_pdf to avoid stale results
        self._query_lru = functools.lru_cache(maxsize=50)(self._do_query)
        self._query_filtered_lru = functools.lru_cache(maxsize=50)(self._do_query_filtered)

    def index_pdf(self, path: str) -> None:
        reader = pypdf.PdfReader(str(path))
        full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
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
            chunks.append(full_text[start : start + chunk_chars])
            start += step

        base_id = str(path).replace("\\", "/")
        ids = [f"{base_id}::{i}" for i in range(len(chunks))]
        self._col.upsert(
            documents=chunks,
            ids=ids,
            metadatas=[{"source": str(path), "chunk": i} for i in range(len(chunks))],
        )

        # Invalidate query caches since the index has changed
        self._query_lru.cache_clear()
        self._query_filtered_lru.cache_clear()

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
    def read_pdf_text(path: str) -> str:
        reader = pypdf.PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    def watch(self, folder: str) -> None:
        folder = str(folder)
        if not os.path.isdir(folder):
            return

        for pdf_path in Path(folder).rglob("*.pdf"):
            try:
                self.index_pdf(str(pdf_path))
            except Exception:
                logger.exception("Erreur lors de l'indexation de %s", pdf_path)

        handler = _PDFHandler(self)
        observer = Observer()
        observer.schedule(handler, folder, recursive=True)
        observer.daemon = True
        observer.start()


class _PDFHandler(FileSystemEventHandler):
    def __init__(self, engine: RAGEngine):
        self._engine = engine

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".pdf"):
            try:
                self._engine.index_pdf(event.src_path)
            except Exception:
                logger.exception("Erreur lors de l'indexation de %s", event.src_path)
