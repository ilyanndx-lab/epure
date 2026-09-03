"""Enrichissement du contenu des résultats de recherche web — phase 4.

`core/websearch.py` trouve des résultats et ne garde que le snippet DuckDuckGo
(~150 caractères, écrit par DDG, pas par la page). Ce module va chercher le
contenu RÉEL des pages les plus pertinentes et retient, par similarité avec la
requête, les passages qui répondent le mieux — en réutilisant le MÊME modèle
d'embedding que le RAG (`core/vector_store.py`), jamais un second chargé en
double (cf. `_vecteurs_partages` plus bas).

Séparé de `core/websearch.py` plutôt qu'ajouté dedans, et ce n'est pas un
découpage arbitraire : `websearch.py` ne dépend que de la bibliothèque standard
(urllib/re/json) pour TROUVER des résultats — une dépendance externe en moins
sur le chemin qui doit rester le plus robuste (une recherche web sans aucun
résultat n'a aucun repli). Ce module-ci a un pied dans un territoire différent
— une dépendance externe (`readability-lxml`) et un calcul vectoriel (numpy) —
et sa panne ne doit JAMAIS empêcher `websearch.py` de fonctionner. Séparer les
fichiers rend cette frontière visible dans les imports au lieu de la laisser
implicite dans un `try/except` au milieu d'un fichier par ailleurs pur stdlib.

── Dépendance choisie : `readability-lxml`, pas `trafilatura` ──────────────

`trafilatura` a été écarté après vérification (pas par principe) : il tire
`dateparser`, qui déclare `regex` en dépendance directe. `regex` est
précisément l'un des deux binaires (`.pyd`, non signé) dont CLAUDE.md documente
le blocage MESURÉ par Smart App Control sur la machine ARM64 cible — via
`sentence-transformers` à l'époque, retiré pour cette raison exacte le
2026-08-26 (cf. `core/embedding.py`). Réintroduire `regex` par une autre porte
recréerait le même risque, vérifié, pas hypothétique.

`readability-lxml` (le portage Python de l'algorithme Readability de Firefox)
ne dépend que de `lxml` (déjà présent dans l'arbre — `python-docx` le tire
depuis le lot bureautique du 2026-08-24), `cssselect` et `chardet`, tous deux
purs Python. Aucun binaire nouveau. Vérifié par installation réelle sur ce
poste (Windows), pas supposé depuis la liste de dépendances publiée.
"""

import concurrent.futures
import html as _htmllib
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

import numpy as np
from readability import Document

from core.embedding_install import EmbeddingIndisponible
from core.websearch import ResultatWeb, _UA_PRINCIPAL, _emettre, tronquer_champ

logger = logging.getLogger(__name__)

# ── Budgets de temps — même principe que `main.py::health` (`_borne`) ───────
#
# Deux plafonds, jamais additionnés : chaque page a son propre budget, ET
# l'étape entière (fetch + extraction + reclassement) en a un second, plus
# large — les pages sont récupérées EN PARALLÈLE (`concurrent.futures`, pas
# une boucle séquentielle), donc le plafond réel est max(pages) + le coût du
# reclassement, jamais la somme des plafonds individuels.
#
# `ThreadPoolExecutor.submit`/`wait(timeout=…)` et non `asyncio.gather` : ce
# module est appelé DEPUIS `core/websearch.py::rechercher`, elle-même invoquée
# via `loop.run_in_executor` par `modules/chat/router.py` — donc déjà dans un
# thread ordinaire, sans boucle asyncio à disposition. C'est exactement le
# motif déjà utilisé par `core/module_workshop.py::engines_status` pour paralléliser
# des sondes synchrones depuis une fonction non-async — repris ici plutôt
# qu'un mécanisme différent.
_TIMEOUT_PAGE_S = 3.0
_TIMEOUT_ETAPE_S = 6.0

#: Nombre de résultats dont on va chercher le contenu réel. `websearch.py`
#: borne déjà `_MAX_RESULTS` à 5 : cette constante existe séparément pour ne
#: pas coupler les deux fichiers — si `_MAX_RESULTS` change un jour, cette
#: étape ne doit pas se mettre à enrichir davantage de pages sans qu'on l'ait
#: décidé explicitement ici.
_MAX_PAGES_ENRICHIES = 5

#: Taille d'un passage, en mots — quelques centaines, sans découpage plus fin :
#: ce chunking sert UNE SEULE requête connue à l'avance (contrairement à
#: l'indexation RAG, qui doit rester trouvable pour des questions futures
#: inconnues), donc pas besoin de chevauchement entre segments pour ne pas
#: perdre un passage à cheval sur une frontière — la requête est déjà là
#: pendant le découpage, elle sera comparée à chaque chunk indépendamment du
#: chunk voisin.
_MOTS_PAR_CHUNK = 200

#: Passages retenus par page après reclassement. 3 passages de ~200 mots
#: (~1200-1500 caractères avant troncature) laissent de quoi répondre à une
#: question précise sans recopier la page entière.
_MAX_PASSAGES_PAR_PAGE = 3

#: Budget de caractères par page enrichie, APRÈS sélection des passages.
#: Il n'existe pas de registre de fenêtre de contexte par modèle actif dans ce
#: dépôt (`core/llm.py::_budget` gère les tokens de SORTIE, pas la fenêtre
#: d'entrée) — inventer un mécanisme dynamique par modèle serait un choix non
#: appuyé par la moindre donnée déjà présente ailleurs. À la place : un budget
#: FIXE et conservateur. 800 caractères/page × au plus 5 pages enrichies
#: (`_MAX_PAGES_ENRICHIES`) borne le total à ~4000 caractères ajoutés au
#: prompt — quelques milliers de caractères, l'ordre de grandeur demandé,
#: choisi pour rester très en-dessous d'une fenêtre de contexte de 8k tokens
#: (le plus petit modèle local courant) une fois le reste du prompt compté
#: (historique, mémoire, consigne système).
_BUDGET_CARACTERES_PAR_PAGE = 800


def _extraire_texte_principal(html: str) -> str:
    """Texte principal d'une page HTML, débarrassé nav/pub/pied de page.

    `readability.Document.summary()` rend un fragment HTML nettoyé (l'article
    probable, pas la barre de navigation ni les liens « articles similaires »)
    — encore de la balise à retirer avant de pouvoir chunker en mots.
    Dégrade en chaîne vide sur tout échec (page qui n'est pas un article,
    HTML tronqué, encodage inattendu) : cette fonction n'est jamais le seul
    rempart, l'appelant retombe sur l'extrait DDG existant.
    """
    try:
        resume_html = Document(html).summary(html_partial=True)
    except Exception:
        return ""
    texte = re.sub(r"<[^>]+>", " ", resume_html)
    texte = _htmllib.unescape(texte)
    return re.sub(r"\s+", " ", texte).strip()


def _decouper_en_chunks(texte: str, mots_par_chunk: int = _MOTS_PAR_CHUNK) -> list[str]:
    """Segments contigus de `mots_par_chunk` mots, sans chevauchement (cf.
    docstring de `_MOTS_PAR_CHUNK` pour le pourquoi)."""
    mots = texte.split()
    if not mots:
        return []
    return [
        " ".join(mots[i:i + mots_par_chunk])
        for i in range(0, len(mots), mots_par_chunk)
    ]


def _recuperer_page(url: str) -> Optional[str]:
    """Récupère le HTML d'une page, timeout court et UNIQUE — contrairement à
    `websearch._fetch`, pas de rotation de User-Agent : celle-ci existe pour
    contourner le blocage anti-bot spécifique de DuckDuckGo, pas pour du
    contenu éditorial ordinaire, et l'essayer ajouterait des allers-retours
    réseau qu'un budget de 3 s par page ne peut pas se permettre.

    `None` sur tout échec (timeout, 403, DNS, contenu non-HTML) : jamais
    d'exception qui remonterait jusqu'à faire échouer la recherche entière.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA_PRINCIPAL, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_PAGE_S) as resp:
            if resp.status != 200:
                return None
            type_contenu = resp.headers.get("Content-Type", "")
            if type_contenu and "html" not in type_contenu.lower():
                return None
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    except Exception:  # pragma: no cover - imprévisible, même garde que _fetch
        logger.exception("Erreur inattendue en récupérant %s", url)
        return None


def _traiter_une_page(resultat: ResultatWeb) -> tuple[ResultatWeb, list[str], str, float]:
    """Fetch + extraction pour UN résultat. Rend (résultat, chunks, statut, ms).

    Tourne dans un thread du pool — jamais sur le thread appelant. N'écrit
    jamais dans `resultat` : les dataclasses `ResultatWeb` restent immuables
    de fait ici, `recuperer_contenu` construit les remplaçants à la fin,
    séquentiellement, une fois tous les fetchs revenus (ou abandonnés).
    """
    t0 = time.time()
    html = _recuperer_page(resultat.url)
    if html is None:
        return resultat, [], "échec", round((time.time() - t0) * 1000)
    texte = _extraire_texte_principal(html)
    if not texte:
        return resultat, [], "échec", round((time.time() - t0) * 1000)
    return resultat, _decouper_en_chunks(texte), "succès", round((time.time() - t0) * 1000)


def _vecteurs_partages():
    """Accès PARESSEUX au store vectoriel partagé de `core/runtime.py`.

    Import LOCAL, jamais en tête de fichier : `core.runtime` instancie TOUS
    les moteurs de l'application par effet de bord dès son import (LLM,
    mémoire, agent de code, thread de préchauffage…) — un import en tête de
    `core/webcontent.py` ferait payer ce coût à quiconque importe ce module
    sans jamais appeler `recuperer_contenu`, et inverserait la direction de
    dépendance du dépôt (core/runtime.py assemble les moteurs feuilles, il
    n'est importé PAR aucun d'eux aujourd'hui). Même idiome que
    `core/embedding.py` important `onnxruntime` dans `__init__` plutôt qu'en
    tête de module, pour la même raison : payer le coût seulement à l'usage
    réel, pas à l'import.

    Rend le `VectorStore` partagé (via son proxy `_LazyEngine`) : PAS un
    second `MoteurEmbedding` instancié ici. Peut lever `EmbeddingIndisponible`
    — l'appelant la traite exactement comme `modules/chat/router.py::
    _corpus_ou_inconnu` le fait déjà pour la même exception : dégrader
    proprement, ne jamais la laisser remonter jusqu'à faire échouer une
    fonctionnalité qui doit marcher au minimum sans le RAG.
    """
    from core.runtime import vector_store
    return vector_store


def _reclasser_par_similarite(
    requete: str, chunks_par_rang: dict[int, list[str]],
) -> tuple[dict[int, list[str]], bool]:
    """Meilleurs passages par rang de page, ou dégradation si l'embedding
    manque. Rend ``(passages_retenus_par_rang, reclassement_effectue)``.

    Un seul appel d'embedding pour TOUT l'appel (la requête + tous les chunks
    de toutes les pages, dans un seul lot) : `MoteurEmbedding.encoder`
    tokenise et infère par lot (`core/embedding.py`, `TAILLE_LOT=32`) — un
    appel par chunk paierait le coût fixe de l'inférence ONNX une fois par
    chunk au lieu d'une fois pour tous.
    """
    tous_les_chunks: list[str] = []
    bornes: list[tuple[int, int, int]] = []  # (rang, début, fin) dans tous_les_chunks
    for rang, chunks in chunks_par_rang.items():
        debut = len(tous_les_chunks)
        tous_les_chunks.extend(chunks)
        bornes.append((rang, debut, len(tous_les_chunks)))

    if not tous_les_chunks:
        return {}, True

    try:
        vecteurs = _vecteurs_partages().embed_texts([requete] + tous_les_chunks)
    except EmbeddingIndisponible:
        logger.warning(
            "Reclassement des passages web ignoré : modèle d'embedding "
            "indisponible (pile pas encore téléchargée) — extraits DuckDuckGo "
            "conservés pour cette étape."
        )
        return {}, False

    vecteur_requete = vecteurs[0]
    vecteurs_chunks = vecteurs[1:]
    # Produit scalaire = cosinus : les vecteurs de `encoder` sont déjà
    # normalisés (norme 1), cf. sa docstring dans core/embedding.py.
    scores = vecteurs_chunks @ vecteur_requete

    retenus: dict[int, list[str]] = {}
    for rang, debut, fin in bornes:
        indices = list(range(debut, fin))
        meilleurs = sorted(indices, key=lambda i: scores[i], reverse=True)[:_MAX_PASSAGES_PAR_PAGE]
        # Ordre D'APPARITION dans la page, pas ordre de score : un passage
        # retenu doit rester lisible dans son contexte naturel.
        retenus[rang] = [tous_les_chunks[i] for i in sorted(meilleurs)]
    return retenus, True


def recuperer_contenu(
    resultats: list[ResultatWeb],
    requete: str,
    on_etape: Optional[Callable[[dict], None]] = None,
) -> list[ResultatWeb]:
    """Remplace l'extrait DDG des résultats les plus pertinents par du contenu
    réel de la page, reclassé par similarité avec `requete`.

    Dégradation À TROIS NIVEAUX, jamais d'exception vers l'appelant :

      1. une page individuelle échoue (timeout, 403, paywall, pas de texte
         extractible) → CETTE page garde son extrait DDG d'origine, les
         autres ne sont pas affectées (dégradation PAR PAGE) ;
      2. le modèle d'embedding est indisponible → tous les extraits DDG sont
         conservés pour l'étape ENTIÈRE (dégradation GLOBALE) : la recherche
         web ne doit jamais dépendre du RAG pour fonctionner au minimum ;
      3. `resultats` vide → rendu tel quel, rien à faire.

    Ne modifie AUCUN `ResultatWeb` en place (dataclasses traitées comme
    immuables) : rend une NOUVELLE liste, dans le même ordre que `resultats`.
    """
    if not resultats:
        return resultats

    a_enrichir = sorted(resultats, key=lambda r: r.rang)[:_MAX_PAGES_ENRICHIES]
    if not a_enrichir:
        return resultats

    # ── 1. Fetch + extraction, en parallèle, deux plafonds jamais additionnés.
    #
    # `shutdown(wait=False)` explicitement — PAS de `with` : le gestionnaire de
    # contexte appellerait `shutdown(wait=True)` à la sortie et attendrait les
    # threads encore en cours, annulant tout l'intérêt du budget global.
    # `cancel_futures=True` écarte les tâches pas encore démarrées ; celles déjà
    # en plein `urlopen()` continuent en arrière-plan et se terminent seules
    # (borne individuelle de `_TIMEOUT_PAGE_S`) — même compromis que `_borne`
    # dans `main.py::health` : on cesse d'ATTENDRE le thread, on ne le tue pas.
    executeur = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(a_enrichir), thread_name_prefix="webcontent",
    )
    try:
        futurs = {executeur.submit(_traiter_une_page, r): r for r in a_enrichir}
        termines, _en_cours = concurrent.futures.wait(futurs, timeout=_TIMEOUT_ETAPE_S)
    finally:
        executeur.shutdown(wait=False, cancel_futures=True)

    chunks_par_rang: dict[int, list[str]] = {}
    statut_par_rang: dict[int, tuple[str, float]] = {}
    for futur in termines:
        resultat, chunks, statut, ms = futur.result()
        statut_par_rang[resultat.rang] = (statut, ms)
        if chunks:
            chunks_par_rang[resultat.rang] = chunks
    # Les futurs de `_en_cours` (budget global dépassé) n'ont simplement pas de
    # statut : la page garde son extrait DDG plus bas, comme un échec normal —
    # pas la peine de les distinguer, le résultat pour l'utilisateur est identique.
    for r in a_enrichir:
        if r.rang not in statut_par_rang:
            statut_par_rang[r.rang] = ("échec", round(_TIMEOUT_ETAPE_S * 1000))

    # ── 2. Reclassement des passages par similarité avec la requête.
    passages_par_rang, reclassement_effectue = _reclasser_par_similarite(requete, chunks_par_rang)
    if not reclassement_effectue:
        _emettre(on_etape, {"etape": "reclassement_indisponible", "raison": "embedding indisponible"})

    # ── 3. Trace + construction des résultats enrichis.
    par_rang = {r.rang: r for r in resultats}
    for r in a_enrichir:
        statut, ms = statut_par_rang[r.rang]
        passages = passages_par_rang.get(r.rang, [])
        _emettre(on_etape, {
            "etape": "page_recuperee", "url": r.url, "statut": statut,
            "ms": ms, "passages_retenus": len(passages),
        })
        if passages:
            extrait_enrichi = tronquer_champ(" […] ".join(passages), _BUDGET_CARACTERES_PAR_PAGE)
            par_rang[r.rang] = ResultatWeb(
                rang=r.rang, titre=r.titre, url=r.url,
                extrait=extrait_enrichi, moteur=r.moteur,
            )

    return [par_rang[r.rang] for r in resultats]
