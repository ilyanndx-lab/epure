"""Routeur du module Chat. Monté avec prefix "" : WebSocket /ws/chat, skill
SSE /skills/résumé, et la recherche web @web (perform_web_search).

Moteurs partagés (llm, memory, rag, orchestrator, history_engine,
consolidation_engine, usage_tracker, provider_of) injectés via core.runtime.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from threading import Thread
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import ws_require_token
from core.embedding_install import EmbeddingIndisponible
from core.instance import modele_local_defaut
from core.paths import PathOutsideDataError, cle_chemin, resolve_user_path
from core.rag import RAGEngine
from core.runtime import (
    SSE_HEADERS,
    consolidation_engine,
    history_engine,
    llm,
    memory,
    orchestrator,
    provider_of as _provider_of,
    rag,
    usage_tracker,
)
from core.citations import (
    RapportCitations, construire_reference, extraire_rangs_cites, extraire_urls, valider_citations,
)
from core.webcontent import recuperer_contenu
from core.websearch import (
    PREFIXE_ERREUR,
    TRACE_LISTE_MAX,
    TRACE_MAX_ETAPES,
    RechercheWebErreur,
    ResultatWeb,
    formater_pour_llm,
    rechercher,
    tronquer_champ,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Recherche web (@web) ─────────────────────────────────────────────────────
# Logique de recherche déplacée dans core/websearch.py le 2026-09-02 (RAG et
# Atelier en ont besoin aussi). `perform_web_search` reste ici comme point
# d'entrée historique du websocket, pour ne rien changer côté chemin de
# streaming des tokens.


def _rechercher_pour_prompt(
    query: str, on_etape=None,
) -> tuple[str, list[ResultatWeb]]:
    """Recherche + formatage, en conservant AUSSI les résultats structurés.

    `perform_web_search` (ci-dessous) ne rend que le texte, pour ses
    appelants historiques. La boucle websocket a besoin des deux : le texte
    pour le prompt, et les `ResultatWeb` pour construire l'ensemble de
    référence de `core.citations` puis le bloc Sources (§4) — deux usages en
    aval qui n'existaient pas quand `perform_web_search` a été écrit.

    `on_etape` (optionnel) est transmis tel quel à `core.websearch.rechercher`
    — c'est le canal de la trace de déroulé (§1) : cette fonction n'a rien à
    en savoir, elle ne fait que le relayer.
    """
    try:
        resultats = rechercher(query, on_etape=on_etape)
    except RechercheWebErreur as exc:
        return f"{PREFIXE_ERREUR}{exc}", []
    # Phase 4 : remplace le snippet DDG (~150 caractères, écrit par DDG) par du
    # contenu réel des pages les plus pertinentes, reclassé par similarité
    # avec `query` — AVANT `formater_pour_llm`, qui ne change pas : il ne fait
    # que mettre en forme des `ResultatWeb`, quelle que soit la richesse de
    # leur `extrait`. `recuperer_contenu` ne lève jamais — dégradation par
    # page et, si l'embedding manque, dégradation de toute l'étape vers les
    # extraits DDG d'origine (cf. sa docstring).
    resultats = recuperer_contenu(resultats, query, on_etape=on_etape)
    return formater_pour_llm(resultats), resultats


def perform_web_search(query: str) -> str:
    """Recherche web DuckDuckGo, formatée pour un prompt LLM.

    Délègue à `core.websearch` : ce module gardait, avant le 2026-09-02, sa
    propre logique de recherche et de parsing HTML — une regex qui ne
    capturait que le TEXTE des ancres de résultat, jamais leur `href`, d'où
    des sources inventées par le LLM (aucune URL ne lui était jamais fournie).
    """
    texte, _resultats = _rechercher_pour_prompt(query)
    return texte


def _construire_web_ctx(web_results: str) -> str:
    """Construit le contexte @web injecté au prompt à partir du retour de
    `perform_web_search`.

    Trois cas, PAS deux : un échec (préfixe `PREFIXE_ERREUR`) doit rester
    distinct d'un résultat réel ET d'une recherche légitimement vide. Les
    confondre avec un résultat ferait présenter le message d'erreur lui-même
    comme un « résultat récent à citer » — une fausse source fabriquée à
    partir d'un texte d'erreur, pire que le silence corrigé par ailleurs. Les
    confondre avec « aucun résultat » recréerait l'échec silencieux que la
    correction du parsing HTML (core/websearch.py) visait à éliminer.

    Le contrat de citation (branche « résultat ») est le premier des deux
    verrous contre les sources inventées — le second est `core.citations`,
    appliqué APRÈS génération (cf. `_verifier_citations`) : un
    prompt ne garantit rien à lui seul, il ne fait que réduire la probabilité
    d'une invention. D'où l'insistance sur « jamais d'URL » (le domaine que
    `formater_pour_llm` fournit suffit à juger la crédibilité d'une source)
    et « jamais un numéro absent » (répété explicitement : c'est justement ce
    que `valider_citations` vérifiera ensuite).
    """
    if web_results.startswith(PREFIXE_ERREUR):
        return (
            f"Recherche web : {web_results} Ce n'est PAS un résultat de recherche, "
            "ne le cite pas comme source. Signale à l'utilisateur que la recherche web "
            "a échoué, et réponds à partir de tes connaissances si pertinent."
        )
    if web_results:
        return (
            "Résultats de recherche web récents (peuvent compléter tes connaissances) :\n"
            f"{web_results}\n\n"
            "Si pertinent, intègre ces informations dans ta réponse. Cite tes sources "
            "UNIQUEMENT par leur numéro entre crochets, par exemple [1] — n'écris "
            "JAMAIS d'URL, et ne cite JAMAIS un numéro absent de la liste ci-dessus. "
            "Si la liste ne permet pas de répondre à la question, dis-le explicitement "
            "plutôt que de compléter avec tes connaissances sans le signaler."
        )
    return (
        "Recherche web : aucun résultat exploitable trouvé pour cette requête. "
        "Réponds à partir de tes connaissances en le signalant."
    )


def _sources_citees(reponse: str, resultats_web: list[ResultatWeb]) -> list[dict]:
    """Sources RÉELLEMENT citées dans `reponse`, sous forme STRUCTURÉE.

    JAMAIS insérées dans le contenu persisté — c'est tout le point de ce
    correctif. La version précédente ajoutait un bloc texte « Sources » (avec
    URL complète) au `content` du message avant de l'écrire : ce `content`
    repart tel quel dans l'historique du prompt au tour suivant, ce qui
    réintroduisait par la porte de derrière les URLs complètes que le contrat
    de citation (`_construire_web_ctx`, phase 2) retire justement du contexte
    — et enseignait au modèle qu'écrire des URLs complètes est normal dans
    cette conversation. Cette liste part en métadonnée séparée
    (`core.history.HistoryEngine.append_messages`, paramètre `sources`), que
    le frontend rend à côté du texte sans jamais la faire repasser par le
    modèle.

    UNIQUEMENT les [n] réellement cités, dans leur ordre d'apparition — les
    résultats récupérés mais non cités n'y figurent pas (leur trace est la
    phase 3, pas celle-ci). Extraction bon marché (une regex sur le texte
    déjà généré) : sans rapport avec `valider_citations`, qui doit rester
    après `done` (§3 de la tâche) — celle-ci reste sur le chemin normal de
    fin de tour, c'est juste finir de composer ce qui part sur le disque.
    """
    if not reponse or not resultats_web:
        return []
    rangs = extraire_rangs_cites(reponse, {r.rang for r in resultats_web})
    if not rangs:
        return []
    par_rang = {r.rang: r for r in resultats_web}
    return [
        {"rang": n, "titre": par_rang[n].titre, "url": par_rang[n].url}
        for n in rangs if n in par_rang
    ]


def _verifier_citations(
    reponse: str, resultats_web: list[ResultatWeb], user_text: str, urls_rag,
) -> Optional[RapportCitations]:
    """Second verrou (core.citations) — calcul PUR, sans effet de bord.

    Appelée avant la persistance (contrairement à la phase 2, où cette
    vérification tournait après l'événement `done`) : cette phase demande de
    persister une éventuelle anomalie comme étape `citations_invalides` de
    `trace_recherche` (§1), et `core/history.py` n'a qu'un `append_messages`
    — pas de correctif de message déjà écrit. Le calcul devait donc précéder
    l'écriture, pas la suivre.

    Ce déplacement ne contredit pas l'esprit de la règle de la phase 2 (« ne
    pas ralentir le chemin chaud du token ») : `valider_citations` est une
    passe de regex pure, sans LLM ni réseau, déjà exécutée UNE FOIS APRÈS la
    fin complète du streaming — la faire tourner quelques lignes plus tôt ne
    coûte rien de plus. Ce que la règle protégeait réellement (le flux de
    tokens) reste inchangé : rien ici ne touche `accumulated`/`_final` avant
    que le dernier token soit parti.

    N'agit JAMAIS sur `reponse` — signale, ne corrige pas (cf. docstring de
    `core.citations` : une suppression silencieuse recrée la classe de bug
    qu'on élimine).
    """
    if not reponse:
        return None
    reference = construire_reference(
        urls_web=[r.url for r in resultats_web],
        rangs_web=[r.rang for r in resultats_web],
        urls_rag=urls_rag,
        texte_utilisateur=user_text,
    )
    return valider_citations(reponse, reference)


def _construire_trace_finale(
    etapes: list[dict], rapport: Optional[RapportCitations], a_des_resultats_recherche: bool,
) -> list[dict]:
    """Assemble la trace persistée : les étapes de recherche (`core.websearch`,
    poussées en direct pendant le tour, vide s'il n'y en a pas eu) + l'étape
    `citations_invalides` si le second verrou a trouvé une anomalie — même
    liste, même schéma extensible (§1 de la tâche initiale).

    IMPÉRATIF (suivi immédiat) — cette étape est construite QUELLE QUE SOIT
    la présence d'une recherche ce tour. La version précédente ne la
    persistait/affichait que si `@web` avait été utilisé, au motif que
    « la trace n'a pas de panneau où s'accrocher » sans recherche — c'était
    l'inverse de la réalité : `@web` est un override manuel rare, et la
    grande majorité des tours (aucune recherche) est précisément là où le
    modèle invente le plus souvent une URL de mémoire. Détecter surtout là où
    c'est rare et se taire là où c'est fréquent était le défaut d'origine.
    `trace_recherche` peut donc ne contenir QUE cette étape, sans aucune
    étape de recherche — le frontend doit l'afficher sans jamais annoncer une
    recherche qui n'a pas eu lieu (cf. `resumeTrace`, Component.tsx).

    `a_des_resultats_recherche` distingue deux affirmations de force
    différente (§3) :
      - une recherche a eu lieu et a rendu des résultats CETTE fois-ci
        (`resultats_web` non vide) : une URL absente de ces résultats est une
        anomalie contre une référence VÉRIFIÉE — affirmation forte,
        `"verifiees_contre": "recherche"` ;
      - aucun résultat de recherche à comparer (pas de `@web`, ou recherche
        vide/en échec) : seules les sources RAG et le message utilisateur ont
        été consultés. Une URL qui n'y figure pas n'est pas prouvée fausse —
        seulement NON VÉRIFIÉE, `"verifiees_contre": "aucune_source"`. Un
        badge qui affirme « faux » à chaque URL de mémoire non vérifiée
        crierait au loup et finirait ignoré — la distinction existe pour ça.

    Bornée à `TRACE_MAX_ETAPES` ICI, au moment de la persistance : c'est le
    point où toutes les sources (recherche + citations) sont réunies, donc le
    seul endroit qui connaît le total réel.
    """
    finale = list(etapes)
    if rapport is not None and rapport.a_des_anomalies():
        finale.append({
            "etape": "citations_invalides",
            "rangs": rapport.rangs_hors_plage[:TRACE_LISTE_MAX],
            "urls": [tronquer_champ(u) for u in rapport.urls_non_reconnues[:TRACE_LISTE_MAX]],
            "verifiees_contre": "recherche" if a_des_resultats_recherche else "aucune_source",
        })
    return finale[:TRACE_MAX_ETAPES]


def _finaliser_citations_et_trace(
    conv_id: str, reponse: str, resultats_web: list[ResultatWeb],
    user_text: str, urls_rag, etapes_recherche: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Finalisation commune aux deux chemins de fin de tour (direct et
    pipeline) : sources citées + trace à persister, anomalies journalisées.

    `_verifier_citations` tourne à CHAQUE tour, avec ou sans `@web` — c'est
    le point de cette révision (cf. `_construire_trace_finale`). Sans
    recherche, l'ensemble de référence se réduit aux sources RAG et aux URLs
    du message utilisateur (`urls_rag`/`user_text`, inchangés). La réponse
    elle-même n'est jamais modifiée.
    """
    sources = _sources_citees(reponse, resultats_web)
    rapport = _verifier_citations(reponse, resultats_web, user_text, urls_rag)
    trace = _construire_trace_finale(etapes_recherche, rapport, bool(resultats_web))
    if rapport is not None and rapport.aucune_citation_malgre_contexte:
        logger.warning("Réponse sans aucune citation malgré un contexte web fourni (conv=%s)", conv_id)
    return sources, trace


# ── Skill /résumé (SSE) ──────────────────────────────────────────────────────

async def _stream_résumé_sse(conversation_id: str = ""):
    """Résumé des fichiers ATTACHÉS À UNE CONVERSATION.

    Lisait `ctx["fichiers_actifs"]`, la liste globale retirée le 2026-08-27 :
    demander un résumé depuis un fil résumait donc les fichiers du dernier import,
    quel que soit le fil. L'identifiant est requis — sans lui il n'y a plus de
    notion de « fichiers actifs » à laquelle se rattacher, et deviner serait
    reproduire exactement le défaut qu'on retire.
    """
    conv = history_engine.get_conversation(conversation_id) if conversation_id else None
    if conv is None:
        yield (
            f"data: {json.dumps({'type': 'error', 'content': 'Conversation inconnue : impossible de savoir quels fichiers résumer.'}, ensure_ascii=False)}\n\n"
        )
        return
    active_files = conv.get("fichiers_attachés", [])
    if not active_files:
        yield (
            f"data: {json.dumps({'type': 'error', 'content': 'Aucun fichier attaché à cette conversation. Ajoutez-en via le panneau 📎.'}, ensure_ascii=False)}\n\n"
        )
        return

    loop = asyncio.get_running_loop()
    text_parts: list[str] = []
    for path in active_files:
        try:
            text = await loop.run_in_executor(None, RAGEngine.read_file_text, path)
            text_parts.append(text[:3000])
        except Exception:
            logger.exception("Erreur lecture fichier %s pour /résumé", path)

    if not text_parts:
        yield f"data: {json.dumps({'type': 'error', 'content': 'Impossible de lire les fichiers actifs.'})}\n\n"
        return

    combined = "\n\n---\n\n".join(text_parts)[:12000]
    prompt = (
        "Résume en 100-150 mots maximum ces documents de cours. "
        "Indique les sujets principaux et les notions clés. Sois factuel.\n\n"
        f"Contenu :\n{combined}"
    )
    # LOCAL, jamais le modèle du chat. Ce flux héritait de `ctx["modèle_actif"]`
    # sans le moindre garde-fou : demander un résumé de ses fiches envoyait leur
    # CONTENU (jusqu'à 12 000 caractères, cf. `combined` ci-dessus) au fournisseur
    # cloud choisi pour discuter — sans que rien ne le dise, et sans qu'aucun
    # `use_cloud` n'existe pour le refuser. C'était le site le plus exposé du lot.
    #
    # Pas de paramètre `use_cloud` ici, et c'est délibéré : `/skills/résumé` est
    # déclenché par un bouton unique, sans écran de choix. Ajouter le drapeau sans
    # interface pour le poser produirait une option que personne ne peut atteindre.
    # Le jour où ce choix existe côté client, ce site prendra `modele_pour_tache`
    # comme les autres.
    model_override = modele_local_defaut()
    queue: asyncio.Queue = asyncio.Queue()

    def _worker(msgs, q, lp, model):
        try:
            for token in llm.stream(msgs, model=model):
                asyncio.run_coroutine_threadsafe(q.put(token), lp)
        except Exception as exc:
            logger.exception("Erreur streaming /skills/résumé")
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
            yield f"data: {json.dumps({'type': 'error', 'content': item['error']})}\n\n"
            return
        if not isinstance(item, str):
            # Sentinelles du générateur (`__stats__`, `__reasoning__`) : ce flux-ci
            # ne sert qu'à afficher un résumé, il n'a rien à en faire.
            #
            # Bug PRÉEXISTANT, pas une précaution ajoutée pour le raisonnement :
            # `__stats__` existe depuis longtemps et était sérialisé tel quel en
            # `{"type": "token", "content": {"__stats__": true, …}}`. Le
            # consommateur fait `last.content + ev.content`, donc un
            # « [object Object] » se collait à la fin de chaque résumé, avec
            # n'importe quel modèle. Le raisonnement n'aurait fait qu'en ajouter
            # un second, plus gros.
            continue
        yield f"data: {json.dumps({'type': 'token', 'content': item}, ensure_ascii=False)}\n\n"


class ResumeRequest(BaseModel):
    conversation_id: str = ""


@router.post("/skills/résumé")
async def skills_résumé(req: ResumeRequest | None = None):
    """Corps optionnel pour rester compatible avec un client qui n'en envoie pas.

    Sans `conversation_id`, le flux rend une erreur explicite plutôt qu'un 422 :
    c'est un flux SSE, et une erreur de validation FastAPI n'y serait pas lisible
    par le consommateur, qui n'écoute que des événements `data:`.
    """
    return StreamingResponse(
        _stream_résumé_sse(req.conversation_id if req else ""),
        media_type="text/event-stream", headers=SSE_HEADERS,
    )


# ── Conversations ────────────────────────────────────────────────────────────
#
# Ces routes vivent dans le module CHAT et non dans le module Historique, qui
# manipule pourtant le même magasin. La raison est son manifeste :
# `history` porte `removable: true`. Y loger le cycle de vie des conversations
# voudrait dire que désactiver l'Historique depuis les Réglages décapite le chat.
# `/history*` reste la VUE de parcours et de recherche sémantique ; le cycle de
# vie est ici. Un seul moteur derrière les deux (`core.runtime.history_engine`),
# donc pas de second magasin — cf. docs/conversations-persistees.md §3.
#
# ⚠️ Le module chat est monté avec `prefix: ""` (son manifeste), donc chaque
# route s'écrit préfixée À LA MAIN. Sans le `/chat/`, collision silencieuse avec
# une route du cœur (CLAUDE.md §3.3).


class ConversationCreate(BaseModel):
    titre: str = ""
    fichiers: list[str] | None = None
    #: Messages préexistants — UN seul appelant : la reprise de ce qui était à
    #: l'écran au moment de la mise à jour (étape 7 du chantier). Les
    #: conversations naissent normalement vides et se remplissent tour par tour.
    #:
    #: `list` NUE et non `list[dict]`, délibérément. Le contenu vient d'un
    #: `localStorage` écrit par une version ANTÉRIEURE du frontend : sa forme
    #: n'est pas garantie, et `list[dict]` fait répondre 422 à Pydantic dès
    #: qu'une seule entrée n'est pas un objet — donc **perd toute la
    #: conversation** pour un message abîmé. Le moteur réduit chaque entrée à
    #: `{role, content}` et écarte le reste (`_messages_propres`) : filtrer est
    #: ici la bonne réponse, refuser ne l'est pas. Mesuré en écrivant le test :
    #: une chaîne dans la liste suffisait à faire échouer la reprise entière.
    messages: list | None = None


class ConversationPatch(BaseModel):
    #: Les deux champs sont OPTIONNELS et distingués par `None` : envoyer
    #: `{"instruction": ""}` doit EFFACER la consigne, pas être confondu avec
    #: « champ non fourni ». Une valeur par défaut `""` rendrait les deux
    #: indistinguables et empêcherait de vider le champ.
    titre: Optional[str] = None
    instruction: Optional[str] = None


class FichiersRequest(BaseModel):
    paths: list[str]


def _corpus_ou_inconnu() -> Optional[list]:
    """Chemins indexés, ou ``None`` si le corpus n'est pas interrogeable.

    **Pour la LECTURE seulement.** Ouvrir une conversation ne doit jamais
    dépendre de la pile d'embedding : dans un paquet fraîchement installé les
    90 Mo du modèle ne sont pas encore là, et `rag` est un `_LazyEngine` dont le
    premier accès lève `EmbeddingIndisponible` — que `main.py` traduit en 503 par
    un gestionnaire GLOBAL. Sans cette rattrapage, `GET /chat/conversations/{id}`
    répondrait 503 et l'utilisateur ne pourrait plus lire ses conversations parce
    que la recherche documentaire n'est pas prête. Ce serait l'incident du §8
    (« la recherche documentaire répond 500 dans un paquet livré ») déplacé d'un
    cran, sur une fonction qui n'a rien à voir.

    `None` n'est pas une liste vide : il se propage en `présent: None` jusqu'au
    client (cf. `core.history.croiser_fichiers`), c'est-à-dire « on ne sait pas »
    et non « ce fichier a disparu ».
    """
    try:
        return rag.get_indexed_files()
    except EmbeddingIndisponible:
        return None
    except Exception:
        logger.exception("Corpus indexé illisible — fichiers marqués « inconnu »")
        return None


def _valider_fichiers(paths: list) -> list[str]:
    """Confine les chemins puis exige leur présence dans le corpus indexé.

    Deux refus distincts, et les confondre serait une faute :

    * hors des dossiers de données → **403**, c'est une tentative de sortie
      (`resolve_user_path`, CLAUDE.md §6). On renvoie le chemin RÉSOLU, jamais la
      chaîne d'origine, sinon la vérification ne porte pas sur ce qui sera lu ;
    * dans les dossiers mais absent du corpus → **400**, c'est une demande
      absurde : attacher un fichier que le moteur n'a pas indexé produirait une
      conversation dont le contexte ne contient rien, sans que rien ne le dise —
      exactement le symptôme « indexé à zéro chunk, en silence » (§3.3 bis).

    ⚠️ Contrairement à :func:`_corpus_ou_inconnu`, on ne rattrape PAS
    `EmbeddingIndisponible` : elle remonte au gestionnaire global, qui rend un 503
    porteur de l'état d'installation. C'est volontaire et c'est l'asymétrie du
    chantier — **lire une conversation ne doit jamais échouer, y attacher un
    fichier peut attendre que le corpus existe.** Le second est une action
    explicite de l'utilisateur, à qui l'on doit une explication, pas un silence.
    """
    resolus: list[str] = []
    for p in paths:
        try:
            resolus.append(str(resolve_user_path(p)))
        except PathOutsideDataError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    connus = {cle_chemin(str(s)) for s in rag.get_indexed_files()}
    inconnus = [p for p in resolus if cle_chemin(p) not in connus]
    if inconnus:
        raise HTTPException(
            status_code=400,
            detail=("Fichiers non indexés (les importer d'abord) : "
                    + ", ".join(Path(p).name for p in inconnus)),
        )
    return resolus


@router.post("/chat/conversations")
async def conversation_create(req: ConversationCreate | None = None):
    loop = asyncio.get_running_loop()
    req = req or ConversationCreate()
    fichiers = _valider_fichiers(req.fichiers) if req.fichiers else []
    conv = await loop.run_in_executor(
        None, lambda: history_engine.create_conversation(
            titre=req.titre, fichiers=fichiers, messages=req.messages)
    )
    return {"id": conv["id"], "titre": conv["titre"], "créée": conv["créée"],
            "modifiée": conv["modifiée"], "fichiers_attachés": conv["fichiers_attachés"],
            "messages": conv["messages"], "n_messages": conv["n_messages"]}


@router.get("/chat/conversations")
async def conversation_list(days: int = 0, limit: int = 100, offset: int = 0):
    """Liste l'INDEX — jamais les messages, qui pèsent ~6,7 Ko par conversation.

    ``days=0`` par défaut, c'est-à-dire tout : une liste de conversations sert à
    retrouver un fil ancien, la borner à 30 jours en cacherait la moitié. Le
    module Historique garde son défaut à 30, il répond à un autre besoin.
    """
    loop = asyncio.get_running_loop()
    toutes = await loop.run_in_executor(None, history_engine.list_conversations, days)
    debut = max(0, offset)
    fin = debut + max(0, min(limit, 500))
    return {"conversations": toutes[debut:fin], "total": len(toutes)}


@router.get("/chat/conversations/{conv_id}")
async def conversation_get(conv_id: str):
    """La conversation entière, fichiers attachés marqués ``présent``.

    Ne 503 jamais : cf. :func:`_corpus_ou_inconnu`. ``corpus_interrogeable`` dit
    au client si les ``présent`` valent quelque chose, pour qu'il puisse afficher
    « état inconnu » plutôt qu'une croix mensongère.
    """
    loop = asyncio.get_running_loop()
    corpus = await loop.run_in_executor(None, _corpus_ou_inconnu)
    vue = await loop.run_in_executor(
        None, history_engine.conversation_view, conv_id, corpus
    )
    if vue is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    vue["corpus_interrogeable"] = corpus is not None
    return vue


#: Longueur maximale de la consigne d'un fil.
#:
#: Elle part dans le prompt système à CHAQUE tour de cette conversation. Sans
#: borne, y coller un document entier coûterait sa place à tous les messages
#: suivants, sans que rien ne le signale — et le premier symptôme serait une
#: fenêtre de contexte pleine, loin de sa cause. 4 000 caractères laissent de
#: quoi écrire un cadrage précis (~1 000 tokens).
MAX_INSTRUCTION = 4000


@router.patch("/chat/conversations/{conv_id}")
async def conversation_patch(conv_id: str, req: ConversationPatch):
    """Renomme et/ou pose la consigne du fil. Au moins l'un des deux.

    Refuse au-delà de `MAX_INSTRUCTION` au lieu de TRONQUER : une consigne
    coupée au milieu reste une consigne, que le modèle suivrait à moitié. Mieux
    vaut un refus visible qu'une obéissance partielle à un texte que
    l'utilisateur croit complet.
    """
    if req.titre is None and req.instruction is None:
        raise HTTPException(status_code=400, detail="Rien à modifier")

    loop = asyncio.get_running_loop()
    rendu: dict = {"ok": True}

    if req.titre is not None:
        titre = req.titre.strip()
        if not titre:
            raise HTTPException(status_code=400, detail="Titre vide")
        ok = await loop.run_in_executor(
            None, history_engine.rename_conversation, conv_id, titre
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Conversation introuvable")
        rendu["titre"] = titre[:80]

    if req.instruction is not None:
        instruction = req.instruction.strip()
        if len(instruction) > MAX_INSTRUCTION:
            raise HTTPException(
                status_code=400,
                detail=(f"Consigne trop longue ({len(instruction)} caractères, "
                        f"maximum {MAX_INSTRUCTION})."),
            )
        ok = await loop.run_in_executor(
            None, history_engine.set_instruction, conv_id, instruction
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Conversation introuvable")
        rendu["instruction"] = instruction

    return rendu


@router.delete("/chat/conversations/{conv_id}")
async def conversation_delete(conv_id: str):
    """404 sur une conversation absente, plutôt qu'un ``{"ok": true}`` menteur.

    `delete_conversation` est idempotent et rend `True` même sur un identifiant
    inconnu (il nettoie l'index et le vecteur au cas où) ; l'existence est donc
    vérifiée ici. Un client qui supprime deux fois doit voir la différence,
    sinon un bug d'identifiant passe pour un succès.
    """
    loop = asyncio.get_running_loop()
    if await loop.run_in_executor(None, history_engine.get_conversation, conv_id) is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    await loop.run_in_executor(None, history_engine.delete_conversation, conv_id)
    return {"ok": True}


@router.put("/chat/conversations/{conv_id}/fichiers")
async def conversation_fichiers(conv_id: str, req: FichiersRequest):
    """Remplace l'ENSEMBLE des fichiers attachés.

    Un `PUT` de l'ensemble et non un `POST`/`DELETE` par fichier : l'interface est
    une liste à cocher, donc c'est la forme de l'interaction réelle, et ça
    supprime toute question d'ordre entre deux requêtes concurrentes.
    """
    resolus = _valider_fichiers(req.paths)
    loop = asyncio.get_running_loop()
    rendus = await loop.run_in_executor(
        None, history_engine.set_conversation_files, conv_id, resolus
    )
    if rendus is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return {"fichiers_attachés": [{"chemin": p, "présent": True} for p in rendus]}


# ── WebSocket de chat ────────────────────────────────────────────────────────

#: Nombre de messages entre deux consolidations d'une même conversation.
#: Ancien déclencheur : `len(history) >= 10` à la déconnexion, donc AU PLUS UNE
#: fois par connexion, et jamais pour qui laisse son onglet ouvert des heures.
_SEUIL_CONSOLIDATION = 10


def _travaux_apres_tour(conv_id: str, use_cloud: bool, annoncer_titre) -> None:
    """Titrage, consolidation, indexation vectorielle — **hors** du chemin du message.

    Les trois étaient accrochés à ``WebSocketDisconnect``, qui n'est plus une
    frontière signifiante : la conversation est désormais écrite à chaque tour,
    et une déconnexion n'est qu'un onglet qu'on ferme. Ils sont donc rejoués ici,
    dans un thread, APRÈS que la réponse est partie.

    ⚠️ **L'indexation vectorielle n'est pas par tour, et c'est le point.**
    ``_indexer_vectoriel`` calcule un embedding sur 8 000 caractères ; l'appeler
    à chaque message mettrait un modèle sur le trajet de la réponse, ce que
    CLAUDE.md §8 interdit explicitement (« ne rien mettre de bloquant sur ce
    chemin »). Elle suit donc la cadence de la consolidation. Conséquence
    assumée : ``@historique`` ne retrouve une conversation qu'à partir de son
    dixième message, plus le rattrapage de la déconnexion.

    Tout est best-effort : ce thread ne doit jamais faire tomber une réponse déjà
    livrée à l'utilisateur.
    """
    try:
        titre = history_engine.generer_titre(conv_id)
        if titre:
            annoncer_titre(titre)
    except Exception:
        logger.exception("Titrage automatique impossible (%s)", conv_id)

    try:
        conv = history_engine.get_conversation(conv_id)
        if conv is None:
            return
        n = len(conv.get("messages", []))
        if n < _SEUIL_CONSOLIDATION:
            return
        if (n - conv.get("dernière_consolidation", 0)) < _SEUIL_CONSOLIDATION:
            return
        # Marquer AVANT de travailler : si la consolidation échoue, on ne veut pas
        # la relancer à chaque message suivant. C'est une tâche d'agrément, pas
        # une transaction dont il faudrait garantir l'exécution.
        history_engine.marquer_consolidation(conv_id, n)
        history_engine.indexer_vectoriel(conv_id)
        consolidation_engine.consolidate_history(conv_id, use_cloud)
    except Exception:
        logger.exception("Travaux de fin de tour impossibles (%s)", conv_id)


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    if not await ws_require_token(websocket):
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()
    _last_model: list[str] = [llm._model]
    #: Dernière conversation touchée sur CETTE connexion — sert au rattrapage de
    #: la déconnexion. L'identifiant de travail, lui, est relu à CHAQUE message :
    #: le client peut changer de conversation sans rouvrir le socket, et une
    #: variable de connexion figerait la première ouverte.
    _derniere_conv: list[Optional[str]] = [None]

    def _annoncer_titre_depuis_thread(conv_id: str):
        """Renvoie une closure qui pousse `{"type": "titre"}` vers ce socket.

        Passer par ``run_coroutine_threadsafe`` : l'appelant est un ``Thread``,
        et toucher le WebSocket depuis un autre fil sans repasser par la boucle
        corromprait le protocole. L'échec est avalé — le socket peut avoir été
        fermé entre-temps, ce qui est normal et sans conséquence : le titre est
        déjà sur le disque, la liste le montrera au prochain chargement.
        """
        def _pousser(titre: str) -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    websocket.send_text(json.dumps(
                        {"type": "titre", "id": conv_id, "titre": titre},
                        ensure_ascii=False,
                    )),
                    loop,
                )
            except Exception:
                logger.debug("Titre non annoncé (socket fermé ?) pour %s", conv_id)
        return _pousser

    async def _enregistrer_reponse(
        conv_id: str, texte: str,
        sources: list[dict] | None = None, trace_recherche: list[dict] | None = None,
    ) -> dict:
        """Seconde transaction du tour : la réponse, puis les travaux de fond.

        Séparée de l'ajout du message utilisateur, et pas par commodité : entre
        les deux il y a le streaming, c'est-à-dire des secondes. Une transaction
        unique qui enjamberait le flux garderait le fichier verrouillé pendant
        toute la génération, et un envoi concurrent dans la même conversation
        attendrait la fin de la réponse précédente.

        `sources` (optionnel) part en métadonnée du message, JAMAIS collée à
        `texte` : cf. `_sources_citees`, dont c'est tout le sujet. `trace_recherche`
        suit exactement le même principe et le même mécanisme (`core.history.
        HistoryEngine.append_messages`), pour la même raison : un déroulé de
        recherche est de la PRÉSENTATION, pas du contenu que le modèle doit
        relire au tour suivant.
        """
        conv = await loop.run_in_executor(
            None, lambda: history_engine.append_messages(
                conv_id, [{
                    "role": "assistant", "content": texte,
                    "sources": sources or [], "trace_recherche": trace_recherche or [],
                }],
                model=_last_model[0],
            )
        )
        use_cloud = bool(memory.get_context().get("consolidation_cloud", False))
        Thread(
            target=_travaux_apres_tour,
            args=(conv_id, use_cloud, _annoncer_titre_depuis_thread(conv_id)),
            daemon=True,
        ).start()
        # Les métadonnées du message qu'on vient d'écrire, pour que le client les
        # affiche sans relire la conversation entière après chaque tour. C'est le
        # SERVEUR qui fait foi : il vient de les poser sur le disque, et une
        # horloge de navigateur qui diverge donnerait deux heures différentes pour
        # le même message selon qu'on le regarde avant ou après un rechargement.
        dernier = (conv or {}).get("messages", [])
        return dict(dernier[-1]) if dernier else {}

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            rag_override: str | None = msg.get("rag_override")
            strict_override: bool = bool(msg.get("strict_override", False))
            web_search_override: bool = bool(msg.get("web_search_override", False))

            ctx = memory.get_context()
            model_override = ctx.get("modèle_actif") or None
            # Réglage de session, comme `strict_mode` : lu ici plutôt que reçu
            # dans le message. Le client n'a donc rien à envoyer, et le réglage
            # vaut pour tous les chemins de ce tour (direct ET pipeline).
            # `.get(..., True)` : un `context_session.json` déjà sur le disque
            # n'a pas la clé, et son absence doit valoir « activé » — le
            # comportement d'avant ce réglage.
            raisonnement = bool(ctx.get("raisonnement", True))
            _last_model[0] = model_override or llm._model

            _req_start = time.time()
            user_text = msg["content"]

            # ── Quelle conversation ? ─────────────────────────────────────────
            #
            # Relu à CHAQUE message et non une fois par connexion : le client
            # change de conversation sans rouvrir le socket. Un seul `/ws/chat`
            # subsiste donc, ce qui évite de reconnecter à chaque bascule — la
            # logique de reconnexion du frontend est déjà délicate.
            #
            # CRÉATION PARESSEUSE : sans identifiant, la conversation naît ici,
            # au premier message, et jamais à l'ouverture d'un onglet vide. Sinon
            # la liste se remplit de coquilles sans contenu, que l'utilisateur
            # devrait nettoyer à la main.
            conv_id = (msg.get("conversation_id") or "").strip()
            if not conv_id:
                # Pas d'identifiant → on CONTINUE la conversation de cette
                # connexion, on n'en ouvre pas une seconde.
                #
                # Sans cette ligne, un client qui n'envoie pas d'identifiant
                # obtient une conversation NEUVE à chaque message : le modèle perd
                # tout le contexte au deuxième tour, et la liste se remplit d'un
                # fil par message. Mesuré — c'est ce qu'ont attrapé les tests de
                # protocole, dont le prompt du second tour ne contenait plus la
                # réponse du premier.
                #
                # « Pas d'identifiant » veut dire « poursuis », jamais
                # « recommence » : pour ouvrir une nouvelle conversation, le
                # client appelle `POST /chat/conversations` et envoie l'id rendu.
                # C'est aussi ce qui préserve à l'identique le comportement des
                # clients d'avant ce chantier — une conversation par connexion.
                conv_id = _derniere_conv[0] or ""
            if conv_id:
                existe = await loop.run_in_executor(
                    None, history_engine.get_conversation, conv_id
                )
                if existe is None:
                    # Identifiant inconnu : supprimée ailleurs, ou client
                    # désynchronisé. On n'échoue PAS — le message vient d'être
                    # tapé, le perdre serait le pire des comportements. On repart
                    # sur une conversation neuve, et on l'ANNONCE pour que le
                    # client se recale au lieu d'écrire dans le vide.
                    logger.warning("Conversation inconnue %r — création d'une neuve", conv_id)
                    conv_id = ""
            if not conv_id:
                neuve = await loop.run_in_executor(
                    None, lambda: history_engine.create_conversation(model=_last_model[0])
                )
                conv_id = neuve["id"]
                await websocket.send_text(json.dumps({
                    "type": "conversation", "id": conv_id,
                }))
            _derniere_conv[0] = conv_id

            # @historique skill
            hist_ctx = ""
            if "@historique" in user_text:
                hist_query = user_text.replace("@historique", "").strip() or user_text
                hist_results = await loop.run_in_executor(
                    None, history_engine.search_history, hist_query
                )
                if hist_results:
                    extraits = "\n\n".join(
                        f"— {r['titre']} ({r['date']}) :\n{r['extrait']}"
                        for r in hist_results
                    )
                    hist_ctx = f"Extraits de conversations précédentes pertinentes :\n{extraits}"
                user_text = hist_query if hist_query != user_text else user_text.replace("@historique", "").strip()
                user_text = user_text or msg["content"]

            # Le message de l'utilisateur entre sur le DISQUE ici, et pas plus
            # tôt : son texte a pu être réécrit juste au-dessus (`@historique`
            # retire sa balise), et c'est le texte réellement soumis au modèle qui
            # doit être conservé — sinon le tour suivant relit une question que
            # personne n'a posée.
            #
            # Cet ajout rend la conversation à jour, donc l'historique du prompt
            # avec : une seule lecture-écriture au lieu d'un `append` puis d'un
            # `get`. Le tour d'assistant sera une SECONDE transaction, plus bas —
            # deux écritures distinctes, jamais une seule qui les enjamberait
            # (docs/conversations-persistees.md §6).
            conv = await loop.run_in_executor(
                None, lambda: history_engine.append_messages(
                    conv_id, [{"role": msg.get("role", "user"), "content": user_text}],
                    model=_last_model[0],
                )
            )
            if conv is None:
                # La conversation a disparu entre sa résolution et cet ajout
                # (suppression depuis un autre onglet). Rare, mais le message de
                # l'utilisateur ne doit pas être perdu en silence.
                logger.warning("Conversation %s disparue en cours de tour", conv_id)
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "content": "Cette conversation n'existe plus. Ouvrez-en une nouvelle.",
                }))
                continue


            # @web skill
            web_ctx = ""
            #: Résultats STRUCTURÉS du tour, conservés pour `core.citations`
            #: (ensemble de référence) et le bloc Sources (§4) — `web_results`
            #: ci-dessous n'est que leur mise en forme texte pour le prompt.
            web_resultats: list[ResultatWeb] = []
            #: Déroulé de la recherche (requête envoyée mot pour mot, résultats,
            #: exclusions publicitaires, erreurs) — rempli par le callback
            #: `_on_etape_recherche` ci-dessous, PENDANT l'exécution de
            #: `rechercher()` dans le thread de l'exécuteur.
            etapes_recherche: list[dict] = []
            if web_search_override:
                def _on_etape_recherche(etape: dict) -> None:
                    """Appelé depuis le THREAD de l'exécuteur (`rechercher()` est
                    synchrone). Pousse l'étape en direct sur le websocket — pour
                    que le panneau se remplisse PENDANT la recherche, pas
                    seulement à la fin (tâche §2) — via `run_coroutine_threadsafe`,
                    comme `_annoncer_titre_depuis_thread` : toucher le websocket
                    depuis un autre fil sans repasser par la boucle corromprait le
                    protocole. Best-effort : le socket peut être fermé entre-temps.
                    """
                    etapes_recherche.append(etape)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_text(json.dumps(
                                {"type": "trace_recherche_etape", "etape": etape},
                                ensure_ascii=False,
                            )),
                            loop,
                        )
                    except Exception:
                        logger.debug("Étape de trace non poussée (socket fermé ?)")

                _t_web = time.time()
                web_query = user_text.strip()
                web_results, web_resultats = await loop.run_in_executor(
                    None, _rechercher_pour_prompt, web_query, _on_etape_recherche
                )
                logger.info("TTFT Web: %.3fs (query=%r, len=%d)", time.time() - _t_web, web_query[:80], len(web_results))
                web_ctx = _construire_web_ctx(web_results)

            # Les fichiers viennent de LA CONVERSATION, plus d'un `fichiers_actifs`
            # global (retiré le 2026-08-27). `conv` a été relu juste au-dessus par
            # l'ajout du message, donc l'attachement est frais sans lecture
            # supplémentaire.
            #
            # Les trois modes restent ceux d'avant, seule la provenance de la
            # liste change : « corpus entier » (`rag_override == "all"`) reste
            # orthogonal à l'attachement — c'est « cherche partout », pas
            # « attache tout ».
            active_files = conv.get("fichiers_attachés", [])
            _t = time.time()
            chunks_struct: list[dict] = []
            if rag_override == "all":
                chunks_struct = await loop.run_in_executor(None, rag.query_avec_sources, user_text)
            elif active_files:
                chunks_struct = await loop.run_in_executor(
                    None, rag.query_filtered_avec_sources, user_text, active_files
                )
            chunks = "\n\n---\n\n".join(c["texte"] for c in chunks_struct)
            logger.info("TTFT RAG: %.3fs", time.time() - _t)

            # Fichiers RAG effectivement INJECTÉS ce tour — resserré aux chunks
            # que la requête a réellement fait remonter, pas « tous les fichiers
            # attachés » ni « tout le corpus indexé » : une conversation peut
            # attacher trois fichiers sans qu'aucun de leurs chunks ne matche la
            # question posée.
            fichiers_rag_injectes = {c["source"] for c in chunks_struct if c.get("source")}

            # URLs légitimes pour l'ensemble de référence de `core.citations` :
            # les vraies URLs http(s) présentes dans le TEXTE des chunks
            # réellement injectés ce tour (`extraire_urls`, déjà publique).
            # C'est le correctif du NO-OP décrit dans `RAGEngine.query_avec_sources` :
            # avant, cet ensemble était peuplé avec des CHEMINS de fichiers
            # (`active_files`) ou `get_indexed_files()`, qui ne matchent jamais
            # le motif `https?://` de `extraire_urls` — une URL réellement citée
            # depuis un document attaché était donc signalée à tort comme
            # inventée. Resserrement assumé pour le mode « all » (tâche §3) :
            # les URLs viennent des chunks REMONTÉS par la requête, pas de la
            # liste de tout le corpus indexé — mécaniquement plus strict qu'avant
            # dans ce mode, mais c'est la même vérité qu'en mode filtré.
            urls_rag: set[str] = set()
            for c in chunks_struct:
                urls_rag |= extraire_urls(c["texte"])

            sys_parts: list[str] = []
            if strict_override:
                sys_parts.append(
                    "Réponds de façon maximalement concise. "
                    "Pas d'introduction, pas de reformulation."
                )
            _t = time.time()
            mem_ctx = await loop.run_in_executor(None, memory.build_system_context, user_text)
            logger.info("TTFT Memory: %.3fs", time.time() - _t)
            if mem_ctx:
                sys_parts.append(mem_ctx)
            # [CONTEXTE ACTIF] : le résumé des fichiers de CETTE conversation.
            # Il était injecté par `memory.build_system_context`, qui n'a aucun
            # moyen de savoir de quelle conversation il s'agit — tolérable tant
            # que le résumé était global, faux dès qu'il y en a un par fil. Il est
            # donc ajouté ici, où la conversation est connue.
            resume_conv = (conv.get("résumé_contexte") or "").strip()
            if resume_conv:
                sys_parts.append(f"[CONTEXTE ACTIF]\n{resume_conv}")
            # Consigne libre de CE fil, écrite par l'utilisateur. Même endroit et
            # même raison que le résumé : la conversation est connue ici, pas
            # dans `MemoryEngine`, qui ne parle que du profil.
            #
            # APRÈS l'instruction de session — celle-ci est injectée par
            # `build_system_context`, donc déjà dans `mem_ctx` juste au-dessus.
            # De deux consignes qui se contredisent, la plus spécifique doit être
            # lue en dernier : « réponds en anglais » posé sur un fil doit
            # l'emporter sur un réglage qui vaut pour toute l'instance.
            instruction_conv = (conv.get("instruction") or "").strip()
            if instruction_conv:
                sys_parts.append(f"[INSTRUCTION DE CETTE CONVERSATION]\n{instruction_conv}")
            if hist_ctx:
                sys_parts.append(hist_ctx)
            if web_ctx:
                sys_parts.append(web_ctx)
            if chunks:
                sys_parts.append(
                    "Contexte extrait de tes fiches de révision :\n"
                    f"{chunks}\n\n"
                    "Réponds à la question en te basant sur ce contexte si pertinent."
                )

            # Horodatage du message qu'on vient d'écrire, renvoyé au client.
            #
            # Le client l'a affiché optimistement dès la frappe, sans heure. Il
            # pourrait en poser une lui-même — mais alors l'heure affichée avant
            # un rechargement viendrait de l'horloge du navigateur et celle
            # d'après du disque, donc deux valeurs pour le même message dès que
            # les deux horloges divergent. Le serveur fait foi, ici comme dans
            # l'événement `done`.
            _meta_user = (conv["messages"] or [{}])[-1]
            await websocket.send_text(json.dumps({
                "type": "meta_message", "role": "user",
                "horodatage": _meta_user.get("horodatage", ""),
                # Le modèle À QUI la question est posée. Utile surtout dans un
                # fil où l'on change de modèle en cours de route : sans lui, on
                # ne peut plus savoir laquelle des questions est partie où.
                "modèle": _meta_user.get("modèle", ""),
            }, ensure_ascii=False))

            messages = list(conv["messages"])
            if sys_parts:
                messages = [{"role": "system", "content": "\n\n".join(sys_parts)}] + messages

            # ── Orchestrator ──────────────────────────────────────────────────
            _effort = msg.get("effort", "direct")
            _client_steps = msg.get("steps", [])  # [{"role": "...", "model": "..."}]
            _direct_mode = bool(msg.get("direct", False)) or _effort == "direct" or not _effort

            if not _direct_mode:
                _pipeline: list[dict] = []

                if _effort == "adaptive":
                    try:
                        _classification = await asyncio.wait_for(
                            loop.run_in_executor(None, orchestrator.classify_task, user_text, ctx),
                            timeout=3.0,
                        )
                    except Exception:
                        _classification = {"complexity": "simple"}
                    _complexity = _classification.get("complexity", "simple")
                    if _complexity == "simple":
                        _direct_mode = True
                    else:
                        _eff = "medium" if _complexity == "moderate" else "high"
                        _pipeline = orchestrator.build_steps(_eff, [], ctx)
                elif _effort in ("low", "medium", "high"):
                    _pipeline = orchestrator.build_steps(_effort, _client_steps, ctx)

                if not _direct_mode and _pipeline:
                    await websocket.send_text(json.dumps({
                        "type": "pipeline_info",
                        "effort": _effort,
                        "steps": [{"role": s["role"], "label": s.get("label", s["role"]), "model": s["model"]} for s in _pipeline],
                    }))
                    _final = ""
                    async for _event in orchestrator.run_pipeline(
                            _pipeline, user_text, messages, loop,
                            raisonnement=raisonnement):
                        if _event.get("type") == "pipeline_done":
                            _final = _event.get("final_output", "")
                        await websocket.send_text(json.dumps(_event))
                    _sources, _trace = _finaliser_citations_et_trace(
                        conv_id, _final, web_resultats, user_text, urls_rag,
                        etapes_recherche,
                    )
                    _meta = await _enregistrer_reponse(conv_id, _final, _sources, _trace) if _final else {}
                    await websocket.send_text(json.dumps({
                        "type": "done",
                        "horodatage": _meta.get("horodatage", ""),
                        "modèle": _meta.get("modèle", ""),
                        # Mêmes sources/trace qu'après un F5 (relecture de la
                        # conversation persistée) : la vue en direct ne doit pas
                        # différer de la vue relue, cf. tâche §3.
                        "sources": _meta.get("sources", []),
                        "trace_recherche": _meta.get("trace_recherche", []),
                    }, ensure_ascii=False))
                    continue
                elif not _direct_mode:
                    _direct_mode = True  # empty pipeline → fall through to direct
            # ─────────────────────────────────────────────────────────────────

            queue: asyncio.Queue = asyncio.Queue()

            def _stream(msgs, q, lp, model):
                try:
                    for token in llm.stream(msgs, model=model, raisonnement=raisonnement):
                        asyncio.run_coroutine_threadsafe(q.put(token), lp)
                except Exception as exc:
                    logger.exception("Erreur streaming chat")
                    asyncio.run_coroutine_threadsafe(q.put({"error": str(exc)}), lp)
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(None), lp)

            Thread(
                target=_stream, args=(messages, queue, loop, model_override), daemon=True
            ).start()

            accumulated = ""
            #: La génération a-t-elle été COUPÉE par le plafond de tokens ?
            #: Renseigné par la sentinelle `__stats__` (`done_reason == "length"`
            #: côté Ollama, `finish_reason` côté API compatible OpenAI).
            _tronque = False
            _first_token = True
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, dict) and "error" in item:
                    await websocket.send_text(
                        json.dumps({"type": "error", "content": item["error"]})
                    )
                    break
                if isinstance(item, dict) and item.get("__reasoning__"):
                    # Raisonnement du modèle, canal distinct du contenu final.
                    # `{"type": "reasoning"}` suit la forme de `{"type": "token"}`
                    # juste en dessous plutôt qu'un format à part : le frontend a
                    # déjà un aiguillage sur `data.type`, et un second format
                    # aurait demandé un second aiguillage pour la même chose.
                    #
                    # N'entre PAS dans `accumulated`, et c'est le point : c'est
                    # `accumulated` qui part dans `history` puis dans le prompt du
                    # tour suivant. Y verser le raisonnement le ferait relire par
                    # le modèle comme s'il l'avait dit à l'utilisateur, et
                    # gonflerait le contexte de plusieurs centaines de tokens par
                    # tour (584 générés pour 14 caractères de réponse, mesuré).
                    await websocket.send_text(json.dumps({
                        "type": "reasoning", "content": item["content"],
                    }, ensure_ascii=False))
                    continue
                if isinstance(item, dict) and "__stats__" in item:
                    _tronque = bool(item.get("tronqué"))
                    usage_tracker.track(
                        _provider_of(_last_model[0]),
                        item.get("prompt_tokens", 0),
                        item.get("output_tokens", 0),
                    )
                    await websocket.send_text(json.dumps({
                        "type": "stats",
                        "prompt_tokens": item.get("prompt_tokens", 0),
                        "output_tokens": item.get("output_tokens", 0),
                        "eval_duration_ms": (item.get("eval_duration_ns", 0) or 0) // 1_000_000,
                        "prompt_duration_ms": (item.get("prompt_duration_ns", 0) or 0) // 1_000_000,
                        "tronqué": _tronque,
                    }))
                    continue
                if _first_token:
                    logger.info("TTFT total: %.3fs", time.time() - _req_start)
                    _first_token = False
                accumulated += item
                await websocket.send_text(json.dumps({"type": "token", "content": item}))

            # Coupé AVANT d'avoir écrit un seul caractère de réponse.
            #
            # C'est le symptôme mesuré chez un destinataire : 613 tokens
            # d'entrée, 2048 produits, réponse VIDE. La réflexion avait consommé
            # tout le budget — les deux puisent au même `num_predict`, aucune API
            # n'expose de quota séparé.
            #
            # Sans ce message, le chat affiche une bulle vide, indiscernable d'un
            # modèle qui n'aurait rien à dire. On nomme la panne et on donne le
            # geste qui la lève, plutôt que de laisser chercher.
            if _tronque and not accumulated:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "content": (
                        "Le modèle a épuisé son budget de génération en réfléchissant, "
                        "sans produire de réponse. Désactivez le raisonnement dans le "
                        "panneau Compétences, ou posez une question plus simple."
                    ),
                }, ensure_ascii=False))

            _sources, _trace = _finaliser_citations_et_trace(
                conv_id, accumulated, web_resultats, user_text, urls_rag,
                etapes_recherche,
            )
            _meta = await _enregistrer_reponse(conv_id, accumulated, _sources, _trace) if accumulated else {}
            await websocket.send_text(json.dumps({
                "type": "done",
                "horodatage": _meta.get("horodatage", ""),
                "modèle": _meta.get("modèle", ""),
                # Mêmes sources/trace qu'après un F5 (relecture de la conversation
                # persistée) : la vue en direct ne doit pas différer de la vue
                # relue, cf. tâche §3.
                "sources": _meta.get("sources", []),
                "trace_recherche": _meta.get("trace_recherche", []),
            }, ensure_ascii=False))

    except WebSocketDisconnect:
        # RATTRAPAGE, plus une sauvegarde. Tout est déjà sur le disque : la
        # déconnexion ne sert qu'à rendre la conversation trouvable par
        # `@historique` sans attendre le dixième message, puisque l'indexation
        # vectorielle suit la cadence de la consolidation (cf.
        # `_travaux_apres_tour`).
        #
        # L'ancien bloc faisait tout ici — `save_conversation` de l'historique
        # accumulé en mémoire, puis consolidation. C'était le SEUL moment où quoi
        # que ce soit était écrit : fermer l'onglet autrement qu'en se
        # déconnectant proprement, ou laisser le processus mourir, perdait la
        # conversation entière.
        conv_id = _derniere_conv[0]
        if conv_id:
            Thread(
                target=history_engine.indexer_vectoriel, args=(conv_id,), daemon=True
            ).start()
