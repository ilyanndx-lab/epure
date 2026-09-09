"""Routeur du module « encre » — pages manuscrites et leur transcription.

Monté par ``core.module_registry.register_routers()`` sous le prefix déclaré
dans ``manifest.json`` (``/encre``). Les chemins ci-dessous sont donc RELATIFS :
``@router.get("/pages")`` → ``GET /encre/pages``. Ne pas les préfixer à la main —
c'est le cas de figure inverse de celui des modules générés par l'Atelier, dont
le prefix est ``""`` et dont chaque route doit porter son ``/<id>`` (CLAUDE.md
§3.3). Le module ``hello`` est le précédent exact suivi ici.

**Aucun appel LLM sur ce chemin, dans aucune branche** — et depuis la phase 2
cette phrase demande une précision, exactement comme la version précédente de ce
fichier s'engageait à le faire. ``POST /pages/{id}/transcrire`` appelle bien un
MODÈLE : ``core/hmer.py``, c'est-à-dire `pix2text-mfr` en ONNX sur le CPU. Ce
n'est pas un LLM et ça ne passe pas par le routeur de modèles — pas de
``modèle_actif``, pas de fournisseur, pas de clé d'API, aucun octet qui sorte de
la machine une fois les poids téléchargés. La règle du §3.7 (une tâche qui n'est
pas le tour de chat de l'utilisateur tourne en local) n'a donc toujours rien à
arbitrer ici : il n'y a pas de choix local/cloud à faire, il n'y a que du local.
``test_taches_locales.py`` ne couvre pas cette route, et c'est correct — il pose
un ``modèle_actif`` cloud et vérifie qu'aucun site ne l'hérite, ce qui n'a pas de
sens pour un chemin qui ne lit jamais ce réglage.

La **post-correction par LLM** du texte transcrit, elle, n'existe pas et n'est
pas dans ce lot : ``docs/module-encre.md`` en fait une phase séparée, affichée
comme suggestion et jamais substituée. Le jour où elle arrive, c'est elle qui
devra passer par ``modele_pour_tache(use_cloud=False)``, et c'est ce
paragraphe-ci qu'il faudra reprendre.

Le moteur est injecté depuis ``core.runtime`` — jamais instancié ici (§3.2).
"""

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.hmer import HmerIndisponible, PageSansEncre, points_de_page
from core.paths import PathOutsideDataError
from core.rag import source_encre
from core.runtime import encre_engine, hmer_engine, rag

logger = logging.getLogger(__name__)

router = APIRouter()


class PageEcriture(BaseModel):
    """Corps de ``POST /encre/pages`` et de ``PUT /encre/pages/{id}``.

    ``strokes`` est typé ``list[Any]`` et non une structure de points, et ce
    n'est pas un relâchement : c'est la même décision que celle documentée en
    tête de ``core/encre.py``. Pydantic validerait volontiers un
    ``list[dict[str, float]]``, ce qui reviendrait à figer ici la forme d'un
    point manuscrit — la donnée que ``docs/module-encre.md`` désigne comme la
    seule irremplaçable, et celle que la phase 2 enrichira. Un 422 sur une page
    d'encre parfaitement dessinée serait le pire résultat possible de cette
    validation.

    Les trois champs sont optionnels et valent ``None`` par défaut, ce qui, sur
    le ``PUT``, signifie « ne touche pas à ce champ » et non « efface-le » :
    c'est ce qui permet à l'interface de renommer une page sans lui renvoyer
    l'encre entière. Un ``strokes: []`` explicite efface bien, lui — une liste
    vide est une valeur, pas une absence.

    ``mode`` n'est pas un ``Literal["maths", "lettres"]`` — délibérément : une
    valeur inconnue ne doit pas produire un 422 sur une page par ailleurs
    valide, elle doit se lire comme "maths" (``EncreEngine._mode``), exactement
    comme un ``strokes`` mal formé se lit comme une page vide. La validation
    stricte vivrait ici pour de mauvaises raisons ; elle vit dans le moteur.
    """

    titre: Optional[str] = None
    strokes: Optional[list[Any]] = None
    mode: Optional[str] = None


def _invalide(exc: PathOutsideDataError) -> HTTPException:
    """Traduit un refus de confinement en 400, jamais en 500.

    ``PathOutsideDataError`` dit « cet identifiant ne désigne pas une page de ce
    dossier ». C'est une requête malformée, donc un 4xx : la laisser remonter au
    gestionnaire d'exceptions de ``main.py`` la transformerait en 500
    « Erreur interne du serveur », c'est-à-dire en un message qui accuse le
    serveur d'un défaut du client.

    Ni 404 ni 403 : un 404 dirait « cette page n'existe pas » à propos d'un
    identifiant qui ne peut désigner aucune page, et un 403 laisserait entendre
    qu'un droit manque alors que rien n'est jamais autorisé ici.
    """
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/pages")
async def encre_pages_list():
    """Liste des pages, SANS les tracés (cf. ``EncreEngine.list_pages``).

    ``run_in_executor`` parce que le moteur fait des entrées-sorties disque
    synchrones sur potentiellement plusieurs dizaines de fichiers : sur la boucle
    d'événements, ça bloquerait tout le reste de l'application pendant le scan.
    Même patron que ``flashcards_decks_list``.
    """
    loop = asyncio.get_running_loop()
    pages = await loop.run_in_executor(None, encre_engine.list_pages)
    return {"pages": pages}


@router.post("/pages")
async def encre_page_create(req: PageEcriture):
    """Crée une page et rend son contenu complet, id et dates compris."""
    loop = asyncio.get_running_loop()
    page = await loop.run_in_executor(
        None, encre_engine.create_page, req.titre, req.strokes, req.mode)
    return page


@router.get("/pages/{page_id}")
async def encre_page_get(page_id: str):
    loop = asyncio.get_running_loop()
    try:
        page = await loop.run_in_executor(None, encre_engine.get_page, page_id)
    except PathOutsideDataError as exc:
        raise _invalide(exc) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Page introuvable")
    return page


@router.put("/pages/{page_id}")
async def encre_page_update(page_id: str, req: PageEcriture):
    loop = asyncio.get_running_loop()
    try:
        page = await loop.run_in_executor(
            None, encre_engine.update_page, page_id, req.titre, req.strokes, req.mode)
    except PathOutsideDataError as exc:
        raise _invalide(exc) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Page introuvable")
    return page


@router.delete("/pages/{page_id}")
async def encre_page_delete(page_id: str):
    """Supprime une page, **et son entrée dans le corpus indexé si elle en a une**.

    Le retrait de l'index est arrivé avec la transcription, et il n'est pas
    facultatif : sans lui, une page supprimée continue de répondre aux recherches
    documentaires avec un texte que plus rien ne peut rouvrir ni corriger. C'est
    le seul chemin par lequel une source `encre:<id>` peut devenir orpheline.

    ⚠️ **Conditionné à la présence d'une transcription, et c'est le point à ne pas
    « simplifier ».** `rag` est un `_LazyEngine` : y toucher construit le moteur
    d'embedding, donc peut déclencher le téléchargement de 90 Mo. Appeler
    `remove_source` inconditionnellement ferait payer ça à quiconque supprime une
    page sur une instance qui ne s'est jamais servie de la recherche
    documentaire — pour retirer une entrée qui n'existe pas.

    Un échec de désindexation est logué et avalé : la page, elle, est bien
    supprimée, et remonter une 500 après coup laisserait croire le contraire. Le
    recours existe et il est dans l'interface — le bouton « retirer » du panneau
    fichiers agit sur n'importe quelle source, y compris celle-ci.
    """
    loop = asyncio.get_running_loop()
    try:
        page = await loop.run_in_executor(None, encre_engine.get_page, page_id)
        ok = await loop.run_in_executor(None, encre_engine.delete_page, page_id)
    except PathOutsideDataError as exc:
        raise _invalide(exc) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Page introuvable")
    if isinstance(page, dict) and page.get("transcription"):
        try:
            await loop.run_in_executor(None, rag.remove_source, source_encre(page_id))
        except Exception:
            logger.exception("Page %s supprimée mais non désindexée", page_id)
    return {"ok": True}


@router.post("/pages/{page_id}/transcrire")
async def encre_page_transcrire(page_id: str):
    """Transcrit la page en LaTeX best-effort, l'enregistre, l'indexe, la rend.

    **Déclenchement MANUEL, une page à la fois.** Pas de transcription
    automatique à chaque sauvegarde : la latence CPU réelle de `pix2text-mfr` sur
    ce poste n'était pas mesurée quand la phase 2 a été décidée, et un
    déclenchement automatique aurait demandé un anti-rebond posé à l'aveugle sur
    un coût inconnu. C'est un bouton, et il le reste tant que personne n'a
    mesuré.

    ``run_in_executor`` et non un flux SSE : l'appel est bloquant mais court et
    surtout **atomique** — il n'y a rien à diffuser au fur et à mesure, un modèle
    seq2seq ne rend son texte qu'à la fin. Ce qu'il faut absolument, c'est ne pas
    le laisser sur la boucle d'événements, où il figerait le backend entier
    (chat compris) pendant tout le chargement du modèle au premier appel.

    Quatre codes d'erreur, quatre causes qui ne se confondent pas :

    * **404** — la page n'existe pas (onglet resté ouvert sur une page effacée) ;
    * **400 (mode)** — la page est en mode "lettres". Refusé ICI, côté serveur,
      et pas seulement par un bouton désactivé côté frontend : `pix2text-mfr`
      est un reconnaisseur de formules mathématiques (sortie LaTeX), pas un OCR
      généraliste, et lui donner du texte manuscrit courant ne produit pas une
      transcription dégradée mais des hallucinations de syntaxe math — un
      résultat qui a l'air d'un LaTeX plausible et qui n'en est pas un. Vérifié
      AVANT `points_de_page` : un refus de principe n'a pas besoin de savoir si
      la page a de l'encre ;
    * **400 (vide)** — la page existe, est en mode "maths", et n'a aucun tracé.
      Vérifié AVANT de toucher au moteur, avec `points_de_page`, qui n'importe
      rien de lourd : découvrir qu'une page est vide ne doit pas coûter 118 Mo
      de poids et 16 s d'import ;
    * **500** — le moteur ne peut pas se construire (dépendances absentes, poids
      introuvables) ou la transcription lève. **Jamais un 200 avec un texte
      vide**, qui aurait l'air d'un succès et écraserait une transcription
      précédente par du néant. Un texte vide RENDU par le modèle, en revanche,
      est un résultat légitime et s'écrit tel quel : c'est ce que le modèle a lu.

    L'indexation est faite APRÈS l'écriture sur disque, et son échec n'annule pas
    la transcription : la page reste transcrite et l'utilisateur voit son texte.
    L'ordre inverse laisserait un index qui parle d'une transcription que la page
    n'a pas.

    ⚠️ **COURSE CONNUE ET NON FERMÉE : l'encre peut changer pendant l'appel.** La
    page est lue au début, la transcription dure de 0,4 s (modèle chaud) à une
    trentaine de secondes (premier appel, chargement des poids), et
    l'enregistrement automatique du canvas part au bout de 1,2 s d'inactivité. Un
    trait ajouté dans cette fenêtre est donc enregistré, puis
    `set_transcription` écrit par-dessus une transcription qui décrit l'encre
    d'AVANT — avec une date fraîche, sans que rien ne le signale.

    Non fermée délibérément, et voici ce qui rend le choix tenable : la
    transcription est un INDEX, pas une sortie (§0 de `docs/module-encre.md`).
    Le pire résultat est une page qui se retrouve sur des symboles qu'elle a
    cessé de porter, jusqu'à la transcription suivante — pas une perte d'encre,
    qui est la seule chose irrécupérable ici. La fermer proprement demanderait
    une notion de révision des tracés que le magasin n'a pas (`core/encre.py`
    traite les traits comme opaques, et c'est sa décision), donc un champ de plus
    sur chaque page pour un artefact bénin.

    Ce qui couvre le cas COURANT, en revanche, est côté frontend : le bouton
    enregistre ce qui est en attente avant d'appeler, sinon la transcription
    porterait systématiquement sur l'état d'avant les derniers traits — ça, ce
    n'était pas une course mais le comportement normal.
    """
    loop = asyncio.get_running_loop()
    try:
        page = await loop.run_in_executor(None, encre_engine.get_page, page_id)
    except PathOutsideDataError as exc:
        raise _invalide(exc) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Page introuvable")
    if page.get("mode") == "lettres":
        # `page.get("mode")` est déjà normalisé par `encre_engine.get_page`
        # (`EncreEngine._mode`) : une page écrite avant ce champ vaut "maths"
        # ici, jamais "lettres" par accident.
        raise HTTPException(
            status_code=400,
            detail=(
                "Cette page est en mode « lettres » : la transcription est "
                "désactivée. pix2text-mfr reconnaît des formules "
                "mathématiques (sortie LaTeX), pas du texte manuscrit "
                "courant — bascule la page en mode « maths » si elle "
                "contient des formules à transcrire."
            ),
        )
    # `run_in_executor` comme tout le reste de ce fichier, et pas par symétrie
    # décorative : MESURÉ sur ce poste, 135 ms pour 300 traits de 200 points et
    # 335 ms pour 600 × 300. C'est une boucle Python sur des dizaines de milliers
    # de dicts ; sur la boucle d'événements, elle fige le backend entier — le
    # streaming du chat compris — pour un contrôle qui ne fait que dire « cette
    # page est-elle vide ? ».
    traits = await loop.run_in_executor(None, points_de_page, page)
    if not traits:
        raise HTTPException(
            status_code=400,
            detail="Cette page ne contient aucun tracé — rien à transcrire.")

    try:
        resultat = await loop.run_in_executor(None, hmer_engine.transcrire, page)
    except PageSansEncre as exc:
        # Second filet : `points_de_page` a dit oui juste au-dessus, donc y
        # arriver voudrait dire que les deux ne s'accordent pas. 400 quand même
        # plutôt qu'une 500 qui accuserait le serveur d'un état de la page.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HmerIndisponible as exc:
        logger.warning("Transcription indisponible pour %s : %s", page_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Échec de la transcription de %s", page_id)
        raise HTTPException(
            status_code=500,
            detail=f"Transcription impossible : {exc}") from exc

    page = await loop.run_in_executor(
        None, encre_engine.set_transcription, page_id,
        resultat["texte"], resultat["modele"], resultat["version"])
    if page is None:
        # Page supprimée pendant la transcription. Rien à écrire, rien à indexer.
        raise HTTPException(status_code=404, detail="Page introuvable")

    try:
        await loop.run_in_executor(
            None, rag.index_page_encre, page_id, resultat["texte"],
            page.get("titre") or "")
    except Exception:
        logger.exception("Page %s transcrite mais non indexée", page_id)
    return page
