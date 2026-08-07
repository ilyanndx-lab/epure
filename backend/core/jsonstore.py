"""Lecture/écriture des JSON de runtime (memory/, history/…), partagée par les moteurs.

Un seul point pour la règle qui a mordu (BOM dans memory_sessions.json) :
LECTURE EN utf-8-sig — un fichier écrit/édité par un outil Windows (PowerShell
5.1, éditeur) peut porter un BOM que json.loads en utf-8 strict refuse ; chaque
moteur avalait l'erreur dans son helper local → données silencieusement
invisibles, puis ÉCRASEMENT du fichier à l'écriture suivante (le moteur
repartait de son défaut). ÉCRITURE toujours en utf-8 SANS BOM.

Remplace les helpers _read/_load/_write dupliqués de memory, flashcards,
history, admin, consolidation, orchestrator, instance, module_registry,
quota_tracker — qui avaient tous le même défaut latent.

Deuxième incident, même mécanisme : l'encodage était corrigé, pas l'atomicité.
``write_text`` tronque le fichier puis le réécrit, donc un lecteur concurrent
voyait du vide ou du JSON partiel ; ``read_json`` attrapait l'exception et
renvoyait ``default``, que le moteur réécrivait ensuite — effacement silencieux.
Mesuré sur le code d'alors : 8 threads × 30 écritures → 106 lectures d'un fichier
corrompu, et 240 écritures concurrentes attendues → 2 conservées. D'où les deux
mécanismes ci-dessous : écriture atomique (tmp + replace) et verrou par fichier
(:func:`transaction` pour les read-modify-write).

⚠️ LES VERROUS SONT INTRA-PROCESSUS (``threading.RLock``). Ils sérialisent les
threads d'UN process — le pool de threads de FastAPI et les ``Thread`` explicites
de la consolidation, ce qui est le cas d'usage réel d'Épure (une instance, un
worker : Dockerfile, epure_tray.py, start.ps1). Ils ne protègent RIEN entre
plusieurs processus : **ne pas lancer uvicorn avec ``--workers > 1``** (ni
plusieurs instances sur le même dossier ``memory/``) sans passer d'abord à un
verrou de fichier — ``msvcrt.locking`` sous Windows, ``fcntl.flock`` sous POSIX.
L'écriture atomique, elle, reste correcte entre processus : un lecteur ne verra
jamais un fichier à moitié écrit, même par un autre process.
"""

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: Un verrou par fichier, créé à la demande. Jamais purgé : il y a une douzaine
#: de JSON de runtime, et retirer un verrou du dictionnaire alors qu'un thread
#: l'attend rendrait le verrou inopérant (deux threads sur deux objets
#: différents pour le même fichier).
_locks: dict[str, RLock] = {}
#: Protège la création d'entrées dans `_locks` (setdefault seul suffirait sur
#: CPython, mais on ne dépend pas du GIL pour une invariante de sécurité).
_locks_guard = RLock()


def _lock_for(path: Path) -> RLock:
    """Verrou associé à un fichier, identifié par son chemin résolu.

    Résolu et non brut : ``memory/profile.json`` et
    ``backend/memory/profile.json`` doivent partager le même verrou.
    """
    key = str(Path(path).resolve())
    with _locks_guard:
        return _locks.setdefault(key, RLock())


def read_json(path: Path | str, default: Any) -> Any:
    """JSON du fichier, ou ``default`` si absent (silencieux) ou illisible (logué).

    ``default`` est renvoyé TEL QUEL (pas copié) : passer un littéral au site
    d'appel (``read_json(p, {"decks": []})``), jamais une constante partagée
    mutable.

    Prend le verrou du fichier, et pas seulement pour la cohérence logique :
    sous Windows, ``os.replace`` échoue en ``PermissionError`` si la CIBLE est
    ouverte par quelqu'un d'autre (``MoveFileEx`` refuse sans
    ``FILE_SHARE_DELETE``, que l'``open()`` de CPython ne demande pas). Un simple
    ``get_context`` concurrent faisait donc échouer l'``update_context`` d'un
    autre thread. Sérialiser lecteurs et écrivains du même fichier supprime le cas
    à l'intérieur du process ; le reste (antivirus, éditeur ouvert sur memory/)
    est couvert par les ré-essais de :func:`_replace_with_retry`.
    """
    p = Path(path)
    if not p.exists():
        return default
    with _lock_for(p):
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            logger.exception("Erreur lecture %s", p)
            return default


def _replace_with_retry(tmp: Path, dest: Path, attempts: int = 20, pause: float = 0.01) -> None:
    """``os.replace`` avec ré-essais courts — nécessaire sous Windows.

    ``MoveFileEx`` refuse de remplacer un fichier ouvert par quelqu'un qui n'a pas
    demandé ``FILE_SHARE_DELETE`` (ce que ne fait pas l'``open()`` de CPython) :
    l'appel remonte alors en ``PermissionError`` (WinError 5). Le cas a été observé
    pour de vrai dans test_jsonstore_concurrency, avec des lecteurs en lecture
    brute. Nos propres threads ne peuvent plus le déclencher (``read_json`` prend
    le verrou), mais un antivirus qui scanne ``memory/``, un éditeur laissé ouvert
    ou un script externe peuvent — et une simple màj du contexte de session ne doit
    pas remonter en 500 à cause de ça.

    Au pire 200 ms d'attente : un lecteur ne garde le fichier ouvert que le temps
    d'un ``read_text``. Passé les essais, l'erreur est propagée — mieux vaut
    échouer visiblement que boucler.
    """
    for reste in range(attempts - 1, -1, -1):
        try:
            tmp.replace(dest)
            return
        except PermissionError:
            if reste == 0:
                logger.warning(
                    "Remplacement de %s refusé après %d essais (fichier ouvert ailleurs ?)",
                    dest, attempts,
                )
                raise
            time.sleep(pause)


def write_json(path: Path | str, data: Any) -> None:
    """Écrit ``data`` en JSON (utf-8 sans BOM, indenté), dossiers créés au besoin.

    Écriture ATOMIQUE : sérialisation dans un ``.tmp`` voisin puis ``replace()``,
    qui est atomique sur NTFS comme sur POSIX. Un lecteur concurrent voit donc
    l'ancien fichier ou le nouveau, jamais un fichier tronqué.

    La sérialisation vient AVANT toute modification du fichier cible : si
    ``json.dumps`` lève (données non sérialisables), l'original est intact et
    seul le ``.tmp`` traîne.

    Le verrou est pris ici aussi (le nom du ``.tmp`` est partagé : deux écritures
    simultanées du même fichier se marcheraient dessus dans le tmp), ce qui rend
    au passage l'ordre de deux écritures concurrentes déterministe au lieu
    d'entrelacé.

    Ne masque pas les erreurs : les appelants qui veulent une écriture
    best-effort gardent leur try/except (et leur message contextualisé).

    Note d'honnêteté : atomique vis-à-vis des lecteurs concurrents, pas
    vis-à-vis d'une coupure de courant (pas de ``fsync`` — ce serait payé sur
    chaque màj de contexte de session, très fréquente).
    """
    p = Path(path)
    with _lock_for(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        # with_name et non with_suffix : `conversations.json` → `.json.tmp`, mais
        # with_suffix écraserait le suffixe d'un nom à points multiples.
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        _replace_with_retry(tmp, p)


@contextmanager
def transaction(path: Path | str, default: Any) -> Iterator[Any]:
    """Charge, cède la main, réécrit — sous verrou.

    LE SEUL CHEMIN CORRECT pour un read-modify-write sur un JSON de runtime.
    Écrit ``read_json`` + modification + ``write_json`` à la main laisse une
    fenêtre entre les deux : deux threads chargent la même version, et le second
    écrase la modification du premier (le compteur mesuré : 240 écritures
    attendues, 2 conservées).

        with transaction(_INDEX_FILE, {"conversations": []}) as doc:
            doc.setdefault("conversations", []).insert(0, entry)

    ⚠️ C'est l'objet cédé qui est réécrit : le modifier EN PLACE
    (``doc["k"] = v``, ``lst.append(...)``, ``lst[:] = [...]``). Le rebinder
    (``doc = {...}``, ``lst = [x for x in lst if …]``) ne persiste rien.

    Si le corps lève, rien n'est écrit (pas de ``finally``) : une modification à
    moitié appliquée ne doit pas atteindre le disque.
    """
    p = Path(path)
    with _lock_for(p):
        data = read_json(p, default)
        yield data
        write_json(p, data)
