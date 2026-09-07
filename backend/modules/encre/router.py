"""Routeur du module « encre » — pages de notes manuscrites (phase 1, zéro ML).

Monté par ``core.module_registry.register_routers()`` sous le prefix déclaré
dans ``manifest.json`` (``/encre``). Les chemins ci-dessous sont donc RELATIFS :
``@router.get("/pages")`` → ``GET /encre/pages``. Ne pas les préfixer à la main —
c'est le cas de figure inverse de celui des modules générés par l'Atelier, dont
le prefix est ``""`` et dont chaque route doit porter son ``/<id>`` (CLAUDE.md
§3.3). Le module ``hello`` est le précédent exact suivi ici.

**Aucun appel LLM sur ce chemin, dans aucune branche.** La règle du §3.7 (une
tâche de fond tourne en local) n'a donc rien à arbitrer ici, et c'est voulu : la
transcription est la phase 2 et vit ailleurs (``core/hmer.py``). Si un jour une
route de ce fichier appelle un modèle, c'est cette phrase qu'il faudra corriger
en même temps que le code.

Le moteur est injecté depuis ``core.runtime`` — jamais instancié ici (§3.2).
"""

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.paths import PathOutsideDataError
from core.runtime import encre_engine

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

    Les deux champs sont optionnels et valent ``None`` par défaut, ce qui, sur le
    ``PUT``, signifie « ne touche pas à ce champ » et non « efface-le » : c'est
    ce qui permet à l'interface de renommer une page sans lui renvoyer l'encre
    entière. Un ``strokes: []`` explicite efface bien, lui — une liste vide est
    une valeur, pas une absence.
    """

    titre: Optional[str] = None
    strokes: Optional[list[Any]] = None


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
        None, encre_engine.create_page, req.titre, req.strokes)
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
            None, encre_engine.update_page, page_id, req.titre, req.strokes)
    except PathOutsideDataError as exc:
        raise _invalide(exc) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Page introuvable")
    return page


@router.delete("/pages/{page_id}")
async def encre_page_delete(page_id: str):
    loop = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, encre_engine.delete_page, page_id)
    except PathOutsideDataError as exc:
        raise _invalide(exc) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Page introuvable")
    return {"ok": True}
