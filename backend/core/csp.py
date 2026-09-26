"""Content-Security-Policy en OBSERVATION, et journal de ses violations.

Pourquoi (``docs/etude-isolation-modules.md`` §1.2 et §3 c1) : l'interface
n'avait aucune CSP, et un composant généré rendu sur la même origine pouvait
envoyer n'importe quoi à un hôte tiers (``fetch``, WebSocket, image). La CSP
limite cette EXFILTRATION VERS L'EXTÉRIEUR, et rien d'autre : un composant sur
la même origine garde le token et l'accès aux endpoints locaux (l'étude le
dit ; seule une iframe sandboxée couvre ce cas).

**Report-Only, et il doit le rester tant qu'Ilyann n'a pas tranché** (décision
du 2026-09-26) : une semaine d'usage réel d'abord, puis lecture du journal.
Rien n'est bloqué ; le navigateur signale seulement ce qu'une politique
bloquante aurait refusé. Si le journal montre qu'une fonction légitime serait
cassée (Vite en dev, KaTeX, images générées, ComfyUI, Monaco), on le RAPPORTE
— on n'élargit pas la politique à l'aveugle pour faire taire le journal.

Une seule définition des directives, ici. Deux consommateurs :

- le backend, sur ``index.html`` quand il sert l'interface construite (paquet,
  ``tools/dev-epure.ps1``) — le backend y est ``'self'`` ;
- Vite en développement (``frontend/vite.config.ts``, ``server.headers``),
  lancé par le tray — le backend y est une AUTRE origine (:8000), d'où
  :data:`ORIGINES_BACKEND_DEV`. La copie côté Vite est comparée à celle-ci par
  ``test_csp_observation.py``.

Le journal est AGRÉGÉ (directive × origine bloquée × document), borné en
nombre d'entrées, et le point de collecte n'exige pas de token — le navigateur
envoie ses rapports sans en-tête ``Authorization``. N'importe quelle page
locale peut donc y écrire : c'est une donnée affichée, jamais interprétée
(React échappe le texte), et l'agrégat borné empêche de remplir le disque.
"""

import time
from typing import Any
from urllib.parse import urlsplit

from core.jsonstore import read_json, transaction
from core.paths import resolve_data_dir

#: Chemin du point de collecte (exempté d'authentification dans main.py).
CHEMIN_RAPPORT = "/csp/report"
#: Taille maximale d'un corps de rapport accepté (octets).
TAILLE_MAX = 64 * 1024
#: Nombre maximal d'entrées agrégées conservées.
ENTREES_MAX = 200

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


def politique(origines_backend: tuple[str, ...] = (), rapport: str = CHEMIN_RAPPORT) -> str:
    """Valeur de ``Content-Security-Policy-Report-Only``."""
    backend = "".join(f" {o}" for o in origines_backend)
    parties = [d.format(backend=backend) for d in DIRECTIVES]
    # ``report-uri`` SEUL, et pas ``report-to`` : mesuré le 2026-09-26 (Edge,
    # CDP), dès que ``report-to`` est présent Chromium ignore ``report-uri`` et
    # met les rapports en file dans l'API Reporting — ils y restaient
    # « Pending » sans jamais atteindre le backend, et le journal restait vide.
    # ``report-uri`` est déprécié mais livré tout de suite (Chromium, Firefox).
    parties.append(f"report-uri {rapport}")
    return "; ".join(parties)


def entetes(rapport: str = CHEMIN_RAPPORT) -> dict[str, str]:
    """En-têtes posés sur ``index.html`` servi par le backend (même origine)."""
    return {"Content-Security-Policy-Report-Only": politique(rapport=rapport)}


def _journal():
    """Fonction et non constante : cf. core.paths.resolve_data_dir."""
    return resolve_data_dir() / "csp_violations.json"


def _origine(url: Any) -> str:
    """Réduit une URL bloquée à son origine (``inline``, ``eval``… tels quels) :
    le chemin complet porterait des paramètres, parfois sensibles, et
    multiplierait les entrées sans rien apprendre de plus."""
    s = str(url or "")
    parts = urlsplit(s)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
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
            })
    return rapports


def enregistrer(brut: Any) -> int:
    """Agrège les rapports reçus dans le journal. Retourne le nombre retenu."""
    rapports = _normaliser(brut)
    if not rapports:
        return 0
    maintenant = int(time.time())
    with transaction(_journal(), {"entrées": {}}) as doc:
        entrees = doc.setdefault("entrées", {})
        for r in rapports:
            directive = str(r["directive"] or "?")[:60]
            bloque = _origine(r["bloque"])
            document = _origine(r["document"])
            cle = f"{directive}|{bloque}|{document}"
            e = entrees.get(cle)
            if e is None:
                if len(entrees) >= ENTREES_MAX:
                    continue  # borné : les nouvelles sortes au-delà sont ignorées
                e = entrees[cle] = {
                    "directive": directive, "bloqué": bloque, "document": document,
                    "source": _origine(r["source"]), "nombre": 0, "premier": maintenant,
                }
            e["nombre"] += 1
            e["dernier"] = maintenant
    return len(rapports)


def lire() -> list[dict]:
    """Entrées du journal, les plus récentes d'abord."""
    doc = read_json(_journal(), {"entrées": {}})
    entrees = list((doc.get("entrées") or {}).values()) if isinstance(doc, dict) else []
    return sorted(entrees, key=lambda e: e.get("dernier", 0), reverse=True)


def vider() -> None:
    with transaction(_journal(), {"entrées": {}}) as doc:
        doc["entrées"] = {}
