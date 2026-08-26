#!/usr/bin/env python3
"""Tests de core.jsonstore (lecture/écriture JSON runtime partagée).

Non-régression du bug BOM : un memory_sessions.json écrit avec BOM par un
outil Windows rendait toute la mémoire de session silencieusement invisible
(json.loads utf-8 strict → exception avalée → défaut), puis le fichier était
écrasé à l'écriture suivante. read_json doit tolérer le BOM ; write_json ne
doit jamais en produire.

Usage :
    python test_jsonstore.py
"""

import json
import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core.jsonstore import read_json, transaction, write_json


class JsonStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_bom(self):
        """Fichier avec BOM UTF-8 (PowerShell 5.1, éditeurs Windows) → lu, pas le défaut."""
        p = self.dir / "bom.json"
        p.write_bytes(b'\xef\xbb\xbf{"sessions": [{"date": "2026-07-01"}]}')
        data = read_json(p, {"sessions": []})
        self.assertEqual(len(data["sessions"]), 1)

    def test_read_sans_bom(self):
        p = self.dir / "plain.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(read_json(p, None), {"a": 1})

    def test_fichier_absent_rend_defaut(self):
        self.assertEqual(read_json(self.dir / "absent.json", {"decks": []}), {"decks": []})

    def test_fichier_corrompu_rend_defaut(self):
        p = self.dir / "corrompu.json"
        p.write_text("pas du json {", encoding="utf-8")
        self.assertEqual(read_json(p, []), [])

    def test_write_sans_bom_et_relisible(self):
        p = self.dir / "sub" / "out.json"  # dossier créé au besoin
        write_json(p, {"clé": "café ↦ é"})
        raw = p.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "write_json ne doit pas émettre de BOM")
        self.assertEqual(read_json(p, None), {"clé": "café ↦ é"})

    def test_roundtrip_ecrase_bom_existant(self):
        """Réécrire un fichier qui portait un BOM le normalise (utf-8 sans BOM)."""
        p = self.dir / "was_bom.json"
        p.write_bytes(b'\xef\xbb\xbf{"v": 1}')
        data = read_json(p, {})
        data["v"] = 2
        write_json(p, data)
        self.assertFalse(p.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"v": 2})


class FsyncTest(unittest.TestCase):
    """``fsync`` opt-in — la durabilité se choisit PAR FICHIER.

    Ajouté pour les conversations, qui deviennent le magasin vivant du chat :
    du contenu produit par l'utilisateur, que rien ne reconstruit, écrit à chaque
    tour d'assistant. Le défaut reste ``False`` parce que le site le plus chaud
    du dépôt est ``update_context``, appelé à chaque message pour un
    ``context_session.json`` que ``MemoryEngine.__init__`` réinitialise de toute
    façon au démarrage.

    On observe l'appel système plutôt que ses effets : vérifier une vraie
    durabilité demanderait de couper le courant. Ce qui est éprouvé ici, c'est
    donc que le drapeau **atteint** ``os.fsync`` — et surtout qu'il ne l'appelle
    pas quand personne ne l'a demandé.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.appels: list[int] = []
        vrai = os.fsync

        def espion(fd):
            self.appels.append(fd)
            return vrai(fd)

        os.fsync = espion
        self.addCleanup(setattr, os, "fsync", vrai)

    def test_defaut_sans_fsync(self):
        """Le comportement historique, et celui de tous les appelants sauf un."""
        write_json(self.dir / "a.json", {"v": 1})
        self.assertEqual(self.appels, [])

    def test_fsync_vrai_synchronise(self):
        write_json(self.dir / "b.json", {"v": 1}, fsync=True)
        self.assertEqual(len(self.appels), 1)

    def test_le_contenu_reste_correct_dans_les_deux_cas(self):
        """Le drapeau ne doit rien changer d'observable au fichier produit.

        `write_json` est passé de ``write_text`` à un ``open()`` explicite pour
        obtenir le descripteur que réclame ``os.fsync`` — donc il faut vérifier
        que l'encodage et l'absence de BOM ont survécu au changement.
        """
        for drapeau in (False, True):
            with self.subTest(fsync=drapeau):
                p = self.dir / f"c{drapeau}.json"
                write_json(p, {"clé": "café ↦ é"}, fsync=drapeau)
                self.assertFalse(p.read_bytes().startswith(b"\xef\xbb\xbf"))
                self.assertEqual(read_json(p, None), {"clé": "café ↦ é"})

    def test_transaction_propage_le_drapeau(self):
        """Sinon la garantie dépendrait de la FORME de l'appel, pas du fichier."""
        p = self.dir / "d.json"
        write_json(p, {"v": 1})
        self.appels.clear()

        with transaction(p, {}, fsync=True) as doc:
            doc["v"] = 2
        self.assertEqual(len(self.appels), 1)
        self.assertEqual(read_json(p, None), {"v": 2})

    def test_transaction_sans_drapeau_ne_synchronise_pas(self):
        p = self.dir / "e.json"
        with transaction(p, {}) as doc:
            doc["v"] = 1
        self.assertEqual(self.appels, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
