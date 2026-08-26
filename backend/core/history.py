import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from core.instance import modele_local_defaut
from core.jsonstore import read_json, transaction, write_json
from core.paths import PathOutsideDataError, resolve_history_dir

logger = logging.getLogger(__name__)


class HistoryEngine:
    def __init__(self, llm, store):
        """``store`` : le ``VectorStore`` partagé (cf. ``core/runtime.py``), qui
        remplace le couple ``chroma_client``/``ef`` pris dans les attributs
        privés de ``RAGEngine``. Seul des trois appelants à supprimer par ``ids``
        et à ne jamais filtrer par ``where``.

        Les chemins sont résolus ICI, à la construction du moteur — jamais à
        l'import du module (CLAUDE.md §3.5, et la convention de
        ``MemoryEngine.__init__``). C'étaient deux constantes de module,
        ``_HISTORY_DIR`` et ``_INDEX_FILE``, calculées en
        ``Path(__file__).parent.parent / "history"`` : un chemin figé avant que
        quoi que ce soit ait pu poser ``$EPURE_HISTORY_DIR``, donc un dossier de
        données réel qu'aucun test ne pouvait détourner. Cf.
        :func:`core.paths.resolve_history_dir` pour pourquoi ça tenait jusqu'ici
        et pourquoi ça cesse de tenir.
        """
        self._llm = llm
        self._dir = resolve_history_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "conversations.json"
        self._col = store.collection("history")

    # ── Chemins ───────────────────────────────────────────────────────────────

    def _conv_path(self, conv_id: str):
        """Fichier d'une conversation, confiné au dossier d'historique.

        Les trois appelants (``save``/``get``/``delete``) composaient
        ``_HISTORY_DIR / f"{conv_id}.json"`` chacun de leur côté, et ``conv_id``
        vient du client sur ``GET`` comme sur ``DELETE /history/{conv_id}`` — qui
        finit en ``unlink()``.

        Mesuré avant d'écrire cette garde, pour ne pas prétendre corriger une
        faille qui n'existait pas : **aucune traversée n'est atteignable
        aujourd'hui**. Un paramètre de chemin Starlette ne peut pas contenir de
        ``/``, même percent-encodé (``..%2F..%2Fx`` → 404, vérifié), et le
        préfixe de lecteur Windows est absorbé par la jonction (``C:evil`` →
        ``<history>/evil.json``, vérifié).

        La garde est donc une ceinture, pas un correctif : elle rend le
        confinement vrai *par construction* plutôt que par une propriété du
        routage qui n'est pas écrite ici. Elle prend son sens à l'étape 3 du
        chantier conversations, où un ``PUT`` **écrit** sous un identifiant
        fourni par le client. Confinement par ``resolve()`` puis comparaison de
        chemins, jamais par ``startswith`` de chaîne (CLAUDE.md §6).
        """
        racine = self._dir.resolve()
        cible = (racine / f"{conv_id}.json").resolve()
        # `parent == racine` et non `is_relative_to(racine)` : le second accepte
        # encore un sous-dossier (`sub/x` → `<history>/sub/x.json`), confiné mais
        # créant une arborescence au premier `write_json`, qui fait un
        # `mkdir(parents=True)`. Un identifiant de conversation est un segment nu
        # — un `uuid4()` — donc on exige un enfant DIRECT. Même philosophie que
        # `safe_upload_name` : refuser, plutôt que nettoyer en silence.
        if cible.parent != racine or cible == racine:
            raise PathOutsideDataError(f"Identifiant de conversation invalide : {conv_id!r}")
        return cible

    # ── Index helpers ─────────────────────────────────────────────────────────

    def _load_index(self) -> list:
        return read_json(self._index_path, {}).get("conversations", [])

    @contextmanager
    def _index_transaction(self):
        """RMW verrouillé de l'index, cédant la LISTE des conversations.

        Le document sur disque est ``{"conversations": [...]}`` ; on cède la liste
        pour que les appelants gardent leur code, mais c'est bien le document
        entier qui est réécrit.
        """
        with transaction(self._index_path, {"conversations": []}) as doc:
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
            # Modèle LOCAL explicite. L'appel n'en passait aucun, donc `LLMEngine`
            # retombait sur `config.yaml` — local, mais hors du réglage, donc
            # impossible à changer depuis l'interface. Le titrage tourne après
            # chaque conversation sans que personne ne le demande : c'est une
            # tâche de fond au sens strict (CLAUDE.md §3.7).
            title = self._llm.generate([{"role": "user", "content": prompt}],
                                       model=modele_local_defaut())
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
        conv_path = self._conv_path(conv_id)
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
        try:
            conv_path = self._conv_path(conv_id)
        except PathOutsideDataError:
            logger.warning("Identifiant de conversation refusé : %r", conv_id)
            return None
        return read_json(conv_path, None)

    def list_conversations(self, days: int = 30) -> list[dict]:
        conversations = self._load_index()
        if days <= 0:
            return conversations
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return [c for c in conversations if c.get("date", "") >= cutoff]

    def delete_conversation(self, conv_id: str) -> bool:
        try:
            conv_path = self._conv_path(conv_id)
        except PathOutsideDataError:
            logger.warning("Suppression refusée, identifiant invalide : %r", conv_id)
            return False
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
