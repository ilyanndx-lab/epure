"""Transcription d'une page d'encre manuscrite en LaTeX — phase 2 de `encre`.

Feuille de route et décisions dans `docs/module-encre.md`. Ce module réalise le
cœur de la phase 2 : rendre une page de tracés en bitmap, la donner à
`pix2text-mfr` (TrOCR ré-entraîné sur formules, MIT, export ONNX publié), et
rendre le LaTeX brut. **L'encre reste le document ; ce texte est un index**, pas
une sortie — c'est la décision §0 du document, et elle explique tout le reste :
on ne corrige pas, on ne vérifie pas, on ne promet aucune exactitude.

ExpRate mesuré en phase 0 sur 50 expressions manuscrites réelles : **24,0 %**
(IC95 Wilson [14,3 %, 37,4 %]). C'est assez pour un index et beaucoup trop peu
pour une sortie — ne pas construire dessus autre chose que de la recherche.

── Les quatre choses qu'on ne redécouvre pas ────────────────────────────────

**1. Le recadrage au contenu, AVANT l'appel au modèle.** La première mesure de
la phase 0 donnait 0 % d'ExpRate avec 66 % d'hallucinations, et ce n'était pas
le modèle : l'encre n'occupait que 2 à 10 % du canevas exporté (900×320, écriture
petite et centrée), donc une fois redimensionnée à 384×384 par le préprocesseur
elle devenait illisible. Recadrer au bounding box du contenu non-blanc (+10 % de
marge) a fait passer la mesure à 24 %. :meth:`HmerEngine._recadrer` porte ce
geste. ⚠️ Il est **presque neutre** sur une page rendue ici, puisque
:meth:`_bitmap` dimensionne déjà le canevas au bounding box des points — c'est
volontairement une ceinture : le jour où l'entrée vient d'ailleurs (une photo,
un PNG importé), c'est elle qui portera tout le gain. D'où un test qui l'éprouve
sur une image à marges blanches et non de bout en bout, où il ne prouverait rien.

**2. Aucun import lourd au niveau module.** `core/runtime.py` fait
``from core.hmer import HmerEngine`` au niveau module, donc tout ce qui est
importé ici l'est au démarrage d'uvicorn ET dans le job rapide de la CI, qui
n'installe aucune de ces dépendances (cf. l'en-tête de `ci.yml`). Un
``from optimum.onnxruntime import …`` en tête de fichier ne coûterait pas
« quelques secondes » : il ferait échouer à la COLLECTE tout test qui importe
`main` ou `core.runtime`, exactement comme `readability-lxml` l'a déjà fait
(CLAUDE.md §8). Mesuré ici : 16,7 s d'import à chaud, 54,2 s à froid, parce
qu'`optimum.onnxruntime` importe `torch`. La paresse du `_LazyEngine` ne couvre
que la CONSTRUCTION, jamais l'import (CLAUDE.md §3.4) — les deux doivent être
tenues séparément, et c'est ce fichier qui tient la seconde.

**3. Les poids viennent d'un dossier LOCAL, jamais du hub.** `core/runtime.py`
pose ``HF_HUB_OFFLINE=1``/``TRANSFORMERS_OFFLINE=1`` dès que le cache Whisper
existe (`_hf_offline_if_cached`, §3.2) — c'est-à-dire sur ce poste. Un
``from_pretrained("breezedeus/pix2text-mfr")`` y échouerait donc à télécharger,
et échouerait en disant « modèle absent du cache », ce qui n'aiderait personne.
On télécharge nous-mêmes par `urllib` + sha256 dans `resolve_hmer_dir()`, puis on
charge ce dossier — exactement l'idiome de `core/voice.py` et
`core/embedding_install.py`, et exactement ce que `docs/module-encre.md`
annonçait. Vérifié : le chargement depuis un dossier local passe avec les deux
variables d'environnement posées à `1`.

**4. Rien de tout ça ne part dans un paquet distribué.** `optimum-onnx` et
`transformers` sont dans `HORS_PAQUET_PIP` (`tools/faire_paquet.py`), donc
`torch` et sa grappe avec eux : aucun module livré — cœur ou catalogue —
n'importe ce fichier, et `encre` n'est pas livrable (il n'est ni dans
`MODULES_COEUR` ni dans `modules-catalogue/`). Le fichier lui-même part bien
dans le paquet, et c'est sans conséquence **précisément grâce au point 2** :
ses imports de niveau module sont tous de la bibliothèque standard.

── Ce qui n'est PAS ici, volontairement ─────────────────────────────────────

Aucune post-correction par LLM. Un modèle de langue transforme volontiers une
expression juste en expression plausible et fausse ; `docs/module-encre.md` en
fait une phase séparée, affichée comme *suggestion* et jamais substituée. Il n'y
a donc aucun appel LLM sur ce chemin, dans aucune branche — la règle du §3.7
(une tâche de fond tourne en local) n'a rien à arbitrer ici, et la transcription
est de toute façon locale par construction.
"""

import hashlib
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from core.paths import resolve_hmer_dir

logger = logging.getLogger(__name__)


class HmerIndisponible(RuntimeError):
    """Le moteur ne peut pas fonctionner : poids absents, dépendances absentes.

    Distincte de :class:`PageSansEncre` parce que le recours n'est pas le même —
    celle-ci dit « cette instance ne sait pas transcrire », l'autre dit « cette
    page n'a rien à transcrire ». Le routeur en fait un 500 et un 400.
    """


class PageSansEncre(ValueError):
    """La page ne contient aucun point exploitable.

    Refus EXPLICITE plutôt que de rendre une image blanche au modèle : celui-ci
    répondrait quelque chose — il répond toujours — et ce quelque chose serait
    indexé comme le contenu d'une page vide.
    """


#: Dépôt amont des poids. La révision est **ÉPINGLÉE** et non `main`, pour la
#: même raison que le tag de `core/voice.py:_VOICES_BASE` : un modèle republié
#: en amont changerait de contenu sous nos pieds et ferait échouer la
#: vérification sha256 sans que rien ne l'explique. Et ici il y a une raison de
#: plus : la baseline de la phase 0 (24,0 % d'ExpRate) a été mesurée sur CES
#: poids. Une évaluation « contre la baseline gelée » (phase 4) n'a de sens que
#: si la baseline est réellement gelée.
_DEPOT = "breezedeus/pix2text-mfr"
_REVISION = "bea257edb2653f2ae413b084f2ac0e8299d08df0"
_BASE_URL = f"https://huggingface.co/{_DEPOT}/resolve/{_REVISION}/"

#: Les huit fichiers du modèle, avec leur sha256 et leur taille — mesurés le
#: 2026-09-07 par téléchargement réel depuis la révision épinglée ci-dessus, puis
#: revérifiés fichier par fichier contre cette même révision. Pas une empreinte
#: recopiée d'un manifeste amont : c'est auto-suffisant, ça détecte aussi bien
#: une corruption réseau qu'un changement de contenu côté HuggingFace.
#:
#: **Les six petits fichiers comptent autant que les deux gros**, et c'est la
#: leçon déjà écrite dans `core/embedding_install.py` à propos de `vocab.txt` :
#: `tokenizer.json` décide de la correspondance identifiant → symbole. Un
#: tokeniseur d'une autre révision ne lèverait rien du tout ; il produirait du
#: LaTeX faux, silencieusement, et l'index entier deviendrait incohérent.
#:
#: `decoder_model.onnx` SANS `decoder_model_merged.onnx` : le dépôt n'en publie
#: pas, et c'est pour ça que le chargement passe `use_cache=False`. `optimum`
#: émet un avertissement (« Could not find any ONNX files with standard file name
#: decoder_model_merged.onnx ») et charge correctement les deux qui existent —
#: bruit, pas défaut.
_FICHIERS: dict[str, tuple[str, int]] = {
    "config.json": (
        "9f3812441d397c871b9b2a74e8d956b939aec5f4f45745bba9214e968d56449d", 4_556),
    "generation_config.json": (
        "cbea88288d5576a9655ad04e2456768544be22273a1c5ca160e0d16384639b4f", 210),
    "preprocessor_config.json": (
        "36a945a7cc645688b9ef64dabae16979cf5f7c1c448569cc306694edc0598b9b", 450),
    "special_tokens_map.json": (
        "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835", 964),
    "tokenizer.json": (
        "3e2ab757277d22639bec28c9d7972e352d3d1dba223051fa674002dc5ab64df3", 39_161),
    "tokenizer_config.json": (
        "7ffff31747c73b1a462b766abfc128e03f669e5b8452fe6e175b1430a078ac8d", 1_181),
    "encoder_model.onnx": (
        "bd8d5c322792e9ec45793af5569e9748f82a3d728a9e00213dbfc56c1486f37d", 87_496_990),
    "decoder_model.onnx": (
        "fd0f92d7a012f3dae41e1ac79421aea0ea888b5a66cb3f9a004e424f82f3daed", 30_114_937),
}

#: Poids total annoncé avant de lancer le téléchargement. **DÉRIVÉ** des tailles
#: ci-dessus et non écrit à la main — `core/embedding_install.py` a déjà payé
#: l'inverse (« 2000 Mo » annoncés pour 198 Mo réels, parce que la constante
#: n'avait pas suivi le contenu).
TAILLE_ESTIMEE_MO = round(sum(t for _, t in _FICHIERS.values()) / 1e6)

#: Nom du modèle tel qu'il est écrit dans chaque transcription.
MODELE = "pix2text-mfr"

#: Version du RENDU bitmap, à incrémenter dès que :meth:`HmerEngine._bitmap` ou
#: :meth:`HmerEngine._recadrer` changent de comportement.
#:
#: Ce n'est pas de la cosmétique de numéro : ce que `docs/module-encre.md`
#: demande de pouvoir faire, c'est **retranscrire tout l'historique quand le
#: modèle change**, en repérant les pages restées sur l'ancien pipeline. Or le
#: pipeline n'est pas seulement les poids — la même encre rendue autrement donne
#: un autre LaTeX. Une version qui ne porterait que la révision amont laisserait
#: croire à l'identité de deux transcriptions qui n'ont rien en commun.
_VERSION_RENDU = 1

#: Ce qui est écrit dans `page["transcription"]["version"]`. Révision amont
#: tronquée (12 caractères : sans ambiguïté pratique, et lisible dans une
#: interface) plus la version du rendu.
VERSION = f"{_REVISION[:12]}+rendu{_VERSION_RENDU}"

#: Couleurs du rendu. Encre TRÈS sombre sur blanc franc : le préprocesseur
#: normalise autour de 0,5 et le modèle a été entraîné sur des formules à fort
#: contraste. On ne reprend PAS la couleur écrite dans le trait (`trait.couleur`)
#: — elle sert à l'affichage fidèle de la page, pas à donner à lire au modèle une
#: encre pâle qu'un utilisateur aurait choisie.
_ENCRE = 0
_PAPIER = 255

#: Épaisseur de repli quand un trait ne dit pas la sienne, en unités logiques.
#: Même valeur que le `TAILLE_TRAIT` du composant : un trait sans `taille` vient
#: d'un client plus ancien, pas d'un choix.
_TAILLE_TRAIT = 3.5

#: Marge du recadrage au contenu, en fraction de la boîte englobante. Les 10 %
#: sont la valeur de la phase 0, celle sur laquelle les 24 % ont été mesurés.
_MARGE_RECADRAGE = 0.10

#: Seuil de « non blanc » pour le recadrage. 250 et non 255 : un rendu antialiasé
#: laisse des pixels à 251-254 sur les bords, et les compter comme du contenu
#: rendrait le recadrage inopérant sur une image pourtant nette.
_SEUIL_CONTENU = 250

#: Borne du canevas de rendu, en pixels par côté. Une page saine tient
#: largement dessous (l'espace logique du composant fait 1240×1754), mais les
#: coordonnées viennent d'un client et `core/encre.py` ne regarde JAMAIS à
#: l'intérieur d'un trait (c'est sa décision, documentée) : rien n'empêche donc
#: un `x` à 10^9 d'arriver jusqu'ici. Sans borne, `Image.new` tenterait
#: l'allocation et le backend mourrait sur un MemoryError. Au-delà, on met à
#: l'échelle plutôt que de refuser — une page démesurée reste une page.
_COTE_MAX = 4000

#: Bride de génération. `generation_config.json` du modèle dit 512 ; on descend à
#: 256 parce qu'au-delà on n'est plus dans une expression mais dans une boucle de
#: répétition — le mode d'échec déjà observé sur `moondream` (§3.3 bis) et
#: classique des décodeurs autorégressifs sur une entrée qu'ils ne comprennent
#: pas. Tronquer une hallucination coûte moins cher que de l'attendre.
_MAX_TOKENS = 256


def _flottant(valeur) -> Optional[float]:
    """Un nombre fini, ou ``None``. Jamais une exception.

    `bool` est écarté explicitement : c'est un `int` pour Python, et un
    ``{"x": true}`` arrivé d'un client cassé ne doit pas devenir l'abscisse 1.
    """
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
        return None
    valeur = float(valeur)
    return valeur if valeur == valeur and abs(valeur) != float("inf") else None


def points_de_page(page: dict) -> list[list[tuple[float, float, float]]]:
    """Les tracés d'une page ramenés à des listes de ``(x, y, pression)``.

    Fonction de MODULE et non méthode : le routeur s'en sert pour répondre 400
    « aucun trait » sans construire le moteur, c'est-à-dire sans charger 118 Mo
    de poids pour découvrir que la page est vide.

    Tolérante par construction, dans le même esprit que
    `EncreEngine._traits` : un point mal formé est ignoré, jamais fatal. La
    donnée irremplaçable est l'encre ; refuser de transcrire une page parce
    qu'un point sur mille porte un ``null`` serait la punir d'un défaut de
    client. Un trait de moins de deux points est écarté — il ne dessine aucun
    segment, et le rendu d'un point isolé n'apporte rien à un modèle qui lit des
    formules.
    """
    traits: list[list[tuple[float, float, float]]] = []
    for trait in page.get("strokes") or []:
        if not isinstance(trait, dict):
            continue
        taille = _flottant(trait.get("taille")) or _TAILLE_TRAIT
        points: list[tuple[float, float, float]] = []
        for point in trait.get("points") or []:
            if not isinstance(point, dict):
                continue
            x, y = _flottant(point.get("x")), _flottant(point.get("y"))
            if x is None or y is None:
                continue
            pression = _flottant(point.get("pression"))
            if pression is None or not 0.0 <= pression <= 1.0:
                pression = 0.5
            points.append((x, y, taille * (0.5 + pression)))
        if len(points) >= 2:
            traits.append(points)
    return traits


def poids_manquants(dossier: Optional[Path] = None) -> list[str]:
    """Les fichiers de modèle absents du cache. Liste vide = tout est là.

    Ne vérifie que la PRÉSENCE, pas les empreintes : le sha256 est contrôlé au
    téléchargement, avant le renommage atomique, donc un fichier présent est un
    fichier qui a été vérifié. Relire 118 Mo à chaque construction pour
    reconfirmer ce qu'on sait déjà coûterait une seconde par transcription.
    """
    racine = Path(dossier) if dossier else resolve_hmer_dir()
    return [nom for nom in _FICHIERS if not (racine / nom).is_file()]


def _autorise_telechargement() -> bool:
    """`EPURE_HMER_AUTOINSTALL=0` coupe tout téléchargement.

    Jumelle d'`EPURE_EMBEDDING_AUTOINSTALL`, et posée à `0` par `_test_env` pour
    toute la suite : aucun test ne doit tirer 118 Mo, ni sur ce poste ni sur le
    runner de la CI.
    """
    return os.environ.get("EPURE_HMER_AUTOINSTALL", "1").strip() not in ("0", "false", "no")


class HmerEngine:
    """Transcription d'une page d'encre. Aucun LLM, aucun réseau après les poids.

    Derrière un `_LazyEngine` dans `core/runtime.py` — et là, contrairement à
    `encre_engine`, la paresse achète quelque chose de réel : la construction
    charge deux sessions ONNX et, avant elles, `optimum`/`torch` (16,7 s à chaud,
    54,2 s à froid, mesuré). La faire à l'import d'uvicorn rendrait le démarrage
    inutilisable pour une capacité qu'on n'emploie qu'en cliquant un bouton.

    Le dossier est résolu DANS ``__init__`` (CLAUDE.md §3.5) : le figer dans un
    défaut d'argument rendrait `$EPURE_HMER_DIR` sans effet, donc la suite
    écrirait 118 Mo dans le vrai cache.
    """

    def __init__(self, dossier: Optional[Path] = None):
        self._dir = Path(dossier).resolve() if dossier else resolve_hmer_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._assurer_poids()
        self._processeur, self._modele = self._charger()
        # Une transcription à la fois. `POST /encre/pages/{id}/transcrire` part
        # dans `run_in_executor`, donc deux clics rapides tombent sur deux
        # threads du pool ; `generate()` d'optimum n'annonce nulle part être
        # réentrant, et le coût d'un verrou est nul comparé à celui de l'appel.
        self._verrou = threading.Lock()

    # ── Poids ─────────────────────────────────────────────────────────────────

    def _assurer_poids(self) -> None:
        """Récupère les fichiers absents, ou refuse en le disant."""
        manquants = poids_manquants(self._dir)
        if not manquants:
            return
        if not _autorise_telechargement():
            raise HmerIndisponible(
                f"Poids de transcription absents ({', '.join(manquants)}) et "
                "téléchargement coupé par EPURE_HMER_AUTOINSTALL=0."
            )
        logger.info(
            "Téléchargement des poids de transcription manuscrite : %d fichier(s), "
            "~%d Mo (%s @ %s)", len(manquants), TAILLE_ESTIMEE_MO, _DEPOT, _REVISION[:12],
        )
        for nom in manquants:
            self._telecharger(nom)

    def _telecharger(self, nom: str) -> None:
        """Télécharge vers un ``.part``, vérifie le sha256, **puis** renomme.

        L'ordre est celui de `core/voice.py:_telecharger`, et pour la raison qui
        y est écrite : écrire directement sur la cible laisserait, sur coupure,
        un fichier tronqué qui *existe*. :func:`poids_manquants` ne teste que la
        présence, donc la construction suivante le croirait valide et
        `onnxruntime` planterait au chargement sans jamais retenter — une panne
        définitive née d'une coupure passagère. Le renommage étant atomique, le
        fichier n'existe que complet et vérifié.
        """
        sha_attendu, taille = _FICHIERS[nom]
        cible = self._dir / nom
        temporaire = cible.with_name(cible.name + ".part")
        temporaire.unlink(missing_ok=True)      # reste d'une tentative interrompue
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(_BASE_URL + nom, timeout=60) as reponse:
                recu = palier = 0
                with open(temporaire, "wb") as sortie:
                    while True:
                        bloc = reponse.read(1 << 20)
                        if not bloc:
                            break
                        sortie.write(bloc)
                        digest.update(bloc)
                        recu += len(bloc)
                        # Un point tous les 25 % sur les gros fichiers seulement :
                        # les six petits sont instantanés et n'ont rien à dire.
                        if taille > 1_000_000 and recu * 4 // taille > palier:
                            palier = recu * 4 // taille
                            logger.info("  %s : %d %% (%.1f/%.1f Mo)",
                                        nom, palier * 25, recu / 1e6, taille / 1e6)
        except OSError as exc:
            # URLError et HTTPError dérivent d'OSError, comme les erreurs disque :
            # un seul filet, et le message dit lequel c'était.
            temporaire.unlink(missing_ok=True)
            raise HmerIndisponible(
                f"Téléchargement de {nom} impossible ({_BASE_URL + nom}) : {exc}"
            ) from exc

        obtenu = digest.hexdigest()
        if obtenu != sha_attendu:
            temporaire.unlink(missing_ok=True)
            raise HmerIndisponible(
                f"Empreinte incorrecte pour {nom} : attendu {sha_attendu}, obtenu "
                f"{obtenu}. Le fichier a été supprimé."
            )
        os.replace(temporaire, cible)
        logger.info("Poids récupéré : %s (%.1f Mo)", nom, cible.stat().st_size / 1e6)

    # ── Chargement du modèle ──────────────────────────────────────────────────

    def _charger(self):
        """Construit le processeur et le modèle ONNX. Imports **ici**, jamais en tête.

        `provider="CPUExecutionProvider"` et `use_io_binding=False` : la phase 0 a
        validé cette configuration sur le Yoga, qui n'a pas de GPU exploitable.
        `use_cache=False` parce que le dépôt ne publie pas de
        `decoder_model_merged.onnx` — ce n'est pas un réglage de performance mais
        la description de ce qui existe en amont.
        """
        try:
            from optimum.onnxruntime import ORTModelForVision2Seq  # noqa: PLC0415
            from transformers import AutoProcessor                 # noqa: PLC0415
        except ImportError as exc:
            raise HmerIndisponible(
                "Pile de transcription manuscrite absente (optimum-onnx, "
                "transformers) — `pip install -r backend/requirements.txt`. "
                "Elle est volontairement retirée des paquets distribués "
                "(HORS_PAQUET_PIP) : aucun module livré ne l'utilise."
            ) from exc

        depart = time.monotonic()
        try:
            processeur = AutoProcessor.from_pretrained(str(self._dir))
            modele = ORTModelForVision2Seq.from_pretrained(
                str(self._dir), use_cache=False, use_io_binding=False,
                provider="CPUExecutionProvider",
            )
        except Exception as exc:
            raise HmerIndisponible(
                f"Chargement du modèle de transcription impossible depuis "
                f"{self._dir} : {exc}"
            ) from exc
        logger.info("Modèle de transcription chargé en %.1f s (%s %s)",
                    time.monotonic() - depart, MODELE, VERSION)
        return processeur, modele

    # ── Rendu bitmap ──────────────────────────────────────────────────────────

    @staticmethod
    def _bitmap(page: dict):
        """Rend les tracés d'une page en niveaux de gris, encre sombre sur blanc.

        **Pas un portage de `perfect-freehand`.** Le frontend s'en sert pour un
        rendu fidèle à l'œil ; ici on donne à lire à un modèle qui redimensionne
        tout en 384×384 avant de regarder quoi que ce soit. Des segments dont la
        largeur suit la pression suffisent, et un portage de l'algorithme de
        contour serait hors de proportion pour ce que le préprocesseur en
        conserverait.

        **Le canevas est dimensionné sur le bounding box RÉEL des points**, pas
        sur la taille logique de la page côté frontend (1240×1754 aujourd'hui).
        Deux raisons : cette taille peut changer, et surtout une page A4 dont
        trois centimètres portent une formule donnerait au modèle une image
        vide à 98 %, c'est-à-dire le défaut de la phase 0 reproduit à la source.

        La marge ajoutée vaut la demi-largeur du trait le plus épais : sans elle,
        un trait passant exactement sur le bord serait tronqué dans sa moitié.
        """
        from PIL import Image, ImageDraw   # noqa: PLC0415 — cf. l'en-tête du module

        traits = points_de_page(page)
        if not traits:
            raise PageSansEncre("Cette page ne contient aucun tracé exploitable.")

        xs = [p[0] for t in traits for p in t]
        ys = [p[1] for t in traits for p in t]
        epaisseur_max = max(p[2] for t in traits for p in t)
        marge = max(1.0, epaisseur_max / 2.0 + 1.0)

        largeur = max(xs) - min(xs) + 2 * marge
        hauteur = max(ys) - min(ys) + 2 * marge
        # Mise à l'échelle plutôt que refus : cf. `_COTE_MAX`. `largeur`/`hauteur`
        # valent au moins `2 * marge`, donc jamais 0 — la division est sûre.
        echelle = min(1.0, _COTE_MAX / max(largeur, hauteur))
        decalage_x, decalage_y = marge - min(xs), marge - min(ys)

        image = Image.new("L", (max(1, round(largeur * echelle)),
                                max(1, round(hauteur * echelle))), _PAPIER)
        crayon = ImageDraw.Draw(image)
        for points in traits:
            precedent = None
            for x, y, epaisseur in points:
                courant = ((x + decalage_x) * echelle, (y + decalage_y) * echelle)
                rayon = max(0.5, epaisseur * echelle / 2.0)
                if precedent is not None:
                    crayon.line([precedent, courant], fill=_ENCRE,
                                width=max(1, round(rayon * 2)))
                # Un disque à chaque point : `line` a des extrémités carrées, donc
                # deux segments d'épaisseurs différentes laissent une encoche à
                # leur jonction. Le disque la comble, et c'est aussi ce qui rend
                # la variation de pression continue au lieu de crénelée.
                crayon.ellipse([courant[0] - rayon, courant[1] - rayon,
                                courant[0] + rayon, courant[1] + rayon], fill=_ENCRE)
                precedent = courant
        return image

    @staticmethod
    def _recadrer(image, marge: float = _MARGE_RECADRAGE):
        """Recadre au bounding box du contenu non-blanc, avec ``marge`` autour.

        Le geste de la phase 0, sans lequel l'ExpRate mesuré était **0 %** au
        lieu de 24 % : une encre occupant 2 à 10 % du canevas devient illisible
        une fois l'image écrasée en 384×384.

        La marge est proportionnelle à la boîte (10 %), pas absolue : une formule
        de trois symboles et une ligne entière ne demandent pas le même
        dégagement, et une marge fixe serait énorme sur l'une et nulle sur
        l'autre. Le résultat est **clippé aux bords** de l'image d'origine — un
        `crop` hors cadre remplit de noir chez Pillow, c'est-à-dire d'encre.

        Une image entièrement blanche est rendue telle quelle : il n'y a pas de
        boîte, et la seule alternative serait de lever, ce qui punirait
        l'appelant d'un cas que :meth:`_bitmap` ne peut de toute façon pas
        produire (il refuse avant, cf. :class:`PageSansEncre`).

        ⚠️ Écrit en `point()` + `getbbox()` et non `ImageChops.invert` : ce qu'on
        cherche est « strictement plus sombre que blanc-cassé » (`_SEUIL_CONTENU`),
        pas « non nul ». Sur un rendu antialiasé, les deux ne donnent pas la même
        boîte.
        """
        gris = image.convert("L")
        boite = gris.point(lambda v: 255 if v < _SEUIL_CONTENU else 0).getbbox()
        if boite is None:
            return image
        x0, y0, x1, y1 = boite
        dx = int((x1 - x0) * marge)
        dy = int((y1 - y0) * marge)
        return image.crop((max(0, x0 - dx), max(0, y0 - dy),
                           min(image.width, x1 + dx), min(image.height, y1 + dy)))

    # ── API publique ──────────────────────────────────────────────────────────

    def transcrire(self, page: dict) -> dict:
        """Transcrit une page. Rend ``{"texte", "modele", "version"}``.

        ``modele`` et ``version`` accompagnent le texte partout où il est écrit
        (`EncreEngine.set_transcription`) parce que `docs/module-encre.md` en
        fait la condition d'une **retranscription en masse** le jour où le
        pipeline change : sans eux, rien ne distingue une page transcrite hier
        d'une page transcrite par un modèle qu'on vient de remplacer.

        Lève :class:`PageSansEncre` sur une page vide — jamais une image blanche
        envoyée au modèle. Le texte rendu peut en revanche être VIDE sans que ce
        soit une erreur : c'est ce que le modèle a lu, et l'appelant l'écrit tel
        quel plutôt que d'inventer.
        """
        image = self._recadrer(self._bitmap(page))
        depart = time.monotonic()
        with self._verrou:
            # `.convert("RGB")` À L'ENTRÉE DU MODÈLE, et pas plus tôt. Mesuré, pas
            # supposé : `DeiTImageProcessor` (celui de `preprocessor_config.json`)
            # appelle `infer_channel_dimension_format`, qui lève
            # `ValueError: Unsupported number of image dimensions: 2` sur une
            # image en niveaux de gris — il exige trois canaux. Le rendu, lui,
            # reste en `L` : c'est un canal au lieu de trois pour la même encre
            # noire, et surtout le seuil de :meth:`_recadrer` raisonne en
            # luminance. La conversion vit donc ici, à la frontière du modèle,
            # là où l'exigence est.
            entrees = self._processeur(images=image.convert("RGB"),
                                       return_tensors="pt").pixel_values
            sortie = self._modele.generate(entrees, max_new_tokens=_MAX_TOKENS)
            texte = self._processeur.batch_decode(sortie, skip_special_tokens=True)[0]
        logger.info("Transcription en %.2f s (image %dx%d) : %d caractères",
                    time.monotonic() - depart, image.width, image.height, len(texte))
        return {"texte": texte.strip(), "modele": MODELE, "version": VERSION}
