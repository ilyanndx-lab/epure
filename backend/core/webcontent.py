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

── Lecture directe d'une URL collée dans le message (`lire_urls`) ──────────

Avant elle, une URL dans le message n'était jamais lue : le modèle répondait
« sur la page » de mémoire, sans le dire — ou, si un motif du classifieur
matchait par accident (une date `/2024/` dans le chemin), le message ENTIER,
URL comprise, partait vers DuckDuckGo. `lire_urls` va chercher la page
elle-même, sans moteur tiers, avec les MÊMES budgets de temps et de nombre
de pages que la phase 4 — mêmes constantes, pas une seconde série à faire
diverger.

Trois différences assumées avec `recuperer_contenu` :

  - **le budget de caractères suit le contexte CHARGÉ du modèle**
    (`budget_lecture`), au lieu des 800 caractères fixes de la phase 4. Ces
    800 caractères sont pensés pour cinq extraits @web à la fois ; pour UNE
    page donnée explicitement, ils perdaient la réponse dès qu'elle se
    trouvait au-delà des trois premiers passages retenus (cf.
    `test_budget_lecture_url.py`) ;

  - **un échec est NOMMÉ** (connexion requise, 403, paywall, délai…) au lieu
    de retomber sur un extrait DDG — il n'y a pas d'extrait de repli ici, et
    un repli silencieux sur les connaissances du modèle est précisément le
    défaut corrigé ;
  - **les adresses locales et privées sont refusées** (`adresse_refusee`),
    y compris après résolution DNS et à chaque redirection : l'URL vient d'un
    texte que l'utilisateur a pu copier d'une page hostile, et sans ce refus
    coller un lien suffirait à faire interroger par Épure le backend lui-même
    (`127.0.0.1:8000`) ou la box du réseau local (CLAUDE.md §6, menace 1).

Limite connue et acceptée : la vérification DNS précède la connexion, qui
résout à nouveau — un domaine contrôlé par un attaquant peut répondre une
adresse publique à la première résolution et privée à la seconde (DNS
rebinding). Fermer cette fenêtre demande d'épingler l'adresse vérifiée dans
la connexion HTTPS (SNI, certificat), hors de portée de `urllib` sans
réécrire son transport ; le refus couvre l'accident et le lien piégé
ordinaire, pas un attaquant qui contrôle un serveur DNS.
"""

import concurrent.futures
import html as _htmllib
import ipaddress
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from readability import Document

from core.embedding_install import EmbeddingIndisponible
from core.websearch import ResultatWeb, _UA_PRINCIPAL, _domaine, _emettre, tronquer_champ

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

# ── Budget de la lecture DIRECTE d'une URL (`lire_urls`) ───────────────────
#
# La phase 4 garde ses 800 caractères par page (ci-dessus). Une URL donnée
# explicitement est un autre besoin : l'utilisateur pose une question précise
# sur CETTE page, souvent sur un détail situé loin du début. Le budget suit
# donc la fenêtre que le modèle a réellement allouée
# (`core.fenetre_contexte.fenetre_de`, jamais le maximum qu'il saurait tenir),
# et le reste du prompt (consigne système, mémoire, RAG, historique, réponse
# et raisonnement) garde les trois quarts.

#: Part du contexte chargé réservée aux pages lues, tous liens confondus.
_PART_CONTEXTE_LECTURE = 0.25
#: Conversion tokens → caractères. Une estimation, pas une mesure par
#: tokeniseur : le français tourne autour de 3 à 4 caractères par token sur
#: les tokeniseurs courants, 3,5 est le milieu.
_CARACTERES_PAR_TOKEN = 3.5
#: Plafond, quel que soit le contexte : au-delà, la lecture d'une page
#: allonge le délai avant le premier token sans gain mesuré. PROVISOIRE, à
#: fixer d'après la mini-évaluation (4 000 / 8 000 / 16 000 caractères).
_BUDGET_LECTURE_MAX = 16000
#: Plancher quand le contexte chargé est INCONNU (modèle pas encore chargé,
#: fournisseur qui ne l'expose pas). Cinq fois le budget de la phase 4 : de
#: quoi tenir une page courte, sans parier sur une fenêtre qu'on ne connaît pas.
_BUDGET_LECTURE_PLANCHER = 4000


def budget_lecture(contexte_charge: Optional[int]) -> int:
    """Budget TOTAL, en caractères, des pages lues d'un même message.

    25 % du contexte chargé × 3,5 caractères par token, plafonné à
    `_BUDGET_LECTURE_MAX`. Le plancher ne s'applique qu'à un contexte
    INCONNU : un contexte connu et petit donne un budget petit, c'est voulu —
    le dépasser déborderait la fenêtre que le modèle a effectivement.
    """
    if not contexte_charge or contexte_charge <= 0:
        return _BUDGET_LECTURE_PLANCHER
    return min(_BUDGET_LECTURE_MAX,
               int(contexte_charge * _PART_CONTEXTE_LECTURE * _CARACTERES_PAR_TOKEN))


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
    scores_par_rang = _scores_similarite(requete, chunks_par_rang)
    if scores_par_rang is None:
        return {}, False

    retenus: dict[int, list[str]] = {}
    for rang, scores in scores_par_rang.items():
        chunks = chunks_par_rang[rang]
        meilleurs = sorted(range(len(chunks)), key=lambda i: scores[i],
                           reverse=True)[:_MAX_PASSAGES_PAR_PAGE]
        # Ordre D'APPARITION dans la page, pas ordre de score : un passage
        # retenu doit rester lisible dans son contexte naturel.
        retenus[rang] = [chunks[i] for i in sorted(meilleurs)]
    return retenus, True


def _scores_similarite(
    requete: str, chunks_par_cle: dict[int, list[str]],
) -> Optional[dict[int, list[float]]]:
    """Similarité de chaque passage avec `requete`, par page. ``None`` si
    l'embedding manque (journalisé ici ; l'appelant le dit dans la trace).

    Un seul appel d'embedding pour toutes les pages (cf.
    `_reclasser_par_similarite`), sur le store vectoriel PARTAGÉ — local, la
    requête ne quitte jamais la machine.
    """
    tous_les_chunks: list[str] = []
    bornes: list[tuple[int, int, int]] = []  # (clé, début, fin) dans tous_les_chunks
    for cle, chunks in chunks_par_cle.items():
        debut = len(tous_les_chunks)
        tous_les_chunks.extend(chunks)
        bornes.append((cle, debut, len(tous_les_chunks)))

    if not tous_les_chunks:
        return {}

    try:
        vecteurs = _vecteurs_partages().embed_texts([requete] + tous_les_chunks)
    except EmbeddingIndisponible:
        logger.warning(
            "Reclassement des passages web ignoré : modèle d'embedding "
            "indisponible (pile pas encore téléchargée)."
        )
        return None

    # Produit scalaire = cosinus : les vecteurs de `encoder` sont déjà
    # normalisés (norme 1), cf. sa docstring dans core/embedding.py.
    scores = vecteurs[1:] @ vecteurs[0]
    return {cle: [float(scores[i]) for i in range(debut, fin)] for cle, debut, fin in bornes}


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


# ── Lecture directe d'une URL fournie par l'utilisateur ─────────────────────
#
# Cf. la dernière section de la docstring de module pour le pourquoi.

class _AdresseRefusee(Exception):
    """Adresse locale ou privée rencontrée en cours de route — levée depuis
    `_RedirectionVerifiee` pour interrompre `urllib` avant qu'il ne suive le
    saut. Porte la raison lisible, qui part telle quelle dans la trace."""


#: Préfixe commun des raisons de refus — c'est ce que l'utilisateur lit dans
#: le panneau de trace, et ce que les tests vérifient.
_RAISON_ADRESSE_PRIVEE = "adresse locale ou privée refusée"


#: Espace d'adressage partagé (CGNAT, RFC 6598). Nommé à part parce
#: qu'AUCUNE des propriétés d'`ipaddress` ci-dessous ne le couvre (mesuré sur
#: 3.11 et 3.14 : `is_private` faux, seul `is_global` est faux) — c'est
#: pourtant le réseau interne d'un opérateur, jamais une page publique.
_ESPACE_PARTAGE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _ip_interdite(ip) -> bool:
    """L'adresse est-elle locale, privée ou non routable ?

    Par les PROPRIÉTÉS d'`ipaddress`, jamais par une liste de plages écrite à
    la main : bouclage (127/8, ::1), privées (10/8, 172.16/12, 192.168/16,
    fc00::/7), lien local (169.254/16 — dont le service de métadonnées des
    hébergeurs —, fe80::/10), réservées, multicast, non spécifiée (0.0.0.0,
    ::). Pas `not is_global` seul : il est VRAI pour 224.0.0.1 (multicast),
    d'où `is_multicast` nommé explicitement.

    Une IPv4 encapsulée dans une IPv6 (`::ffff:10.0.0.1`) est jugée sur
    l'IPv4 qu'elle porte : avant Python 3.13, `::ffff:0:0/96` entier est
    « privé/réservé » quelle que soit l'IPv4 dedans (mesuré sur 3.11), donc
    sans ce déballage le verdict changerait avec la version de Python.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        or (ip.version == 4 and ip in _ESPACE_PARTAGE_CGNAT)
    )


def _ip_litterale(hote: str):
    """IP écrite en clair dans l'URL, sous TOUTES ses formes, sans DNS.

    `ipaddress` n'accepte que la forme canonique ; mais `urllib` passe l'hôte
    au résolveur système, qui accepte aussi `2130706433` (décimal),
    `0x7f000001` (hexadécimal), `0177.0.0.1` (octal) ou `127.1` (forme
    courte) — toutes `127.0.0.1`. Sans ce second essai, ces formes ne
    seraient refusées que si la résolution DNS, plus bas, les ramène à
    l'adresse qu'elles désignent : un comportement de résolveur, pas une
    garantie. `inet_aton` est exactement l'analyseur de ces formes
    historiques (sans réseau), et se comporte de même sous Windows.
    """
    try:
        return ipaddress.ip_address(hote)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(hote))
    except (OSError, ValueError):
        return None


def adresse_refusee(url: str) -> Optional[str]:
    """Raison du refus de contacter `url`, ou `None` si elle est autorisée.

    Le nom d'hôte est RÉSOLU avant d'être jugé : `piege.example` peut pointer
    vers `192.168.1.1` aussi bien qu'une IP écrite en clair, et une forme
    exotique (`127.1`, décimal) est ramenée par la résolution à l'adresse
    qu'elle désigne. Un échec de résolution n'est pas un refus : l'ouverture
    échouera ensuite d'elle-même, avec sa propre raison (« site injoignable »).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"schéma « {parsed.scheme or '?'} » non pris en charge (http ou https seulement)"
    hote = (parsed.hostname or "").rstrip(".").lower()
    if not hote:
        return "adresse invalide (aucun hôte)"
    if hote == "localhost" or hote.endswith(".localhost"):
        return f"{_RAISON_ADRESSE_PRIVEE} ({hote})"
    litterale = _ip_litterale(hote)
    if litterale is not None:
        return f"{_RAISON_ADRESSE_PRIVEE} ({hote})" if _ip_interdite(litterale) else None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return "adresse invalide (port)"
    try:
        infos = socket.getaddrinfo(hote, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    for info in infos:
        adresse = str(info[4][0]).split("%", 1)[0]  # zone IPv6 (`fe80::1%eth0`)
        try:
            ip = ipaddress.ip_address(adresse)
        except ValueError:
            continue
        if _ip_interdite(ip):
            return f"{_RAISON_ADRESSE_PRIVEE} ({hote} → {adresse})"
    return None


class _RedirectionVerifiee(urllib.request.HTTPRedirectHandler):
    """`urllib` suit les redirections tout seul : sans ce contrôle à CHAQUE
    saut, une page publique pourrait renvoyer `302 → http://127.0.0.1:8000/`
    et contourner la vérification faite sur l'URL de départ."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raison = adresse_refusee(newurl)
        if raison:
            raise _AdresseRefusee(raison)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OUVREUR = urllib.request.build_opener(_RedirectionVerifiee)


def _ouvrir(req: urllib.request.Request, timeout: float):
    """Seul point d'appel réseau de la lecture directe — remplacé par les
    tests (`test_lecture_url.py`), jamais contourné par le reste du module."""
    return _OUVREUR.open(req, timeout=timeout)


#: Borne de lecture du corps : une URL collée peut désigner un fichier de
#: plusieurs centaines de Mo servi en `text/html` par erreur ; le budget de
#: caractères final (800) ne justifie jamais d'en lire davantage.
_TAILLE_MAX_OCTETS = 5 * 1024 * 1024

#: En dessous de ce nombre de caractères extraits, une page qui porte un champ
#: de mot de passe est traitée comme une page de connexion. Au-dessus, le
#: champ est vraisemblablement le formulaire « se connecter » d'un en-tête de
#: site, à côté d'un vrai contenu lisible — refuser l'article pour ça serait
#: un faux positif sur la moitié du web.
_TEXTE_MIN_HORS_CONNEXION = 500

_MOTIF_MOT_DE_PASSE = re.compile(r"<input\b[^>]*\btype\s*=\s*['\"]?password", re.IGNORECASE)

#: Marqueur schema.org standard des articles payants (`isAccessibleForFree:
#: false`), posé par les sites de presse justement pour que les robots
#: sachent que le texte servi n'est qu'un chapeau. Le texte extrait serait
#: une amorce présentée au modèle comme l'article entier.
_MOTIF_PAYWALL = re.compile(r"[\"']isAccessibleForFree[\"']\s*:\s*[\"']?false", re.IGNORECASE)

#: Chemin ou hôte d'une page de connexion, pour reconnaître une redirection
#: « vous devez être connecté » servie en 200 après un 302.
_MOTIF_CHEMIN_CONNEXION = re.compile(
    r"(?:^|[/._-])(?:log-?in|sign-?in|connexion|se-connecter|auth|authentication|sso)(?:$|[/._-])",
    re.IGNORECASE,
)
_PREFIXES_HOTE_CONNEXION = ("login.", "accounts.", "auth.", "sso.", "signin.", "connexion.")


def _ressemble_a_une_page_de_connexion(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    hote = (parsed.hostname or "").lower()
    return hote.startswith(_PREFIXES_HOTE_CONNEXION) or bool(_MOTIF_CHEMIN_CONNEXION.search(parsed.path))


def _raison_delai() -> str:
    return f"délai dépassé ({_TIMEOUT_PAGE_S:g} s)"


def _raison_http(code: int) -> str:
    if code in (401, 407):
        return f"page protégée par une connexion (HTTP {code})"
    if code == 402:
        return f"contenu réservé aux abonnés (HTTP {code})"
    if code == 403:
        return ("accès refusé par le site (HTTP 403) — protection anti-robot, "
                "connexion ou abonnement requis")
    if code in (404, 410):
        return f"page introuvable (HTTP {code})"
    if code == 429:
        return "le site limite le nombre de requêtes (HTTP 429)"
    if code >= 500:
        return f"erreur du site (HTTP {code})"
    return f"réponse inattendue du site (HTTP {code})"


def _recuperer_page_detaillee(url: str) -> tuple[Optional[str], str, str]:
    """Comme `_recuperer_page`, mais rend AUSSI pourquoi ça a échoué et où
    l'on a atterri : ``(html | None, raison, url_finale)``.

    `_recuperer_page` (phase 4) peut se contenter de `None` — l'extrait DDG
    prend le relais. Ici rien ne prend le relais : la raison est tout ce que
    l'utilisateur saura. Même timeout par page, même User-Agent unique.

    Lève `_AdresseRefusee` si une redirection vise une adresse interdite —
    l'appelant en fait un statut « refusée », distinct d'un échec réseau.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA_PRINCIPAL, "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with _ouvrir(req, _TIMEOUT_PAGE_S) as resp:
            url_finale = resp.geturl() or url
            statut = getattr(resp, "status", 200)
            if statut != 200:
                return None, _raison_http(statut), url_finale
            type_contenu = resp.headers.get("Content-Type", "") or ""
            if type_contenu and "html" not in type_contenu.lower():
                type_court = type_contenu.split(";", 1)[0].strip()
                return None, f"type de contenu non pris en charge ({type_court})", url_finale
            return resp.read(_TAILLE_MAX_OCTETS).decode("utf-8", errors="replace"), "", url_finale
    except urllib.error.HTTPError as exc:
        exc.close()  # porte la réponse, donc la socket : ne pas attendre le GC
        return None, _raison_http(exc.code), url
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, _raison_delai(), url
        return None, tronquer_champ(f"site injoignable ({exc.reason})"), url
    except TimeoutError:
        return None, _raison_delai(), url
    except OSError as exc:
        return None, tronquer_champ(f"site injoignable ({exc})"), url


def _titre_de_page(html: str) -> str:
    try:
        return (Document(html).short_title() or "").strip()
    except Exception:
        return ""


def _lire_une_url(url: str) -> tuple[list[str], str, str, float, str]:
    """Refus, fetch, contrôles, extraction pour UNE URL, dans un thread du
    pool. Rend ``(chunks, statut, raison, ms, titre)`` — `statut` ∈
    {"succès", "échec", "refusée"}. Ne lève jamais : une exception imprévue
    devient un échec nommé, pas une lecture qui disparaît de la trace."""
    t0 = time.time()

    def fin(statut: str, raison: str, chunks: Optional[list[str]] = None, titre: str = ""):
        return chunks or [], statut, raison, round((time.time() - t0) * 1000), titre

    try:
        raison = adresse_refusee(url)
        if raison:
            return fin("refusée", raison)
        try:
            html, raison, url_finale = _recuperer_page_detaillee(url)
        except _AdresseRefusee as exc:
            return fin("refusée", f"redirection refusée : {exc}")
        if html is None:
            return fin("échec", raison)
        if url_finale != url and _ressemble_a_une_page_de_connexion(url_finale):
            return fin("échec", "redirigé vers une page de connexion — page protégée par une connexion")
        if _MOTIF_PAYWALL.search(html):
            return fin("échec", "contenu réservé aux abonnés (paywall)")
        texte = _extraire_texte_principal(html)
        if _MOTIF_MOT_DE_PASSE.search(html) and len(texte) < _TEXTE_MIN_HORS_CONNEXION:
            return fin("échec", "page protégée par une connexion (formulaire de mot de passe, aucun contenu lisible)")
        if not texte:
            return fin("échec", "aucun texte exploitable dans la page (contenu généré en JavaScript ?)")
        return fin("succès", "", _decouper_en_chunks(texte), _titre_de_page(html))
    except Exception:
        logger.exception("Erreur inattendue en lisant %s", url)
        return fin("échec", "erreur inattendue pendant la lecture")


@dataclass(frozen=True)
class LectureUrl:
    """Issue de la lecture d'UNE URL. `resultat` n'existe qu'en cas de
    succès : c'est la source citable `[n]` qui rejoint `web_resultats`
    (modules/chat/router.py) ; un échec n'a que sa `raison`."""
    url: str
    statut: str  # "succès" | "échec" | "refusée"
    raison: str  # vide en cas de succès
    passages: int
    resultat: Optional[ResultatWeb]


def _assembler(chunks: list[str], indices: list[int]) -> str:
    """Les passages retenus dans l'ordre de la page. Deux passages voisins
    sont recollés tels quels (c'est du texte continu) ; un saut est marqué
    « […] », comme dans les extraits de la phase 4."""
    morceaux: list[str] = []
    precedent = None
    for i in sorted(indices):
        if precedent is not None and i == precedent + 1:
            morceaux[-1] += " " + chunks[i]
        else:
            morceaux.append(chunks[i])
        precedent = i
    return " […] ".join(morceaux)


def _couper_sur_un_mot(texte: str, limite: int) -> str:
    """`texte` ramené à `limite` caractères, « … » compris, coupé avant un
    mot plutôt qu'au milieu."""
    if len(texte) <= limite:
        return texte
    coupe = texte[:max(0, limite - 1)]
    if " " in coupe:
        coupe = coupe[:coupe.rindex(" ")]
    return coupe.rstrip() + "…"


def _selectionner(chunks: list[str], priorite: list[int], budget: int) -> tuple[str, int]:
    """Prend les passages par ordre de `priorite` tant qu'ils tiennent dans
    `budget`, puis les remet dans l'ordre de la page. Rend ``(extrait,
    nombre de passages)``.

    La coupe tombe sur une FRONTIÈRE de passage : un passage qui ne tient pas
    entier est sauté (un suivant, plus court, peut encore tenir), jamais
    tronqué. Exception unique : quand même le premier ne tient pas (contexte
    minuscule partagé entre plusieurs pages), il est coupé sur un mot —
    mieux qu'une page déclarée « sans passage exploitable » alors qu'elle a
    été lue.
    """
    retenus: list[int] = []
    for i in priorite:
        if len(_assembler(chunks, retenus + [i])) <= budget:
            retenus.append(i)
    if retenus:
        return _assembler(chunks, retenus), len(retenus)
    if not priorite or budget <= 1:
        return "", 0
    return _couper_sur_un_mot(chunks[priorite[0]], budget), 1


def _priorites(
    question: str, chunks_par_index: dict[int, list[str]],
    on_etape: Optional[Callable[[dict], None]],
) -> tuple[dict[int, list[int]], str]:
    """Ordre de préférence des passages de chaque page, et le mode de
    sélection pour la trace : ``"similarite"`` (avec la question, sans ses
    URLs) ou ``"ordre_page"``.

    L'ordre de la page sert quand il n'y a rien à quoi comparer (message
    réduit à l'URL : « lis ça » demande l'essentiel, pas un passage précis)
    ou que l'embedding manque. À la différence de `recuperer_contenu`, pas de
    repli « extrait DDG » possible : le début de page est le seul repli
    honnête — et la trace le dit.
    """
    ordre_page = {i: list(range(len(chunks))) for i, chunks in chunks_par_index.items()}
    if not question.strip():
        return ordre_page, "ordre_page"
    scores = _scores_similarite(question, chunks_par_index)
    if scores is None:
        _emettre(on_etape, {"etape": "reclassement_indisponible", "raison": "embedding indisponible"})
        return ordre_page, "ordre_page"
    # `sorted` est stable : à score égal, le passage le plus tôt dans la page passe d'abord.
    return {i: sorted(range(len(sc)), key=lambda k: sc[k], reverse=True)
            for i, sc in scores.items()}, "similarite"


def lire_urls(
    urls: list[str],
    question: str,
    rang_de_depart: int = 0,
    on_etape: Optional[Callable[[dict], None]] = None,
    contexte_charge: Optional[int] = None,
    source_contexte: Optional[str] = None,
) -> list[LectureUrl]:
    """Lit directement chaque URL fournie par l'utilisateur, SANS moteur de
    recherche. Une `LectureUrl` par URL lue, dans l'ordre de `urls`.

    `question` (le message sans ses URLs) sert au reclassement des passages ;
    elle ne quitte jamais la machine — seule l'URL est contactée.

    Une étape `lecture_url` par URL part dans `on_etape` (url, statut, ms,
    `passages_retenus`, et `raison` si échec ou refus) : c'est ce qui rend un
    échec visible dans le panneau de trace ET dans l'historique persisté.

    Mêmes bornes de temps et de nombre que `recuperer_contenu` : au plus
    `_MAX_PAGES_ENRICHIES` URLs, `_TIMEOUT_PAGE_S` par page,
    `_TIMEOUT_ETAPE_S` pour l'étape (pages en parallèle, plafonds jamais
    additionnés). Les pages lues reçoivent des rangs contigus à partir de
    `rang_de_depart + 1` — ceux des résultats @web déjà attribués ce tour
    restent intacts. Ne lève jamais.

    Caractères : `budget_lecture(contexte_charge)` au total, partagé
    également entre les pages LUES — une page en échec n'occupe rien dans le
    prompt, lui garder sa part la perdrait. `contexte_charge` et
    `source_contexte` (d'où vient ce chiffre) viennent de
    `core.fenetre_contexte.fenetre_de` ; ``None`` = inconnu, donc plancher.
    Chaque étape `lecture_url` les porte, avec le budget de la page, la
    taille réelle de l'extrait et le mode de `selection`.
    """
    a_lire = list(dict.fromkeys(urls))[:_MAX_PAGES_ENRICHIES]
    if not a_lire:
        return []

    # Même compromis que `recuperer_contenu` : on cesse d'ATTENDRE un thread
    # trop lent, on ne le tue pas (cf. son commentaire sur `shutdown`).
    executeur = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(a_lire), thread_name_prefix="lecture-url",
    )
    try:
        futurs = {executeur.submit(_lire_une_url, url): i for i, url in enumerate(a_lire)}
        termines, _en_cours = concurrent.futures.wait(futurs, timeout=_TIMEOUT_ETAPE_S)
    finally:
        executeur.shutdown(wait=False, cancel_futures=True)

    issues = {futurs[f]: f.result() for f in termines}
    for i in range(len(a_lire)):
        if i not in issues:
            issues[i] = ([], "échec", f"délai dépassé (budget de {_TIMEOUT_ETAPE_S:g} s pour l'étape)",
                         round(_TIMEOUT_ETAPE_S * 1000), "")

    chunks_par_index = {i: issue[0] for i, issue in issues.items()
                        if issue[0] and issue[1] == "succès"}
    budget_total = budget_lecture(contexte_charge)
    budget_page = budget_total // len(chunks_par_index) if chunks_par_index else 0
    priorites, selection = (_priorites(question, chunks_par_index, on_etape)
                            if chunks_par_index else ({}, ""))

    lectures: list[LectureUrl] = []
    rang = rang_de_depart
    for i, url in enumerate(a_lire):
        chunks, statut, raison, ms, titre = issues[i]
        extrait, nb_passages = "", 0
        if i in chunks_par_index:
            extrait, nb_passages = _selectionner(chunks, priorites[i], budget_page)
        resultat = None
        if nb_passages:
            rang += 1
            resultat = ResultatWeb(
                rang=rang, titre=tronquer_champ(titre or _domaine(url)), url=url,
                extrait=extrait, moteur="lecture-directe",
            )
        elif statut == "succès":
            statut, raison = "échec", "aucun passage exploitable dans la page"
        etape = {
            "etape": "lecture_url", "url": tronquer_champ(url), "statut": statut,
            "ms": ms, "passages_retenus": nb_passages,
            "contexte_charge": contexte_charge, "source_contexte": source_contexte,
            "budget_caracteres": budget_total,
        }
        if resultat is not None:
            etape.update(selection=selection, budget_page=budget_page, caracteres=len(extrait))
        if raison:
            etape["raison"] = tronquer_champ(raison)
        _emettre(on_etape, etape)
        lectures.append(LectureUrl(url=url, statut=statut, raison=raison,
                                   passages=nb_passages, resultat=resultat))
    return lectures
