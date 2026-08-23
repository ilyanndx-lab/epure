"""Installation à la demande de la pile d'embedding (torch + sentence-transformers).

**L'incident que ce module ferme**, décrit sous le nom d'« écarts 2 et 3 » dans
`docs/distribution-empaquetee.md` : dans tout paquet livré, la recherche
documentaire répondait 500 et rien ne pouvait la réparer depuis l'application.

Trois décisions prises séparément se composaient en panne, chacune défendable
seule :

1. `HORS_PAQUET_PIP` exclut `sentence-transformers` de l'installation du paquet —
   il tire torch, ~2 Go, pour une capacité que le destinataire n'utilisera
   peut-être jamais. Le docstring de `tools/faire_paquet.py` promettait qu'il
   « s'installe au premier usage du RAG ».
2. **Aucun chemin de code ne faisait cette installation.** `VectorStore.__init__`
   importait `sentence_transformers` et laissait remonter l'`ImportError` : le
   premier document chargé produisait une erreur, pas un téléchargement.
3. `PURGE_SITE_PACKAGES` retirait `pip` du paquet, donc même le rattrapage à la
   main demandait de rebootstrapper `pip` par `get-pip.py`.

La procédure manuelle qui en découlait fonctionnait, mais elle transformait
« ouvrir le panneau fichiers » en session d'accompagnement à deux. Ce module est
la réponse retenue : **l'application installe elle-même sa pile d'embedding**, et
`pip`/`setuptools` restent dans le paquet pour qu'elle puisse (≈ 10 Mo,
négligeables devant les 2 Go de torch qu'ils servent à installer).

Ce qui est délibéré ici, et pourquoi :

- **L'ordre des deux commandes est un invariant, pas une préférence.** `torch`
  d'abord et depuis l'index PyTorch (`https://download.pytorch.org/whl/cpu`),
  `sentence-transformers` ensuite. PyPI ne publie que des wheels `win_amd64` pour
  torch ; l'index PyTorch publie bien
  `torch-2.13.0+cpu-cp312-cp312-win_arm64.whl` — donc cp312, le Python embarqué
  dans le paquet. Installer `sentence-transformers` d'abord ferait résoudre torch
  depuis PyPI, où il n'y a rien pour Windows ARM64
  (`docs/remplacement-vectoriel.md`, étape E ; `backend/requirements.txt` porte
  la même consigne pour un lecteur humain).

- **L'installation tourne dans un thread à elle**, jamais dans celui de la
  requête : 2 Go se comptent en minutes, et l'endpoint doit répondre tout de
  suite « c'est en cours ». Même idiome que le `_warmup` de `core/runtime.py`.

- **Une seule tentative automatique par process.** C'est le point qui demande un
  état partagé : `GET /rag/files`, `GET /rag/capabilities` et l'ouverture du
  panneau fichiers arrivent ensemble, et sans garde chacun lancerait son `pip`.
  Le verrou + :data:`_tentee` répondent à ça. Un échec ne se relance pas tout
  seul non plus — il se relance sur demande explicite (`POST /rag/install`), pour
  qu'une machine hors ligne ne reparte pas en boucle sur un téléchargement voué.

- **Le fichier d'état ne porte que des verdicts TERMINAUX** (`prêt` / `échec`),
  jamais `en_cours`. Un process tué au milieu d'un `pip install` laisserait sinon
  un fichier qui dit « en cours » pour toujours, indistinguable d'une
  installation vivante. L'état « en cours » n'appartient qu'à un process qui
  tourne, donc à la mémoire ; le disque sert à expliquer un échec passé après un
  redémarrage, sans attendre une nouvelle tentative pour l'apprendre.

- **« Pas de réseau » et « pip a échoué » sont deux verdicts distincts**, parce
  que ce n'est pas la même chose à dire à quelqu'un. La distinction est mesurée
  et non devinée : une sonde HEAD sur l'index PyTorch AVANT de lancer `pip`. Un
  code HTTP, même 403 ou 405, prouve qu'un serveur a répondu — donc réseau
  présent ; seule une erreur de transport (résolution DNS, connexion refusée,
  délai dépassé) vaut « hors ligne ». La sortie de `pip` est relue ensuite comme
  signal secondaire, pour les coupures qui arrivent *pendant* le téléchargement.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from core.jsonstore import read_json, write_json
from core.paths import resolve_data_dir

logger = logging.getLogger(__name__)

#: Index de PyTorch, seul à publier des wheels `win_arm64` pour torch. Cf.
#: docstring du module : l'utiliser AVANT `sentence-transformers` est un
#: invariant, pas un réglage.
INDEX_TORCH = "https://download.pytorch.org/whl/cpu"

#: Version épinglée, identique à `backend/requirements.txt`. Deux endroits pour
#: une version, c'est un de trop — mais le second lecteur n'est pas le même :
#: `requirements.txt` sert `pip install -r`, celui-ci sert l'installation
#: déclenchée par l'application. `test_embedding_install.py` vérifie qu'ils ne
#: divergent pas.
VERSION_SENTENCE_TRANSFORMERS = "5.5.1"

#: Poids annoncé à l'utilisateur AVANT de lancer le téléchargement, comme le fait
#: déjà `core/voice.py` pour les 76 Mo du modèle Piper. Ordre de grandeur mesuré
#: (torch CPU + ses dépendances), pas une valeur exacte : c'est une phrase
#: d'interface, pas une comptabilité.
TAILLE_ESTIMEE_MO = 2000

#: États possibles. `absent` = jamais tenté dans ce process et rien de mémorisé.
ABSENT = "absent"
EN_COURS = "en_cours"
PRET = "prêt"
ECHEC = "échec"

#: Causes d'échec, pour que l'interface ne dise pas « erreur » à trois problèmes
#: qui ne se règlent pas de la même façon.
CAUSE_RESEAU = "réseau"
CAUSE_PIP = "pip"
CAUSE_PIP_ABSENT = "pip_absent"
CAUSE_DESACTIVE = "désactivé"

#: Les modules qui doivent être importables pour que `VectorStore` se construise,
#: avec le paquet qui les fournit. `torch` est listé bien que
#: `sentence-transformers` le déclare : c'est LUI le gros morceau et le seul dont
#: l'index d'installation change selon l'architecture. Un environnement où
#: `sentence_transformers` serait présent sans torch existe (installation
#: interrompue entre les deux commandes) et rendrait « prêt » un moteur qui lève
#: à la construction.
_MODULES_REQUIS = (("torch", "torch"), ("sentence_transformers", "sentence-transformers"))

#: Délai de la sonde réseau. Court volontairement : elle ne sert qu'à distinguer
#: « hors ligne » de « pip a échoué », et une machine hors ligne doit le savoir en
#: secondes, pas attendre le timeout de pip.
_TIMEOUT_SONDE = 10

#: Marqueurs de panne réseau dans la sortie de `pip`, en minuscules. Signal
#: SECONDAIRE : la sonde ci-dessus a déjà répondu avant le lancement, ceci
#: attrape la coupure qui arrive pendant les 2 Go.
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
    "proxyerror",
    "certificate verify failed",
)


class EmbeddingIndisponible(RuntimeError):
    """La pile d'embedding n'est pas là — installation en cours, ou échouée.

    Type dédié, exactement pour la raison qui a fait naître
    `core.voice.VoiceModelUnavailable` : sans lui, une dépendance simplement
    absente remonte en `ImportError` jusqu'au gestionnaire générique de
    `main.py`, qui répond ``500 {"detail": "Erreur interne du serveur", "type":
    "ImportError"}``. C'est le corps de réponse exact qui a tué le panneau
    fichiers du module Docs (CLAUDE.md §8) : illisible pour l'utilisateur, et
    d'une forme qu'aucun frontend ne peut interpréter autrement que « panne ».

    Porte :attr:`etat`, le dict de :func:`etat_installation` au moment où elle est
    levée, pour que le 503 dise *où en est* l'installation et pas seulement
    qu'elle manque.
    """

    def __init__(self, etat: dict):
        super().__init__(etat.get("message") or "Moteur de recherche documentaire indisponible.")
        self.etat = etat


# ── État partagé ──────────────────────────────────────────────────────────────
#
# `_verrou` protège les deux globales ensemble : sans lui, deux requêtes
# simultanées passent toutes les deux le test « personne n'installe » et lancent
# deux `pip` sur le même site-packages.
_verrou = threading.RLock()

#: Verdict courant du process, ou None si rien n'a encore été tenté ni lu.
_etat: dict | None = None

#: Une tentative automatique a-t-elle déjà été lancée dans ce process ? Distinct
#: de `_etat` : après un échec, `_etat` dit pourquoi et `_tentee` empêche de
#: recommencer à chaque appel. Remis à False par une demande explicite.
_tentee = False


def _fichier_etat() -> Path:
    """Appelée, jamais figée dans une constante de module — `EPURE_DATA_DIR` est
    lue à chaque appel (CLAUDE.md §3.5).
    """
    return resolve_data_dir() / "embedding_install.json"


def _maintenant() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _module_importable(nom: str) -> bool:
    """Le module est-il présent, SANS l'importer ?

    `find_spec` et non un `import` dans un `try`, pour la raison qui vaut dans
    tout ce dépôt : importer `torch`, c'est payer plusieurs secondes et charger
    des bibliothèques natives pour répondre à une question de disque
    (CLAUDE.md §3.4).

    Jumeau de `core.voice._module_present`, délibérément non partagé : ce module
    est importé par `core/vector_store.py`, donc sur le chemin d'import du RAG. Y
    tirer `core.voice` — et avec lui hashlib, wave, urllib et le tableau des
    empreintes des voix Piper — pour cinq lignes serait payer un import au
    démarrage pour économiser une duplication triviale.

    `find_spec` peut lever et pas seulement renvoyer None : `ModuleNotFoundError`
    quand un paquet parent manque, `ValueError` sur une entrée de `sys.modules`
    abîmée. Un diagnostic ne doit jamais être la cause de la panne qu'il cherche.
    """
    try:
        return importlib.util.find_spec(nom) is not None
    except (ImportError, ValueError):
        return False


def paquets_manquants() -> list[str]:
    """Les paquets à installer, dans l'ordre où ils doivent l'être."""
    return [paquet for module, paquet in _MODULES_REQUIS if not _module_importable(module)]


def pile_presente() -> bool:
    """La pile d'embedding est-elle installée ? Question de disque, pas d'usage."""
    return not paquets_manquants()


def _verdict(etat: str, *, message: str, cause: str = "", etape: str = "") -> dict:
    return {
        "état": etat,
        "disponible": etat == PRET,
        "message": message,
        "cause": cause,
        "étape": etape,
        "paquets_manquants": [] if etat == PRET else paquets_manquants(),
        "taille_estimée_mo": TAILLE_ESTIMEE_MO,
        "réseau_requis": True,
        "horodatage": _maintenant(),
    }


_MSG_PRET = "Moteur de recherche documentaire prêt."
_MSG_EN_COURS = (
    "Préparation du moteur de recherche documentaire — téléchargement d'environ "
    f"{TAILLE_ESTIMEE_MO // 1000} Go, quelques minutes, connexion réseau nécessaire."
)
_MSG_ABSENT = (
    "Le moteur de recherche documentaire n'est pas encore installé — il se prépare "
    "au premier usage."
)
_MSG_RESEAU = (
    "Préparation impossible : l'index de téléchargement est injoignable. Vérifiez "
    "la connexion réseau, puis réessayez."
)
_MSG_PIP_ABSENT = (
    "Préparation impossible : « pip » est absent de cette installation, "
    "l'application ne peut donc rien installer elle-même."
)
_MSG_DESACTIVE = (
    "Le moteur de recherche documentaire n'est pas installé, et l'installation "
    "automatique est désactivée sur cette instance (EPURE_EMBEDDING_AUTOINSTALL=0)."
)


def autoinstall_actif() -> bool:
    """L'application a-t-elle le droit de lancer `pip` toute seule ?

    Interrupteur nécessaire, et pas une politesse : le contraire signifie qu'un
    process qui touche le moteur documentaire télécharge ~2 Go sans avoir été
    interrogé. Deux besoins concrets, l'un aussi sérieux que l'autre :

    - **La suite de tests.** `backend/_test_env.py` pose la variable à `0`. Sans
      elle, le job `backend` de la CI — dont le jeu de dépendances minimal
      n'installe NI torch NI sentence-transformers, exactement la configuration
      d'un paquet livré — verrait le premier test touchant le RAG lancer un
      `pip install torch` de 2 Go sur le runner. Le garde-fou vaut mieux que la
      confiance dans le fait qu'aucun test n'y touche : c'est justement le genre
      d'invariant qu'un test futur casse sans le savoir.
    - **Une instance qu'on ne veut pas voir installer 2 Go**, par exemple sur une
      connexion facturée. Refuser proprement, en le disant.

    Lue à chaque appel et non figée : un lanceur peut la poser après l'import de
    ce module (même règle que `resolve_data_dir`, CLAUDE.md §3.5).
    """
    return os.environ.get("EPURE_EMBEDDING_AUTOINSTALL", "1").strip() != "0"


def _persister(verdict: dict) -> None:
    """N'écrit QUE les verdicts terminaux. Cf. docstring du module : un
    « en cours » sur le disque survivrait au process qui l'a écrit et deviendrait
    un mensonge que rien ne peut réfuter.
    """
    if verdict["état"] not in (PRET, ECHEC):
        return
    if verdict["cause"] == CAUSE_DESACTIVE:
        # Une CONFIGURATION, pas un verdict : l'écrire ferait croire à une panne
        # au démarrage suivant, quand la variable ne sera plus là.
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
        # Un état d'installation non mémorisé ne justifie pas de casser
        # l'installation elle-même : au pire l'interface repart de « absent ».
        logger.warning("État d'installation non mémorisé (%s)", _fichier_etat(), exc_info=True)


def _depuis_disque() -> dict:
    """Dernier verdict terminal connu, ou « absent ».

    Sert à l'affichage, pas à la décision : une tentative a lieu de toute façon au
    premier besoin réel dans ce process, parce qu'un échec réseau d'hier ne dit
    rien du réseau d'aujourd'hui — et que la sonde le règle en dix secondes sans
    télécharger un octet.
    """
    brut = read_json(_fichier_etat(), {})
    etat = brut.get("état") if isinstance(brut, dict) else None
    if etat != ECHEC:
        return _verdict(ABSENT, message=_MSG_ABSENT)
    return _verdict(
        ECHEC,
        message=brut.get("message") or _MSG_RESEAU,
        cause=brut.get("cause") or CAUSE_PIP,
        etape=brut.get("étape") or "",
    )


def etat_installation() -> dict:
    """Où en est la pile d'embedding — sans rien installer ni rien importer.

    Même rôle que `GET /voice/capabilities` : répondre AVANT que l'utilisateur
    clique, pour que l'interface explique au lieu d'afficher une erreur. La
    différence avec la voix est le seul point qui compte ici — un paquet vocal
    absent ne s'installe pas en cliquant (aucune wheel `win_arm64`), celui-ci si.
    D'où un état à quatre valeurs et non un booléen.
    """
    with _verrou:
        if pile_presente():
            return _verdict(PRET, message=_MSG_PRET)
        if _etat is not None:
            return dict(_etat)
        if not autoinstall_actif():
            # Sans ce cas, l'interface annoncerait « il se prépare au premier
            # usage » sur une instance où il ne se préparera jamais.
            return _verdict(ECHEC, message=_MSG_DESACTIVE, cause=CAUSE_DESACTIVE)
        return _depuis_disque()


def declencher_installation(explicite: bool = False) -> dict:
    """Lance l'installation si elle n'est pas déjà en cours. Ne bloque jamais.

    `explicite=False` (le cas normal, appelé par `VectorStore.__init__`) : une
    seule tentative par process, quel que soit le nombre d'appels concurrents.
    C'est ce qui empêche l'ouverture du panneau fichiers — qui déclenche
    `GET /rag/files` et `GET /rag/capabilities` presque en même temps — de lancer
    deux `pip install torch`.

    `explicite=True` (`POST /rag/install`) : autorise une nouvelle tentative après
    un échec, parce que la cause la plus probable — pas de réseau — se corrige en
    dehors de l'application et que l'utilisateur est le seul à savoir quand. Ne
    relance jamais par-dessus une installation en cours.
    """
    global _etat, _tentee
    with _verrou:
        if pile_presente():
            _etat = _verdict(PRET, message=_MSG_PRET)
            return dict(_etat)
        if _etat is not None and _etat["état"] == EN_COURS:
            return dict(_etat)
        if not autoinstall_actif():
            # Ni mémorisé ni persisté : c'est une CONFIGURATION, pas un verdict.
            # L'écrire figerait un « échec » qui disparaîtrait au prochain
            # démarrage sans la variable, et `_tentee` resterait faux pour que
            # l'activer suffise.
            return _verdict(ECHEC, message=_MSG_DESACTIVE, cause=CAUSE_DESACTIVE)
        if _tentee and not explicite:
            # Échec déjà constaté dans ce process : on ne repart pas en boucle.
            return dict(_etat) if _etat is not None else _depuis_disque()
        _tentee = True
        manquants = paquets_manquants()
        _etat = _verdict(EN_COURS, message=_MSG_EN_COURS, etape=manquants[0])
        depart = dict(_etat)
    logger.info("Installation de la pile d'embedding lancée (%s)", ", ".join(manquants))
    threading.Thread(target=_installer, daemon=True, name="epure-embedding-install").start()
    return depart


def _poser(verdict: dict) -> None:
    """Le DISQUE d'abord, la mémoire ensuite — l'ordre inverse a une course.

    Quiconque voit un verdict terminal en mémoire doit pouvoir compter sur le
    fichier : publier l'état avant de l'écrire laisse une fenêtre, courte mais
    réelle, où l'application annonce « échec réseau » alors qu'un redémarrage
    immédiat repartirait de « absent ». La fenêtre a été observée, et pas
    supposée — un test qui attendait le verdict puis relisait le fichier tombait
    dessus une fois sur deux.
    """
    global _etat
    _persister(verdict)
    with _verrou:
        _etat = verdict


def _index_joignable(url: str = INDEX_TORCH) -> bool:
    """Un serveur répond-il à cette adresse ?

    Le point subtil, et c'est lui qui rend le verdict « réseau » fiable : une
    `HTTPError` compte comme JOIGNABLE. 403, 404, 405 sur un HEAD veulent dire
    qu'un serveur a lu la requête — le réseau marche, et un `pip install` qui
    échouerait ensuite n'échouerait pas pour cette raison. Seule une erreur de
    transport (`URLError`, timeout, socket) vaut « hors ligne ». Compter un code
    HTTP comme une panne réseau afficherait « vérifiez votre connexion » à
    quelqu'un dont la connexion va très bien.
    """
    requete = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(requete, timeout=_TIMEOUT_SONDE):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def _sortie_dit_reseau(sortie: str) -> bool:
    bas = sortie.lower()
    return any(motif in bas for motif in _MOTIFS_RESEAU)


def commandes_installation() -> tuple[tuple[str, list[str]], ...]:
    """Les commandes à jouer, dans l'ordre, avec l'étape qu'elles couvrent.

    Fonction et non constante : `sys.executable` doit être lu à l'appel, et
    surtout c'est la forme que `test_embedding_install.py` interroge pour vérifier
    l'ordre et l'index — deux choses qu'une relecture ne peut pas garantir et dont
    une inversion ne se verrait que sur une machine ARM64.

    Listes d'arguments, jamais de chaîne : `shell=True` est interdit dans ce dépôt
    (CLAUDE.md §6), et rien ici ne vient d'une entrée utilisateur de toute façon —
    l'invariant ne se relâche pas parce que le cas est bénin.
    """
    py = sys.executable
    return (
        ("torch", [py, "-m", "pip", "install", "torch", "--index-url", INDEX_TORCH]),
        ("sentence-transformers",
         [py, "-m", "pip", "install",
          f"sentence-transformers=={VERSION_SENTENCE_TRANSFORMERS}"]),
    )


def _pip(cmd: list[str]) -> tuple[int, str]:
    """Joue une commande pip et rend (code, sortie fusionnée).

    `errors="replace"` : la sortie de pip sous Windows n'est pas toujours de
    l'UTF-8 valide, et un `UnicodeDecodeError` ici transformerait une installation
    réussie en échec inexplicable.
    """
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def _installer() -> None:
    """Corps du thread d'installation. Ne lève jamais : tout finit en verdict."""
    try:
        if not sys.executable or not _module_importable("pip"):
            _poser(_verdict(ECHEC, message=_MSG_PIP_ABSENT, cause=CAUSE_PIP_ABSENT))
            return
        if not _index_joignable():
            _poser(_verdict(ECHEC, message=_MSG_RESEAU, cause=CAUSE_RESEAU))
            return
        for etape, cmd in commandes_installation():
            if _module_importable(etape.replace("-", "_")):
                continue
            if not autoinstall_actif():
                # Relu avant CHAQUE commande, pas seulement au déclenchement :
                # l'interrupteur doit pouvoir arrêter une installation entamée,
                # pas seulement empêcher d'en démarrer une. Il y a deux commandes
                # et la première dure des minutes — laisser partir la seconde
                # après que la variable a changé serait tenir la moitié d'une
                # promesse.
                _poser(_verdict(ECHEC, message=_MSG_DESACTIVE,
                                cause=CAUSE_DESACTIVE, etape=etape))
                return
            _poser(_verdict(EN_COURS, message=_MSG_EN_COURS, etape=etape))
            logger.info("pip install %s …", etape)
            code, sortie = _pip(cmd)
            if code != 0:
                reseau = _sortie_dit_reseau(sortie)
                logger.warning("Installation de %s échouée (code %s)\n%s",
                               etape, code, sortie[-2000:])
                _poser(_verdict(
                    ECHEC,
                    message=(_MSG_RESEAU if reseau else
                             f"Préparation impossible : l'installation de « {etape} » a "
                             f"échoué (code {code}). Détail dans les journaux."),
                    cause=CAUSE_RESEAU if reseau else CAUSE_PIP,
                    etape=etape,
                ))
                return
        # `pip` vient d'écrire dans site-packages : sans invalidation, `find_spec`
        # peut encore servir le cache de répertoire d'avant l'installation et
        # déclarer manquant ce qui vient d'arriver.
        importlib.invalidate_caches()
        if not pile_presente():
            _poser(_verdict(
                ECHEC,
                message=("Préparation incomplète : l'installation s'est terminée sans "
                         f"erreur mais {', '.join(paquets_manquants())} reste introuvable."),
                cause=CAUSE_PIP,
            ))
            return
        logger.info("Pile d'embedding installée — recherche documentaire disponible.")
        _poser(_verdict(PRET, message=_MSG_PRET))
    except Exception as exc:  # noqa: BLE001 — un thread qui lève ne dit rien à personne
        logger.exception("Installation de la pile d'embedding interrompue")
        _poser(_verdict(ECHEC, message=f"Préparation interrompue : {exc}", cause=CAUSE_PIP))


def exiger_pile() -> None:
    """Appelée par `VectorStore.__init__` : installe en fond, ou lève, jamais bloque.

    Le contrat que ce module existe pour tenir : `sentence_transformers` absent
    n'est plus une erreur terminale. On lance l'installation dans un thread et on
    lève `EmbeddingIndisponible` porteuse de l'état — l'appelant HTTP la traduit en
    503 lisible, et le prochain appel verra « en cours » puis « prêt ».
    """
    if pile_presente():
        return
    raise EmbeddingIndisponible(declencher_installation())


def _reinitialiser_pour_tests() -> None:
    """Remet l'état du process à zéro. Réservé aux tests — un module d'état global
    n'est pas testable sans, et un `setUp` qui touche les globales à la main les
    touche différemment dans chaque fichier.
    """
    global _etat, _tentee
    with _verrou:
        _etat = None
        _tentee = False
