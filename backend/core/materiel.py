"""Ce que CETTE machine peut réellement faire tourner : RAM, GPU, NPU.

Le panneau modèles listait jusqu'ici ce qui est installé, jamais ce qui est
tenable. Un utilisateur qui sélectionne un modèle de 22 Gio sur une machine qui
en a 8 ne l'apprend qu'au bout d'une minute de chargement, par un échec dont
rien ne dit la cause. Ce module répond à la question avant le clic.

────────────────────────────────────────────────────────────────────────────
MESURÉ SUR CE POSTE (Lenovo Yoga, AMD Radeon 840M, 2026-09-06), pas supposé
────────────────────────────────────────────────────────────────────────────

**dxdiag fonctionne, et son résultat interdit la règle naïve.** Sur un iGPU :

    Display Memory:   16548 MB       ← la somme des deux lignes suivantes
    Dedicated Memory:   512 MB       ← le seul vrai pool séparé
    Shared Memory:    16036 MB       ← de la RAM SYSTÈME, prêtée au GPU

Afficher 16548 Mo de « VRAM » à côté de 32 Go de RAM annoncerait ~49 Go à
quelqu'un qui en a 32 : le même octet compté deux fois. Et prendre l'un des
deux comme dénominateur donne la mauvaise réponse dans les deux sens —
`glm-4.7-flash` (17,7 Gio, réellement installé ici) sort « ne tiendra pas »
contre 80 % de 16548 Mo, alors qu'il tourne, servi depuis la RAM.

D'où la règle retenue, et c'est un ÉCART ASSUMÉ à « VRAM si connue, sinon
RAM » : **la VRAM n'est le dénominateur que lorsqu'elle est un pool
RÉELLEMENT séparé, c'est-à-dire sur le chemin `nvidia-smi`.** Ce que dxdiag
rend est publié tel quel, en DEUX nombres nommés (`vram_octets` =
dédiée, `vram_partagee_octets` = partagée), jamais fondus en un seul —
l'interface doit pouvoir dire « 512 Mo dédiés + 16 Go partagés avec la RAM »
plutôt que d'inventer un troisième pool.

Le fond de l'affaire : la question posée est « est-ce que ça tient ? », pas
« est-ce que ça ira vite ». Ollama et LM Studio retombent tous deux sur le CPU
quand un modèle ne rentre pas dans le GPU — la borne de FAISABILITÉ est donc
la RAM, la VRAM ne borne que la VITESSE. Sur une carte discrète (`nvidia-smi`)
on garde quand même la VRAM, parce que c'est la borne que l'utilisateur a en
tête quand il a payé pour elle.

**Trois autres mesures qui ont chacune changé une ligne de ce fichier :**

* `subprocess.run` ATTEND bien dxdiag, et le fichier est écrit au retour :
  16,3 s, `rc=0`, 119 779 octets. Pas de boucle d'attente à écrire — juste un
  timeout. (Un `&` PowerShell rend la main en 0,03 s : c'est une bizarrerie de
  PowerShell face à un binaire de sous-système GUI, pas de Python. Ne pas
  transposer l'un à l'autre.)
* `/whql:off`, censé sauter la vérification des signatures de pilotes, ne fait
  RIEN gagner : 16,74 s contre 16,31 s. Le drapeau n'est pas passé — il aurait
  ajouté une variable au diagnostic sans rien accélérer.
* Le fichier n'est **ni UTF-16 ni UTF-8** mais du **cp1252** : `utf-16` casse en
  fin de fichier, `utf-8` sur l'octet `0xa0` (l'espace insécable de
  `Driver Model: WDDM<nbsp>3.2`). Lu en `cp1252, errors="replace"`, mesuré.

**La RAM ne passe pas par psutil**, qui n'est ni installé ici, ni déclaré dans
`requirements.txt`, ni dans le `pip install` du job backend de la CI — un
`import psutil` au niveau module aurait donc transformé chaque
`TestClient(app)` en ERREUR DE COLLECTE, exactement l'incident `openai` puis
`readability-lxml` que l'en-tête de `ci.yml` raconte deux fois. Et il embarque
`_psutil_windows.pyd`, catégorie de binaire non signé dont le blocage par
Smart App Control sur la machine ARM64 cible est mesuré dans ce dépôt (§8).
Pour UN nombre, la bibliothèque standard suffit : `GlobalMemoryStatusEx` sous
Windows, `/proc/meminfo` sous Linux, `None` ailleurs.

Recoupement fait plutôt que supposé : `ullTotalPhys` rend 33 631 817 728
octets = 32 073,8 Mio, ce qui correspond au `Available OS Memory: 32074MB` de
dxdiag et NON à son `Memory: 32768MB`. L'écart de 694 Mo est ce que le
micrologiciel réserve avant que l'OS n'existe (dont les 512 Mo dédiés à
l'iGPU) : c'est bien le second nombre qui est le bon dénominateur, puisque
c'est le seul que le système peut réellement distribuer.

────────────────────────────────────────────────────────────────────────────

────────────────────────────────────────────────────────────────────────────
DEUX DURÉES DE VIE, ET LES CONFONDRE DONNE UN VERDICT FAUX
────────────────────────────────────────────────────────────────────────────

**Ce qui est FIGÉ est mis en cache ; ce qui BOUGE est relu à chaque verdict.**
La frontière n'est pas un raffinement : elle a rendu de mauvaises réponses.

Jusqu'au 2026-09-06, le dénominateur était la mémoire **TOTALE**, calculée une
fois pour la vie du process. Mesuré sur ce poste ce jour-là, en chargeant
`qwen2.5:7b` via Ollama :

    total (ullTotalPhys)        31,32 Gio   ← inchangé, c'est un fait matériel
    libre AVANT chargement      15,65 Gio
    libre APRÈS chargement       9,47 Gio   ← -6,18 Gio
    /api/ps                      6,44 Gio résidents pour ce modèle

Le calcul ignorait ces 6,18 Gio. `mistral-small:24b` (13,35 Gio, installé ici)
sortait donc « tient » — 43 % de 31,32 Gio — alors qu'il en réclame **141 % de
ce qui restait**. L'utilisateur lisait « tient » sur un modèle qui, à cet
instant précis, ne pouvait pas se charger sans faire tomber l'autre.

**Le libre n'est PAS déductible du cache**, quelle que soit l'ingéniosité :
c'est une valeur supplémentaire, et elle change entre deux ouvertures du
panneau. D'où la règle, non négociable :

| valeur | durée de vie | pourquoi |
|---|---|---|
| RAM totale, GPU, VRAM totale | cache, une fois | faits matériels figés — 16 s de dxdiag ne se paient pas deux fois |
| RAM libre, VRAM libre | **relue à chaque verdict** | change à chaque chargement de modèle |
| joignabilité de FLM (NPU) | **relue à chaque verdict** | un serveur démarré après l'app resterait « éteint » à vie |

`materiel()` (le cache) ne contient donc **que des faits figés**, par
construction et pas par vigilance : `npu` n'y est plus, et aucune valeur
« libre » n'y entre. C'est `etat_frais()` qui assemble les deux durées de vie,
**une seule fois par requête**, pour que les verdicts et la ligne qui les
motive (« calculés sur X libres sur Y ») parlent du même instant.

**Ne PAS déduire les modèles résidents d'Ollama (`/api/ps`) du libre** : la
mesure ci-dessus montre que `ullAvailPhys` les compte DÉJÀ (-6,18 Gio pour
6,44 Gio résidents). Les soustraire compterait les mêmes octets deux fois,
exactement comme additionner la mémoire dédiée et la mémoire partagée d'un
iGPU.

**Une lecture fraîche ratée ne retombe JAMAIS sur le total.** Elle rend
`inconnu`, comme partout ailleurs dans ce fichier : le total est justement la
valeur qui donnait la mauvaise réponse, s'y replier en silence rejouerait le
bug sous un autre nom.

────────────────────────────────────────────────────────────────────────────

**Le cache ne s'invite jamais sur le chemin de démarrage d'uvicorn** (§3.2 —
c'est l'incident `RAGEngine` qui empêchait `/health` de répondre) : un fil
démon le fait en tâche de fond, et l'endpoint le calcule lui-même, sous verrou
et dans un exécuteur, si la requête arrive avant que le préchauffage n'ait
abouti. Les sondes fraîches, elles, sont bon marché — `GlobalMemoryStatusEx`
est un appel système, et rien de plus — et ne partent qu'**une fois par
requête**, jamais par modèle.

**Le coût du chemin `nvidia-smi` n'est PAS mesuré ici** : ce poste n'a pas de
carte NVIDIA. Un `nvidia-smi` par ouverture de panneau est réputé rapide, mais
c'est de la réputation, pas un chiffre de ce dépôt ; `_TIMEOUT_NVIDIA_SMI_S`
(10 s) borne le pire cas, et l'endpoint tourne dans un exécuteur, donc une
carte occupée coûterait de la latence de panneau, jamais un blocage de la
boucle d'événements. À mesurer le jour où quelqu'un fait tourner Épure sur une
machine à carte discrète.

**`EPURE_MATERIEL_SONDE=0` coupe toute sonde réelle** et rend un matériel
entièrement inconnu. `_test_env.py` la pose : aucun test ne doit lancer un
`dxdiag` de 16 s ni dépendre du matériel du poste — même idiome, et même
raison, que `EPURE_EMBEDDING_AUTOINSTALL=0`.
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

from core.models import (
    FLM_MODELS_STATIC,
    check_flm,
    flm_model_ids,
    get_flm_installed,
    get_lmstudio_installed,
)
from core.ollama_memoire import tailles_installees as _tailles_ollama

logger = logging.getLogger(__name__)

#: Marge de sécurité : un modèle n'occupe pas que ses poids. En deçà, c'est
#: confortable ; au-dessus mais sous 100 %, ça passe peut-être et ça rame.
#:
#: **Elle ne couvre PAS entièrement l'écart réel, et il faut le savoir avant de
#: lire un « tient » comme une garantie.** Mesuré sur ce poste : `qwen2.5:7b`
#: pèse 4,36 Gio sur le disque et 6,44 Gio une fois résident (`/api/ps`), soit
#: +48 % de cache KV et de contexte. Un modèle à 80 % de la ressource d'après
#: sa taille disque en réclamerait donc ~118 % en mémoire. Le verdict est un
#: INDICE avant le clic, pas une promesse — et c'est aussi pourquoi
#: `ne_tiendra_pas` était, lui, présenté comme une information SÛRE tant que le
#: dénominateur était la mémoire totale : si les poids seuls ne rentraient pas
#: dans la machine, rien ne rentrerait. **Ce n'est plus vrai depuis que le
#: dénominateur est la mémoire LIBRE** (2026-09-06), et il faut le savoir dans
#: les deux sens : un `ne_tiendra_pas` calculé sur 9 Gio libres devient un
#: `tient` dès qu'Ollama éjecte le modèle résident, sans que rien n'ait changé
#: sur la machine. Les quatre verdicts décrivent désormais un INSTANT, pas une
#: propriété de la machine — c'est le prix, assumé, de ne plus mentir dans
#: l'autre sens (« tient » sur un modèle qui ne pouvait pas se charger).
MARGE_CONFORT = 0.80

#: Verdicts de mémoire — quatre états, et `INCONNU` n'est le repli d'aucun autre.
VERDICT_TIENT = "tient"
VERDICT_LIMITE = "limite"
VERDICT_NE_TIENDRA_PAS = "ne_tiendra_pas"
VERDICT_INCONNU = "inconnu"

#: Verdicts de FLM, qui ne parlent pas de la même chose (cf. `verdict_flm`).
VERDICT_DISPONIBLE = "disponible"
VERDICT_INDISPONIBLE = "indisponible"

#: Toutes les valeurs qu'un `verdict` peut prendre. Exportée pour que le test et
#: le frontend aient une liste à confronter plutôt qu'à recopier.
VERDICTS = (
    VERDICT_TIENT, VERDICT_LIMITE, VERDICT_NE_TIENDRA_PAS, VERDICT_INCONNU,
    VERDICT_DISPONIBLE, VERDICT_INDISPONIBLE,
)

#: dxdiag met ~16 s sur ce poste (mesuré). Le plafond laisse de la marge à une
#: machine plus lente sans laisser le fil démon pendre indéfiniment.
_TIMEOUT_DXDIAG_S = 90
_TIMEOUT_NVIDIA_SMI_S = 10

_MO = 1024 * 1024


def sondes_autorisees() -> bool:
    """Faux quand `EPURE_MATERIEL_SONDE=0` — aucun sous-processus ne part.

    Lue à chaque appel et jamais figée dans une constante de module : c'est la
    règle de `core/paths.py` (§3.5), et pour la même raison — un test qui pose
    la variable après l'import doit être entendu.
    """
    return os.environ.get("EPURE_MATERIEL_SONDE", "1").strip() not in ("0", "false", "no")


# ── Analyse des sorties : fonctions PURES, testables sans matériel ───────────

def _colonnes_nvidia_smi(sortie: str) -> Optional[list[str]]:
    """La première ligne de DONNÉES du CSV, découpée. `None` si illisible.

    Le PREMIER GPU listé, pas le plus gros : sur une machine à deux cartes, la
    0 est celle qu'utilisent les runtimes par défaut.
    """
    for ligne in sortie.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.lower().startswith("name"):
            continue  # en-tête CSV
        parts = [p.strip() for p in ligne.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        return parts
    return None


def _mib(colonne: Optional[str]) -> Optional[int]:
    """`8188 MiB` → octets. `None` sur `[N/A]`, colonne absente, ou texte.

    `[N/A]` (pilote en cours d'installation) n'est pas zéro : on sait qu'il y a
    une carte, on ne sait pas ce qu'elle a. Rendre 0 se lirait « aucune
    mémoire » et ferait sortir `ne_tiendra_pas` sur tout.
    """
    if not colonne:
        return None
    m = re.search(r"(\d+)", colonne)
    return int(m.group(1)) * _MO if m else None


def analyser_nvidia_smi(sortie: str) -> Optional[dict]:
    """`name, memory.total, memory.free` en CSV → nom + VRAM TOTALE.

    **`memory.free` est délibérément ABSENT du dict rendu**, alors que la
    commande la demande : ce dict part dans le cache de `materiel()`, et le
    cache ne contient que des faits figés (cf. l'en-tête). La colonne libre se
    lit par `analyser_nvidia_smi_libre`, sur la même sortie, au moment du
    verdict. Une seule commande, deux lecteurs, aucune valeur périssable
    mémorisée — l'oubli inverse (poser `vram_libre_octets` ici) rejouerait le
    bug du 2026-09-06 sous un nouveau nom, et personne ne le verrait.
    """
    parts = _colonnes_nvidia_smi(sortie)
    if parts is None:
        return None
    return {
        "nom": parts[0],
        "vram_octets": _mib(parts[1] if len(parts) > 1 else None),
        "vram_partagee_octets": None,
        # Une carte discrète a sa propre mémoire. C'est le SEUL cas où on
        # sait que le pool est séparé, et donc le seul où il sert de
        # dénominateur (cf. l'en-tête du module).
        "partage_la_ram": False,
        "source": "nvidia-smi",
    }


def analyser_nvidia_smi_libre(sortie: str) -> Optional[int]:
    """La colonne `memory.free` de la même sortie → octets, ou `None`.

    Troisième colonne, et rien d'autre. Un `nvidia-smi` plus ancien qui ne
    connaîtrait pas `memory.free` rendrait deux colonnes : `None`, donc verdict
    `inconnu` — jamais un repli sur le total, qui est exactement la valeur
    fausse qu'on vient de retirer du calcul.
    """
    parts = _colonnes_nvidia_smi(sortie)
    if parts is None or len(parts) < 3:
        return None
    return _mib(parts[2])


#: Les trois lignes mémoire du bloc « Display Devices », telles que dxdiag les
#: écrit (mesuré). L'indentation varie, le libellé non.
_DX_MEM = {
    "vram_octets": re.compile(r"^\s*Dedicated Memory:\s*(\d+)\s*MB", re.IGNORECASE),
    "vram_partagee_octets": re.compile(r"^\s*Shared Memory:\s*(\d+)\s*MB", re.IGNORECASE),
}
_DX_CARTE = re.compile(r"^\s*Card name:\s*(.+?)\s*$", re.IGNORECASE)


def analyser_dxdiag(texte: str) -> Optional[dict]:
    """Bloc « Display Devices » d'un rapport dxdiag → nom + mémoires.

    **Borné au premier bloc de carte**, et ce n'est pas de la prudence
    théorique : le rapport de ce poste porte une SECONDE ligne
    `Dedicated Memory: 0 MB` 143 lignes plus bas, dans un bloc qui décrit autre
    chose (ligne 207 contre 63). Une recherche non bornée trouve l'une ou
    l'autre selon l'ordre du fichier, et `0` se lirait comme « pas de GPU ».

    Rend `None` si aucune carte n'est nommée. Une carte nommée sans ligne
    mémoire rend un dict avec des `None` : « on a vu un GPU, on ignore sa
    mémoire » n'est pas « pas de GPU », et les deux ne doivent pas se
    confondre (§3.3 bis, même discipline que les capacités).
    """
    lignes = texte.splitlines()
    debut = next((i for i, l in enumerate(lignes) if _DX_CARTE.match(l)), None)
    if debut is None:
        return None
    nom = _DX_CARTE.match(lignes[debut]).group(1)
    fin = next((i for i in range(debut + 1, len(lignes)) if _DX_CARTE.match(lignes[i])),
               len(lignes))
    trouve: dict[str, Optional[int]] = {"vram_octets": None, "vram_partagee_octets": None}
    for ligne in lignes[debut:fin]:
        for cle, motif in _DX_MEM.items():
            m = motif.match(ligne)
            if m and trouve[cle] is None:
                trouve[cle] = int(m.group(1)) * _MO
    partagee = trouve["vram_partagee_octets"]
    return {
        "nom": nom or None,
        **trouve,
        # `None` tant qu'on n'a pas lu la ligne « Shared Memory » : ne pas savoir
        # si la mémoire est partagée n'est pas savoir qu'elle ne l'est pas.
        "partage_la_ram": None if partagee is None else partagee > 0,
        "source": "dxdiag",
    }


def _champ_meminfo(texte: str, champ: str) -> Optional[int]:
    """`<champ>:  32943 kB` de `/proc/meminfo` → octets. `None` si absent.

    Ancré en début de ligne ET suivi des deux-points : sans l'ancre,
    `MemFree` matcherait à l'intérieur de `MemFreeFoo` d'un futur noyau, et
    surtout la confusion inverse coûterait cher — `MemAvailable` et `MemFree`
    ne veulent pas dire la même chose (le second ignore le cache réclamable,
    donc sous-estime massivement ce qu'un modèle peut prendre).
    """
    m = re.search(rf"^{champ}:\s+(\d+)\s*kB", texte, re.MULTILINE)
    return int(m.group(1)) * 1024 if m else None


def analyser_meminfo(texte: str) -> Optional[int]:
    """`MemTotal` → octets. Sous Linux (la CI). Fait FIGÉ, mis en cache."""
    return _champ_meminfo(texte, "MemTotal")


def analyser_meminfo_libre(texte: str) -> Optional[int]:
    """`MemAvailable` → octets. Fait DYNAMIQUE, relu à chaque verdict.

    `MemAvailable` et non `MemFree` : le noyau y estime ce qu'une allocation
    peut réellement obtenir, cache réclamable compris. `MemFree` sur une
    machine qui tourne depuis une heure est presque toujours minuscule, et
    s'en servir ferait sortir `ne_tiendra_pas` sur des modèles qui se chargent
    sans peine.

    Absent (noyau antérieur à 3.14) → `None`, donc `inconnu`. **Pas de repli
    sur `MemFree`** : ce serait remplacer une ignorance par un chiffre faux.
    """
    return _champ_meminfo(texte, "MemAvailable")


# ── Sondes réelles ───────────────────────────────────────────────────────────

def _memoire_windows() -> Optional[tuple[int, int]]:
    """`(totale, libre)` en octets via `GlobalMemoryStatusEx`. `None` si échec.

    Les deux nombres sortent du MÊME appel parce qu'ils sortent de la même
    structure — mais leurs durées de vie sont opposées, et c'est l'appelant qui
    tranche : `_ram_totale_octets` garde le premier (mis en cache),
    `_ram_libre_octets` le second (jeté aussitôt lu). Ne pas mémoriser le tuple.

    `ullAvailPhys` est bien la valeur voulue, et ce n'est pas un choix par
    défaut : mesuré sur ce poste, il TOMBE de 6,18 Gio quand `qwen2.5:7b`
    (6,44 Gio dans `/api/ps`) devient résident. Il compte donc déjà les modèles
    chargés — les soustraire en plus doublerait la note (cf. l'en-tête).
    """
    try:
        import ctypes  # noqa: PLC0415 — hors de portée des autres plateformes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        etat = _MemoryStatusEx()
        etat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(etat)):
            logger.warning("GlobalMemoryStatusEx a échoué — RAM inconnue")
            return None
        return int(etat.ullTotalPhys), int(etat.ullAvailPhys)
    except Exception:
        logger.exception("Lecture de la RAM impossible (Windows)")
        return None


def _ram_totale_octets() -> Optional[int]:
    """RAM que le SYSTÈME peut distribuer, en octets. `None` si on ne sait pas.

    Sous Windows, `GlobalMemoryStatusEx().ullTotalPhys` — la mémoire vue par
    l'OS, donc déjà amputée de ce que le micrologiciel a réservé (694 Mo ici,
    dont les 512 Mo dédiés à l'iGPU). C'est bien ce nombre-là qu'on veut, et
    non la capacité des barrettes : on ne peut pas allouer ce que le système
    n'a pas.

    Fait FIGÉ : c'est la seule des deux valeurs qui a le droit d'entrer dans le
    cache de `materiel()`.
    """
    if sys.platform == "win32":
        mem = _memoire_windows()
        return mem[0] if mem else None
    try:
        return analyser_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except Exception:
        # macOS, BSD, /proc absent : inconnu, jamais zéro.
        logger.warning("RAM totale indisponible sur %s", sys.platform)
        return None


def _ram_libre_octets() -> Optional[int]:
    """RAM réellement allouable MAINTENANT. **Jamais mise en cache.**

    C'est la valeur que le calcul de faisabilité ignorait jusqu'au 2026-09-06,
    et son absence rendait « tient » sur des modèles qui ne pouvaient pas se
    charger (cf. l'en-tête, mesure à l'appui). Elle est relue à chaque verdict :
    la mettre en cache, ne serait-ce qu'une seconde, la ramènerait au statut de
    fait figé qu'elle n'a pas.
    """
    if sys.platform == "win32":
        mem = _memoire_windows()
        return mem[1] if mem else None
    try:
        return analyser_meminfo_libre(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except Exception:
        logger.warning("RAM libre indisponible sur %s", sys.platform)
        return None


#: UNE seule définition de la commande, pour les DEUX moments qui l'appellent —
#: la détection (qui garde le total) et le verdict (qui garde le libre). Les
#: trois colonnes partent ensemble à chaque fois : c'est « une seule requête »
#: au sens qui compte, et chaque appelant jette la colonne qui ne le regarde
#: pas. Deux commandes distinctes divergeraient le jour où l'une gagne un
#: champ.
_CMD_NVIDIA_SMI = [
    "nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv",
]


def _appeler_nvidia_smi() -> Optional[str]:
    """La sortie brute de `nvidia-smi`. `None` s'il est absent, échoue, ou ment."""
    try:
        cp = subprocess.run(
            _CMD_NVIDIA_SMI, capture_output=True, timeout=_TIMEOUT_NVIDIA_SMI_S,
        )
    except FileNotFoundError:
        # Le cas NORMAL sur une machine sans carte NVIDIA. Pas un incident.
        logger.debug("nvidia-smi absent — on tentera dxdiag")
        return None
    except Exception:
        logger.warning("nvidia-smi a échoué — GPU cherché ailleurs", exc_info=True)
        return None
    if cp.returncode != 0:
        logger.warning("nvidia-smi a répondu %s — ignoré", cp.returncode)
        return None
    return cp.stdout.decode("utf-8", "replace")


def _sonder_nvidia_smi() -> Optional[dict]:
    """Le GPU et sa VRAM TOTALE — la moitié FIGÉE de la sortie."""
    sortie = _appeler_nvidia_smi()
    return analyser_nvidia_smi(sortie) if sortie is not None else None


def _sonder_vram_libre() -> Optional[int]:
    """La VRAM libre MAINTENANT — la moitié PÉRISSABLE de la même sortie.

    Relance la commande au lieu de réutiliser celle de la détection, et c'est
    tout l'intérêt : celle-là date du démarrage du process. `nvidia-smi` répond
    en moins d'une seconde, et l'appel part **une fois par requête**, jamais
    par modèle (cf. `etat_frais`).
    """
    sortie = _appeler_nvidia_smi()
    return analyser_nvidia_smi_libre(sortie) if sortie is not None else None


def _sonder_dxdiag() -> Optional[dict]:
    """dxdiag dans un temporaire, puis analyse. `None` si quoi que ce soit cloche.

    Le rapport part dans `tempfile.gettempdir()` et pas sous `backend/` : 120 ko
    dans un dossier de données feraient tomber `test_zz_donnees_reelles`, qui
    parle d'autre chose.
    """
    if sys.platform != "win32":
        return None  # dxdiag n'existe que sous Windows (le runner CI est Linux)
    racine = os.environ.get("SystemRoot")
    if not racine:
        logger.warning("SystemRoot absent — dxdiag introuvable")
        return None
    exe = Path(racine) / "System32" / "dxdiag.exe"
    if not exe.is_file():
        logger.warning("dxdiag introuvable (%s) — VRAM inconnue", exe)
        return None
    with tempfile.TemporaryDirectory(prefix="epure-dxdiag-") as tmp:
        rapport = Path(tmp) / "dxdiag.txt"
        try:
            cp = subprocess.run(
                [str(exe), "/t", str(rapport)],
                capture_output=True, timeout=_TIMEOUT_DXDIAG_S,
            )
        except Exception:
            # Y COMPRIS le timeout : mieux vaut « inconnu » qu'un fil démon
            # accroché à un dxdiag qui ne rendra jamais la main.
            logger.warning("dxdiag n'a pas abouti — VRAM inconnue", exc_info=True)
            return None
        if cp.returncode != 0 or not rapport.is_file():
            logger.warning("dxdiag a répondu %s sans rapport exploitable", cp.returncode)
            return None
        # cp1252 MESURÉ sur ce poste : ni utf-16 (casse en fin de fichier) ni
        # utf-8 (casse sur l'espace insécable de « WDDM 3.2 »).
        texte = rapport.read_text(encoding="cp1252", errors="replace")
    return analyser_dxdiag(texte)


def _gpu_inconnu() -> dict:
    """Le TROISIÈME état, celui qu'aucune branche ne doit produire par défaut.

    « Inconnu » n'est ni « pas de GPU » (qui autoriserait à conclure) ni « GPU
    illimité » (qui autoriserait à promettre). Il ne dit rien, et l'interface
    doit le dire aussi.
    """
    return {
        "nom": None,
        "vram_octets": None,
        "vram_partagee_octets": None,
        "partage_la_ram": None,
        "source": "inconnu",
    }


def _sonder_gpu() -> dict:
    """nvidia-smi d'abord, dxdiag en repli, `inconnu` si les deux se taisent."""
    for nom, sonde in (("nvidia-smi", _sonder_nvidia_smi), ("dxdiag", _sonder_dxdiag)):
        try:
            trouve = sonde()
        except Exception:
            # Le libellé est écrit ici et non tiré de `sonde.__name__` : ce
            # champ n'existe pas sur un Mock, donc un test qui fait lever la
            # première sonde échouerait DANS le log au lieu de vérifier le repli.
            logger.exception("Sonde GPU %s en échec", nom)
            continue
        if trouve:
            return trouve
    return _gpu_inconnu()


def ressource_totale(ram_octets: Optional[int], gpu: dict) -> dict:
    """Le POOL dans lequel se joue le verdict, et d'où il vient. Fait figé.

    **La VRAM ne l'emporte que si c'est un pool RÉELLEMENT séparé**, ce qu'on
    ne sait que par `nvidia-smi` (`partage_la_ram is False`). `is False` et non
    une vérité JS-style : `None` veut dire « on ne sait pas si c'est partagé »,
    et sur cette ignorance-là on retombe sur la RAM plutôt que d'affirmer un
    second pool. Justification complète dans l'en-tête du module.

    **Ce n'est PLUS le dénominateur du verdict** — c'était son rôle jusqu'au
    2026-09-06, et c'est ce qui rendait de fausses réponses. Elle nomme
    désormais le pool ET sert de référence d'affichage (« 9,5 Gio libres sur
    31,3 Gio »). Le dénominateur, lui, est `ressource_libre()`. Le nom a changé
    avec le rôle : `ressource_disponible` disait « disponible » pour parler du
    total, exactement le mot du chiffre qui manquait.
    """
    if gpu.get("vram_octets") and gpu.get("partage_la_ram") is False:
        return {"octets": gpu["vram_octets"], "origine": "vram"}
    if ram_octets:
        return {"octets": ram_octets, "origine": "ram"}
    return {"octets": None, "origine": "inconnu"}


def ressource_libre(origine: str) -> Optional[int]:
    """Ce qui est RÉELLEMENT allouable à cet instant, dans le pool `origine`.

    Le dénominateur du verdict. **Jamais mis en cache, jamais dérivé du
    total** — cf. l'en-tête, c'est le cœur du correctif du 2026-09-06.

    Sonde choisie par le pool et non par la plateforme : sur le chemin
    `nvidia-smi` (seul cas d'un pool séparé) c'est la VRAM libre de la carte,
    partout ailleurs la RAM libre du système. Interroger la RAM alors que le
    verdict se joue en VRAM donnerait un chiffre juste pour la mauvaise
    question.
    """
    if not sondes_autorisees():
        # Même porte que tout le reste du module : un test ne lance pas
        # `nvidia-smi` et ne lit pas la mémoire du poste qui l'exécute.
        return None
    if origine == "vram":
        return _sonder_vram_libre()
    if origine == "ram":
        return _ram_libre_octets()
    return None


def ressource_fraiche(etat: dict) -> dict:
    """`{"octets", "origine"}` mesuré MAINTENANT, dans le pool du matériel.

    **Aucun repli sur le total quand la lecture fraîche échoue** : `origine`
    retombe à `inconnu` et `octets` à `None`, donc tous les verdicts mémoire
    passent à `inconnu`. C'est délibéré et c'est la règle du fichier — le total
    est précisément la valeur qui donnait la mauvaise réponse, y revenir en
    silence rejouerait le bug en le faisant passer pour une dégradation
    prudente. Un `nvidia-smi` qui expire une fois rend donc `inconnu` le temps
    d'une requête, ce qui est vrai, plutôt qu'un « tient » qui ne l'est pas.
    """
    origine = (etat.get("ressource") or {}).get("origine") or "inconnu"
    octets = ressource_libre(origine)
    if not octets:
        return {"octets": None, "origine": "inconnu"}
    return {"octets": octets, "origine": origine}


def npu_joignable() -> bool:
    """FLM répond-il MAINTENANT ? Relu à chaque requête, jamais mis en cache.

    Même famille de bug que la mémoire libre, trouvée en même temps : la
    joignabilité était figée au démarrage du process, si bien qu'un FLM lancé
    APRÈS l'application restait « éteint » à vie — et l'utilisateur voyait
    `indisponible` sur tous ses modèles NPU sans qu'aucun redémarrage de FLM ne
    change quoi que ce soit. L'incohérence était déjà visible dans le code :
    `verdicts_modeles` appelait `get_flm_installed()` et `flm_model_ids()`
    frais, tout en les conditionnant à un `joignable` périmé.

    **Ne PAS dériver la joignabilité de `flm_model_ids() is not None`** pour
    économiser un appel : `check_flm` teste le code 200, `flm_model_ids` parse
    le corps. « Répond 200 avec un catalogue illisible » est un état réel et
    distinct, que le verdict traite exprès avec indulgence ; les confondre le
    ferait disparaître.
    """
    if not sondes_autorisees():
        return False
    try:
        return check_flm()
    except Exception:
        logger.exception("Sonde FLM en échec — NPU marqué absent")
        return False


def detecter() -> dict:
    """Les FAITS FIGÉS du matériel, sondés pour de bon. Voir `materiel()` pour
    la version en cache — c'est celle que tout le monde appelle.

    **`npu` n'est plus ici**, et son absence est le mécanisme, pas un oubli :
    ce que rend `detecter()` part droit dans un cache qui vit aussi longtemps
    que le process, donc rien de périssable ne doit pouvoir y entrer. La
    joignabilité de FLM et la mémoire libre sont ajoutées par `etat_frais()`,
    à chaque requête. Un futur champ dynamique posé ici serait figé sans que
    personne ne le remarque — c'est exactement ce qui est arrivé au NPU.
    """
    if not sondes_autorisees():
        # Chemin des tests : rien ne part, et le résultat est honnêtement vide.
        gpu = _gpu_inconnu()
        return {
            "ram_octets": None,
            "gpu": gpu,
            "ressource": ressource_totale(None, gpu),
        }
    ram = _ram_totale_octets()
    gpu = _sonder_gpu()
    return {
        "ram_octets": ram,
        "gpu": gpu,
        "ressource": ressource_totale(ram, gpu),
    }


# ── Cache : une seule détection pour la durée du process ─────────────────────

_cache: Optional[dict] = None
_verrou = threading.Lock()


def materiel() -> dict:
    """Les faits FIGÉS du matériel, calculés une fois pour la vie du process.

    Double contrôle autour du verrou, comme `_LazyEngine` : la requête qui
    arrive pendant le préchauffage attend, celle qui arrive après ne paie rien,
    et dxdiag ne part jamais deux fois en parallèle.

    **Ne contient AUCUNE valeur périssable** — ni mémoire libre, ni
    joignabilité de FLM (cf. `detecter`). Et le dict rendu est TOUJOURS le même
    objet : ne jamais le muter pour y greffer une valeur fraîche, la greffe
    survivrait à la requête et contaminerait toutes les suivantes. C'est
    `etat_frais()` qui en fait une copie superficielle.
    """
    global _cache
    if _cache is None:
        with _verrou:
            if _cache is None:
                _cache = detecter()
                logger.info(
                    "Matériel détecté : RAM=%s Mio, GPU=%s (%s)",
                    (_cache["ram_octets"] or 0) // _MO or "inconnue",
                    _cache["gpu"]["nom"] or "inconnu",
                    _cache["gpu"]["source"],
                )
    return _cache


def prechauffer() -> None:
    """Lance la détection en tâche de fond. Appelé par `core/runtime.py`.

    Fil DÉMON et non un appel direct : dxdiag coûte 16 s, et l'import de
    `core.runtime` est sur le chemin de démarrage d'uvicorn. Le bloquer, c'est
    rejouer l'incident qui empêchait `/health` de répondre (§3.2).

    Ne fait rien quand les sondes sont coupées : inutile de lancer un fil pour
    ne rien mesurer.
    """
    if not sondes_autorisees():
        return

    def _travail() -> None:
        try:
            materiel()
        except Exception:
            logger.exception("Préchauffage matériel échoué")

    threading.Thread(target=_travail, daemon=True, name="epure-materiel").start()


# ── Verdicts ─────────────────────────────────────────────────────────────────

def verdict_memoire(taille_octets: Optional[int], dispo_octets: Optional[int]) -> str:
    """Un modèle contre une ressource. **`INCONNU` n'est le repli de rien.**

    Taille manquante OU ressource manquante → `inconnu`, jamais l'un des trois
    autres : dire « tient » sans savoir est une promesse, dire « ne tiendra
    pas » sans savoir est un refus — les deux sont des mensonges, et
    l'utilisateur les lit comme des faits.
    """
    if not taille_octets or not dispo_octets:
        return VERDICT_INCONNU
    part = taille_octets / dispo_octets
    if part <= MARGE_CONFORT:
        return VERDICT_TIENT
    if part <= 1.0:
        return VERDICT_LIMITE
    return VERDICT_NE_TIENDRA_PAS


def verdict_flm(joignable: bool, installe: bool, au_catalogue: bool) -> str:
    """FLM ne passe PAS par le calcul mémoire : il gère sa mémoire NPU seul, et
    aucune de nos sondes ne voit ce qu'il en reste.

    Les trois conditions sont celles que `main.py:list_models` applique déjà au
    champ `disponible` d'un modèle FLM — reprises telles quelles plutôt que
    resserrées sur la seule joignabilité, sinon un modèle que FLM ne peut pas
    servir s'afficherait « disponible ».
    """
    return VERDICT_DISPONIBLE if (joignable and installe and au_catalogue) else VERDICT_INDISPONIBLE


def verdicts_modeles(etat: dict) -> list[dict]:
    """Un verdict par modèle installé, tous backends locaux confondus.

    Trois sources, trois qualités d'information — et le fichier ne prétend pas
    qu'elles se valent :

    * **Ollama** : `/api/tags` porte la taille du blob sur disque. C'est un
      proxy, pas l'empreinte résidente (le cache KV s'ajoute à l'usage) ;
      la marge de 80 % couvre en partie cet écart.
    * **LM Studio** : aucune taille. MESURÉ le 2026-09-06 sur ce poste — ni
      `/v1/models` (format OpenAI, `{id, object, owned_by}`) ni `/api/v0/models`
      (qui porte pourtant `type`, `arch`, `quantization`, `state`,
      `max_context_length`, `capabilities`) ne rendent d'octets. Verdict
      `inconnu`, donc, et surtout pas une taille devinée depuis le nom du
      modèle ou son quantum.
    * **FLM** : joignabilité, cf. `verdict_flm`. `taille_octets` reste `None` —
      `FLM_MODELS_STATIC` n'en porte aucune, et en inventer serait exactement
      la fabrication que ce module existe pour éviter.

    Les modèles CLOUD sont absents de cette liste : la mémoire de cette machine
    ne dit rien de ce qui tourne chez un fournisseur.

    **Le dénominateur est `ressource_libre`, pas `ressource`**, et cette
    fonction ne le sonde PAS elle-même : elle le lit dans l'état qu'on lui
    passe. Deux raisons, et la seconde est la vraie. La première est le coût —
    sonder ici mettrait un `nvidia-smi` par modèle, sept sur ce poste. La
    seconde : le corps servi porte aussi cette valeur, dans la ligne qui
    MOTIVE les verdicts (« calculés sur 9,5 Gio libres ») ; deux lectures
    afficheraient un motif qui ne décrit pas le calcul montré juste à côté.
    `etat_frais()` mesure une fois, tout le monde lit la même chose.

    Un état sans `ressource_libre` (appelant qui ne passe pas par
    `etat_frais`) donne `None`, donc `inconnu` partout. Dégradation honnête, et
    surtout pas un repli sur `ressource` — cf. `ressource_fraiche`.
    """
    dispo = (etat.get("ressource_libre") or {}).get("octets")
    out: list[dict] = []

    tailles = _tailles_ollama()
    for mid, taille in (tailles or {}).items():
        out.append({
            "id": mid,
            "provider": "ollama",
            "taille_octets": taille,
            "verdict": verdict_memoire(taille, dispo),
        })

    for nom in (get_lmstudio_installed() or []):
        out.append({
            "id": f"lmstudio:{nom}",
            "provider": "lmstudio",
            "taille_octets": None,
            "verdict": verdict_memoire(None, dispo),
        })

    # Relu par `etat_frais()` à chaque requête, jamais tiré du cache : un FLM
    # démarré après l'application resterait « éteint » à vie (cf.
    # `npu_joignable`).
    joignable = bool((etat.get("npu") or {}).get("disponible"))
    installes = get_flm_installed() if joignable else set()
    live = flm_model_ids() if joignable else None
    for m in FLM_MODELS_STATIC:
        court = m["id"].split("flm:", 1)[1]
        out.append({
            "id": m["id"],
            "provider": "flm",
            "taille_octets": None,
            "verdict": verdict_flm(
                joignable,
                court in installes,
                # `live is None` = FLM joignable mais son catalogue illisible :
                # on ne punit pas le modèle pour une lecture ratée. Même
                # tolérance que `premier_modele_vision_disponible()`.
                live is None or court in live,
            ),
        })

    return out


def etat_frais() -> dict:
    """Le matériel : faits figés du cache + ce qui bouge, relu à l'instant.

    **Le seul endroit où les deux durées de vie se rencontrent**, et une seule
    fois par requête. `ram_octets`, `gpu` et `ressource` sortent du cache ;
    `ressource_libre` et `npu` sont mesurés ici. Les verdicts et la ligne qui
    les motive lisent ensuite le MÊME dict, donc le même instant — deux
    mesures séparées afficheraient « calculés sur 9,5 Gio » à côté de verdicts
    calculés sur autre chose.

    **Copie superficielle, jamais une mutation** : `materiel()` rend toujours
    le même objet et le garde pour la vie du process ; un `etat["npu"] = …` y
    figerait la valeur fraîche pour toutes les requêtes suivantes, c'est-à-dire
    reproduirait très exactement le bug qu'on corrige.
    """
    etat = materiel()
    return {
        **etat,
        "npu": {"disponible": npu_joignable()},
        "ressource_libre": ressource_fraiche(etat),
    }


def etat_complet() -> dict:
    """Le corps servi par `GET /models/materiel` : matériel + verdicts.

    Les deux ensemble et non deux endpoints : un verdict sans le matériel qui
    l'a produit est un jugement sans motif, et l'interface doit pouvoir dire
    « ne tiendra pas, sur 9,5 Gio libres de 31,3 Gio de RAM » plutôt que « ne
    tiendra pas ». Les DEUX nombres sont servis pour ça : le libre est le
    dénominateur, le total est ce qui le rend lisible — « 9,5 Gio » seul ne dit
    pas si la machine est petite ou simplement occupée.
    """
    etat = etat_frais()
    return {"materiel": etat, "modeles": verdicts_modeles(etat)}
