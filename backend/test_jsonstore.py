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

from core.jsonstore import read_json, write_json


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


if __name__ == "__main__":
    unittest.main(verbosity=1)
