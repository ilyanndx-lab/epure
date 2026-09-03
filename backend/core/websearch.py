"""Recherche web DuckDuckGo (@web du chat), extraite de modules/chat/router.py
le 2026-09-02.

Pourquoi ici et pas dans le routeur chat : la recherche web n'est pas un
détail du chat — le RAG (citations sourcées) et l'Atelier (contexte d'un
module généré) en auront besoin, et dupliquer ce parsing serait dupliquer
son piège.

Le piège, justement : `_HTML_RESULT_RE` (l'ancienne regex, dans
modules/chat/router.py) capturait le TEXTE de l'ancre
``<a class="result__a">``, jamais son attribut ``href``. Les résultats
avaient donc un titre et un extrait, mais aucune URL — le LLM, invité à
« citer la source », n'avait rien à citer et en inventait une. `ResultatWeb`
porte désormais l'URL RÉELLE (le lien de redirection ``duckduckgo.com/l/``
est résolu vers sa cible), pour que la validation d'une citation en aval soit
possible.
"""

import html as _htmllib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

from core.instance import modele_local_defaut
from core.runtime import llm

logger = logging.getLogger(__name__)


@dataclass
class ResultatWeb:
    rang: int  # 1-indexé, position dans les résultats retournés
    titre: str
    url: str  # absolue, https:// — jamais un lien de redirection DDG
    extrait: str
    moteur: str  # "ddg-instant" | "ddg-html"


class RechercheWebErreur(Exception):
    """Échec distinct d'un « 0 résultat » légitime.

    Deux cas : les deux stratégies (Instant Answer + HTML) ont échoué au
    niveau réseau, ou la page HTML a répondu mais sa structure ne correspond
    plus à ce que le parseur attend (cf. `_html_search`) — un 0 résultat
    silencieux dans ce second cas serait indiscernable d'une recherche
    vide, alors que c'est le parseur qui est cassé.
    """


#: Préfixe du message renvoyé par `perform_web_search` (modules/chat/router.py)
#: quand `rechercher` lève `RechercheWebErreur`. Exporté (pas de `_`) pour que
#: l'appelant websocket puisse distinguer un échec d'un vrai résultat sans
#: dupliquer la chaîne — la confusion des deux est précisément le bug que
#: cette phase corrige (un message d'erreur présenté au LLM comme un
#: « résultat récent à citer »).
PREFIXE_ERREUR = "Erreur de recherche web : "

# ── Trace de déroulé (@web, audit de confidentialité) ────────────────────────
#
# Exigence explicite de l'utilisateur, pas un confort de debug : la requête
# RÉELLEMENT envoyée à DuckDuckGo doit être visible mot pour mot dans
# l'application — c'est la seule garantie auditable qu'aucune donnée
# supplémentaire ne parte vers l'extérieur. `rechercher()` accepte pour ça un
# callback `on_etape`, appelé à CHAQUE étape significative (requête sur le
# point de partir, résultats reçus, exclusion publicitaire, échec). Le
# paramètre est optionnel et par défaut `None` : les appelants existants
# (`perform_web_search`, tous les tests d'avant cette phase) ne changent pas
# de comportement d'un octet.
#
# La trace part ensuite dans le JSON d'historique (`core/history.py`,
# `modules/chat/router.py`) — donc bornée ICI, à la source, plutôt que d'en
# confier la discipline à chaque appelant :

#: Nombre maximal d'étapes conservées pour un tour. Une recherche @web normale
#: en émet 2 à 4 (début, résultats ou erreur, plus une exclusion publicitaire
#: éventuelle) ; la marge est pour la phase 4 (`page_recuperee`,
#: `passages_retenus`), qui ajoutera des étapes SANS changer ce contrat.
TRACE_MAX_ETAPES = 20

#: Longueur maximale d'un champ TEXTE de trace (requête, titre, message
#: d'erreur, URL). Une requête ou un message d'erreur de plusieurs Ko dans
#: l'historique en tripleraient le poids pour de la télémétrie — mauvais
#: échange (cf. tâche, §4). Le marqueur « … » rend la troncature visible :
#: un champ coupé sans marqueur se lirait comme un champ complet.
TRACE_TEXTE_MAX = 300

#: Nombre maximal d'éléments dans une sous-liste d'une étape (URLs non
#: reconnues, rangs hors plage d'une étape `citations_invalides`) — distinct
#: de `_MAX_RESULTS`, qui borne les résultats de recherche eux-mêmes.
TRACE_LISTE_MAX = 10


def tronquer_champ(valeur: str, max_len: int = TRACE_TEXTE_MAX) -> str:
    """Coupe un champ texte de trace à `max_len`, marqueur « … » si coupé.

    Exportée (pas de `_`) : `modules/chat/router.py` l'utilise aussi pour ses
    propres champs de trace (l'étape `citations_invalides`, assemblée côté
    routeur) — une seule règle de troncature, pas une par module.
    """
    if len(valeur) <= max_len:
        return valeur
    return valeur[:max_len] + "…"


def _emettre(on_etape: Optional[Callable[[dict], None]], etape: dict) -> None:
    """Notifie `on_etape` d'une étape, sans jamais laisser un callback
    défaillant casser la recherche elle-même — la trace est un à-côté
    observable, pas une dépendance de `rechercher()`.
    """
    if on_etape is None:
        return
    try:
        on_etape(etape)
    except Exception:
        logger.exception("on_etape a levé pour l'étape %r", etape.get("etape"))


def _resultats_pour_trace(resultats: list[ResultatWeb]) -> list[dict]:
    """Projection bornée d'une liste de résultats pour la trace.

    Jamais l'extrait (déjà dans le prompt formaté, pas la peine de le
    dupliquer dans l'historique) : rang, titre tronqué, URL COMPLÈTE — c'est
    justement ce que `formater_pour_llm` retire du prompt, et que la trace
    doit au contraire montrer en clair pour être auditable.
    """
    return [{"rang": r.rang, "titre": tronquer_champ(r.titre), "url": r.url} for r in resultats]


_UA_PRINCIPAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_TIMEOUT_S = 8.0
# User-Agent alternatifs essayés en cas de blocage (403 Cloudflare, etc.)
_USER_AGENTS = [
    _UA_PRINCIPAL,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

# Nombre de résultats exploitables retenus par recherche.
_MAX_RESULTS = 5

# En dessous, une page vide de résultats est vraisemblablement une vraie
# absence de résultat DuckDuckGo (page d'erreur, redirection, etc.) plutôt
# qu'un changement de structure — cf. `_html_search`.
_TAILLE_MIN_STRUCTURE_SUSPECTE = 5000

# Cache mémoire LRU avec TTL court : évite de re-frapper DuckDuckGo pour une
# même requête (utile quand l'utilisateur reformule peu ou relance @web).
# Met en cache les RÉSULTATS STRUCTURÉS, pas une chaîne déjà formatée — c'est
# ce qui permet à un futur appelant (RAG, Atelier) de les consommer autrement
# qu'en les réinjectant tels quels dans un prompt.
_CACHE_TTL_S = 300.0
_CACHE_MAX = 64
_cache: "OrderedDict[str, tuple[float, list[ResultatWeb]]]" = OrderedDict()


def _cache_get(key: str) -> Optional[list[ResultatWeb]]:
    """Retourne la valeur en cache si présente et non expirée, sinon None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if (time.time() - ts) > _CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)  # marque comme récemment utilisé
    return value


def _cache_set(key: str, value: list[ResultatWeb]) -> None:
    """Insère/rafraîchit une entrée et évince les plus anciennes (LRU)."""
    _cache[key] = (time.time(), value)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def _fetch(url: str, accept: str) -> tuple[Optional[str], Optional[str]]:
    """Récupère une URL en essayant plusieurs User-Agent.

    Retourne ``(texte, None)`` en cas de succès, ``(None, erreur)`` sinon.
    """
    last_exc: Optional[str] = None
    for ua in _USER_AGENTS:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                if resp.status != 200:
                    logger.warning("Web search HTTP %s pour %s (UA: %s)", resp.status, url, ua)
                    last_exc = f"HTTP {resp.status}"
                    continue  # essayer prochain UA
                return resp.read().decode("utf-8", errors="replace"), None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            logger.warning("Web search impossible pour %s (UA: %s) : %s", url, ua, exc)
            last_exc = str(exc)
            continue
        except Exception as exc:  # pragma: no cover - imprévisible
            logger.exception("Web search erreur inattendue pour %s (UA: %s)", url, ua)
            last_exc = str(exc)
            continue
    return None, (last_exc or "erreur inconnue")


def _instant_answer(q: str) -> tuple[list[ResultatWeb], Optional[str]]:
    """Stratégie 1 : API DuckDuckGo Instant Answer (JSON).

    Ne garde que les champs porteurs d'une URL propre (Abstract+AbstractURL,
    Definition+DefinitionURL) : les RelatedTopics sont des listes de
    désambiguïsation DuckDuckGo sans URL fiable par élément, du bruit
    présenté comme des résultats — supprimés, pas seulement dégradés.
    """
    params = {
        "q": q,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
        "t": "epure",
    }
    url = "https://api.duckduckgo.com/" + urllib.parse.urlencode(params)
    raw, err = _fetch(url, "application/json")
    if raw is None:
        return [], err

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Web search JSON invalide pour %s", q)
        return [], "réponse JSON invalide"

    resultats: list[ResultatWeb] = []

    abstract = (data.get("Abstract") or "").strip()
    abstract_url = (data.get("AbstractURL") or "").strip()
    abstract_source = (data.get("AbstractSource") or "").strip()
    if abstract and abstract_url:
        resultats.append(ResultatWeb(
            rang=len(resultats) + 1,
            titre=abstract_source or "Résumé",
            url=abstract_url,
            extrait=abstract,
            moteur="ddg-instant",
        ))

    definition = (data.get("Definition") or "").strip()
    definition_url = (data.get("DefinitionURL") or "").strip()
    definition_source = (data.get("DefinitionSource") or "").strip()
    if definition and definition_url and definition != abstract:
        resultats.append(ResultatWeb(
            rang=len(resultats) + 1,
            titre=definition_source or "Définition",
            url=definition_url,
            extrait=definition,
            moteur="ddg-instant",
        ))

    return resultats, None


_HTML_RESULT_TAG_RE = re.compile(
    r'<a\s+([^>]*\bclass="[^"]*result__a[^"]*"[^>]*)>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_HTML_SNIPPET_RE = re.compile(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]*)"', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Base utilisée pour résoudre un href relatif ou protocole-relatif
# (`//duckduckgo.com/...`) : la page de résultats elle-même.
_DDG_BASE = "https://duckduckgo.com/"

# Motifs publicitaires. POURQUOI ce filtrage existe : la phase suivante rend
# les résultats CITABLES par le modèle — un placement payant glissé en tête
# de liste deviendrait une source légitimée dans une réponse d'assistant, et
# n'importe qui pourrait acheter un mot-clé pour s'y placer. Ce n'est pas du
# bruit à filtrer pour l'UX, c'est un vecteur d'empoisonnement du contexte.
# Centralisés ici (une seule fonction, une seule liste) pour rester
# maintenables quand DDG changera ses paramètres publicitaires.
_MOTIFS_PUBLICITAIRES = (
    # Rebond interne DDG vers un placement payant (Bing Ads), observé comme
    # cible de la redirection `/l/?uddg=` d'un résultat sponsorisé.
    re.compile(r"^https?://[^/]*duckduckgo\.com/y\.js(?:\?|$)", re.IGNORECASE),
    # Cible finale d'un clic publicitaire Bing.
    re.compile(r"^https?://(?:[^/]*\.)?bing\.com/aclick(?:\?|$)", re.IGNORECASE),
    # Marqueurs du réseau publicitaire DDG dans la query string, quel que
    # soit l'hôte final — plus robuste qu'un hôte fixe si DDG change de
    # partenaire publicitaire.
    re.compile(r"[?&](?:ad_domain|ad_provider|ad_type)=", re.IGNORECASE),
)


def _est_publicitaire(url: str) -> bool:
    """Teste si `url` correspond à un des motifs publicitaires connus.

    Appelé à la fois sur le href BRUT (avant résolution de la redirection
    DDG) et sur l'URL RÉSOLUE : un placement payant peut apparaître sous les
    deux formes selon que DDG l'expose derrière `/l/?uddg=` ou en clair.
    """
    return any(motif.search(url) for motif in _MOTIFS_PUBLICITAIRES)


def _strip_html(fragment: str) -> str:
    """Retire les balises et déséchappe les entités d'un fragment HTML."""
    return _htmllib.unescape(_HTML_TAG_RE.sub("", fragment)).strip()


def _resoudre_url_ddg(href: str) -> Optional[str]:
    """Résout le href d'un `<a class="result__a">` vers l'URL réelle absolue.

    Trois transformations, dans l'ordre :
      1. déséchappement des entités HTML (`&amp;` → `&`) — le href brut d'une
         page réelle contient `&amp;rut=...` après le paramètre `uddg`, et le
         laisser tel quel casserait le split des paramètres de requête ;
      2. protocole-relatif (`//host/...`) → `https:` ;
      3. redirection DuckDuckGo (`/l/?uddg=<urlencodé>`) → URL cible. Le
         paramètre `uddg` est parfois DOUBLEMENT urlencodé (observé en
         pratique) : on décode jusqu'à stabilité, borné à 3 itérations pour
         ne jamais boucler sur une entrée pathologique.

    Retourne None si l'URL finale n'est pas http(s) exploitable — jamais un
    lien de redirection DDG brut.
    """
    if not href:
        return None
    href = _htmllib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    resolved = urllib.parse.urljoin(_DDG_BASE, href)
    parsed = urllib.parse.urlparse(resolved)

    if parsed.netloc.endswith("duckduckgo.com") and "/l/" in parsed.path:
        cible = urllib.parse.parse_qs(parsed.query).get("uddg")
        if not cible:
            return None
        target = cible[0]
        for _ in range(3):
            decoded = urllib.parse.unquote(target)
            if decoded == target:
                break
            target = decoded
        resolved = target
        parsed = urllib.parse.urlparse(resolved)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return resolved


def _html_search(q: str) -> tuple[list[ResultatWeb], Optional[str], int]:
    """Stratégie 2 (fallback) : endpoint HTML html.duckduckgo.com.

    Retourne ``(résultats, erreur, nombre_écarté_publicité)`` sur un échec
    réseau. Lève `RechercheWebErreur` si la réponse a été reçue mais
    qu'aucun `result__a` n'a pu être extrait d'un corps de taille
    substantielle : DuckDuckGo change sa structure CSS sans préavis, et un
    0 résultat silencieux dans ce cas est indiscernable d'une recherche
    légitimement vide — cf. `RechercheWebErreur`.

    Le troisième élément est un compteur, ajouté pour la trace de déroulé
    (§1 de la tâche « rendre visible la recherche ») — la DÉCISION de
    filtrage (`_est_publicitaire`, phase 1.5) n'est ni touchée ni dupliquée,
    seulement comptée là où elle s'applique déjà.
    """
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
    raw, err = _fetch(url, "text/html")
    if raw is None:
        return [], err, 0

    tag_matches = _HTML_RESULT_TAG_RE.findall(raw)
    if not tag_matches:
        if len(raw) > _TAILLE_MIN_STRUCTURE_SUSPECTE:
            logger.error(
                "Web search « %s » : 0 résultat parsé pour une réponse de %d octets — "
                "structure HTML DuckDuckGo probablement modifiée",
                q, len(raw),
            )
            raise RechercheWebErreur("structure HTML DuckDuckGo probablement modifiée")
        return [], None, 0

    snippets = [_strip_html(m) for m in _HTML_SNIPPET_RE.findall(raw)]

    resultats: list[ResultatWeb] = []
    nombre_ecarte_pub = 0
    for i, (attrs, text) in enumerate(tag_matches):
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        href_brut = _htmllib.unescape(href_match.group(1))
        if _est_publicitaire(href_brut):
            nombre_ecarte_pub += 1
            continue
        url_reelle = _resoudre_url_ddg(href_match.group(1))
        if url_reelle is None:
            continue
        if _est_publicitaire(url_reelle):
            nombre_ecarte_pub += 1
            continue
        titre = _strip_html(text)
        if not titre:
            continue
        extrait = snippets[i] if i < len(snippets) else ""
        # `rang` prend la longueur ACTUELLE de `resultats`, après tous les
        # `continue` ci-dessus (href absent, publicité, titre vide) : la
        # numérotation reste contiguë sans étape de renumérotage séparée.
        resultats.append(ResultatWeb(
            rang=len(resultats) + 1,
            titre=titre,
            url=url_reelle,
            extrait=extrait,
            moteur="ddg-html",
        ))
        if len(resultats) >= _MAX_RESULTS:
            break
    return resultats, None, nombre_ecarte_pub


#: Seuils de validation de `reformuler_requete` — des heuristiques
#: volontairement simples (longueur, absence de saut de ligne, absence
#: d'URL/de bloc de code), PAS une garantie sémantique que le modèle n'a rien
#: inventé. C'est le prompt qui porte l'essentiel de cette garantie ; ces
#: seuils ne font que rejeter les formes qui trahissent une phrase/explication
#: plutôt que des mots-clés.
_REFORMULATION_MAX_LEN = 120


def reformuler_requete(
    question: str, on_etape: Optional[Callable[[dict], None]] = None,
) -> str:
    """Réduit `question` à 2-3 mots-clés de recherche, via le modèle LOCAL.

    Tâche de fond au sens de CLAUDE.md §3.7 : ce n'est pas la réponse au tour
    de chat, donc `modele_local_defaut()` toujours, jamais de paramètre cloud
    — même principe que `core.history._generate_title`, dont cette fonction
    reprend le style (try/except large, repli silencieux).

    Isolation stricte : ne reçoit et n'utilise QUE `question` (le texte brut
    de l'utilisateur). Aucun chunk RAG, aucun contenu de fichier attaché ne
    doit jamais transiter par ici — c'est ce qui part, en clair, vers
    DuckDuckGo.

    Ne lève jamais : un échec (modèle absent, timeout, sortie invalide)
    replie silencieusement sur `question` telle quelle, sans bloquer la
    recherche qui suit.
    """
    if not question or not question.strip():
        return question
    prompt = (
        "Réduis la question suivante à 2 ou 3 mots-clés de recherche web, "
        "rien d'autre. N'utilise QUE des mots présents dans la question "
        "(ou leurs variantes évidentes : singulier/pluriel, conjugaison). "
        "N'ajoute JAMAIS de nom propre, de date, de lieu ou d'entité absente "
        "de la question. Pas de phrase, pas de ponctuation superflue, pas de "
        "guillemets. Réponds UNIQUEMENT les mots-clés, sans explication ni "
        "préambule.\n\n"
        f"Question : {question}"
    )
    try:
        sortie = llm.generate([{"role": "user", "content": prompt}], model=modele_local_defaut())
        candidate = sortie.strip().strip('"').strip("'")
        if (
            not candidate
            or len(candidate) > _REFORMULATION_MAX_LEN
            or "\n" in candidate
            or "http://" in candidate
            or "https://" in candidate
            or "```" in candidate
            or "`" in candidate
        ):
            return question
        _emettre(on_etape, {
            "etape": "requete_reformulee",
            "originale": tronquer_champ(question),
            "reformulee": tronquer_champ(candidate),
        })
        return candidate
    except Exception:
        logger.debug("Reformulation de requête échouée, repli sur la question brute", exc_info=True)
        return question


def rechercher(
    requete: str, on_etape: Optional[Callable[[dict], None]] = None,
) -> list[ResultatWeb]:
    """Recherche web via DuckDuckGo, avec fallback HTML et cache LRU.

    Stratégie : (1) API Instant Answer (JSON) ; (2) si rien d'exploitable,
    fallback sur l'endpoint HTML. Lève `RechercheWebErreur` quand les deux
    stratégies ont échoué au niveau réseau, ou quand `_html_search` détecte
    une structure HTML changée — dans les deux cas, distinct d'une liste
    vide légitime.

    `on_etape`, si fourni, reçoit CHAQUE étape significative du déroulé —
    voir le commentaire au-dessus de `TRACE_MAX_ETAPES`. `None` par défaut :
    coût nul pour les appelants existants.

    IMPÉRATIF pour l'audit de confidentialité — une étape `recherche_debut`
    n'est émise QUE juste avant un VRAI appel réseau. Le chemin « servi par
    le cache » émet `recherche_cache`, jamais `recherche_debut` : prétendre
    qu'une requête est partie alors qu'aucune ne l'a fait serait exactement
    le genre d'information trompeuse que cette trace existe pour éliminer.
    """
    if not requete or not requete.strip():
        return []
    q = requete.strip()

    cached = _cache_get(q)
    if cached is not None:
        logger.info("Web search « %s » : %d résultat(s) servis depuis le cache", q, len(cached))
        _emettre(on_etape, {"etape": "recherche_cache", "requete": tronquer_champ(q)})
        _emettre(on_etape, {
            "etape": "recherche_resultats", "nombre": len(cached), "moteur": "cache",
            "ms": 0, "resultats": _resultats_pour_trace(cached),
        })
        return cached

    _emettre(on_etape, {"etape": "recherche_debut", "requete": tronquer_champ(q), "moteur": "ddg-instant"})
    _t0 = time.time()
    resultats, err_instant = _instant_answer(q)
    if resultats:
        ms = round((time.time() - _t0) * 1000)
        logger.info("Web search « %s » : %d résultat(s) via ddg-instant", q, len(resultats))
        _cache_set(q, resultats)
        _emettre(on_etape, {
            "etape": "recherche_resultats", "nombre": len(resultats), "moteur": "ddg-instant",
            "ms": ms, "resultats": _resultats_pour_trace(resultats),
        })
        return resultats

    _emettre(on_etape, {"etape": "recherche_debut", "requete": tronquer_champ(q), "moteur": "ddg-html"})
    _t0 = time.time()
    try:
        resultats, err_html, nombre_ecarte_pub = _html_search(q)
    except RechercheWebErreur as exc:
        _emettre(on_etape, {"etape": "recherche_erreur", "message": tronquer_champ(str(exc))})
        raise
    ms = round((time.time() - _t0) * 1000)
    if nombre_ecarte_pub:
        _emettre(on_etape, {
            "etape": "recherche_filtree", "nombre_ecarte": nombre_ecarte_pub, "raison": "publicite",
        })
    if resultats:
        logger.info("Web search « %s » : %d résultat(s) via ddg-html", q, len(resultats))
        _cache_set(q, resultats)
        _emettre(on_etape, {
            "etape": "recherche_resultats", "nombre": len(resultats), "moteur": "ddg-html",
            "ms": ms, "resultats": _resultats_pour_trace(resultats),
        })
        return resultats

    if err_instant and err_html:
        logger.error("Web search échoué pour « %s » : instant=%s ; html=%s", q, err_instant, err_html)
        _emettre(on_etape, {"etape": "recherche_erreur", "message": tronquer_champ(err_instant)})
        raise RechercheWebErreur(err_instant)

    logger.info("Web search « %s » : 0 résultat", q)
    _emettre(on_etape, {
        "etape": "recherche_resultats", "nombre": 0, "moteur": "ddg-html", "ms": ms, "resultats": [],
    })
    return []


def _domaine(url: str) -> str:
    """Domaine affichable d'une URL, préfixe ``www.`` retiré.

    ``www.python.org`` et ``python.org`` sont le même signal de crédibilité
    pour un lecteur (ou un modèle) ; le préfixe n'ajoute rien et alourdit
    chaque ligne du contexte.
    """
    netloc = urllib.parse.urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or url


def formater_pour_llm(resultats: list[ResultatWeb]) -> str:
    """Formate une liste de résultats pour l'injecter dans un prompt.

    N'écrit QUE le domaine, jamais l'URL complète — l'URL reste côté serveur,
    portée par les `ResultatWeb` eux-mêmes (`core.citations` la retrouve par
    ce biais pour la validation post-génération). Le domaine donne au modèle
    le signal de crédibilité dont il a besoin pour arbitrer entre sources,
    sans lui fournir une chaîne copiable qu'il recomposerait de travers — une
    URL complète dans le prompt, c'est un modèle qui en réécrit une variante
    légèrement fausse dans sa réponse, indiscernable d'une invention pure.

    Pas de consigne de citation ici : le format est un bloc numéroté brut,
    la consigne d'usage (« cite par [n] ») reste du ressort de l'appelant
    (`modules/chat/router.py:_construire_web_ctx`).
    """
    if not resultats:
        return ""
    lignes: list[str] = []
    for r in resultats:
        lignes.append(f"[{r.rang}] {r.titre} ({_domaine(r.url)}) — {r.extrait}")
    return "\n".join(lignes)
