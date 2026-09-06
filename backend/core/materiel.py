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

**Calculé UNE FOIS pour la durée du process.** Aucun rafraîchissement, aucun
endpoint pour le forcer : la RAM d'une machine ne change pas pendant qu'elle
tourne, et 16 s de sous-processus par ouverture de panneau seraient
absurdes. Le calcul ne s'invite jamais sur le chemin de démarrage d'uvicorn
(§3.2 — c'est l'incident `RAGEngine` qui empêchait `/health` de répondre) : un
fil démon le fait en tâche de fond, et l'endpoint le calcule lui-même, sous
verrou et dans un exécuteur, si la requête arrive avant que le préchauffage
n'ait abouti.

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
#: `ne_tiendra_pas` reste, lui, une information sûre : si les poids seuls ne
#: rentrent pas, rien ne rentrera.
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

def analyser_nvidia_smi(sortie: str) -> Optional[dict]:
    """`name, memory.total [MiB]` en CSV → nom + VRAM, ou `None` si illisible.

    Le PREMIER GPU listé, pas le plus gros : sur une machine à deux cartes, la
    0 est celle qu'utilisent les runtimes par défaut. Une valeur `[N/A]`
    (pilote en cours d'installation) laisse `vram_octets` à `None` en gardant le
    nom — on sait qu'il y a une carte, on ne sait pas ce qu'elle a.
    """
    for ligne in sortie.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.lower().startswith("name"):
            continue  # en-tête CSV
        parts = [p.strip() for p in ligne.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        m = re.search(r"(\d+)", parts[1])
        return {
            "nom": parts[0],
            "vram_octets": int(m.group(1)) * _MO if m else None,
            "vram_partagee_octets": None,
            # Une carte discrète a sa propre mémoire. C'est le SEUL cas où on
            # sait que le pool est séparé, et donc le seul où il sert de
            # dénominateur (cf. l'en-tête du module).
            "partage_la_ram": False,
            "source": "nvidia-smi",
        }
    return None


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


def analyser_meminfo(texte: str) -> Optional[int]:
    """`MemTotal:  32943 kB` de `/proc/meminfo` → octets. Sous Linux (la CI)."""
    m = re.search(r"^MemTotal:\s+(\d+)\s*kB", texte, re.MULTILINE)
    return int(m.group(1)) * 1024 if m else None


# ── Sondes réelles ───────────────────────────────────────────────────────────

def _ram_totale_octets() -> Optional[int]:
    """RAM que le SYSTÈME peut distribuer, en octets. `None` si on ne sait pas.

    Sous Windows, `GlobalMemoryStatusEx().ullTotalPhys` — la mémoire vue par
    l'OS, donc déjà amputée de ce que le micrologiciel a réservé (694 Mo ici,
    dont les 512 Mo dédiés à l'iGPU). C'est bien ce nombre-là qu'on veut, et
    non la capacité des barrettes : on ne peut pas allouer ce que le système
    n'a pas.
    """
    if sys.platform == "win32":
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
            return int(etat.ullTotalPhys)
        except Exception:
            logger.exception("Lecture de la RAM impossible (Windows)")
            return None
    try:
        return analyser_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except Exception:
        # macOS, BSD, /proc absent : inconnu, jamais zéro.
        logger.warning("RAM totale indisponible sur %s", sys.platform)
        return None


def _sonder_nvidia_smi() -> Optional[dict]:
    """`nvidia-smi` s'il est là. `None` s'il est absent, échoue, ou ment."""
    try:
        cp = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv"],
            capture_output=True, timeout=_TIMEOUT_NVIDIA_SMI_S,
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
    return analyser_nvidia_smi(cp.stdout.decode("utf-8", "replace"))


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


def ressource_disponible(ram_octets: Optional[int], gpu: dict) -> dict:
    """Le dénominateur du verdict, et d'où il vient.

    **La VRAM ne l'emporte que si c'est un pool RÉELLEMENT séparé**, ce qu'on
    ne sait que par `nvidia-smi` (`partage_la_ram is False`). `is False` et non
    une vérité JS-style : `None` veut dire « on ne sait pas si c'est partagé »,
    et sur cette ignorance-là on retombe sur la RAM plutôt que d'affirmer un
    second pool. Justification complète dans l'en-tête du module.
    """
    if gpu.get("vram_octets") and gpu.get("partage_la_ram") is False:
        return {"octets": gpu["vram_octets"], "origine": "vram"}
    if ram_octets:
        return {"octets": ram_octets, "origine": "ram"}
    return {"octets": None, "origine": "inconnu"}


def detecter() -> dict:
    """L'état matériel, sondé pour de bon. Voir `materiel()` pour la version en
    cache — c'est celle que tout le monde appelle."""
    if not sondes_autorisees():
        # Chemin des tests : rien ne part, et le résultat est honnêtement vide.
        gpu = _gpu_inconnu()
        return {
            "ram_octets": None,
            "gpu": gpu,
            "npu": {"disponible": False},
            "ressource": ressource_disponible(None, gpu),
        }
    ram = _ram_totale_octets()
    gpu = _sonder_gpu()
    # NPU : aucune sonde système. Le signal EST la joignabilité de FLM, et
    # `check_flm()` (core/models.py) la porte déjà pour `/models` — une seconde
    # implémentation divergerait du jour où l'une des deux changerait de port.
    npu = False
    try:
        npu = check_flm()
    except Exception:
        logger.exception("Sonde FLM en échec — NPU marqué absent")
    return {
        "ram_octets": ram,
        "gpu": gpu,
        "npu": {"disponible": npu},
        "ressource": ressource_disponible(ram, gpu),
    }


# ── Cache : une seule détection pour la durée du process ─────────────────────

_cache: Optional[dict] = None
_verrou = threading.Lock()


def materiel() -> dict:
    """L'état matériel, calculé une fois et gardé pour la vie du process.

    Double contrôle autour du verrou, comme `_LazyEngine` : la requête qui
    arrive pendant le préchauffage attend, celle qui arrive après ne paie rien,
    et dxdiag ne part jamais deux fois en parallèle.
    """
    global _cache
    if _cache is None:
        with _verrou:
            if _cache is None:
                _cache = detecter()
                logger.info(
                    "Matériel détecté : RAM=%s Mio, GPU=%s (%s), NPU=%s",
                    (_cache["ram_octets"] or 0) // _MO or "inconnue",
                    _cache["gpu"]["nom"] or "inconnu",
                    _cache["gpu"]["source"],
                    _cache["npu"]["disponible"],
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
    """
    dispo = etat.get("ressource", {}).get("octets")
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

    joignable = bool(etat.get("npu", {}).get("disponible"))
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


def etat_complet() -> dict:
    """Le corps servi par `GET /models/materiel` : matériel + verdicts.

    Les deux ensemble et non deux endpoints : un verdict sans le matériel qui
    l'a produit est un jugement sans motif, et l'interface doit pouvoir dire
    « ne tiendra pas, sur 31 Gio de RAM » plutôt que « ne tiendra pas ».
    """
    etat = materiel()
    return {"materiel": etat, "modeles": verdicts_modeles(etat)}
