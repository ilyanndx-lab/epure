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
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Mapping

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
    return set(extraire_urls_ordonnees(texte))


def extraire_urls_ordonnees(texte: str) -> list[str]:
    """Mêmes URLs qu'`extraire_urls`, dans leur ordre d'apparition, sans doublon.

    Existe pour la lecture directe d'URL (`core.webcontent.lire_urls`) : les
    pages lues y reçoivent un rang `[n]`, et ce rang doit suivre l'ordre dans
    lequel l'utilisateur les a écrites — un `set` le rendrait arbitraire d'un
    tour à l'autre. Une seule regex pour les deux fonctions : une URL que la
    lecture récupère est exactement celle que la validation des citations
    reconnaîtra ensuite comme fournie par l'utilisateur.
    """
    vues: list[str] = []
    for m in _URL_RE.findall(texte or ""):
        url = m.rstrip(_PONCTUATION_FINALE)
        if url not in vues:
            vues.append(url)
    return vues


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


# ── Fidélité : ce qu'une phrase rattachée à [n] affirme figure-t-il dans [n] ? ──
#
# `valider_citations` ne vérifie que la FORME : un [n] existe, une URL a été
# fournie. Elle ne voit pas une phrase qui attribue à [1] ce que [1] ne dit pas
# (cas réel, #83 : sur « photoélectrique » à 4 000 caractères, un modèle local
# répond « selon les extraits… l'exposant varie entre 4 et 5 » alors que
# l'extrait injecté ne contient pas ces valeurs). `verifier_fidelite` est le
# troisième verrou : un contrôle MATÉRIEL, sans LLM ni réseau, qui compare
# deux sortes d'éléments vérifiables — les nombres et les citations entre
# guillemets — au texte RÉELLEMENT injecté pour la source citée (pas la page
# entière : ce que le modèle a eu sous les yeux). Même contrat que le reste du
# module : générique, pur, et il SIGNALE sans jamais supprimer ni réécrire.

#: Phrases rattachées à la source SANS numéro : le modèle invoque « les
#: extraits », « la page », « le texte »… Formules cherchées sur le texte
#: normalisé (casse et accents retirés), hors guillemets.
_ATTRIBUTION_RE = re.compile(
    r"\b(?:selon|d'apres|dans|sur|par)\s+(?:les|ces|ce|cet|cette|l'|la|le|l)\s*"
    r"(?:extraits?|fragments?|passages?|page|texte|document|sources?)\b"
    r"|\b(?:les|ces|ce|cet|cette|l'|la|le|l)\s*(?:extraits?|fragments?|passages?|page|texte|document)\s+"
    r"(?:fournis?|fournies?|indiqu\w*|precis\w*|mentionn\w*|affirm\w*|stipul\w*|explique\w*|dit|disent|decri\w*|montr\w*)\b"
    r"|\b(?:extraits?|texte|page|passages?|fragments?|document)\s+(?:fournis?|fournies?)\b"
    # Tout nom qui désigne la source, quelle que soit la tournure (#85).
    r"|\b(?:extraits?|textes?|pages?|sources?|documents?)\b"
)

_MARQUEUR_RANG_RE = re.compile(r"\[\s*\d+\s*\]")
#: Un renvoi de note d'une page web copiée : « [ 4 ] », « [4] », « [ N 1 ] »,
#: « [réf 4] », « [ note 3 ] », plages « [4-6] », « [ 4 , 5 ] ». Le crochet ne doit pas être
#: collé à une lettre, un « _ », un « ] » ou un « ) » : `arr[0]`,
#: `matrice[1][2]`, `f(x)[3]` sont des indices de code, pas des notes (le
#: cœur est générique, une page de programmation se lit aussi). Contrepartie :
#: « Hertz[4] », collé, reste.
_RENVOI_NOTE = r"\[\s*(?:[^\W\d]{1,5}\.?\s*)?\d+(?:\s*[-–—,]\s*\d+)*\s*\]"
#: Une SUITE de renvois et leurs séparateurs (« [ 4 ] , [ 5 ] », « [4][5] »,
#: « [4]–[7] », « [ 4 ] à [ 7 ] »), espaces de tête compris. Le garde
#: anti-indice ne porte que sur le premier crochet de la suite.
_RENVOIS_NOTES_RE = re.compile(
    r"\s*(?<![^\W\d])(?<![\])])" + _RENVOI_NOTE + r"(?:\s*(?:[,;–—-]|à)?\s*" + _RENVOI_NOTE + r")*")


def retirer_renvois_de_notes(texte: str) -> str:
    """`texte` privé de ses renvois de notes (#86).

    Deux usages, UNE fonction, pour qu'ils restent alignés : (1) l'extrait
    injecté au modèle (`core.websearch.formater_pour_llm`) — sur
    photoélectrique à 4 000 caractères, le modèle recopiait « Millikan [4] »
    ou « les sections [1] à [10] » comme des numéros de source ; (2) la
    source comparée par `verifier_fidelite` — « [ 4 ] , [ 5 ] » y faisait
    croire que 4 et 5 figuraient dans l'extrait. Un renvoi collé au mot
    suivant laisse une espace, pour ne pas souder deux mots.

    **Limite connue** : un renvoi collé à un mot (« Hertz[4] ») n'est pas
    retiré, pour préserver les indices de code (`arr[0]`, `matrice[1][2]`) —
    seul un crochet précédé d'une espace, d'un chiffre ou d'une ponctuation
    est lu comme un renvoi.
    """
    def _remplacer(m: re.Match) -> str:
        suite = texte[m.end():m.end() + 1]
        return " " if suite and (suite.isalnum() or suite == "_") else ""
    return _RENVOIS_NOTES_RE.sub(_remplacer, texte)

_EXPOSANTS = str.maketrans("⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹", "+-0123456789")
_EXPOSANTS_RE = re.compile(r"[⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+")

_SEP_MILLIERS = "[    ]"
_NOMBRE_RE = re.compile(
    r"(?<![\w.,])([-−])?(\d{1,3}(?:" + _SEP_MILLIERS + r"\d{3})+|\d+)(?:[.,](\d+))?(?!\w)"
)
_MOTS_NOMBRES = {
    "zero": 0, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
    "huit": 8, "dix-sept": 17, "dix-huit": 18, "dix-neuf": 19, "dix": 10, "onze": 11,
    "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "vingt": 20,
}
#: « un », « une » (articles) et « neuf » (adjectif) : jamais lus comme nombres
#: dans la RÉPONSE, trop ambigus ; « neuf » compte côté source, où il ne
#: coûte qu'un faux négatif de plus.
_MOTS_NOMBRES_SOURCE = {**_MOTS_NOMBRES, "neuf": 9}
_MOT_NOMBRE_RE = re.compile(
    r"(?<![\w-])(dix-sept|dix-huit|dix-neuf|zero|deux|trois|quatre|cinq|six|sept|huit|"
    r"dix|onze|douze|treize|quatorze|quinze|seize|vingt)(?![\w-])"
)
_MOT_NOMBRE_SOURCE_RE = re.compile(
    r"(?<![\w-])(dix-sept|dix-huit|dix-neuf|zero|deux|trois|quatre|cinq|six|sept|huit|neuf|"
    r"dix|onze|douze|treize|quatorze|quinze|seize|vingt)(?![\w-])"
)

_DETERMINANT_AVANT_RE = re.compile(r"\b(?:les|ces|ses|mes|tes|nos|vos|leurs|des|aux)\s+$")
#: Une désignation de la source (« extrait 1 », « source n° 2 ») : un numéro d'ordre, pas une valeur.
_REFERENCE_AVANT_RE = re.compile(r"\b(?:extraits?|sources?|passages?|fragments?|resultats?)\s+(?:n°\s*|no\s*)?$")

#: Mots différents tolérés par tranche de cinq mots d'une citation : AUCUN.
#: Une tolérance d'un mot (essayée dans la première version de #85) laissait
#: passer une citation ALTÉRÉE : dans la réponse de référence « relativité 4k »
#: (étage 2 de #75), « dans tous les référentiels inertels (ou inertiels) »
#: remplace « galiléens » par un mot inventé. Un mot changé dans des
#: guillemets est une altération, pas un bruit de recopie. Reste toléré : un
#: écart d'une lettre sur un mot de 6 lettres ou plus (faute de frappe :
#: « inertels » pour « inertiels »), et les passages sautés par une ellipse
#: (chaque fragment est cherché à part).
_MOTS_DIFFERENTS_PAR_CINQ = 0

_ELLIPSE_RE = re.compile(r"\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.")
_FIN_DE_PHRASE_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÖØ-Þ«“\"(\[0-9])")
_PROTEGE = {".": "", "!": "", "?": "", "…": ""}
_RESTAURE = {v: k for k, v in _PROTEGE.items()}
_EXTRAIT_PHRASE_MAX = 300


@dataclass
class AffirmationNonEtayee:
    """Un élément d'une phrase rattachée à des sources qui n'est dans aucune.

    `phrase` : la phrase entière (balisage gras/italique retiré, [n]
    conservés). `element` : le nombre tel qu'écrit, ou la citation (fragment
    fautif). `type` : « nombre » ou « citation ». `sources` : rangs auxquels la
    phrase était rattachée.
    """
    phrase: str
    element: str
    type: str
    sources: list[int]


def _plat(texte: str) -> str:
    """Casse, accents, apostrophes — et exposants ramenés à `^`+ASCII AVANT
    NFKD (qui écrase `⁻⁹` en « −9 » et perd qu'il s'agit d'un exposant)."""
    texte = _EXPOSANTS_RE.sub(lambda m: "^" + m.group(0).translate(_EXPOSANTS), texte)
    texte = unicodedata.normalize("NFKD", texte.replace("’", "'").replace("‘", "'"))
    return "".join(c for c in texte if not unicodedata.combining(c)).casefold()


def _mots(texte_plat: str) -> list[str]:
    return re.findall(r"\w+", texte_plat)


def _a_une_lettre_pres(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 6 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        return a[i + 1:] == b[i + 1:]
    return a[i:] == b[i + 1:]


def _fragment_etaye(fragment: list[str], source: list[str]) -> bool:
    """`fragment` (mots) figure-t-il, dans l'ordre et contigu, dans `source` ?
    Avec la tolérance décrite plus haut (`_MOTS_DIFFERENTS_PAR_CINQ`)."""
    n = len(fragment)
    if n == 0:
        return True
    marge = (n // 5) * _MOTS_DIFFERENTS_PAR_CINQ
    for i in range(len(source) - n + 1):
        ecarts = 0
        for j in range(n):
            if not _a_une_lettre_pres(fragment[j], source[i + j]):
                ecarts += 1
                if ecarts > marge:
                    break
        else:
            return True
    return False


def _valeur(entier: str, decimales: str | None, signe: str | None) -> str:
    chiffres = re.sub(r"\D", "", entier)
    d = Decimal(f"{chiffres}.{decimales}" if decimales else chiffres).normalize()
    texte = format(d, "f")
    return "-" + texte if signe and d != 0 else texte


def _nombres(texte: str, mots_source: bool = False) -> list[tuple[str, str]]:
    """`[(affichage, valeur normalisée)]` : chiffres (virgule ou point
    décimal, espaces de milliers, signe moins ASCII ou Unicode, exposants) puis
    nombres en toutes lettres de zéro à vingt. « 4 et 5 » donne 4 et 5 ;
    « 4 à 5 », « 4–5 » aussi — l'intervalle s'éclate de lui-même."""
    plat = _plat(texte)
    trouves = [(m.group(0).strip(), _valeur(m.group(2), m.group(3), m.group(1)))
               for m in _NOMBRE_RE.finditer(plat)]
    table = _MOTS_NOMBRES_SOURCE if mots_source else _MOTS_NOMBRES
    motif = _MOT_NOMBRE_SOURCE_RE if mots_source else _MOT_NOMBRE_RE
    if not mots_source:
        trouves = [t for t, m in zip(trouves, _NOMBRE_RE.finditer(plat))
                   if not _REFERENCE_AVANT_RE.search(plat[:m.start()])]
    for m in motif.finditer(plat):
        # « les deux autres noms », « ces trois cas » : un quantificateur sur un
        # ensemble déjà posé (souvent par la question), pas une valeur mesurée.
        if not mots_source and _DETERMINANT_AVANT_RE.search(plat[:m.start()]):
            continue
        trouves.append((m.group(1), str(table[m.group(1)])))
    return trouves


def _preparer_source(texte: str) -> tuple[set[str], list[str]]:
    """(valeurs numériques, mots) d'un passage injecté, marqueurs de note retirés."""
    texte = retirer_renvois_de_notes(texte)
    return ({v for _, v in _nombres(texte, mots_source=True)}, _mots(_plat(texte)))


def _separer_guillemets(texte: str) -> tuple[list[str], str]:
    """`(contenus des guillemets de plus haut niveau, texte sans eux)`.

    « … » et “ … ”, imbriqués compris (le contenu d'un niveau garde ses
    guillemets internes, que l'appelant retire). Un guillemet ouvrant jamais
    refermé n'ouvre rien : le reste du texte reste du texte.
    """
    spans: list[str] = []
    dehors: list[str] = []
    profondeur, debut = 0, 0
    for i, c in enumerate(texte):
        if c in "«“":
            if profondeur == 0:
                debut = i
            profondeur += 1
        elif c in "»”" and profondeur > 0:
            profondeur -= 1
            if profondeur == 0:
                spans.append(texte[debut + 1:i])
                dehors.append(" ")
                debut = i + 1
        elif profondeur == 0:
            dehors.append(c)
    if profondeur > 0:
        dehors.append(texte[debut:])
    return spans, "".join(dehors)


def _fragments_de_citation(contenu: str) -> list[str]:
    """Une citation, privée de ses guillemets internes et découpée aux
    ellipses (« … » et « […] » marquent un passage sauté)."""
    contenu = re.sub(r"[«»“”]", " ", contenu)
    return [re.sub(r"\s+", " ", f).strip() for f in _ELLIPSE_RE.split(contenu) if f.strip()]


def _proteger_guillemets(ligne: str) -> str:
    """Remplace la ponctuation de fin de phrase DANS des guillemets par un
    caractère neutre, pour qu'une citation de deux phrases ne soit pas coupée."""
    sortie, profondeur = [], 0
    for c in ligne:
        if c in "«“":
            profondeur += 1
        elif c in "»”" and profondeur > 0:
            profondeur -= 1
        sortie.append(_PROTEGE[c] if profondeur > 0 and c in _PROTEGE else c)
    return "".join(sortie)


def _paragraphes(reponse: str) -> list[list[str]]:
    """Réponse (code et maths déjà masqués) → paragraphes → phrases.

    Un paragraphe se termine à une ligne vide ; chaque ligne perd son
    marqueur de liste, de citation ou de titre (« 1. », « - », « > », « # »),
    sans quoi « 1. » compterait comme un nombre. Une phrase qui ne tient qu'à un
    renvoi « [n] » après le point final est recollée à la précédente.
    """
    resultat: list[list[str]] = []
    for bloc in re.split(r"\n\s*\n", reponse):
        phrases: list[str] = []
        for ligne in bloc.split("\n"):
            ligne = re.sub(r"^\s*(?:>\s*)*(?:[-*•+]\s+|\d+[.)]\s+|#+\s+)?", "", ligne)
            ligne = ligne.replace("*", "").strip()
            if not ligne:
                continue
            for morceau in _FIN_DE_PHRASE_RE.split(_proteger_guillemets(ligne)):
                morceau = "".join(_RESTAURE.get(c, c) for c in morceau).strip()
                if not morceau:
                    continue
                seul_renvoi = _MARQUEUR_RANG_RE.search(morceau) and not _MARQUEUR_RANG_RE.sub("", morceau).strip(" .,;:")
                if phrases and seul_renvoi:
                    phrases[-1] += " " + morceau
                else:
                    phrases.append(morceau)
        if phrases:
            resultat.append(phrases)
    return resultat


def verifier_fidelite(
    reponse: str, sources: Mapping[int, str], texte_utilisateur: str = "",
) -> list[AffirmationNonEtayee]:
    """Nombres et citations d'une réponse absents des sources auxquelles leur
    phrase se rattache. Calcul PUR, sans LLM ni réseau, APRÈS le flux ; ne
    modifie jamais `reponse` (signaler, jamais réécrire).

    `sources` : rang `[n]` → texte EXACT injecté pour cette source (titre et
    extrait, tel que `formater_pour_llm` l'écrit), pas la page entière.

    **Rattachement.** Une phrase est rattachée à ses `[n]` valides. Sans `[n]`,
    elle l'est à l'UNION des sources du tour si elle invoque la source (« selon
    les extraits », « d'après la page », « le texte indique »…) ; cette
    attribution vaut ensuite pour les phrases suivantes du MÊME paragraphe (cas
    réel : « Selon les extraits fournis… la section efficace… Z. L'exposant
    varie entre 4 et 5. » — la seconde phrase n'a ni numéro ni formule),
    jusqu'au premier `[n]` explicite : passé ce renvoi, le modèle désigne de
    nouveau ses sources lui-même, et une phrase sans numéro qui suit peut tenir
    de ses connaissances (risque de faux positif). Invoquer la source, c'est
    la NOMMER hors guillemets — « extrait(s) », « texte(s) », « page(s) »,
    « source(s) », « document(s) » —, quelle que soit la tournure : les
    formules figées ratent « Réponse basée sur l'analyse des extraits » et
    « les lois physiques sous-jacentes évoquées dans ces textes »
    (photoélectrique 4k, tirages 3 et 5 du 2026-10-29). Une phrase sans `[n]`
    et hors d'une attribution n'est pas vérifiée : elle peut venir des
    connaissances du modèle, ce n'est pas à ce contrôle d'en juger. Une phrase
    dont tous les `[n]` sont hors liste est comparée à l'union des sources
    (`valider_citations` signale en plus le rang).

    `texte_utilisateur` : le message de l'utilisateur. Un nombre ou une
    citation de la réponse qui s'y trouve est étayé — le modèle reprend la
    question (« deux autres noms »), il n'invente pas. Ses URLs sont retirées
    avant comparaison.

    **Vérifié.** (1) Les nombres, normalisés : virgule ou point décimal,
    espaces de milliers, signe moins Unicode, exposants (`10⁻⁹`), nombres en
    toutes lettres de zéro à vingt (« un », « une », « neuf » exclus côté
    réponse), intervalles éclatés (« 4 à 5 » → 4 et 5). (2) Les citations entre
    « … » ou “ … ”, comparées mot à mot, dans l'ordre, après normalisation de la
    casse, des accents et des espaces, avec la tolérance de
    `_MOTS_DIFFERENTS_PAR_CINQ`. Le numéro `[n]` lui-même n'est pas un nombre ;
    code et maths sont masqués comme dans `valider_citations`. Un nombre écrit
    entre guillemets est vérifié comme partie de la citation, pas à part.

    **Limites connues, v1.**
      * Les nombres à l'intérieur des formules (`$Z^4$`) ne sont pas vérifiés
        (maths masquées).
      * Présence n'est pas sens : un nombre qui figure dans l'extrait ailleurs
        (« ≤ 3 × 10⁻⁹ s » pour un « autour de 3 ») n'est pas signalé.
      * Une ERREUR D'INTERPRÉTATION n'est pas une invention : sur « relativité
        restreinte » à 4 000 caractères, le modèle prend « classe des référentiels
        inertiels » pour un nom du référentiel galiléen — les mots sont dans
        l'extrait, rien n'est signalé.
      * La page est prise telle qu'injectée : sa bibliographie compte comme
        source, et ses renvois « [ 4 ] » sont ignorés mais pas ses listes
        « 1 2 3 4 ».
      * Seuls les passages portant un rang `[n]` (pages lues, résultats web)
        sont comparés ; les extraits de documents (RAG) ne le sont pas.
      * Une phrase qui NIE est vérifiée comme une affirmation : « l'extrait ne
        donne pas d'autre nom (comme « référentiel propre ») » signale
        « référentiel propre » (relativité 4k, tirage 2 du 2026-10-29).
    """
    if not reponse or not sources:
        return []
    preparees = {rang: _preparer_source(texte) for rang, texte in sources.items()}
    # URLs retirées : une URL désigne la page, elle n'affirme rien (« 2013 »
    # dans un chemin n'étaie pas « 2013 » dans la réponse).
    nombres_user, mots_user = _preparer_source(_URL_RE.sub(" ", texte_utilisateur or ""))
    masque = _masquer_zones_non_citables(reponse)

    signalees: list[AffirmationNonEtayee] = []
    for phrases in _paragraphes(masque):
        attribution_active = False
        for phrase in phrases:
            citations, hors_guillemets = _separer_guillemets(phrase)
            cites = [int(n) for n in re.findall(r"\[(\d+)\]", phrase)]
            valides = sorted({n for n in cites if n in preparees})
            invoque = bool(_ATTRIBUTION_RE.search(_plat(hors_guillemets)))
            # Héritage borné : il s'arrête au premier [n] explicite (même
            # hors plage, même dans la phrase qui invoque la source).
            attribution_active = (attribution_active or invoque) and not cites
            if valides:
                rangs = valides
            elif cites or attribution_active:
                # Numéros tous hors liste (renvois de notes de la page
                # recopiés, photoélectrique 4k) : la phrase invoque des
                # sources sans en désigner une offerte — comparée à toutes.
                # `valider_citations` signale en plus le rang.
                rangs = sorted(preparees)
            else:
                continue

            nombres_sources = set().union(*(preparees[r][0] for r in rangs))
            mots_sources = [m for r in rangs for m in preparees[r][1]]
            vus: set[str] = set()
            trouvees: list[tuple[str, str]] = []
            sans_renvoi = _MARQUEUR_RANG_RE.sub(" ", hors_guillemets)
            for affichage, valeur in _nombres(sans_renvoi):
                if valeur not in vus and valeur not in nombres_sources and valeur not in nombres_user:
                    vus.add(valeur)
                    trouvees.append((affichage, "nombre"))
            for contenu in citations:
                for fragment in _fragments_de_citation(contenu):
                    mots = _mots(_plat(fragment))
                    if not (_fragment_etaye(mots, mots_sources) or _fragment_etaye(mots, mots_user)):
                        trouvees.append((fragment, "citation"))
            for element, genre in trouvees:
                signalees.append(AffirmationNonEtayee(
                    phrase=phrase[:_EXTRAIT_PHRASE_MAX], element=element, type=genre, sources=list(rangs)))
    return signalees
