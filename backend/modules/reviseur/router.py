"""Routeur du module Réviseur — plan de révision quotidien ciblé.

Croise trois briques existantes (aucune logique dupliquée) :
  - core.memory     : lacunes confirmées / forces du profil + erreurs récentes ;
  - core.rag        : fiches indexées + extraits couvrant chaque lacune ;
  - core.flashcards : cartes dues (répétition espacée déjà en place).
Le LLM partagé (core.runtime.llm — jamais de moteur en dur) assemble le tout
en plan de séance : blocs de 25 min, matière, objectif, fiches à relire,
cartes à réviser. Si le LLM échoue ou renvoie un JSON inexploitable, un plan
de repli déterministe est construit à partir des mêmes données — le module
reste fonctionnel avec un petit modèle local.

Monté sous le prefix /reviseur (manifest.backend.prefix) : les chemins
ci-dessous sont relatifs (``@router.get("/plan")`` → ``GET /reviseur/plan``).

Chaque bloc terminé est réinscrit en mémoire de session (memory.add_session,
fichier="reviseur") : la consolidation et promote_lacunes existantes en
tiennent compte sans aucune modification de leur côté.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.runtime import flashcards_engine, llm, memory, rag

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_CIBLES = 3     # lacunes prioritaires retenues
_BLOC_MIN = 25      # durée d'un bloc (pomodoro)
_MAX_BLOCS = 4


# ── Collecte (briques existantes, aucun appel LLM) ───────────────────────────

def _cibles_prioritaires() -> list[str]:
    """2-3 lacunes prioritaires : les lacunes confirmées du profil, classées par
    fréquence d'apparition dans les erreurs des sessions récentes ; à défaut,
    les erreurs récentes les plus fréquentes (lacunes pas encore promues)."""
    profile = memory.load_profile()
    lacunes = list(profile.get("lacunes_confirmées") or [])
    counts: dict[str, int] = {}
    for s in memory.get_sessions(days=14):
        for err in s.get("erreurs", []):
            counts[err] = counts.get(err, 0) + 1
    if lacunes:
        lacunes.sort(key=lambda l: counts.get(l, 0), reverse=True)
        return lacunes[:_MAX_CIBLES]
    return [e for e, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:_MAX_CIBLES]]


def _fiches_disponibles() -> list[str]:
    """Noms des fiches indexées dans le RAG (basenames, dédupliqués)."""
    try:
        return sorted({Path(f).name for f in rag.get_indexed_files()})
    except Exception:
        logger.exception("Réviseur : lecture des fiches indexées échouée")
        return []


def _extraits_rag(cibles: list[str]) -> dict[str, str]:
    """Extraits de fiches couvrant chaque cible (contexte donné au LLM)."""
    out: dict[str, str] = {}
    for c in cibles:
        try:
            ext = rag.query(c, n_results=2)
        except Exception:
            logger.exception("Réviseur : requête RAG échouée pour %s", c)
            ext = ""
        if ext:
            out[c] = ext[:1200]
    return out


def _cartes_dues() -> tuple[int, list[dict]]:
    """Nombre total de cartes dues + répartition par deck [{deck, n}]."""
    dues = flashcards_engine.get_due()
    par_deck: dict[str, int] = {}
    for c in dues:
        nom = c.get("deck_nom", "?")
        par_deck[nom] = par_deck.get(nom, 0) + 1
    return len(dues), [{"deck": d, "n": n} for d, n in sorted(par_deck.items(), key=lambda kv: -kv[1])]


# ── Plan : LLM + parsing robuste + repli déterministe ────────────────────────

def _prompt_plan(cibles: list[str], forces: list[str], fiches: list[str],
                 extraits: dict[str, str], n_dues: int, par_deck: list[dict]) -> list[dict]:
    ctx = [f"Lacunes prioritaires à travailler : {', '.join(cibles) or 'aucune identifiée'}"]
    if forces:
        ctx.append(f"Points forts (ne pas y passer du temps) : {', '.join(forces[:5])}")
    if fiches:
        ctx.append(f"Fiches disponibles (noms EXACTS, n'en invente aucune) : {', '.join(fiches[:30])}")
    for cible, ext in extraits.items():
        ctx.append(f"Extrait de fiche couvrant « {cible} » :\n{ext}")
    if n_dues:
        rep = ", ".join(f"{d['deck']} ({d['n']})" for d in par_deck)
        ctx.append(f"Flashcards dues aujourd'hui : {n_dues} au total — {rep}")
    system = (
        "Tu es un coach de révision pour un élève de classe préparatoire. "
        "À partir des données fournies, construis le plan de la séance du jour : "
        f"2 à {_MAX_BLOCS} blocs de {_BLOC_MIN} minutes, chacun ciblé sur UNE lacune "
        "(ou sur les flashcards dues pour l'un des blocs s'il y en a). "
        "Réponds UNIQUEMENT avec ce JSON valide, sans texte avant ou après :\n"
        '{"blocs": [{"matière": "...", "objectif": "...", "durée_min": '
        f"{_BLOC_MIN}"
        ', "fiches": ["nom_exact.pdf"], "cartes": "", "consigne": "..."}]}\n'
        "— matière : la discipline du bloc (Maths, Physique…), déduite de la lacune ;\n"
        "— objectif : formulation précise et vérifiable de ce que le bloc doit corriger ;\n"
        "— fiches : uniquement des noms présents dans la liste fournie (sinon []) ;\n"
        "— cartes : '' sauf pour un bloc flashcards (ex. \"12 cartes du deck X\") ;\n"
        "— consigne : méthode de travail concrète pour les 25 minutes."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(ctx)},
    ]


def _parse_blocs(raw: str, fiches_connues: list[str]) -> list[dict]:
    """Extrait blocs[] du JSON LLM (tolère les fences et le texte autour).
    Filtre les fiches hallucinées (absentes de l'index RAG). Lève ValueError
    si rien d'exploitable — l'appelant bascule sur le plan de repli."""
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        raise ValueError("aucun objet JSON dans la réponse")
    data = json.loads(m.group())
    blocs_bruts = data.get("blocs")
    if not isinstance(blocs_bruts, list) or not blocs_bruts:
        raise ValueError("clé 'blocs' absente ou vide")
    connues = set(fiches_connues)
    blocs: list[dict] = []
    for b in blocs_bruts[:_MAX_BLOCS]:
        if not isinstance(b, dict) or not str(b.get("objectif", "")).strip():
            continue
        try:
            duree = int(b.get("durée_min") or _BLOC_MIN)
        except (TypeError, ValueError):
            duree = _BLOC_MIN
        blocs.append({
            "matière": str(b.get("matière") or "Général").strip(),
            "objectif": str(b["objectif"]).strip(),
            "durée_min": duree,
            "fiches": [f for f in (b.get("fiches") or []) if isinstance(f, str) and f in connues],
            "cartes": str(b.get("cartes") or "").strip(),
            "consigne": str(b.get("consigne") or "").strip(),
        })
    if not blocs:
        raise ValueError("aucun bloc valide après filtrage")
    return blocs


def _plan_de_repli(cibles: list[str], n_dues: int, par_deck: list[dict]) -> list[dict]:
    """Plan déterministe sans LLM : un bloc par lacune + un bloc flashcards.
    Garantit un module utilisable même sans modèle joignable."""
    blocs = [{
        "matière": "Général",
        "objectif": f"Retravailler : {c}",
        "durée_min": _BLOC_MIN,
        "fiches": [],
        "cartes": "",
        "consigne": "Relire la fiche correspondante puis refaire un exercice type sans regarder la correction.",
    } for c in cibles[:_MAX_BLOCS - 1]]
    if n_dues:
        rep = ", ".join(f"{d['deck']} ({d['n']})" for d in par_deck)
        blocs.append({
            "matière": "Flashcards",
            "objectif": f"Réviser les {n_dues} cartes dues",
            "durée_min": _BLOC_MIN,
            "fiches": [],
            "cartes": rep,
            "consigne": "Passer les cartes dues dans le module Flashcards, sans sauter les ratées.",
        })
    return blocs[:_MAX_BLOCS]


def _construire_plan() -> dict:
    """Assemble le plan du jour (synchrone — appelé via run_in_executor)."""
    profile = memory.load_profile()
    forces = list(profile.get("forces") or [])
    cibles = _cibles_prioritaires()
    n_dues, par_deck = _cartes_dues()

    if not cibles and not n_dues:
        return {"date": datetime.now().date().isoformat(), "cibles": [], "cartes_dues": 0,
                "par_deck": [], "blocs": [], "fallback": False,
                "note": ("Aucune lacune identifiée et aucune carte due : travaillez via le chat, "
                         "la kholle ou les flashcards pour alimenter le profil, puis revenez.")}

    fiches = _fiches_disponibles()
    extraits = _extraits_rag(cibles)
    model = memory.get_context().get("modèle_actif") or None

    fallback = False
    try:
        raw = llm.generate(_prompt_plan(cibles, forces, fiches, extraits, n_dues, par_deck), model=model)
        blocs = _parse_blocs(raw, fiches)
    except Exception as exc:
        logger.warning("Réviseur : plan LLM inexploitable (%s) — plan de repli", exc)
        blocs = _plan_de_repli(cibles, n_dues, par_deck)
        fallback = True

    return {
        "date": datetime.now().date().isoformat(),
        "cibles": cibles,
        "cartes_dues": n_dues,
        "par_deck": par_deck,
        "blocs": blocs,
        "fallback": fallback,
        "note": "",
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/plan")
async def reviseur_plan():
    """Plan de séance du jour : lacunes prioritaires → fiches RAG → cartes dues
    → blocs de 25 min structurés par le LLM (repli déterministe sans LLM)."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _construire_plan)
    except Exception as exc:
        logger.exception("Réviseur : construction du plan échouée")
        raise HTTPException(status_code=500, detail=f"Construction du plan échouée : {exc}")


class BlocTermine(BaseModel):
    matière: str
    objectif: str
    ressenti: str = "acquis"  # "acquis" | "a_retravailler"


@router.post("/bloc/termine")
async def reviseur_bloc_termine(req: BlocTermine):
    """Observation de fin de bloc en mémoire de session : la consolidation et
    promote_lacunes existantes s'en nourrissent telles quelles (un objectif
    « à retravailler » compte comme une erreur, donc redevient prioritaire)."""
    if req.ressenti not in ("acquis", "a_retravailler"):
        raise HTTPException(status_code=400, detail="ressenti doit être 'acquis' ou 'a_retravailler'")
    acquis = req.ressenti == "acquis"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: memory.add_session(
            matière=req.matière.strip() or "Général",
            fichier="reviseur",
            erreurs=[] if acquis else [req.objectif.strip()],
            réussies=1 if acquis else 0,
            ratées=0 if acquis else 1,
        ),
    )
    return {"ok": True, "ressenti": req.ressenti}
