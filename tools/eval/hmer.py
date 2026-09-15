#!/usr/bin/env python3
"""Banc ExpRate du module HMER (`core/hmer.py`, `pix2text-mfr`) — rejoue la mesure
de la phase 0 de `docs/module-encre.md` sur le pipeline RÉEL, pas une copie.

Le corpus est figé sous `tools/eval/data/hmer_baseline/` : 50 images manuscrites
(`001.png` … `050.png`) et leur vérité terrain (`verite.jsonl`), tels que
récupérés depuis `machinelearn` (cf. l'historique git de
`tools/eval/data/hmer_baseline/`). Le code de mesure — tokenisation LaTeX,
normalisation, intervalle de Wilson — vit sous `tools/eval/hmer_metriques/` et
est une copie STRICTE (renommage + adaptation d'imports uniquement, aucune
règle réordonnée, aucun alias ajouté) de `exprate_reference.py` /
`latex_tokenise_reference.py`, le code exact qui a mesuré 24,0 % d'ExpRate
normalisé (IC95 Wilson [14,3 %, 37,4 %]) en phase 0. Ne PAS reformuler cette
normalisation ici : c'est précisément ce qui rendrait le chiffre obtenu
incomparable à celui de la phase 0 — l'améliorer est un sujet séparé,
volontairement hors de ce script.

Usage :
    python tools/eval/hmer.py
    python tools/eval/hmer.py --corpus D:\\autre_corpus

Ce script tourne dans le venv dédié d'Épure (`.venv/`, cf. CLAUDE.md §2) : il
lui faut `optimum`/`transformers`/`torch`, la même pile que `core/hmer.py` en
production (import différé, jamais au niveau module — cf. son en-tête). Ne
tourne jamais en CI, comme `tools/bench/perf_llm.py` : le job rapide n'installe
aucune de ces dépendances.

────────────────────────────────────────────────────────────────────────────
N'IMPORTE JAMAIS `core.runtime`
────────────────────────────────────────────────────────────────────────────
Même raisonnement que `tools/bench/perf_llm.py` et
`tools/export_dataset_encre.py` (cf. CLAUDE.md §3.2 et §8 — « un script
d'intégration lancé pour voir efface les réglages de la séance en cours ») :
importer `core.runtime` instancierait tous les moteurs (RAG, mémoire,
historique…) pour un besoin qui n'en touche aucun, et réinitialiserait
`backend/memory/context_session.json` au passage. Ce banc n'importe que
`core.hmer.HmerEngine`, qui n'a AUCUN import lourd à son niveau module (cf.
`core/hmer.py`, en-tête, point 2 — `optimum`/`transformers`/`PIL` sont importés
dans ses méthodes) : le seul coût lourd est la CONSTRUCTION du moteur, payée
une fois pour les 50 images, exactement comme en production.

────────────────────────────────────────────────────────────────────────────
Pourquoi `transcrire_image`, pas `transcrire`
────────────────────────────────────────────────────────────────────────────
`HmerEngine.transcrire(page)` part de tracés `(x, y, pression)` et les rend en
bitmap lui-même (`_bitmap`) — c'est le chemin de production, appelé par
`POST /encre/pages/{id}/transcrire`. Le corpus de la phase 0 est fait de PNG
déjà rendus par une app de dessin quelconque : il n'y a pas de tracés à
rendre, seulement une image à recadrer puis donner au modèle. `transcrire_image`
a été extrait de `transcrire` pour exactement ce besoin (voir son docstring
dans `core/hmer.py`) : le recadrage bbox + 10 % de marge et l'appel au modèle
que ce script mesure sont donc CEUX de production, pas une réimplémentation.

────────────────────────────────────────────────────────────────────────────
Ce que ce script NE fait PAS
────────────────────────────────────────────────────────────────────────────
* Aucune modification de `tools/eval/hmer_metriques/` — copie gelée, cf.
  son propre avertissement.
* Aucun ajustement du calcul si le chiffre obtenu est loin de 24,0 % : l'écart
  est écrit dans le JSON de sortie (`comparaison_phase0`) et affiché sur la
  console, jamais masqué.
* Aucune répétition par image — l'inférence de `pix2text-mfr` est un décodage
  glouton (pas d'échantillonnage), donc une répétition ne mesurerait que le
  bruit de l'horloge, pas une variance réelle du modèle.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    # La console Windows (cp1252) ne sait pas tout encoder — même piège que
    # `tools/sonde_onnx.py`/`tools/bench/perf_llm.py`.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ICI = Path(__file__).resolve().parent
_REPO_ROOT = _ICI.parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
_METRIQUES_DIR = _ICI / "hmer_metriques"
_CORPUS_DIR_DEFAUT = _ICI / "data" / "hmer_baseline"
_DOSSIER_RESULTATS = _ICI / "resultats"

# Insérés AVANT tout `from core...`/`from exprate...` : ce script peut être
# lancé depuis n'importe quel répertoire de travail (idiome de
# `tools/bench/perf_llm.py` et `tools/export_dataset_encre.py`).
sys.path.insert(0, str(_BACKEND_DIR))
sys.path.insert(0, str(_METRIQUES_DIR))

from core.hmer import HmerEngine, HmerIndisponible, MODELE, VERSION  # noqa: E402

from exprate import calculer_exprate  # noqa: E402
from latex_tokenise import normaliser, tokens_vers_chaine  # noqa: E402

from PIL import Image  # noqa: E402

#: La baseline de la phase 0 (`docs/module-encre.md`) — pour comparaison
#: explicite, jamais pour ajuster le calcul ci-dessus.
_PHASE0_NORMALISE = 0.240
_PHASE0_IC95 = (0.143, 0.374)


def _sha_git() -> str:
    """Même idiome que `tools/bench/perf_llm.py` — jamais fatal si git manque."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "inconnu"
    except Exception:
        return "inconnu"


def _charger_verite(chemin: Path) -> dict[str, str]:
    """Lit `verite.jsonl` — une ligne JSON par image, jamais un `json.load` du

    fichier entier : c'est un JSONL (une entrée par ligne), pas un document JSON
    unique, donc hors du périmètre de `core/jsonstore.py` (CLAUDE.md §3.4 — ce
    module lit/écrit des documents JSON complets de `backend/memory`/
    `backend/history`, pas des fixtures de test ligne à ligne). `utf-8-sig` par
    précaution, même si ce fichier n'est pas un JSON de runtime : un BOM posé
    par un export Windows ne coûte rien à tolérer.
    """
    verite: dict[str, str] = {}
    with chemin.open("r", encoding="utf-8-sig") as f:
        for numero, ligne in enumerate(f, start=1):
            ligne = ligne.strip()
            if not ligne:
                continue
            entree = json.loads(ligne)
            fichier, latex = entree["fichier"], entree["latex"]
            if fichier in verite:
                raise ValueError(f"{chemin} : {fichier} apparaît deux fois (ligne {numero}).")
            verite[fichier] = latex
    return verite


def _transcrire_corpus(
    moteur: HmerEngine, corpus_dir: Path, verite: dict[str, str],
) -> list[dict]:
    """Transcrit chaque image du corpus avec le pipeline de production.

    Une image manquante ou illisible arrête le script — un corpus qui ne peut
    pas être mesuré en entier ne doit pas produire un chiffre qui a l'air
    complet. Une erreur du MODÈLE sur une image précise (pas la lecture du
    fichier) est en revanche capturée par image : un échec d'inférence isolé
    devient une entrée en échec du rapport, pas un script qui s'arrête après
    N images sur 50 et ne dit jamais pourquoi.
    """
    predictions: list[dict] = []
    for fichier in sorted(verite):
        chemin_image = corpus_dir / fichier
        if not chemin_image.is_file():
            raise FileNotFoundError(f"Image manquante pour {fichier!r} : {chemin_image}")
        with Image.open(chemin_image) as image:
            image.load()
            try:
                resultat = moteur.transcrire_image(image)
                latex_predit: Optional[str] = resultat["texte"]
                erreur = None
            except Exception as exc:  # noqa: BLE001 — isolé par image, cf. docstring
                latex_predit = None
                erreur = f"{type(exc).__name__}: {exc}"
        predictions.append({
            "fichier": fichier,
            "latex_predit": latex_predit,
            "latex_verite": verite[fichier],
            "erreur": erreur,
        })
    return predictions


def _echecs(predictions: list[dict]) -> list[dict]:
    """Items dont la comparaison normalisée échoue, pour inspection manuelle.

    Réutilise `normaliser`/`tokens_vers_chaine` de `tools/eval/hmer_metriques/`
    tels quels — ce n'est PAS une seconde implémentation de la normalisation,
    seulement le détail par item que `calculer_exprate` (la référence,
    inchangée) n'expose pas dans son résultat agrégé.
    """
    echecs = []
    for entree in predictions:
        pred = entree["latex_predit"] or ""
        verite = entree["latex_verite"]
        egal = tokens_vers_chaine(normaliser(pred)) == tokens_vers_chaine(normaliser(verite))
        if not egal:
            echecs.append({
                "fichier": entree["fichier"],
                "latex_predit": entree["latex_predit"],
                "latex_verite": entree["latex_verite"],
                "erreur": entree["erreur"],
            })
    return echecs


def _pourcent(x: float) -> str:
    return f"{x * 100:.1f} %"


def _comparaison_phase0(taux_normalise) -> dict:
    """Compare au 24,0 % [14,3 %, 37,4 %] de la phase 0 — jamais pour corriger,
    seulement pour dire, sans détour, si la mesure d'aujourd'hui s'y range."""
    borne_basse, borne_haute = _PHASE0_IC95
    dans_intervalle = borne_basse <= taux_normalise.taux <= borne_haute
    return {
        "phase0_normalise": _PHASE0_NORMALISE,
        "phase0_ic95": {"borne_basse": borne_basse, "borne_haute": borne_haute},
        "mesure_dans_l_ic95_phase0": dans_intervalle,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--corpus", type=Path, default=_CORPUS_DIR_DEFAUT,
                    help="Dossier du corpus (images + verite.jsonl). "
                         f"Défaut : {_CORPUS_DIR_DEFAUT}")
    args = p.parse_args(argv)

    corpus_dir: Path = args.corpus.resolve()
    verite_path = corpus_dir / "verite.jsonl"
    if not verite_path.is_file():
        print(f"Vérité terrain introuvable : {verite_path}", file=sys.stderr)
        return 1
    verite = _charger_verite(verite_path)
    print(f"Corpus : {corpus_dir} ({len(verite)} images attendues)")

    try:
        moteur = HmerEngine()
    except HmerIndisponible as exc:
        print(f"Moteur HMER indisponible : {exc}", file=sys.stderr)
        return 1

    print(f"Modèle : {MODELE} ({VERSION})")
    predictions = _transcrire_corpus(moteur, corpus_dir, verite)

    resultat_exprate = calculer_exprate(predictions)
    echecs = _echecs(predictions)
    # Garde-fou interne : les deux comptes doivent coïncider, sinon
    # `_echecs` a divergé de la référence qu'il est censé seulement relire.
    assert len(predictions) - len(echecs) == resultat_exprate.normalise.k, (
        "Le compte d'échecs recalculé ne correspond pas à celui de "
        "tools/eval/hmer_metriques/exprate.py — cf. _echecs()."
    )

    comparaison = _comparaison_phase0(resultat_exprate.normalise)

    resultat = {
        "date": datetime.now(timezone.utc).isoformat(),
        "sha_git": _sha_git(),
        "corpus": str(corpus_dir),
        "n": len(predictions),
        "modele": MODELE,
        "version_pipeline": VERSION,
        "brut": vars(resultat_exprate.brut),
        "normalise": vars(resultat_exprate.normalise),
        "journal_normalisation": resultat_exprate.journal_normalisation,
        "fichiers_passes_grace_a_la_normalisation": resultat_exprate.passes_a_juste_grace_normalisation,
        "echecs": echecs,
        "comparaison_phase0": comparaison,
    }

    _DOSSIER_RESULTATS.mkdir(parents=True, exist_ok=True)
    date_jour = resultat["date"][:10]
    chemin_sortie = _DOSSIER_RESULTATS / f"{date_jour}-{resultat['sha_git']}-hmer.json"
    chemin_sortie.write_text(json.dumps(resultat, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"ExpRate brut       : {resultat_exprate.brut.k}/{resultat_exprate.brut.n} "
          f"= {_pourcent(resultat_exprate.brut.taux)} "
          f"[{_pourcent(resultat_exprate.brut.borne_basse)}, "
          f"{_pourcent(resultat_exprate.brut.borne_haute)}]")
    print(f"ExpRate normalisé  : {resultat_exprate.normalise.k}/{resultat_exprate.normalise.n} "
          f"= {_pourcent(resultat_exprate.normalise.taux)} "
          f"[{_pourcent(resultat_exprate.normalise.borne_basse)}, "
          f"{_pourcent(resultat_exprate.normalise.borne_haute)}]")
    print(f"Phase 0 (référence) : {_pourcent(_PHASE0_NORMALISE)} "
          f"[{_pourcent(_PHASE0_IC95[0])}, {_pourcent(_PHASE0_IC95[1])}]")
    if comparaison["mesure_dans_l_ic95_phase0"]:
        print("→ dans l'IC95 de la phase 0 : reproduction cohérente.")
    else:
        print("→ HORS de l'IC95 de la phase 0. Ne pas ajuster le calcul pour "
              "faire coïncider — documenter l'écart et ses hypothèses "
              "(version du modèle, recadrage, corpus altéré, régression du "
              "pipeline...) avant toute conclusion.")
    print(f"\nRésultat écrit : {chemin_sortie}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
