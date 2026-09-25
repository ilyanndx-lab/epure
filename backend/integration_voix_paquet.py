"""La voix du paquet, éprouvée avec les versions du paquet — job CI `paquet-voix`.

POURQUOI CE FICHIER. Le job `backend` de la CI n'installe ni `faster-whisper`
ni `huggingface_hub` : les chemins qui les traversent n'y sont jamais exécutés.
Le venv de dev, lui, les a — mais `transformers` (pile de `core/hmer.py`, hors
paquet) y retient `huggingface_hub` en 0.x, quand le paquet livré embarque la
1.x de `tools/contraintes-paquet.txt`. Mesuré le 2026-09-24 : 0.36.2 dans le
venv, 1.33.0 dans le paquet. Une rupture de l'API 1.x sur ces chemins n'était
donc vue que chez le destinataire.

Ce script se lance dans un environnement installé comme un paquet x64
(`faire_paquet._exigences_du_paquet` + `-c tools/contraintes-paquet.txt`), en
DEUX process, parce que `HF_HUB_OFFLINE` est lu une fois pour toutes à l'import
de `huggingface_hub` (CLAUDE.md §3.2) :

    python integration_voix_paquet.py en-ligne
        Téléchargement (ou revalidation du cache) puis chargement du modèle,
        réseau autorisé : `EPURE_HF_OFFLINE=0` empêche `_hf_offline_if_cached`
        de couper le réseau quand le cache est déjà là.

    python integration_voix_paquet.py hors-ligne
        Nouveau process : `import core.runtime` doit trouver le cache et poser
        `HF_HUB_OFFLINE=1` lui-même, puis `runtime.whisper` doit se construire
        sans réseau — c'est le démarrage d'un destinataire qui a déjà servi la
        dictée une fois.

Le modèle est celui de `config.yaml` (`voice.whisper_model`) : le job y écrit
`tiny` avant de lancer ce script, pour que `_hf_offline_if_cached` cherche le
même dossier de cache que celui qu'on a rempli. Le code parcouru est le même
qu'avec `small`, seule la taille des poids change.

Préfixe `integration_` : exclu de la découverte `test_*.py` du job rapide.
N'importe PAS `_test_env` (qui force `HF_HUB_OFFLINE=1`, ce qui viderait la
première phase de son sens) ; isole donc lui-même les données
(`EPURE_DATA_DIR`), sans quoi `core.runtime` construirait ses moteurs sur les
vraies données d'un poste de dev (cf. `docs/claude/pieges-connus.md`, script
d'intégration qui efface les réglages de la séance).
"""

import os
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(_BACKEND))


def _isoler_donnees() -> None:
    if not os.environ.get("EPURE_DATA_DIR"):
        os.environ["EPURE_DATA_DIR"] = tempfile.mkdtemp(prefix="epure-voix-")


def _taille() -> str:
    import yaml

    cfg = yaml.safe_load((_BACKEND / "config.yaml").read_text(encoding="utf-8")) or {}
    return (cfg.get("voice") or {}).get("whisper_model", "small")


def en_ligne() -> None:
    os.environ["EPURE_HF_OFFLINE"] = "0"
    os.environ.pop("HF_HUB_OFFLINE", None)
    import huggingface_hub
    from huggingface_hub import constants
    from core.voice import WhisperEngine

    print(f"huggingface_hub {huggingface_hub.__version__}, modèle {_taille()}")
    assert not constants.HF_HUB_OFFLINE, "hors ligne dès la phase en ligne"
    moteur = WhisperEngine(model_size=_taille())
    assert moteur._model is not None
    print("en ligne : modèle téléchargé (ou revalidé) et chargé")


def hors_ligne() -> None:
    os.environ.pop("EPURE_HF_OFFLINE", None)
    os.environ.pop("HF_HUB_OFFLINE", None)
    from core import runtime  # pose HF_HUB_OFFLINE=1 si le cache est complet

    assert os.environ.get("HF_HUB_OFFLINE") == "1", (
        "_hf_offline_if_cached n'a pas reconnu le cache "
        f"(models--Systran--faster-whisper-{_taille()} sous HF_HOME={os.environ.get('HF_HOME')})"
    )
    # `from … import` et non `huggingface_hub.constants` : la 1.x charge ses
    # sous-modules paresseusement, l'attribut n'existe pas avant cet import.
    from huggingface_hub import constants

    assert constants.HF_HUB_OFFLINE, "HF_HUB_OFFLINE posé trop tard pour huggingface_hub"
    runtime.whisper._model  # construit le vrai _LazyEngine, sans réseau
    print("hors ligne : cache reconnu, modèle chargé sans réseau")


if __name__ == "__main__":
    _isoler_donnees()
    phases = {"en-ligne": en_ligne, "hors-ligne": hors_ligne}
    if len(sys.argv) != 2 or sys.argv[1] not in phases:
        sys.exit(f"usage : {Path(__file__).name} {{{'|'.join(phases)}}}")
    phases[sys.argv[1]]()
