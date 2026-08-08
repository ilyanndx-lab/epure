#!/usr/bin/env python3
"""Atomicité et verrous de core/jsonstore.py (durcissement v1, lot 4).

Aucun attaquant ici : c'est de la perte de données en usage normal. FastAPI
exécute les handlers synchrones dans un pool de threads, et le code lance en plus
des `Thread` explicites (consolidation depuis kholle et chat), donc les écritures
sont réellement concurrentes.

Les deux défauts mesurés sur le code d'avant ce lot :

  - `write_text` tronque puis réécrit → un lecteur voit du JSON partiel,
    `read_json` renvoie `default`, le moteur réécrit son défaut : effacement
    silencieux. 8 threads × 30 écritures → 106 lectures corrompues.
  - read-modify-write sans verrou : deux threads chargent la même version, le
    second écrase le premier. 240 écritures attendues → 2 conservées.

Ces tests doivent rester RAPIDES (< 5 s) pour tenir dans le job CI léger : pas de
sleep, des boucles bornées par un compteur et une seule fenêtre de 1 s pour le
test lecteurs/écrivains.

Usage :
    python test_jsonstore_concurrency.py
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# Permettre d'importer le package `core` depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.jsonstore import read_json, transaction, write_json  # noqa: E402


class _TmpFileCase(unittest.TestCase):
    """Chaque test travaille dans son propre dossier temporaire."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "data.json"


class TransactionCountersTest(_TmpFileCase):
    """Le cas de référence du lot : un compteur incrémenté en parallèle."""

    def test_8_threads_50_increments(self):
        write_json(self.path, {"n": 0})

        def worker():
            for _ in range(50):
                with transaction(self.path, {"n": 0}) as data:
                    data["n"] = data.get("n", 0) + 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(read_json(self.path, {}).get("n"), 400)

    def test_appends_concurrents_ne_se_perdent_pas(self):
        """Variante liste : c'est la forme des sites convertis (log, index)."""
        def worker(k: int):
            for i in range(30):
                with transaction(self.path, []) as items:
                    items.append(f"{k}-{i}")

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        items = read_json(self.path, [])
        self.assertEqual(len(items), 240)
        self.assertEqual(len(set(items)), 240, "des entrées ont été écrasées")

    def test_le_verrou_serialise_vraiment(self):
        """Sans verrou, ce read-modify-write perdrait des écritures : on force
        l'entrelacement en cédant la main au milieu de la transaction."""
        write_json(self.path, {"n": 0})

        def worker():
            for _ in range(20):
                with transaction(self.path, {"n": 0}) as data:
                    n = data.get("n", 0)
                    time.sleep(0)      # laisse le scheduler basculer de thread
                    data["n"] = n + 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(read_json(self.path, {}).get("n"), 80)


class ReadersNeverSeePartialTest(_TmpFileCase):
    """4 écrivains + 4 lecteurs : zéro lecture d'un JSON invalide.

    Le compteur ne regarde pas les retours de `read_json` (qui masque l'erreur en
    renvoyant `default` — c'est précisément le mécanisme de l'effacement
    silencieux) mais le fichier BRUT : toute lecture doit être du JSON valide de
    la forme attendue.

    Les lecteurs lisent EXPRÈS sans passer par `read_json`, donc sans prendre le
    verrou : ils jouent le rôle d'un lecteur externe (antivirus, éditeur ouvert
    sur memory/). C'est ce qui a révélé que `os.replace` échoue sous Windows
    quand la cible est ouverte ailleurs — d'où l'assertion sur les erreurs des
    écrivains, qui échouerait si `_replace_with_retry` disparaissait.

    La pause d'1 ms entre deux lectures n'est pas cosmétique : en boucle serrée,
    4 lecteurs gardent le fichier ouvert ~100 % du temps et AUCUN nombre de
    ré-essais ne suffit (mesuré). C'est une limite Windows assumée, pas un bug à
    corriger ici : un lecteur externe lit par à-coups, il ne spinne pas.
    """

    def test_une_seconde_de_lectures_concurrentes(self):
        write_json(self.path, {"payload": "x" * 5000, "n": 0})
        stop = threading.Event()
        corrompues: list[str] = []
        erreurs_ecriture: list[str] = []
        lectures = [0]
        verrou_compteur = threading.Lock()

        def writer(k: int):
            i = 0
            while not stop.is_set():
                try:
                    # Charge utile volumineuse : agrandit la fenêtre pendant
                    # laquelle un write_text non atomique laisserait un fichier
                    # tronqué.
                    write_json(self.path, {"payload": "x" * 5000, "n": i, "w": k})
                except Exception as exc:
                    erreurs_ecriture.append(f"{type(exc).__name__}: {exc}")
                i += 1

        def reader():
            n = 0
            while not stop.is_set():
                time.sleep(0.001)   # lecteur externe réaliste, cf. docstring
                try:
                    raw = self.path.read_text(encoding="utf-8-sig")
                except OSError:
                    # Ouverture refusée pendant un replace : pas une lecture
                    # corrompue, aucun contenu partiel n'a été observé.
                    continue
                n += 1
                try:
                    doc = json.loads(raw)
                except Exception as exc:
                    corrompues.append(f"{type(exc).__name__}: {raw[:60]!r}")
                    continue
                if not isinstance(doc, dict) or "payload" not in doc:
                    corrompues.append(f"document inattendu : {str(doc)[:60]}")
            with verrou_compteur:
                lectures[0] += n

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join()

        self.assertEqual(corrompues[:5], [], f"{len(corrompues)} lectures corrompues")
        self.assertEqual(
            erreurs_ecriture[:5], [],
            f"{len(erreurs_ecriture)} écritures ont échoué (replace refusé ?)",
        )
        self.assertGreater(lectures[0], 100, "le test n'a rien lu — résultat non concluant")


class InterruptedWriteTest(_TmpFileCase):
    """Une écriture qui échoue ne doit pas détruire le fichier d'origine."""

    def test_dumps_qui_leve_laisse_l_original_intact(self):
        write_json(self.path, {"important": True})
        avant = self.path.read_text(encoding="utf-8")

        with mock.patch("core.jsonstore.json.dumps", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                write_json(self.path, {"important": False})

        self.assertEqual(self.path.read_text(encoding="utf-8"), avant)
        self.assertEqual(read_json(self.path, {}), {"important": True})

    def test_objet_non_serialisable(self):
        """Le cas réel : un objet que json ne sait pas encoder."""
        write_json(self.path, {"important": True})
        with self.assertRaises(TypeError):
            write_json(self.path, {"bad": object()})
        self.assertEqual(read_json(self.path, {}), {"important": True})

    def test_seul_le_tmp_traine(self):
        """Le résidu attendu est un `.tmp` voisin, pas un fichier cible tronqué."""
        write_json(self.path, {"important": True})
        with mock.patch("core.jsonstore.Path.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                write_json(self.path, {"important": False})
        self.assertEqual(read_json(self.path, {}), {"important": True})
        self.assertTrue(self.path.with_name(self.path.name + ".tmp").exists())

    def test_corps_de_transaction_qui_leve_n_ecrit_rien(self):
        write_json(self.path, {"n": 1})
        with self.assertRaises(RuntimeError):
            with transaction(self.path, {"n": 0}) as data:
                data["n"] = 99
                raise RuntimeError("boom")
        self.assertEqual(read_json(self.path, {}).get("n"), 1)


class LockIdentityTest(_TmpFileCase):
    """Le verrou est indexé par chemin RÉSOLU, et il est réentrant."""

    def test_meme_verrou_pour_chemin_relatif_et_absolu(self):
        from core import jsonstore

        cwd = os.getcwd()
        os.chdir(self.path.parent)
        self.addCleanup(os.chdir, cwd)
        self.assertIs(
            jsonstore._lock_for(Path("data.json")),
            jsonstore._lock_for(self.path),
        )

    def test_transaction_reentrante_dans_le_meme_thread(self):
        """write_json prend le même verrou que transaction : un RLock est
        indispensable, un Lock provoquerait un interblocage immédiat."""
        write_json(self.path, {"n": 0})
        with transaction(self.path, {"n": 0}) as data:
            data["n"] = 1
            write_json(self.path, {"n": 42})      # ré-entrée dans le verrou
            self.assertEqual(read_json(self.path, {}).get("n"), 42)
        self.assertEqual(read_json(self.path, {}).get("n"), 1)


class EncodingTest(_TmpFileCase):
    """Non-régression de l'incident d'origine du module : le BOM."""

    def test_bom_toujours_tolere_en_lecture(self):
        self.path.write_text('{"n": 7}', encoding="utf-8-sig")
        self.assertEqual(read_json(self.path, {}).get("n"), 7)

    def test_ecriture_sans_bom(self):
        write_json(self.path, {"clé": "accentué é"})
        octets = self.path.read_bytes()
        self.assertFalse(octets.startswith(b"\xef\xbb\xbf"), "BOM écrit")
        self.assertIn("accentué é", octets.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
