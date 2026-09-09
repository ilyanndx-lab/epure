#!/usr/bin/env python3
"""Export du dataset d'entraînement du module `encre` (phase 3) — usage MANUEL.

Bundle les exemples collectés (`core/encre_exemples.py`) en paires image+label
portables : un PNG rendu par exemple (le même rendu bitmap que
`core/hmer.py` utilise pour la transcription — bounding box du contenu,
recadrage à 10 % de marge) et un manifeste `.jsonl` qui les référence, une ligne
par exemple.

**Pourquoi un script, pas une route HTTP** (`docs/module-encre.md`, phase 3) :
l'export sert à déplacer le dataset du Yoga (où l'encre est écrite) vers l'Acer
(où l'entraînement tournerait, phase 4, conditionnelle) — un geste manuel et
occasionnel, pas un besoin de l'application en usage normal.

**N'IMPORTE PAS `core.runtime`.** Ce module a des effets de bord assumés (charge
`config.yaml`, instancie tous les moteurs, lance un thread de préchauffage —
CLAUDE.md §3.2) dont ce script n'a besoin d'AUCUN : il ne lui faut que
`core.paths`, `core.encre_exemples` (lecture de fichiers JSON) et les méthodes
STATIQUES de `core.hmer.HmerEngine` (`_bitmap`/`_recadrer`, qui n'appellent
jamais le modèle — importer `HmerEngine` ne construit rien et ne télécharge
rien). Même choix que `smoke_runner.py`/`module_worker.py` (CLAUDE.md §3.2).

Usage :
    python tools/export_dataset_encre.py
    python tools/export_dataset_encre.py --out D:\\dataset --source-dir D:\\encre_dataset

Ce que l'export NE fait PAS : nettoyer un ancien export. Une image dont
l'exemple a été supprimé depuis reste sur le disque de sortie — un export est
un geste manuel et occasionnel, effacer un dossier fourni par l'utilisateur
serait une action destructive que ce script n'a aucune raison de prendre pour
lui. Vider `--out` à la main avant un export si on veut un résultat strictement
à jour.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    # cf. `tools/sonde_onnx.py` : la console Windows en français est en cp1252,
    # qui ne sait pas tout encoder (un accent isolé passe, un symbole hors de sa
    # table lève `UnicodeEncodeError` avant la première ligne utile).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO = Path(__file__).resolve().parent.parent
_BACKEND = _REPO / "backend"
# Inséré AVANT tout `from core...` : ce script peut être lancé depuis n'importe
# quel répertoire de travail, `backend/` n'est sur `sys.path` que si on l'y met.
sys.path.insert(0, str(_BACKEND))

from core import hmer  # noqa: E402 — statique seulement, cf. l'en-tête du fichier
from core.encre_exemples import ExemplesEncreEngine  # noqa: E402
from core.hmer import HmerEngine, PageSansEncre  # noqa: E402
from core.paths import resolve_encre_dataset_dir  # noqa: E402

#: Défaut, hors du dépôt versionné mais à sa racine — pratique pour la copie
#: manuelle annoncée par le docstring. Gitignoré (`.gitignore`).
DEFAUT_SORTIE = _REPO / "dataset_encre_export"


def exporter(source_dir: Path, sortie: Path) -> tuple[int, int]:
    """Écrit les PNG et le manifeste. Rend ``(exportés, ignorés)``.

    Un exemple sans tracé exploitable (ne devrait pas exister — les deux flux de
    création le refusent, cf. `core/encre_exemples.py` — mais un fichier édité à
    la main ou une régression amont ne doit pas arrêter tout l'export pour
    autant) est IGNORÉ avec un message, pas fatal.
    """
    moteur = ExemplesEncreEngine(source_dir)
    exemples = moteur.list_exemples()

    images_dir = sortie / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    manifeste = sortie / "manifest.jsonl"

    exportes = 0
    ignores = 0
    with open(manifeste, "w", encoding="utf-8") as f:
        for exemple in exemples:
            try:
                image = HmerEngine._recadrer(HmerEngine._bitmap(exemple))
            except PageSansEncre:
                print(f"  ignoré (aucun tracé exploitable) : {exemple['id']}")
                ignores += 1
                continue
            nom_image = f"{exemple['id']}.png"
            image.save(images_dir / nom_image)
            f.write(json.dumps({
                "id": exemple["id"],
                "source": exemple.get("source"),
                "date": exemple.get("date"),
                "texte_verite": exemple.get("texte_verite", ""),
                "texte_modele": exemple.get("texte_modele"),
                "image": f"images/{nom_image}",
                # Version du RENDU bitmap (cf. core/hmer.py:_VERSION_RENDU) —
                # écrite pour la même raison que `modele`+`version` dans
                # `EncreEngine.set_transcription` : la même encre rendue
                # autrement donne une image différente, donc un dataset exporté
                # sous un rendu et un sous un autre ne sont mélangeables qu'en
                # le sachant.
                "rendu_version": hmer.VERSION,
            }, ensure_ascii=False) + "\n")
            exportes += 1

    return exportes, ignores


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=DEFAUT_SORTIE,
        help=f"Dossier de sortie (défaut : {DEFAUT_SORTIE})")
    parser.add_argument(
        "--source-dir", type=Path, default=None,
        help="Dossier des exemples (défaut : resolve_encre_dataset_dir())")
    args = parser.parse_args(argv)

    source_dir = args.source_dir.resolve() if args.source_dir else resolve_encre_dataset_dir()
    if not source_dir.is_dir():
        print(f"Aucun dataset trouvé dans {source_dir}.")
        return 1

    sortie = args.out.resolve()
    exportes, ignores = exporter(source_dir, sortie)
    print(f"{exportes} exemple(s) exporté(s) vers {sortie}"
          + (f" ({ignores} ignoré(s))" if ignores else "") + ".")
    print(f"  images/ : {exportes} PNG")
    print("  manifest.jsonl : une ligne par exemple")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
