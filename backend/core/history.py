import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

from core.instance import modele_local_defaut
from core.jsonstore import read_json, transaction, write_json
from core.paths import PathOutsideDataError, cle_chemin, resolve_history_dir

logger = logging.getLogger(__name__)


class _ConversationAbsente(Exception):
    """Sentinelle interne : abandonne une ``transaction`` SANS rien écrire.

    Elle existe pour un danger précis, déjà payé une fois dans ce dépôt sous une
    autre forme (cf. l'en-tête de ``core/jsonstore.py``, « effacement
    silencieux »). ``transaction(chemin, {})`` sur un fichier **corrompu**
    n'échoue pas : ``read_json`` loggue, renvoie le défaut ``{}``, et le corps du
    ``with`` construirait alors une conversation neuve par-dessus — c'est-à-dire
    qu'un fichier illisible serait remplacé par un fichier contenant le seul
    dernier tour. Le contenu réel serait perdu au moment précis où on essaie de
    l'enrichir.

    Lever depuis le corps du ``with`` est le seul moyen correct : ``transaction``
    n'a pas de ``finally``, donc rien n'atteint le disque. Un simple
    ``if chemin.exists()`` avant la transaction ne suffirait pas — il répond à
    « le fichier est-il là », pas à « son contenu est-il exploitable ».
    """


def _horodatage() -> str:
    """Instant courant, à la seconde. Local et sans fuseau, comme ``date``.

    Le dépôt stocke déjà ``date`` en ``%Y-%m-%d`` local ; un ``créée`` en UTC à
    côté rendrait les deux champs incomparables sans que rien ne le signale.
    """
    return datetime.now().isoformat(timespec="seconds")


def croiser_fichiers(attaches, indexes) -> list[dict]:
    """``[{"chemin": str, "présent": bool}]`` — jamais un filtrage silencieux.

    ``attaches`` sont les chemins que la conversation revendique ; ``indexes``
    ceux que le moteur RAG connaît réellement (``rag.get_indexed_files()``).

    **Le point de cette fonction est ce qu'elle ne fait pas.** Retirer les
    absents rendrait la liste « propre » et la réponse du modèle inexplicable :
    l'utilisateur verrait un contexte rétrécir sans cause visible. C'est la forme
    inverse du symptôme « indexé à zéro chunk, en silence » (CLAUDE.md §3.3 bis),
    et ce dépôt l'a déjà payé une fois. On rend donc l'écart, on ne le corrige
    pas.

    L'ordre des attachements est conservé : c'est celui que l'utilisateur a posé.

    ⚠️ **Trois états, pas deux.** ``indexes`` vaut ``None`` quand le corpus n'est
    pas interrogeable — cas réel et fréquent : dans un paquet fraîchement
    installé, les 90 Mo du modèle d'embedding ne sont pas encore là, et
    ``rag.get_indexed_files()`` lève ``EmbeddingIndisponible``. ``présent`` vaut
    alors ``None``, qui se lit « on ne sait pas ».

    Répondre ``False`` dans ce cas serait une affirmation fausse, et pire qu'un
    filtrage silencieux : l'interface annoncerait « ce fichier n'est plus indexé »
    à propos de fichiers parfaitement présents, et l'utilisateur les
    ré-importerait pour rien. Une liste vide, elle, reste un vrai ``False`` — un
    corpus réellement vide est une information, pas une ignorance.

    Pure et hors de la classe **exprès** : ``HistoryEngine`` ne doit pas
    connaître le RAG. Lui injecter ``rag`` en construction le coupleraient à un
    ``_LazyEngine`` dont le premier accès construit ``RAGEngine`` — donc peut
    déclencher 90 Mo de téléchargement (CLAUDE.md §3.2). L'appelant fournit les
    deux listes, personne n'instancie rien.
    """
    if indexes is None:
        return [{"chemin": str(a), "présent": None} for a in (attaches or [])]
    connus = {cle_chemin(str(s)) for s in indexes}
    return [
        {"chemin": str(a), "présent": cle_chemin(str(a)) in connus}
        for a in (attaches or [])
    ]


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
        self._store = store
        self._col_cache = None

    @property
    def _col(self):
        """Collection vectorielle, obtenue AU PREMIER USAGE — pas à la construction.

        ⚠️ Ce n'est pas une optimisation, c'est une correction de panne, et elle
        est mesurée. ``store.collection(...)`` construit le ``VectorStore``, dont
        le ``__init__`` construit ``MoteurEmbedding`` — qui lève
        ``EmbeddingIndisponible`` tant que les 90 Mo du modèle ne sont pas
        téléchargés. Le faire depuis ``__init__`` rendait **tout** le moteur
        dépendant de la pile d'embedding, y compris ses opérations purement JSON.

        Conséquence, vérifiée en exécutant la configuration d'un paquet
        fraîchement installé : ``GET /history`` répondait **503**, donc le module
        Historique était mort chez tout destinataire n'ayant pas encore
        téléchargé le modèle. Bug antérieur à ce chantier ; il aurait été hérité
        tel quel par les conversations, où il aurait empêché d'ouvrir le moindre
        fil de discussion.

        Ce que le vecteur sert réellement : ``search_history`` (le skill
        ``@historique``) et ``_indexer_vectoriel``. Deux fonctions de recherche
        sémantique — il est normal qu'elles soient indisponibles sans modèle, et
        leurs appelants l'absorbent déjà (``except Exception`` → liste vide ou
        avertissement). Il n'est pas normal que **lister ses conversations** en
        dépende.
        """
        if self._col_cache is None:
            self._col_cache = self._store.collection("history")
        return self._col_cache

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
        #
        # ⚠️ Ce qui compte comme séparateur DÉPEND DE LA PLATEFORME, et c'est
        # correct dans les deux cas — mais il faut le savoir avant d'écrire un
        # test. `a\b` est une évasion sous Windows (deux segments) et un simple
        # nom de fichier sous POSIX (un segment, backslash littéral), donc
        # confiné et inoffensif. Contrairement à `safe_upload_name`, on ne passe
        # PAS par `ntpath` sur les deux plateformes : là-bas le nom vient du
        # navigateur et doit être jugé au plus strict, ici il vient d'un
        # `uuid4()` que nous avons nous-mêmes écrit, et un fichier nommé `a\b`
        # sur un serveur POSIX ne menace rien.
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

    # ── Primitives : normaliser, écrire, indexer ──────────────────────────────

    def _normaliser(self, conv: dict, chemin=None) -> dict:
        """Complète EN MÉMOIRE les clés qu'un fichier d'avant ce chantier n'a pas.

        Les 18 conversations déjà sur le disque n'ont ni ``créée``, ni
        ``modifiée``, ni ``fichiers_attachés``, ni ``résumé_contexte``, ni
        ``dernière_consolidation``. Elles restent parfaitement lisibles :
        c'est ici, et nulle part ailleurs, que l'écart est comblé.

        ⚠️ **Ne réécrit rien.** Le fichier ne gagne ses clés que le jour où
        quelqu'un le modifie pour une vraie raison. C'est ce qui rend la
        migration sûre au sens de ``docs/conversations-persistees.md`` §5 : une
        conversation qu'on se contente de relire n'est pas touchée, donc une
        régression de ce code ne peut pas abîmer un historique ancien.

        ``n_messages`` est **recalculé**, jamais lu : c'est une projection de
        ``messages``, et deux sources pour un même fait divergent (la leçon de
        ``modules_state.json``, CLAUDE.md §3.3). Les fichiers anciens le portent
        déjà juste — mais le garantir coûte un ``len()``.

        Les types sont vérifiés plutôt que supposés (``isinstance(..., list)``) :
        un JSON de runtime peut avoir été édité à la main, et un
        ``fichiers_attachés`` valant une chaîne ferait boucler dessus caractère
        par caractère. Même raisonnement que le ``Array.isArray`` imposé côté
        frontend (CLAUDE.md §8).
        """
        msgs = conv.get("messages")
        conv["messages"] = msgs if isinstance(msgs, list) else []

        fichiers = conv.get("fichiers_attachés")
        conv["fichiers_attachés"] = (
            [str(f) for f in fichiers] if isinstance(fichiers, list) else []
        )

        conv["n_messages"] = len(conv["messages"])

        resume = conv.get("résumé_contexte")
        conv["résumé_contexte"] = resume if isinstance(resume, str) else ""

        consol = conv.get("dernière_consolidation")
        conv["dernière_consolidation"] = consol if isinstance(consol, int) else 0

        # `créée` : la date du jour de la conversation, seule information
        # d'époque que portent les anciens fichiers. Minuit plutôt qu'une heure
        # inventée — faux mais honnête, et trié correctement entre deux jours.
        date_str = conv.get("date") or ""
        if not conv.get("créée"):
            conv["créée"] = f"{date_str}T00:00:00" if date_str else _horodatage()

        # `modifiée` : le mtime du fichier est la meilleure approximation
        # disponible, et c'est une vraie mesure, pas une reconstruction.
        if not conv.get("modifiée"):
            secours = conv["créée"]
            if chemin is not None:
                try:
                    secours = datetime.fromtimestamp(
                        chemin.stat().st_mtime
                    ).isoformat(timespec="seconds")
                except OSError:
                    pass
            conv["modifiée"] = secours

        return conv

    @staticmethod
    def _entree_index(conv: dict) -> dict:
        """Projection d'une conversation dans l'index — UNE seule définition.

        Elle était écrite à la main dans ``save_conversation``, ce qui allait
        devenir trois copies avec les méthodes de ce chantier. L'index est un
        **cache dérivé** : tout ce qu'il contient se relit dans les fichiers de
        conversation, et :meth:`rebuild_index` le prouve en le reconstruisant.
        C'est aussi pourquoi lui seul n'a pas besoin de ``fsync`` (§1 du
        document de conception).
        """
        msgs = conv.get("messages", [])
        premiers = [m for m in msgs if m.get("role") == "user"]
        return {
            "id": conv.get("id", ""),
            "date": conv.get("date", ""),
            "titre": conv.get("titre", ""),
            "apercu": (premiers[0].get("content", "")[:200] if premiers else ""),
            "modèle": conv.get("modèle", ""),
            "n_messages": len(msgs),
            "modules": conv.get("modules", ["chat"]),
            "modifiée": conv.get("modifiée", ""),
            "n_fichiers": len(conv.get("fichiers_attachés", [])),
        }

    def _maj_index(self, conv: dict) -> None:
        """Insère ou remplace l'entrée, la plus récemment touchée en tête.

        Retrait puis insertion en 0 plutôt qu'une modification en place : c'est
        ce qui donne l'ordre « activité la plus récente d'abord » attendu d'une
        liste de conversations, sans champ de tri supplémentaire à maintenir.
        """
        entree = self._entree_index(conv)
        try:
            with self._index_transaction() as conversations:
                conversations[:] = [
                    c for c in conversations if c.get("id") != entree["id"]
                ]
                conversations.insert(0, entree)
        except Exception:
            logger.exception("Erreur màj index conversations (%s)", entree["id"])

    def _indexer_vectoriel(self, conv: dict) -> None:
        """Indexe la conversation pour ``@historique``.

        ⚠️ **Jamais sur le chemin d'un message.** Cet appel calcule un embedding
        sur 8 000 caractères ; le passer à chaque tour mettrait un modèle sur le
        trajet de la réponse, ce que CLAUDE.md §8 interdit explicitement. La
        cadence appartient à l'appelant (tous les 10 messages et à la
        déconnexion, cf. §3 du document de conception), pas à cette méthode.
        """
        try:
            doc = "\n".join(
                f"[{m.get('role', 'user')}] {m.get('content', '')}"
                for m in conv.get("messages", [])
            )[:8000]
            self._col.upsert(
                documents=[doc],
                ids=[conv["id"]],
                metadatas=[{
                    "id": conv["id"], "date": conv.get("date", ""),
                    "titre": conv.get("titre", ""), "modèle": conv.get("modèle", ""),
                }],
            )
        except Exception:
            logger.exception("Erreur indexation vectorielle conversation %s", conv.get("id"))

    @contextmanager
    def _conversation_transaction(self, conv_id: str):
        """RMW verrouillé d'une conversation : normalisée à l'entrée, indexée à la sortie.

        ``fsync=True`` — c'est LE fichier pour lequel le paramètre a été ajouté à
        ``core/jsonstore.py`` (décision §1 du document de conception).

        Lève :class:`_ConversationAbsente` si le document chargé n'a pas d'``id``,
        ce qui couvre deux cas d'un coup : le fichier n'existe pas, ou il existe
        et n'est pas exploitable. Dans les deux, ``read_json`` a rendu ``{}`` et
        continuer écrirait une conversation neuve **par-dessus** — voir la
        docstring de la sentinelle.
        """
        chemin = self._conv_path(conv_id)
        with transaction(chemin, {}, fsync=True) as conv:
            if not conv.get("id"):
                raise _ConversationAbsente(conv_id)
            self._normaliser(conv, chemin)
            yield conv
            conv["n_messages"] = len(conv.get("messages", []))
            conv["modifiée"] = _horodatage()
        self._maj_index(conv)

    # ── Public API ────────────────────────────────────────────────────────────

    def create_conversation(
        self, titre: str = "", fichiers: list | None = None,
        model: str = "", modules: list | None = None,
    ) -> dict:
        """Crée une conversation vide et la renvoie. Ne génère aucun titre.

        Le titre est laissé vide **exprès** : le générer demande un appel LLM, et
        une conversation sans message n'a rien à résumer. Il est produit après le
        premier tour d'assistant (étape 4), et l'interface affiche d'ici là un
        libellé provisoire.
        """
        conv = {
            "id": str(uuid.uuid4()),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "titre": titre,
            "modèle": model,
            "modules": modules if modules is not None else ["chat"],
            "messages": [],
            "fichiers_attachés": [str(f) for f in (fichiers or [])],
            "résumé_contexte": "",
            "dernière_consolidation": 0,
            "créée": _horodatage(),
            "modifiée": _horodatage(),
            "n_messages": 0,
        }
        write_json(self._conv_path(conv["id"]), conv, fsync=True)
        self._maj_index(conv)
        logger.info("Conversation créée : %s", conv["id"])
        return conv

    def append_messages(
        self, conv_id: str, nouveaux: list[dict], model: str = "",
    ) -> dict | None:
        """Ajoute des messages à une conversation existante. ``None`` si absente.

        ``model`` n'écrase l'existant que s'il est non vide : le modèle actif peut
        changer en cours de conversation, et le dernier utilisé est celui qui
        décrit le mieux l'échange dans la liste.
        """
        try:
            with self._conversation_transaction(conv_id) as conv:
                conv["messages"].extend(
                    {"role": m.get("role", "user"), "content": m.get("content", "")}
                    for m in nouveaux
                )
                if model:
                    conv["modèle"] = model
                return conv
        except _ConversationAbsente:
            logger.warning("Ajout de messages à une conversation absente : %s", conv_id)
            return None
        except PathOutsideDataError:
            logger.warning("Ajout refusé, identifiant invalide : %r", conv_id)
            return None

    def rename_conversation(self, conv_id: str, titre: str) -> bool:
        """Renomme. Utilisé par l'utilisateur ET par le titrage automatique."""
        try:
            with self._conversation_transaction(conv_id) as conv:
                conv["titre"] = titre.strip()[:80]
            return True
        except (_ConversationAbsente, PathOutsideDataError):
            logger.warning("Renommage impossible : %r", conv_id)
            return False

    def set_conversation_files(self, conv_id: str, paths: list) -> list[str] | None:
        """Remplace l'ensemble des fichiers attachés. ``None`` si absente.

        Remplacement de l'ENSEMBLE et non ajout/retrait unitaire : l'interface est
        une liste à cocher, donc c'est la forme de l'interaction réelle, et ça
        supprime toute question d'ordre entre deux requêtes concurrentes.

        ⚠️ **Ne valide pas les chemins.** Le confinement (``resolve_user_path``)
        et l'appartenance au corpus indexé sont l'affaire de l'appelant :
        ce moteur ne connaît ni les dossiers de données ni le RAG, et lui donner
        l'un des deux le coupleraient à des moteurs qu'il n'a pas à construire.
        Les doublons sont écartés en conservant l'ordre posé par l'utilisateur.
        """
        vus: set[str] = set()
        propres: list[str] = []
        for p in paths or []:
            cle = cle_chemin(str(p))
            if cle not in vus:
                vus.add(cle)
                propres.append(str(p))
        try:
            with self._conversation_transaction(conv_id) as conv:
                conv["fichiers_attachés"] = propres
            return propres
        except (_ConversationAbsente, PathOutsideDataError):
            logger.warning("Attachement impossible : %r", conv_id)
            return None

    def add_conversation_files(self, conv_id: str, paths: list) -> list[str] | None:
        """AJOUTE des fichiers aux attachements, sans toucher aux existants.

        Distinct de :meth:`set_conversation_files`, et les deux sont nécessaires :
        le panneau de fichiers coche un ensemble (donc remplace), mais un import
        ajoute au contexte en cours (donc complète). Faire un `set` à l'import
        détacherait en silence ce que l'utilisateur avait déjà mis là.

        Doublons écartés en gardant la PREMIÈRE position : ré-importer un fichier
        déjà attaché ne doit pas le faire remonter dans la liste.
        """
        nouveaux = [str(p) for p in (paths or [])]
        try:
            with self._conversation_transaction(conv_id) as conv:
                fusion = list(conv["fichiers_attachés"])
                vus = {cle_chemin(p) for p in fusion}
                for p in nouveaux:
                    if cle_chemin(p) not in vus:
                        vus.add(cle_chemin(p))
                        fusion.append(p)
                conv["fichiers_attachés"] = fusion
                resultat = list(fusion)
            return resultat
        except (_ConversationAbsente, PathOutsideDataError):
            logger.warning("Ajout de fichiers impossible : %r", conv_id)
            return None

    def set_resume_contexte(self, conv_id: str, texte: str) -> bool:
        """Résumé des fichiers de CETTE conversation, injecté dans son prompt.

        Remplace la clé globale ``résumé_contexte`` de ``context_session.json``,
        qui était partagée par toutes les conversations et remise à zéro à chaque
        import : résumer les fiches d'un fil polluait le contexte de tous les
        autres.
        """
        try:
            with self._conversation_transaction(conv_id) as conv:
                conv["résumé_contexte"] = texte or ""
            return True
        except (_ConversationAbsente, PathOutsideDataError):
            return False

    def conversation_view(self, conv_id: str, sources_indexees=None) -> dict | None:
        """La conversation telle qu'elle part au client, avec ``présent`` par fichier.

        Distincte de :meth:`get_conversation`, qui rend la forme STOCKÉE. Ici
        ``fichiers_attachés`` devient ``[{"chemin", "présent"}]`` — un fichier
        désindexé reste dans la liste, marqué absent, au lieu d'en disparaître
        sans un mot (cf. :func:`croiser_fichiers`).

        ``sources_indexees`` est fourni par l'appelant (``rag.get_indexed_files()``)
        pour la raison expliquée dans :func:`croiser_fichiers`.
        """
        conv = self.get_conversation(conv_id)
        if conv is None:
            return None
        vue = dict(conv)
        vue["fichiers_attachés"] = croiser_fichiers(
            conv.get("fichiers_attachés", []), sources_indexees
        )
        return vue

    def generer_titre(self, conv_id: str) -> Optional[str]:
        """Titre automatique d'une conversation qui n'en a pas encore. Appel LLM.

        Rend le titre posé, ou ``None`` si rien n'a été fait (conversation
        absente, titre déjà présent, pas encore d'échange à résumer).

        ⚠️ **Jamais sur le chemin d'un message.** C'est un appel au modèle, donc
        des secondes ; l'appelant le lance dans un thread après avoir rendu la
        main. Le modèle est LOCAL et non négociable (CLAUDE.md §3.7) : le titrage
        tourne sans que personne ne l'ait demandé, c'est une tâche de fond au sens
        strict — `_generate_title` passe déjà `modele_local_defaut()`.

        Le seuil de deux messages n'est pas cosmétique : titrer une conversation
        qui ne contient que la question de l'utilisateur produit une paraphrase de
        cette question, pas un titre.
        """
        conv = self.get_conversation(conv_id)
        if conv is None or conv.get("titre") or len(conv.get("messages", [])) < 2:
            return None
        titre = self._generate_title(conv["messages"])
        if not titre or not self.rename_conversation(conv_id, titre):
            return None
        return titre.strip()[:80]

    def marquer_consolidation(self, conv_id: str, n_messages: int) -> bool:
        """Note qu'une consolidation a eu lieu à ce nombre de messages.

        C'est ce qui rend le déclenchement **idempotent** : sans cette marque,
        deux tours consécutifs au-delà du seuil relanceraient la consolidation à
        chaque fois, donc un appel LLM par message. Comparer à un multiple exact
        ne suffirait pas — un tour ajoute deux messages, donc `n % 10 == 0`
        sauterait la moitié des franchissements dès qu'un tour n'en ajoute qu'un
        (erreur de streaming, message sans réponse).
        """
        try:
            with self._conversation_transaction(conv_id) as conv:
                conv["dernière_consolidation"] = int(n_messages)
            return True
        except (_ConversationAbsente, PathOutsideDataError):
            return False

    def indexer_vectoriel(self, conv_id: str) -> bool:
        """Rend la conversation trouvable par ``@historique``. Best-effort.

        Enveloppe publique de :meth:`_indexer_vectoriel` — le routeur n'a pas à
        appeler un attribut privé, et surtout pas à charger la conversation
        lui-même pour le faire.

        Rend ``False`` sans lever si la conversation est absente ; l'indexation
        elle-même absorbe déjà l'absence du modèle d'embedding (recherche
        sémantique indisponible ≠ panne).
        """
        conv = self.get_conversation(conv_id)
        if conv is None:
            return False
        self._indexer_vectoriel(conv)
        return True

    def rebuild_index(self) -> int:
        """Reconstruit ``conversations.json`` depuis les fichiers. Rend leur nombre.

        Ce que cette méthode prouve autant qu'elle répare : **l'index est un
        cache**, entièrement dérivable des conversations. C'est ce qui justifie
        de ne PAS lui appliquer ``fsync`` alors que les conversations l'ont — la
        seule chose qu'une coupure de courant puisse lui faire perdre est
        reconstructible ici.

        Tri par ``modifiée`` décroissant, pour retrouver l'ordre que
        :meth:`_maj_index` maintient au fil de l'eau.
        """
        entrees: list[dict] = []
        for fichier in sorted(self._dir.glob("*.json")):
            if fichier.name == self._index_path.name:
                continue
            conv = read_json(fichier, None)
            if not isinstance(conv, dict) or not conv.get("id"):
                logger.warning("Fichier de conversation ignoré (illisible) : %s", fichier.name)
                continue
            entrees.append(self._entree_index(self._normaliser(conv, fichier)))
        entrees.sort(key=lambda e: e.get("modifiée", ""), reverse=True)
        write_json(self._index_path, {"conversations": entrees})
        logger.info("Index des conversations reconstruit : %d entrées", len(entrees))
        return len(entrees)

    def save_conversation(
        self, messages: list[dict], model: str = "", modules: list | None = None
    ) -> str:
        """Écrit une conversation complète d'un coup, titre généré. Rend son id.

        Chemin historique, conservé tel quel pour la déconnexion du WebSocket
        tant que l'étape 4 du chantier ne l'a pas remplacé. Réécrit ici sur les
        primitives ci-dessus au lieu de composer son fichier, son entrée d'index
        et son upsert à la main — les trois existaient en double exemplaire dès
        que :meth:`create_conversation` est arrivée.
        """
        conv = {
            "id": str(uuid.uuid4()),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "titre": self._generate_title(messages),
            "modèle": model,
            "modules": modules if modules is not None else ["chat"],
            "messages": list(messages),
            "fichiers_attachés": [],
            "résumé_contexte": "",
            "dernière_consolidation": 0,
            "créée": _horodatage(),
            "modifiée": _horodatage(),
            "n_messages": len(messages),
        }
        try:
            write_json(self._conv_path(conv["id"]), conv, fsync=True)
        except Exception:
            logger.exception("Erreur sauvegarde conversation %s", conv["id"])
            return conv["id"]

        self._maj_index(conv)
        self._indexer_vectoriel(conv)
        logger.info("Conversation sauvegardée : %s — %s", conv["id"], conv["titre"])
        return conv["id"]

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
        """La conversation dans sa forme STOCKÉE, complétée en mémoire.

        La normalisation ne réécrit pas le fichier (cf. :meth:`_normaliser`) :
        les consommateurs — le module Historique, ``consolidate_history`` —
        reçoivent les clés du nouveau modèle même sur une conversation de mai,
        sans qu'une simple lecture ne touche au disque.

        Rend ``None`` plutôt qu'un ``{}`` pour un fichier illisible : la route
        appelante en fait un 404, et un dict vide se serait propagé comme une
        conversation valide et sans messages.
        """
        try:
            conv_path = self._conv_path(conv_id)
        except PathOutsideDataError:
            logger.warning("Identifiant de conversation refusé : %r", conv_id)
            return None
        conv = read_json(conv_path, None)
        if not isinstance(conv, dict) or not conv.get("id"):
            return None
        return self._normaliser(conv, conv_path)

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
