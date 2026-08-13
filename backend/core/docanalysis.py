import hashlib
import logging
import os
from pathlib import Path
from typing import Generator, Optional

import pypdf

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 800
_CHUNK_OVERLAP = 100
_CHUNK_STEP = _CHUNK_SIZE - _CHUNK_OVERLAP
_BATCH_SIZE = 100


class DocAnalysisEngine:
    """Analyse de documents PDF chargés à la demande (collection « doc_analysis »).

    Reçoit le ``VectorStore`` partagé (cf. ``core/runtime.py``) au lieu du couple
    ``chroma_client``/``embedding_function`` qu'il allait chercher dans les
    attributs privés de ``RAGEngine``. Le partage était déjà réel — les trois
    collections vivaient dans un seul ``PersistentClient`` — mais il passait par
    ``rag._client``, ce qui le rendait invisible et fragile. Le store est
    maintenant un paramètre, donc une dépendance déclarée.

    La fonction d'embedding n'est plus un argument : elle appartient au store,
    qui la partage entre ses collections (cf. ``core/vector_store.py``).
    """

    def __init__(self, store, llm):
        self._llm = llm
        self._col = store.collection("doc_analysis")

    def _make_doc_id(self, path: str) -> str:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        return hashlib.sha256(f"{path}:{mtime}".encode()).hexdigest()[:16]

    # ── Load ─────────────────────────────────────────────────────────────────

    def load_document_streaming(self, path: str) -> Generator:
        """Sync generator yielding progress/done/error dicts. Run in a thread."""
        path = str(path)
        if not os.path.exists(path):
            yield {"type": "error", "message": f"Fichier introuvable : {path}"}
            return

        doc_id = self._make_doc_id(path)

        # Check cache by doc_id
        try:
            existing = self._col.get(where={"doc_id": doc_id}, include=[])
            if existing.get("ids"):
                n_chunks = len(existing["ids"])
                reader = pypdf.PdfReader(path)
                n_pages = len(reader.pages)
                apercu = ""
                try:
                    first = self._col.get(
                        where={"doc_id": doc_id, "chunk_index": 0},
                        include=["documents"],
                    )
                    apercu = (first.get("documents") or [""])[0][:300]
                except Exception:
                    pass
                yield {
                    "type": "done",
                    "doc": {
                        "id": doc_id,
                        "titre": Path(path).stem,
                        "path": path,
                        "n_pages": n_pages,
                        "n_chunks": n_chunks,
                        "apercu": apercu,
                        "cached": True,
                    },
                }
                return
        except Exception:
            logger.exception("Erreur vérification cache %s", path)

        # Extract text
        try:
            reader = pypdf.PdfReader(path)
            n_pages = len(reader.pages)
            full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            logger.exception("Erreur lecture PDF %s", path)
            yield {"type": "error", "message": "Impossible de lire ce PDF"}
            return

        if not full_text.strip():
            yield {"type": "error", "message": "Aucun texte extractible dans ce PDF"}
            return

        # Remove stale chunks for this path (old doc_id)
        try:
            self._col.delete(where={"source": path})
        except Exception:
            logger.exception("Erreur suppression anciens chunks %s", path)

        # Build chunks
        chunks: list[str] = []
        start = 0
        while start < len(full_text):
            chunks.append(full_text[start : start + _CHUNK_SIZE])
            start += _CHUNK_STEP

        n_chunks = len(chunks)
        ids = [f"{doc_id}::{i}" for i in range(n_chunks)]
        metadatas = [
            {
                "doc_id": doc_id,
                "source": path,
                "chunk_index": i,
                "n_pages": n_pages,
                "n_chunks": n_chunks,
            }
            for i in range(n_chunks)
        ]

        # Upsert in batches, yielding progress events
        for b_start in range(0, n_chunks, _BATCH_SIZE):
            b_end = min(b_start + _BATCH_SIZE, n_chunks)
            self._col.upsert(
                documents=chunks[b_start:b_end],
                ids=ids[b_start:b_end],
                metadatas=metadatas[b_start:b_end],
            )
            yield {"type": "progress", "chunk": b_end, "total": n_chunks}

        yield {
            "type": "done",
            "doc": {
                "id": doc_id,
                "titre": Path(path).stem,
                "path": path,
                "n_pages": n_pages,
                "n_chunks": n_chunks,
                "apercu": full_text[:300],
                "cached": False,
            },
        }

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, doc_id: str, query: str, n_results: int = 5) -> list:
        try:
            count_res = self._col.get(where={"doc_id": doc_id}, include=[])
            total = len(count_res.get("ids", []))
            if total == 0:
                return []
            results = self._col.query(
                query_texts=[query],
                n_results=min(n_results, total),
                where={"doc_id": doc_id},
                include=["documents", "metadatas", "distances"],
            )
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            dists = results.get("distances", [[]])[0]
            out = []
            for doc, meta, dist in zip(docs, metas, dists):
                chunk_idx = meta.get("chunk_index", 0)
                n_pg = meta.get("n_pages", 1)
                n_ck = meta.get("n_chunks", 1)
                page_approx = max(1, round(chunk_idx * n_pg / max(1, n_ck)))
                out.append({
                    "chunk": doc,
                    "page_approx": page_approx,
                    "score": round(1.0 - float(dist), 4),
                    "chunk_index": chunk_idx,
                })
            return out
        except Exception:
            logger.exception("Erreur search doc %s", doc_id)
            return []

    # ── Synthesis ─────────────────────────────────────────────────────────────

    def summarize_section(self, chunks: list, query: Optional[str] = None, model: Optional[str] = None) -> Generator:
        combined = "\n\n---\n\n".join(chunks)
        if query:
            prompt = f"À partir de ces extraits, réponds précisément : {query}\n\nExtraits :\n{combined}"
        else:
            prompt = f"Résume ces extraits de façon concise et structurée :\n{combined}"
        for token in self._llm.stream([{"role": "user", "content": prompt}], model=model, max_tokens=2048):
            if isinstance(token, str):
                yield token

    def summarize_document(self, doc_id: str, level: str = "short", model: Optional[str] = None) -> Generator:
        try:
            result = self._col.get(
                where={"doc_id": doc_id},
                include=["documents", "metadatas"],
            )
        except Exception:
            logger.exception("Erreur récupération chunks doc %s", doc_id)
            return

        docs = result.get("documents", [])
        metas = result.get("metadatas", [])
        if not docs:
            return

        pairs = sorted(zip(metas, docs), key=lambda x: x[0].get("chunk_index", 0))
        all_chunks = [d for _, d in pairs]
        total = len(all_chunks)

        if level == "full":
            batch_size = 10
            batch_summaries: list[str] = []
            for b in range(0, total, batch_size):
                batch_text = "\n\n---\n\n".join(all_chunks[b : b + batch_size])
                prompt = f"Résume ce passage de façon concise :\n{batch_text}"
                summary = ""
                for token in self._llm.stream([{"role": "user", "content": prompt}], model=model, max_tokens=512):
                    if isinstance(token, str):
                        summary += token
                batch_summaries.append(summary)

            final_prompt = (
                "Synthétise ces résumés partiels en un résumé complet, "
                "structuré avec des titres de sections :\n\n"
                + "\n\n---\n\n".join(batch_summaries)
            )
            for token in self._llm.stream([{"role": "user", "content": final_prompt}], model=model, max_tokens=2048):
                if isinstance(token, str):
                    yield token

        else:
            n_sample = 10 if level == "short" else 30
            if total > n_sample:
                step = total / n_sample
                sampled = [all_chunks[int(i * step)] for i in range(n_sample)]
            else:
                sampled = all_chunks

            combined = "\n\n---\n\n".join(sampled)
            if level == "short":
                prompt = f"Résume ce document en environ 200 mots, de façon claire et structurée :\n\n{combined}"
            else:
                prompt = (
                    "Résume ce document de façon structurée avec des sections clairement délimitées "
                    "et les notions clés :\n\n" + combined
                )
            for token in self._llm.stream([{"role": "user", "content": prompt}], model=model, max_tokens=2048):
                if isinstance(token, str):
                    yield token

    # ── Catalog ───────────────────────────────────────────────────────────────

    def get_loaded_docs(self) -> list:
        try:
            result = self._col.get(include=["metadatas"])
            metas = result.get("metadatas", [])
            seen: dict = {}
            for meta in metas:
                if not meta:
                    continue
                doc_id = meta.get("doc_id")
                if doc_id and doc_id not in seen:
                    seen[doc_id] = {
                        "id": doc_id,
                        "titre": Path(meta.get("source", "")).stem,
                        "path": meta.get("source", ""),
                        "n_pages": meta.get("n_pages", 0),
                        "n_chunks": meta.get("n_chunks", 0),
                    }
            return list(seen.values())
        except Exception:
            logger.exception("Erreur liste docs")
            return []

    def unload_document(self, doc_id: str) -> None:
        try:
            self._col.delete(where={"doc_id": doc_id})
        except Exception:
            logger.exception("Erreur suppression doc %s", doc_id)

    def get_doc_info(self, doc_id: str) -> Optional[dict]:
        try:
            result = self._col.get(where={"doc_id": doc_id}, include=["metadatas"])
            metas = result.get("metadatas", [])
            if not metas:
                return None
            meta = metas[0]
            return {
                "id": doc_id,
                "titre": Path(meta.get("source", "")).stem,
                "path": meta.get("source", ""),
                "n_pages": meta.get("n_pages", 0),
                "n_chunks": meta.get("n_chunks", 0),
            }
        except Exception:
            logger.exception("Erreur get_doc_info %s", doc_id)
            return None
