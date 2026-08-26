"""Mise à disposition du modèle d'embedding — téléchargement, pas installation.

**CE MODULE A CHANGÉ DE NATURE le 2026-08-26**, et l'histoire explique sa forme
actuelle mieux que sa forme ne l'explique elle-même.

Il installait `torch` puis `sentence-transformers` par `pip`, dans un thread,
avec un état exposé en 503 : ~198 Mo de wheels, 843 Mo sur disque, pour utiliser
d'une bibliothèque de 4 Mo **une seule** fonction — `SentenceTransformer.encode`.
Ce qui a forcé la sortie n'est pas le poids mais un blocage dur : sur la machine
ARM64 d'un destinataire, **Smart App Control bloque durablement
`sklearn/utils/_isfinite`**, que `sentence-transformers` importe sans condition
au chargement (`__init__` → `.backend` → `.quantize` → `.util` → `.retrieval` →
`.similarity` → `sklearn.metrics.pairwise_distances`, pour un `cos_sim` que
`core/vector_store.py` n'appelle jamais — il calcule son cosinus en numpy).
`pip install` réussissait, l'import plantait, systématiquement, deux mesures à
huit minutes d'écart.

La pile est donc désormais **`onnxruntime` + `core/wordpiece.py`**, sur le MÊME
modèle (`sentence-transformers/all-MiniLM-L6-v2`) dont le dépôt HuggingFace
publie déjà l'export ONNX. Vecteurs identiques : cosinus 1.0 et écart absolu
maximal 1,9e-07 sur les 40 chunks réels comparés, donc **aucune réindexation**.
Et un seul binaire compilé sur le chemin, `onnxruntime`, dont les trois fichiers
sont signés `CN=Microsoft Corporation` — vérifié sur la machine ARM64 elle-même,
où l'import passe en 0,08 s et où le vecteur produit est identique **bit pour
bit** à celui du poste x64.

**CE QUI RESTE À FAIRE À LA DEMANDE, ET POURQUOI CE MODULE SURVIT.**
`onnxruntime` est maintenant une dépendance **déclarée** de `requirements.txt`
(13,7 Mo de wheel, `win_arm64` publiée) : il part dans le paquet, il n'y a plus
rien à installer par `pip`. Ce qui ne peut pas partir dans le paquet, ce sont les
**90,4 Mo de poids du modèle** — les embarquer ferait grossir l'archive de 90 Mo
pour une capacité que le destinataire n'utilisera peut-être jamais, alors que le
dépôt a déjà tranché ce cas exact pour les 76 Mo du modèle Piper : téléchargement
au premier usage, vérifié par sha256 (`core/voice.py`). C'est le même choix, la
même mécanique, et c'est tout ce que fait ce module aujourd'hui.

Le contrat HTTP est **inchangé** — `GET /rag/capabilities`, `POST /rag/install`,
503 porteur d'un état, et les mêmes clés dans le verdict. Le frontend
(`src/recherche.ts`) n'a rien à réapprendre : ce qui a changé est ce qui se
télécharge, pas la façon de l'annoncer.

Ce qui est délibéré ici, et pourquoi :

- **Deux causes d'indisponibilité, pas une.** `onnxruntime` absent n'est PAS un
  téléchargement en attente : c'est une dépendance déclarée qui manque, donc une
  installation abîmée, et ce module ne tente **pas** de la réparer par `pip` —
  il le dit. Confondre les deux ramènerait exactement ce qu'on vient de retirer :
  un `pip install` déclenché par une requête HTTP.
- **`.part` puis renommage atomique**, comme `core/voice.py`. Écrire directement
  sur la cible laisserait, sur coupure, un fichier tronqué qui *existe* : le
  démarrage suivant le croirait valide et `onnxruntime` planterait au chargement
  sans jamais retenter — panne définitive née d'une coupure passagère.
- **sha256 des DEUX fichiers**, pas seulement du gros. `vocab.txt` fait 231 ko et
  décide de la tokenisation : un vocabulaire d'une autre révision ne lèverait
  rien, il produirait des vecteurs faux, silencieusement, et l'index entier
  deviendrait incohérent.
- **Une seule tentative automatique par process**, relançable explicitement.
  L'ouverture du panneau fichiers déclenche `GET /rag/files` et
  `GET /rag/capabilities` presque en même temps ; sans garde, chacun lancerait son
  téléchargement.
- **Le fichier d'état ne porte que des verdicts TERMINAUX** (`prêt`/`échec`).
  Un process tué au milieu d'un téléchargement laisserait sinon un « en cours »
  éternel, indistinguable d'un téléchargement vivant.
- **« Pas de réseau » et « le téléchargement a échoué » restent deux verdicts
  distincts**, mesurés par une sonde HEAD avant de commencer : un code HTTP,
  même 403 ou 405, prouve qu'un serveur a répondu ; seule une erreur de
  transport vaut « hors ligne ».
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from core.jsonstore import read_json, write_json
from core.paths import resolve_data_dir, resolve_embedding_dir

logger = logging.getLogger(__name__)

#: Révision **épinglée** du dépôt HuggingFace, jamais `main`. Les empreintes
#: ci-dessous n'ont de sens que contre un commit précis, et `main` peut bouger :
#: un `resolve/main` qui servirait un autre fichier ferait échouer le sha256 sans
#: que rien n'explique pourquoi. Vérifiée bout en bout (téléchargement réel).
REVISION_MODELE = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"

_BASE = ("https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/"
         + REVISION_MODELE)

#: Ce qu'il faut avoir sur le disque : nom local → (chemin distant, sha256,
#: taille attendue en octets). Les tailles servent le message d'interface et le
#: contrôle grossier ; le sha256 fait foi.
#:
#: `onnx/model.onnx` est l'export **fp32**, celui qui reproduit les vecteurs
#: existants. Le dépôt publie aussi des variantes int8 (23 Mo, dont un
#: `model_qint8_arm64.onnx` tentant) : mesurées à un cosinus de 0,988 à 0,994
#: contre la référence, donc elles imposeraient de réindexer. Écartées pour ça,
#: pas par méfiance.
#:
#: `vocab.txt` et non `tokenizer.json` : les deux portent le même vocabulaire —
#: vérifié entrée par entrée sur les 30 522 — pour 231 ko contre 466 ko.
FICHIERS_MODELE: dict[str, tuple[str, str, int]] = {
    "model.onnx": (
        "onnx/model.onnx",
        "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
        90405214,
    ),
    "vocab.txt": (
        "vocab.txt",
        "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
        231508,
    ),
}

#: Poids annoncé AVANT de lancer le téléchargement, comme le fait `core/voice.py`
#: pour les 76 Mo de Piper. **Somme réelle des deux fichiers**, arrondie — et non
#: plus les « 2000 » de l'époque torch, qui annonçaient 2 Go pour 198 Mo de
#: wheels : une surestimation d'un facteur 10 dans une phrase d'interface.
TAILLE_ESTIMEE_MO = round(sum(t for _, _, t in FICHIERS_MODELE.values()) / 1e6)

#: Le seul module compilé de la pile. Déclaré en dépendance DIRECTE dans
#: `requirements.txt` — pas transitif. Le distinguo n'est pas cosmétique :
#: `onnxruntime` n'arrivait jusqu'ici que par `faster-whisper` et `piper-tts`,
#: tous deux retirés des paquets ARM64 (`HORS_PAQUET_PIP_ARM64`), donc la pile
#: d'embedding aurait dépendu d'un paquet vocal absent sur l'architecture même
#: qui a motivé ce chantier. C'est mot pour mot l'incident `websockets` /
#: `uvicorn[standard]` (CLAUDE.md §8), et `test_dependances_declarees.py` le
#: verrouille.
MODULE_RUNTIME = "onnxruntime"

#: États possibles. `absent` = jamais tenté dans ce process et rien de mémorisé.
ABSENT = "absent"
EN_COURS = "en_cours"
PRET = "prêt"
ECHEC = "échec"

#: Causes d'échec, pour que l'interface ne dise pas « erreur » à des problèmes
#: qui ne se règlent pas de la même façon.
CAUSE_RESEAU = "réseau"
CAUSE_TELECHARGEMENT = "téléchargement"
CAUSE_RUNTIME_ABSENT = "runtime_absent"
CAUSE_DESACTIVE = "désactivé"

#: Délai de la sonde réseau. Court volontairement : elle ne sert qu'à distinguer
#: « hors ligne » de « le téléchargement a raté », et une machine hors ligne doit
#: le savoir en secondes.
_TIMEOUT_SONDE = 10

#: Marqueurs de panne réseau dans un message d'erreur, en minuscules. Signal
#: SECONDAIRE : la sonde a déjà répondu avant le lancement, ceci attrape la
#: coupure qui arrive PENDANT les 90 Mo.
_MOTIFS_RESEAU = (
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "newconnectionerror",
    "network is unreachable",
    "connection reset",
    "connection refused",
    "read timed out",
    "readtimeouterror",
    "timed out",
    "certificate verify failed",
)


class EmbeddingIndisponible(RuntimeError):
    """Le moteur d'embedding n'est pas prêt — téléchargement en cours, ou échoué.

    Type dédié, pour la raison exacte qui a fait naître
    `core.voice.VoiceModelUnavailable` : sans lui, un fichier simplement absent
    remonte au gestionnaire générique de `main.py`, qui répond
    ``500 {"detail": "Erreur interne du serveur", "type": "…"}``. C'est le corps
    de réponse qui a tué le panneau fichiers du module Docs (CLAUDE.md §8) :
    illisible pour l'utilisateur, et d'une forme qu'aucun frontend ne peut
    interpréter autrement que « panne ».

    Porte :attr:`etat`, le dict de :func:`etat_installation` au moment où elle
    est levée, pour que le 503 dise *où en est* la préparation.
    """

    def __init__(self, etat: dict):
        super().__init__(etat.get("message") or "Moteur de recherche documentaire indisponible.")
        self.etat = etat


# ── État partagé ──────────────────────────────────────────────────────────────
#
# `_verrou` protège les deux globales ensemble : sans lui, deux requêtes
# simultanées passent toutes les deux le test « personne ne télécharge » et
# lancent deux téléchargements sur le même dossier.
_verrou = threading.RLock()

#: Verdict courant du process, ou None si rien n'a encore été tenté ni lu.
_etat: dict | None = None

#: Une tentative automatique a-t-elle déjà été lancée dans ce process ? Distinct
#: de `_etat` : après un échec, `_etat` dit pourquoi et `_tentee` empêche de
#: recommencer à chaque appel. Remis à False par une demande explicite.
_tentee = False

#: Progression du téléchargement en cours, pour que l'interface montre autre
#: chose qu'un sablier sur 90 Mo. Même forme que `PiperEngine.progres()`.
_progres: dict = {"actif": False, "reçu": 0, "total": 0, "fichier": ""}


def _fichier_etat() -> Path:
    """Appelée, jamais figée dans une constante de module — `EPURE_DATA_DIR` est
    lue à chaque appel (CLAUDE.md §3.5).
    """
    return resolve_data_dir() / "embedding_install.json"


def chemin_fichier_modele(nom: str) -> Path:
    """Où vit un des fichiers du modèle. Appelée, jamais figée (même règle)."""
    return resolve_embedding_dir() / nom


def _maintenant() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _module_importable(nom: str) -> bool:
    """Le module est-il présent, SANS l'importer ?

    `find_spec` et non un `import` dans un `try` : importer `onnxruntime` coûte
    0,37 s et charge trois bibliothèques natives pour répondre à une question de
    disque. C'est 40 fois moins que les 15 s de `sentence_transformers`, mais la
    règle ne change pas de nature parce que le coût a baissé.

    `find_spec` peut lever et pas seulement rendre None (`ModuleNotFoundError`
    sur un parent absent, `ValueError` sur une entrée de `sys.modules` abîmée).
    Un diagnostic ne doit jamais être la cause de la panne qu'il cherche.
    """
    try:
        return importlib.util.find_spec(nom) is not None
    except (ImportError, ValueError):
        return False


def runtime_present() -> bool:
    """`onnxruntime` est-il importable ? Question de disque, pas d'usage."""
    return _module_importable(MODULE_RUNTIME)


def fichiers_manquants() -> list[str]:
    """Les fichiers du modèle à récupérer, dans l'ordre de téléchargement.

    Présence ET taille : un `.onnx` de 12 octets (page d'erreur HTML enregistrée
    par une tentative d'un autre outil, quota HuggingFace, proxy captif) passerait
    un test de présence et ferait planter `onnxruntime` au chargement. La taille
    ne remplace pas le sha256 — elle attrape le cas grossier sans relire 90 Mo à
    chaque appel de `GET /rag/capabilities`, qui est interrogé en boucle par le
    frontend.
    """
    manquants = []
    for nom, (_, _, taille) in FICHIERS_MODELE.items():
        chemin = chemin_fichier_modele(nom)
        try:
            if not chemin.is_file() or chemin.stat().st_size != taille:
                manquants.append(nom)
        except OSError:
            manquants.append(nom)
    return manquants


def pile_presente() -> bool:
    """Tout est-il là pour construire le moteur ? Runtime **et** poids.

    Nom conservé de l'époque `torch` : `core/runtime.py` l'importe sous l'alias
    `pile_embedding_presente` et `modules/settings/router.py` s'appuie dessus.
    Le renommer aurait été plus propre et n'aurait rien apporté.
    """
    return runtime_present() and not fichiers_manquants()


_MSG_PRET = "Moteur de recherche documentaire prêt."
_MSG_EN_COURS = (
    "Préparation du moteur de recherche documentaire — téléchargement du modèle "
    f"({TAILLE_ESTIMEE_MO} Mo), une à deux minutes, connexion réseau nécessaire."
)
_MSG_ABSENT = (
    "Le moteur de recherche documentaire n'est pas encore prêt — son modèle se "
    "télécharge au premier usage."
)
_MSG_RESEAU = (
    "Préparation impossible : le serveur du modèle est injoignable. Vérifiez la "
    "connexion réseau, puis réessayez."
)
_MSG_RUNTIME_ABSENT = (
    "Préparation impossible : « onnxruntime » n'est pas installé alors qu'il fait "
    "partie des dépendances d'Épure. L'installation est incomplète — réinstallez "
    "le paquet, ou lancez « pip install -r requirements.txt »."
)
_MSG_DESACTIVE = (
    "Le modèle de recherche documentaire n'est pas là, et son téléchargement "
    "automatique est désactivé sur cette instance (EPURE_EMBEDDING_AUTOINSTALL=0)."
)


def autoinstall_actif() -> bool:
    """A-t-on le droit de télécharger 90 Mo tout seul ?

    Interrupteur nécessaire, et pas une politesse — le contraire signifie qu'un
    process qui touche le moteur documentaire télécharge sans avoir été
    interrogé. Deux besoins concrets :

    - **La suite de tests.** `backend/_test_env.py` pose la variable à `0`. Sans
      elle, le premier test touchant le RAG tirerait 90 Mo sur le runner de la
      CI — et le garde-fou vaut mieux que la confiance dans le fait qu'aucun test
      n'y touche : c'est exactement le genre d'invariant qu'un test futur casse
      sans le savoir.
    - **Une instance qu'on ne veut pas voir télécharger**, par exemple sur une
      connexion facturée. Refuser proprement, en le disant.

    Lue à chaque appel et non figée : un lanceur peut la poser après l'import de
    ce module (même règle que `resolve_data_dir`, CLAUDE.md §3.5).
    """
    return os.environ.get("EPURE_EMBEDDING_AUTOINSTALL", "1").strip() != "0"


def _verdict(etat: str, *, message: str, cause: str = "", etape: str = "") -> dict:
    """Le dict que voient le 503, `GET /rag/capabilities` et le frontend.

    Les clés sont **le contrat** de `src/recherche.ts` (`état`, `disponible`,
    `message`, `cause`, `taille_estimée_mo`) : elles n'ont pas bougé en changeant
    de pile, et ne doivent pas bouger sans toucher le frontend.
    """
    return {
        "état": etat,
        "disponible": etat == PRET,
        "message": message,
        "cause": cause,
        "étape": etape,
        "fichiers_manquants": [] if etat == PRET else fichiers_manquants(),
        "taille_estimée_mo": TAILLE_ESTIMEE_MO,
        "réseau_requis": True,
        "progression": progres(),
        "horodatage": _maintenant(),
    }


def progres() -> dict:
    with _verrou:
        return dict(_progres)


def _poser_progres(champs: dict) -> None:
    """Dict et non kwargs : la clé « reçu » est accentuee comme les autres cles
    de ce module, et un mot-cle accentue passerait mais se lirait mal.
    """
    global _progres
    with _verrou:
        _progres = {**_progres, **champs}


def _persister(verdict: dict) -> None:
    """N'écrit QUE les verdicts terminaux. Cf. docstring du module : un « en
    cours » sur le disque survivrait au process qui l'a écrit et deviendrait un
    mensonge que rien ne peut réfuter.
    """
    if verdict["état"] not in (PRET, ECHEC):
        return
    if verdict["cause"] in (CAUSE_DESACTIVE, CAUSE_RUNTIME_ABSENT):
        # Des CONFIGURATIONS (ou une installation abîmée), pas des verdicts de
        # téléchargement : les écrire ferait croire à une panne réseau au
        # démarrage suivant, quand la cause aura disparu.
        return
    try:
        write_json(_fichier_etat(), {
            "état": verdict["état"],
            "message": verdict["message"],
            "cause": verdict["cause"],
            "étape": verdict["étape"],
            "horodatage": verdict["horodatage"],
        })
    except OSError:
        logger.warning("État de préparation non mémorisé (%s)", _fichier_etat(),
                       exc_info=True)


def _depuis_disque() -> dict:
    """Dernier verdict terminal connu, ou « absent ».

    Sert à l'affichage, pas à la décision : une tentative a lieu de toute façon
    au premier besoin réel dans ce process, parce qu'un échec réseau d'hier ne
    dit rien du réseau d'aujourd'hui — et que la sonde le règle en dix secondes
    sans télécharger un octet.
    """
    brut = read_json(_fichier_etat(), {})
    etat = brut.get("état") if isinstance(brut, dict) else None
    if etat != ECHEC:
        return _verdict(ABSENT, message=_MSG_ABSENT)
    return _verdict(
        ECHEC,
        message=brut.get("message") or _MSG_RESEAU,
        cause=brut.get("cause") or CAUSE_TELECHARGEMENT,
        etape=brut.get("étape") or "",
    )


def etat_installation() -> dict:
    """Où en est le moteur — sans rien télécharger ni rien importer.

    Même rôle que `GET /voice/capabilities` : répondre AVANT que l'utilisateur
    clique, pour que l'interface explique au lieu d'afficher une erreur.
    """
    with _verrou:
        if pile_presente():
            return _verdict(PRET, message=_MSG_PRET)
        if not runtime_present():
            # Priorité à cette cause : elle ne se répare pas en attendant, et
            # annoncer « téléchargement en cours » serait faux.
            return _verdict(ECHEC, message=_MSG_RUNTIME_ABSENT,
                            cause=CAUSE_RUNTIME_ABSENT)
        if _etat is not None:
            return dict(_etat)
        if not autoinstall_actif():
            # Sans ce cas, l'interface annoncerait « se télécharge au premier
            # usage » sur une instance où il ne se téléchargera jamais.
            return _verdict(ECHEC, message=_MSG_DESACTIVE, cause=CAUSE_DESACTIVE)
        return _depuis_disque()


def declencher_installation(explicite: bool = False) -> dict:
    """Lance le téléchargement s'il n'est pas déjà en cours. Ne bloque jamais.

    `explicite=False` (le cas normal, appelé par `VectorStore.__init__`) : une
    seule tentative par process, quel que soit le nombre d'appels concurrents.

    `explicite=True` (`POST /rag/install`) : autorise une nouvelle tentative
    après un échec, parce que la cause la plus probable — pas de réseau — se
    corrige en dehors de l'application et que l'utilisateur est le seul à savoir
    quand. Ne relance jamais par-dessus un téléchargement en cours.
    """
    global _etat, _tentee
    with _verrou:
        if pile_presente():
            _etat = _verdict(PRET, message=_MSG_PRET)
            return dict(_etat)
        if not runtime_present():
            return _verdict(ECHEC, message=_MSG_RUNTIME_ABSENT,
                            cause=CAUSE_RUNTIME_ABSENT)
        if _etat is not None and _etat["état"] == EN_COURS:
            return dict(_etat)
        if not autoinstall_actif():
            # Ni mémorisé ni persisté : c'est une CONFIGURATION. L'écrire
            # figerait un « échec » qui disparaîtrait au prochain démarrage sans
            # la variable, et `_tentee` reste faux pour que l'activer suffise.
            return _verdict(ECHEC, message=_MSG_DESACTIVE, cause=CAUSE_DESACTIVE)
        if _tentee and not explicite:
            return dict(_etat) if _etat is not None else _depuis_disque()
        _tentee = True
        manquants = fichiers_manquants()
        _etat = _verdict(EN_COURS, message=_MSG_EN_COURS, etape=manquants[0])
        depart = dict(_etat)
    logger.info("Téléchargement du modèle d'embedding lancé (%s)", ", ".join(manquants))
    threading.Thread(target=_telecharger_tout, daemon=True,
                     name="epure-embedding-download").start()
    return depart


def _poser(verdict: dict) -> None:
    """Le DISQUE d'abord, la mémoire ensuite — l'ordre inverse a une course.

    Quiconque voit un verdict terminal en mémoire doit pouvoir compter sur le
    fichier : publier l'état avant de l'écrire laisse une fenêtre, courte mais
    réelle, où l'application annonce « échec réseau » alors qu'un redémarrage
    immédiat repartirait de « absent ». La fenêtre a été observée à l'époque
    `pip`, pas supposée — un test qui attendait le verdict puis relisait le
    fichier tombait dessus une fois sur deux.
    """
    global _etat
    _persister(verdict)
    with _verrou:
        _etat = verdict


def _hote_joignable(url: str = _BASE) -> bool:
    """Un serveur répond-il à cette adresse ?

    Le point subtil, et c'est lui qui rend le verdict « réseau » fiable : une
    `HTTPError` compte comme JOIGNABLE. 403, 404, 405 sur un HEAD veulent dire
    qu'un serveur a lu la requête — le réseau marche, et un téléchargement qui
    échouerait ensuite n'échouerait pas pour cette raison. Seule une erreur de
    transport vaut « hors ligne ». Compter un code HTTP comme une panne réseau
    afficherait « vérifiez votre connexion » à quelqu'un dont la connexion va
    très bien.
    """
    requete = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(requete, timeout=_TIMEOUT_SONDE):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def _dit_reseau(message: str) -> bool:
    bas = message.lower()
    return any(motif in bas for motif in _MOTIFS_RESEAU)


def url_fichier(nom: str) -> str:
    """L'URL d'un fichier du modèle. Fonction et non constante, parce que c'est
    la forme que `test_embedding_install.py` interroge pour vérifier que la
    révision est bien épinglée — une bascule vers `main` ne se verrait autrement
    que le jour où le dépôt amont bougerait.
    """
    return _BASE + "/" + FICHIERS_MODELE[nom][0]


def _telecharger(nom: str) -> None:
    """Un fichier : `.part`, sha256, **puis** renommage atomique.

    L'ordre n'est pas un détail — cf. docstring du module et le jumeau
    `PiperEngine._telecharger`, dont ceci reprend délibérément la mécanique
    plutôt que d'en inventer une seconde.
    """
    chemin_distant, sha_attendu, taille = FICHIERS_MODELE[nom]
    cible = chemin_fichier_modele(nom)
    cible.parent.mkdir(parents=True, exist_ok=True)
    temporaire = cible.with_name(cible.name + ".part")
    temporaire.unlink(missing_ok=True)   # reste d'une tentative interrompue
    url = url_fichier(nom)
    logger.info("Téléchargement du modèle d'embedding : %s", url)
    digest = hashlib.sha256()
    recu = palier = 0
    try:
        with urllib.request.urlopen(url, timeout=120) as reponse:
            total = int(reponse.headers.get("Content-Length") or taille)
            _poser_progres({"actif": True, "reçu": 0, "total": total, "fichier": nom})
            with open(temporaire, "wb") as sortie:
                while True:
                    bloc = reponse.read(1 << 20)
                    if not bloc:
                        break
                    sortie.write(bloc)
                    digest.update(bloc)
                    recu += len(bloc)
                    _poser_progres({"reçu": recu})
                    if total and recu * 10 // total > palier:
                        palier = recu * 10 // total
                        logger.info("  %s : %d %% (%.1f/%.1f Mo)",
                                    nom, palier * 10, recu / 1e6, total / 1e6)
    except OSError as exc:
        # urllib.error.URLError et HTTPError dérivent d'OSError, comme les
        # erreurs disque : un seul filet, et le message dit lequel c'était.
        temporaire.unlink(missing_ok=True)
        raise OSError(f"téléchargement de {nom} impossible ({url}) : {exc}") from exc
    finally:
        _poser_progres({"actif": False, "reçu": 0, "total": 0, "fichier": ""})

    obtenu = digest.hexdigest()
    if obtenu != sha_attendu:
        temporaire.unlink(missing_ok=True)
        raise ValueError(
            f"empreinte incorrecte pour {nom} : attendu {sha_attendu}, obtenu "
            f"{obtenu}. Le fichier a été supprimé."
        )
    os.replace(temporaire, cible)
    logger.info("Modèle d'embedding : %s récupéré (%.1f Mo)", nom, recu / 1e6)


def _telecharger_tout() -> None:
    """Corps du thread. Ne lève jamais : tout finit en verdict."""
    try:
        if not _hote_joignable():
            _poser(_verdict(ECHEC, message=_MSG_RESEAU, cause=CAUSE_RESEAU))
            return
        for nom in list(FICHIERS_MODELE):
            if nom not in fichiers_manquants():
                continue
            if not autoinstall_actif():
                # Relu avant CHAQUE fichier, pas seulement au déclenchement :
                # l'interrupteur doit pouvoir arrêter un téléchargement entamé,
                # pas seulement empêcher d'en démarrer un.
                _poser(_verdict(ECHEC, message=_MSG_DESACTIVE,
                                cause=CAUSE_DESACTIVE, etape=nom))
                return
            _poser(_verdict(EN_COURS, message=_MSG_EN_COURS, etape=nom))
            try:
                _telecharger(nom)
            except (OSError, ValueError) as exc:
                reseau = _dit_reseau(str(exc))
                logger.warning("Téléchargement de %s échoué : %s", nom, exc)
                _poser(_verdict(
                    ECHEC,
                    message=(_MSG_RESEAU if reseau else
                             f"Préparation impossible : le téléchargement de "
                             f"« {nom} » a échoué. Détail dans les journaux."),
                    cause=CAUSE_RESEAU if reseau else CAUSE_TELECHARGEMENT,
                    etape=nom,
                ))
                return
        if not pile_presente():
            _poser(_verdict(
                ECHEC,
                message=("Préparation incomplète : le téléchargement s'est terminé "
                         f"sans erreur mais {', '.join(fichiers_manquants())} reste "
                         "introuvable."),
                cause=CAUSE_TELECHARGEMENT,
            ))
            return
        logger.info("Modèle d'embedding prêt — recherche documentaire disponible.")
        _poser(_verdict(PRET, message=_MSG_PRET))
    except Exception as exc:  # noqa: BLE001 — un thread qui lève ne dit rien à personne
        logger.exception("Préparation du moteur d'embedding interrompue")
        _poser(_verdict(ECHEC, message=f"Préparation interrompue : {exc}",
                        cause=CAUSE_TELECHARGEMENT))


def exiger_pile() -> None:
    """Appelée par `VectorStore.__init__` : télécharge en fond, ou lève, jamais
    ne bloque.

    Le contrat que ce module existe pour tenir : un modèle absent n'est pas une
    erreur terminale. On lance le téléchargement dans un thread et on lève
    `EmbeddingIndisponible` porteuse de l'état — l'appelant HTTP la traduit en
    503 lisible, et le prochain appel verra « en cours » puis « prêt ».
    """
    if pile_presente():
        return
    raise EmbeddingIndisponible(declencher_installation())


def _reinitialiser_pour_tests() -> None:
    """Remet l'état du process à zéro. Réservé aux tests — un module d'état
    global n'est pas testable sans, et un `setUp` qui touche les globales à la
    main les touche différemment dans chaque fichier.
    """
    global _etat, _tentee, _progres
    with _verrou:
        _etat = None
        _tentee = False
        _progres = {"actif": False, "reçu": 0, "total": 0, "fichier": ""}
