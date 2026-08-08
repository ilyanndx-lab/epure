import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from core.jsonstore import read_json, transaction, write_json

logger = logging.getLogger(__name__)

_HISTORY_DIR = Path(__file__).parent.parent / "history"
_INDEX_FILE = _HISTORY_DIR / "conversations.json"


class HistoryEngine:
    def __init__(self, llm, chroma_client, ef):
        self._llm = llm
        _HISTORY_DIR.mkdir(exist_ok=True)
        self._col = chroma_client.get_or_create_collection(
            "history", embedding_function=ef
        )

    # ── Index helpers ─────────────────────────────────────────────────────────

    def _load_index(self) -> list:
        return read_json(_INDEX_FILE, {}).get("conversations", [])

    @contextmanager
    def _index_transaction(self):
        """RMW verrouillé de l'index, cédant la LISTE des conversations.

        Le document sur disque est ``{"conversations": [...]}`` ; on cède la liste
        pour que les appelants gardent leur code, mais c'est bien le document
        entier qui est réécrit.
        """
        with transaction(_INDEX_FILE, {"conversations": []}) as doc:
            yield doc.setdefault("conversations", [])

    # ── LLM title ────────────────────────────────────────────────────────────

    def _generate_title(self, messages: list[dict]) -> str:
        user_msgs = [m["content"] for m in messages if m.get("role") == "user"]
        fallback = f"Conversation du {datetime.now().strftime('%d/%m/%Y')}"
        if not user_msgs:
            return fallback
        sample = "\n".join(user_msgs[:3])[:500]
        prompt = (
            "Génère un titre ultra-court (3-6 mots) pour cette conversation. "
            "Réponds UNIQUEMENT avec le titre, rien d'autre.\n\n"
            f"Extraits :\n{sample}"
        )
        try:
            title = self._llm.generate([{"role": "user", "content": prompt}])
            title = title.strip().strip('"').strip("'")[:80]
            return title or fallback
        except Exception:
            logger.exception("Erreur génération titre conversation")
            return fallback

    # ── Public API ────────────────────────────────────────────────────────────

    def save_conversation(
        self, messages: list[dict], model: str = "", modules: list | None = None
    ) -> str:
        if modules is None:
            modules = ["chat"]
        conv_id = str(uuid.uuid4())
        date_str = datetime.now().strftime("%Y-%m-%d")
        title = self._generate_title(messages)

        user_msgs = [m for m in messages if m.get("role") == "user"]
        apercu = user_msgs[0]["content"][:200] if user_msgs else ""

        # Persist full conversation
        conv_path = _HISTORY_DIR / f"{conv_id}.json"
        conv_data = {
            "id": conv_id,
            "date": date_str,
            "titre": title,
            "modèle": model,
            "modules": modules,
            "n_messages": len(messages),
            "messages": messages,
        }
        try:
            write_json(conv_path, conv_data)
        except Exception:
            logger.exception("Erreur sauvegarde conversation %s", conv_id)
            return conv_id

        # Update index
        entry = {
            "id": conv_id,
            "date": date_str,
            "titre": title,
            "apercu": apercu,
            "modèle": model,
            "n_messages": len(messages),
            "modules": modules,
        }
        try:
            with self._index_transaction() as conversations:
                conversations.insert(0, entry)
        except Exception:
            logger.exception("Erreur màj index conversations")

        # Index in ChromaDB
        try:
            doc = "\n".join(
                f"[{m.get('role', 'user')}] {m.get('content', '')}"
                for m in messages
            )[:8000]
            self._col.upsert(
                documents=[doc],
                ids=[conv_id],
                metadatas=[{"id": conv_id, "date": date_str, "titre": title, "modèle": model}],
            )
        except Exception:
            logger.exception("Erreur indexation ChromaDB conversation %s", conv_id)

        logger.info("Conversation sauvegardée : %s — %s", conv_id, title)
        return conv_id

    def search_history(self, query: str, n_results: int = 3) -> list[dict]:
        try:
            count = self._col.count()
            if count == 0:
                return []
            results = self._col.query(
                query_texts=[query],
                n_results=min(n_results, count),
                include=["documents", "metadatas"],
            )
            items = []
            for doc, meta in zip(
                results.get("documents", [[]])[0],
                results.get("metadatas", [[]])[0],
            ):
                items.append({
                    "id": meta.get("id", ""),
                    "date": meta.get("date", ""),
                    "titre": meta.get("titre", ""),
                    "modèle": meta.get("modèle", ""),
                    "extrait": doc[:300],
                })
            return items
        except Exception:
            logger.exception("Erreur search_history")
            return []

    def get_conversation(self, conv_id: str) -> dict | None:
        conv_path = _HISTORY_DIR / f"{conv_id}.json"
        return read_json(conv_path, None)

    def list_conversations(self, days: int = 30) -> list[dict]:
        conversations = self._load_index()
        if days <= 0:
            return conversations
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return [c for c in conversations if c.get("date", "") >= cutoff]

    def delete_conversation(self, conv_id: str) -> bool:
        conv_path = _HISTORY_DIR / f"{conv_id}.json"
        if conv_path.exists():
            try:
                conv_path.unlink()
            except Exception:
                logger.exception("Erreur suppression fichier %s", conv_id)

        try:
            with self._index_transaction() as conversations:
                # En place : c'est l'objet cédé qui est réécrit (cf. transaction).
                conversations[:] = [c for c in conversations if c.get("id") != conv_id]
        except Exception:
            logger.exception("Erreur màj index après suppression %s", conv_id)

        try:
            self._col.delete(ids=[conv_id])
        except Exception:
            logger.exception("Erreur suppression ChromaDB %s", conv_id)

        return True
