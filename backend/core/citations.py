"""Validation des citations d'une réponse LLM, après génération.

POURQUOI ce module existe, séparé de core/websearch.py et du chat : donner au
modèle des sources réelles à citer (core/websearch.py, formater_pour_llm) est
un contrat en ENTRÉE — un prompt, aussi explicite soit-il, ne garantit rien
en sortie. Un [7] hors de la liste fournie, une URL qui ressemble à une vraie
mais n'a jamais été donnée : rien dans le prompt n'empêche ça. Ce module est
le second verrou, en SORTIE : il vérifie ce que le premier n'a fait que
demander.

Générique par construction — aucune notion de chat, de RAG, de recherche web
ni de filière (cf. `test_coeur_generique.py`) : il prend un texte et un
ensemble de références légitimes, il rend un rapport. C'est l'appelant
(`modules/chat/router.py`) qui sait d'où viennent les rangs et les URLs
légitimes, et qui assemble `ReferenceCitations` en conséquence.

IMPÉRATIF — ne jamais corriger silencieusement : `valider_citations` ne
modifie ni ne supprime jamais rien dans `reponse`. Une suppression invisible
d'URL recrée exactement la classe de bug que ce module existe pour éliminer
(une source qui disparaît sans que personne ne le sache est aussi grave
qu'une source inventée qui apparaît). Signaler, jamais réécrire.
"""

import re
from dataclasses import dataclass, field
from typing import Iterable

# ── Zones qui ne sont PAS des citations, malgré la syntaxe ───────────────────
#
# Épure sert des maths et du code : `arr[0]` dans un bloc ```python```, `x[1]`
# entre backticks inline, ou un indice `v[1]` dans du LaTeX ($...$) ont
# exactement la syntaxe d'une citation [n] sans en être une. Un faux positif
# ici détruit la confiance dans le signal plus vite qu'un faux négatif — ces
# zones sont donc retirées AVANT toute détection, qu'il s'agisse d'un [n] ou
# d'une URL (un exemple de code montrant une URL d'illustration ne doit pas
# être confondu avec une source de la réponse).
_BLOC_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_CODE_INLINE_RE = re.compile(r"`[^`]*`")
# $$...$$ AVANT $...$ : sinon le motif inline apparie les deux `$` d'ouverture
# d'un bloc $$...$$ entre eux et laisse son contenu réel intact.
_MATH_DISPLAY_DOLLAR_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_MATH_DISPLAY_CROCHET_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)
_MATH_INLINE_RE = re.compile(r"\$[^$\n]*\$")


def _masquer_zones_non_citables(texte: str) -> str:
    """Retire code et environnements mathématiques d'un texte.

    Remplacés par un espace (pas supprimés) : ça évite de recoller par
    accident deux mots qui encadraient la zone retirée en un troisième mot
    qui, lui, ressemblerait à une citation.
    """
    texte = _BLOC_CODE_RE.sub(" ", texte)
    texte = _CODE_INLINE_RE.sub(" ", texte)
    texte = _MATH_DISPLAY_DOLLAR_RE.sub(" ", texte)
    texte = _MATH_DISPLAY_CROCHET_RE.sub(" ", texte)
    texte = _MATH_INLINE_RE.sub(" ", texte)
    return texte


_CITATION_RE = re.compile(r"\[(\d+)\]")
_URL_RE = re.compile(r"https?://[^\s)>\]}\"'«»]+")
# Ponctuation de fin de phrase qu'un match d'URL peut avaler par accident
# (« Voir https://python.org. » — le point final n'est pas dans l'URL).
_PONCTUATION_FINALE = ".,;:!?)\"'”»"


def extraire_urls(texte: str) -> set[str]:
    """URLs http(s) présentes dans un texte brut, ponctuation de fin retirée.

    Exposé publiquement : sert à construire l'ensemble de référence (URLs du
    message utilisateur, cf. `construire_reference`) et, en interne, à
    détecter les URLs écrites en dur dans une réponse.
    """
    return {m.rstrip(_PONCTUATION_FINALE) for m in _URL_RE.findall(texte)}


def extraire_rangs_cites(reponse: str, rangs_valides: Iterable[int]) -> list[int]:
    """Rangs [n] VALIDES réellement cités dans `reponse`.

    Dans l'ordre de leur première apparition, sans doublon — c'est cet ordre
    que reflète le bloc Sources (`modules/chat/router.py:_construire_bloc_sources`) :
    il ne doit lister que ce sur quoi la réponse s'appuie, pas ce qui a été
    récupéré sans être utilisé (cf. tâche, §4).
    """
    valides = set(rangs_valides)
    masque = _masquer_zones_non_citables(reponse)
    vus: list[int] = []
    for m in _CITATION_RE.finditer(masque):
        n = int(m.group(1))
        if n in valides and n not in vus:
            vus.append(n)
    return vus


@dataclass(frozen=True)
class ReferenceCitations:
    """Ce à quoi une réponse a le droit de faire référence, pour un tour.

    L'appelant a déjà filtré ce qui ne devrait pas s'y trouver (publicité
    écartée du côté recherche web, par exemple) — cet objet ne fait que
    porter le résultat de ce filtrage, il n'en refait pas le travail.
    """
    rangs_valides: frozenset[int] = frozenset()
    urls_valides: frozenset[str] = frozenset()


def construire_reference(
    urls_web: Iterable[str] = (),
    rangs_web: Iterable[int] = (),
    urls_rag: Iterable[str] = (),
    texte_utilisateur: str = "",
) -> ReferenceCitations:
    """Assemble l'ensemble de référence à partir de ses trois provenances.

    Une URL présente dans le message de l'utilisateur (collée à la main, ou
    tirée d'un PDF attaché dont le texte a été repris) n'est pas une
    invention si la réponse la reprend telle quelle — c'est le faux positif
    à éviter absolument (cf. tâche).
    """
    urls = set(urls_web) | set(urls_rag) | extraire_urls(texte_utilisateur)
    return ReferenceCitations(rangs_valides=frozenset(rangs_web), urls_valides=frozenset(urls))


@dataclass
class RapportCitations:
    """Anomalies trouvées dans une réponse, au regard d'une `ReferenceCitations`.

    `rangs_hors_plage` et `urls_non_reconnues` sont des ANOMALIES, à signaler
    (`a_des_anomalies`). `aucune_citation_malgre_contexte` est un signal
    FAIBLE — la réponse n'avait peut-être simplement pas besoin de citer —
    à logger seulement, jamais à afficher (cf. tâche, §3) : `est_vide` le
    traite donc comme n'exigeant PAS d'événement côté client.
    """
    rangs_hors_plage: list[int] = field(default_factory=list)
    urls_non_reconnues: list[str] = field(default_factory=list)
    rangs_cites: list[int] = field(default_factory=list)
    aucune_citation_malgre_contexte: bool = False

    def a_des_anomalies(self) -> bool:
        """Anomalies à SIGNALER au client (pas le signal faible, log-only)."""
        return bool(self.rangs_hors_plage or self.urls_non_reconnues)

    def est_vide(self) -> bool:
        """Rien à faire apparaître côté client.

        Alias de ``not a_des_anomalies()`` : le signal faible
        (`aucune_citation_malgre_contexte`) compte comme "vide" ICI — il n'a
        droit qu'au log, jamais à l'événement `citation_invalide` (cf. tâche,
        §3 : "signal faible, à logger seulement, pas à afficher").
        """
        return not self.a_des_anomalies()


def valider_citations(reponse: str, reference: ReferenceCitations) -> RapportCitations:
    """Vérifie APRÈS génération ce que le prompt n'a fait que demander.

    Trois vérifications, sur `reponse` débarrassée du code et des maths
    (`_masquer_zones_non_citables`) :
      1. chaque [n] cité doit être dans `reference.rangs_valides` — MAIS
         seulement si `reference.rangs_valides` est non vide (cf. ci-dessous) ;
      2. chaque URL écrite en dur doit être dans `reference.urls_valides` ;
      3. si des rangs étaient offerts (`reference.rangs_valides` non vide) et
         qu'aucun [n] n'apparaît DU TOUT — pas même hors plage — c'est un
         signal faible : la réponse n'a peut-être pas eu besoin du contexte,
         ou l'a ignoré sans le dire.

    Même classe de faux positif que `_masquer_zones_non_citables`, un cran
    plus haut : `[n]` n'est le marqueur d'une citation que dans un tour où une
    liste de sources numérotée a été OFFERTE au modèle (le contrat de
    citation n'est injecté au prompt que dans ce cas,
    `modules/chat/router.py:_construire_web_ctx`). Sans liste offerte
    (`reference.rangs_valides` vide), un `[1]` est une syntaxe de bracket
    ordinaire — note de bas de page, renvoi bibliographique, numérotation de
    prose — au même titre que `arr[0]` ou `v[1]`, et ne doit produire AUCUNE
    anomalie. D'où la garde `if reference.rangs_valides` ci-dessous : quand
    elle est vide, on ne peuple même pas `rangs_hors_plage`, on ne fait que
    détecter la présence d'un [n] pour le signal faible du point 3.
    """
    if not reponse:
        return RapportCitations()

    masque = _masquer_zones_non_citables(reponse)

    rang_trouve = False
    rangs_hors_plage: list[int] = []
    for m in _CITATION_RE.finditer(masque):
        rang_trouve = True
        if not reference.rangs_valides:
            continue
        n = int(m.group(1))
        if n not in reference.rangs_valides and n not in rangs_hors_plage:
            rangs_hors_plage.append(n)

    urls_non_reconnues: list[str] = []
    for url in extraire_urls(masque):
        if url not in reference.urls_valides and url not in urls_non_reconnues:
            urls_non_reconnues.append(url)

    return RapportCitations(
        rangs_hors_plage=rangs_hors_plage,
        urls_non_reconnues=urls_non_reconnues,
        rangs_cites=extraire_rangs_cites(reponse, reference.rangs_valides),
        aucune_citation_malgre_contexte=bool(reference.rangs_valides) and not rang_trouve,
    )
