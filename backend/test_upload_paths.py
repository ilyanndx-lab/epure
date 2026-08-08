#!/usr/bin/env python3
"""Confinement des uploads et des chemins clients (durcissement v1, lots 3.2 et 3.3).

`dossier / filename` n'est pas une composition anodine quand `filename` vient du
client : `Path("/a") / "/etc/passwd"` vaut `/etc/passwd` (un nom absolu remplace
la base) et `../../x` sort du dossier.

Sur `/files/upload` la conséquence allait plus loin qu'une écriture arbitraire :
`.json` est un type supporté (légitimement, pour les fiches), donc un envoi nommé
`../../backend/memory/instance_config.json` réécrivait la configuration
d'instance — **donc le token d'API**, avec une valeur choisie par l'attaquant.

Sont aussi couverts les deux points d'entrée symétriques en lecture
(`/files/load`, `/docanalysis/load`), où un chemin client faisait entrer
n'importe quel fichier du disque dans le RAG et dans le résumé renvoyé.

Prudence dans ce fichier : les dossiers de destination sont TOUS redirigés vers
un dossier temporaire. Une régression du code testé ferait sinon réellement
écraser `backend/memory/instance_config.json` en lançant les tests.

Usage :
    python test_upload_paths.py
"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Permettre d'importer le package `core` depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core import paths as core_paths  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.paths import PathOutsideDataError, resolve_user_path, safe_upload_name  # noqa: E402
from modules.docs import router as docs_router  # noqa: E402
from modules.settings import router as settings_router  # noqa: E402

#: Le fichier qui contient le token d'API — la cible réelle de 3.3.
_INSTANCE_CONFIG = Path(__file__).parent / "memory" / "instance_config.json"

#: Noms d'upload qui ne doivent jamais être acceptés tels quels.
#: `..\\..\\evil.json` est le cas piège : sous Linux l'antislash n'est pas un
#: séparateur, donc un `Path(...).name` naïf le laisserait passer en CI et
#: créerait un fichier dont le nom traverse dès qu'il est relu sous Windows.
NOMS_INVALIDES = (
    "../../evil.json",
    "../../backend/memory/instance_config.json",
    "/etc/evil.json",
    "..\\..\\evil.json",
    "..\\..\\backend\\memory\\instance_config.json",
    "C:evil.json",
    "sous/dossier/fiche.pdf",
    "..",
)


def _client() -> TestClient:
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 54321))


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "absent"


class SafeUploadNameTest(unittest.TestCase):
    """Le helper partagé par les deux endpoints d'upload."""

    def test_nom_simple_conserve(self):
        self.assertEqual(safe_upload_name("fiche.pdf", "upload.bin"), "fiche.pdf")

    def test_nom_avec_espaces_et_accents(self):
        self.assertEqual(
            safe_upload_name("Chapitre 3 — séries entières.pdf", "upload.bin"),
            "Chapitre 3 — séries entières.pdf",
        )

    def test_nom_vide_prend_le_defaut(self):
        for vide in ("", "   ", None):
            with self.subTest(nom=vide):
                self.assertEqual(safe_upload_name(vide, "upload.bin"), "upload.bin")

    def test_noms_invalides_refuses(self):
        for nom in NOMS_INVALIDES:
            with self.subTest(nom=nom), self.assertRaises(PathOutsideDataError):
                safe_upload_name(nom, "upload.bin")

    def test_antislash_refuse_aussi_sous_linux(self):
        """Le cas qui distingue ntpath de Path : doit échouer sur les 2 OS."""
        with self.assertRaises(PathOutsideDataError):
            safe_upload_name(r"..\..\evil.json", "upload.bin")


class ResolveUserPathTest(unittest.TestCase):
    """Confinement des chemins acceptés en lecture."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(core_paths, "user_data_roots", return_value=[self.racine])
        p.start()
        self.addCleanup(p.stop)

    def test_fichier_sous_une_racine(self):
        cible = self.racine / "fiche.pdf"
        self.assertEqual(resolve_user_path(str(cible)), cible)

    def test_hors_racines(self):
        for chemin in (str(_INSTANCE_CONFIG), str(self.racine / ".." / "evil.json"), "/etc/passwd"):
            with self.subTest(chemin=chemin), self.assertRaises(PathOutsideDataError):
                resolve_user_path(chemin)


class RacinesReellesTest(unittest.TestCase):
    """Sans patch : ce que `user_data_roots()` couvre vraiment sur ce poste."""

    def test_le_fichier_du_token_n_est_sous_aucune_racine(self):
        for root in core_paths.user_data_roots():
            self.assertFalse(
                _INSTANCE_CONFIG.resolve().is_relative_to(root),
                f"{root} couvre instance_config.json",
            )

    def test_les_racines_attendues_sont_presentes(self):
        from core.instance import fiches_root

        roots = core_paths.user_data_roots()
        self.assertIn(fiches_root().expanduser().resolve(), roots)
        self.assertIn(core_paths.resolve_workspace(), roots)
        self.assertIn(core_paths.DOC_UPLOADS_DIR.resolve(), roots)


async def _stub_stream(*_a, **_k):
    """Remplace les flux SSE d'indexation : le RAG (torch + chromadb) et
    l'analyse documentaire n'ont rien à faire dans un test de chemin."""
    yield "data: {}\n\n"


class UploadFichesTest(unittest.TestCase):
    """POST /files/upload — l'écriture qui menait au token d'API."""

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self._tmp.name).resolve() / "fiches"
        self.racine.mkdir()
        self.addCleanup(self._tmp.cleanup)
        # Leurre : joue le rôle d'instance_config.json, un cran au-dessus de la
        # racine des fiches. Une régression le réécrirait — sans toucher au vrai.
        self.leurre = self.racine.parent / "leurre.json"
        self.leurre.write_text('{"intact": true}', encoding="utf-8")
        self.leurre_hash = _hash(self.leurre)

        p = mock.patch.object(settings_router, "fiches_root", return_value=self.racine)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(settings_router, "_stream_load_sse", _stub_stream)
        s.start()
        self.addCleanup(s.stop)

    def _upload(self, nom: str, contenu: bytes = b"%PDF-1.4 x"):
        return self.client.post(
            "/files/upload",
            files={"files": (nom, contenu, "application/octet-stream")},
            headers=self.headers,
        )

    def test_nom_legitime_ecrit_dans_la_racine(self):
        res = self._upload("fiche.pdf")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue((self.racine / "fiche.pdf").is_file())

    def test_noms_traversants_refuses(self):
        avant = _hash(_INSTANCE_CONFIG)
        for nom in NOMS_INVALIDES:
            with self.subTest(nom=nom):
                res = self._upload(nom, b'{"auth": {"token": "vole"}}')
                self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(_hash(self.leurre), self.leurre_hash, "le leurre a été réécrit")
        self.assertEqual(_hash(_INSTANCE_CONFIG), avant, "instance_config.json a été réécrit !")

    def test_aucun_fichier_hors_de_la_racine(self):
        for nom in NOMS_INVALIDES:
            self._upload(nom)
        hors = [p for p in self.racine.parent.rglob("*") if p.is_file() and p != self.leurre]
        self.assertEqual(
            [p for p in hors if not p.is_relative_to(self.racine)], [],
            "un fichier a été écrit hors de la racine des fiches",
        )

    def test_extension_non_supportee_toujours_refusee(self):
        """Non-régression du comportement d'origine (400 explicite)."""
        res = self._upload("virus.exe")
        self.assertEqual(res.status_code, 400, res.text)


class UploadDocsTest(unittest.TestCase):
    """POST /docanalysis/upload — même motif, autre dossier."""

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.uploads = Path(self._tmp.name).resolve() / "doc_uploads"
        self.uploads.mkdir()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(docs_router, "_DOC_UPLOADS", self.uploads)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(docs_router, "_stream_docload", _stub_stream)
        s.start()
        self.addCleanup(s.stop)

    def _upload(self, nom: str):
        return self.client.post(
            "/docanalysis/upload",
            files={"file": (nom, b"%PDF-1.4 x", "application/pdf")},
            headers=self.headers,
        )

    def test_nom_legitime(self):
        res = self._upload("cours.pdf")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue((self.uploads / "cours.pdf").is_file())

    def test_noms_traversants_refuses(self):
        for nom in NOMS_INVALIDES:
            with self.subTest(nom=nom):
                self.assertEqual(self._upload(nom).status_code, 400)
        hors = [p for p in self.uploads.parent.rglob("*") if p.is_file()]
        self.assertEqual(
            [p for p in hors if not p.is_relative_to(self.uploads)], [],
            "un fichier a été écrit hors de doc_uploads",
        )


class LectureParCheminTest(unittest.TestCase):
    """Les deux endpoints qui LISENT un chemin fourni par le client."""

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self._tmp.name).resolve()
        self.fiche = self.racine / "fiche.pdf"
        self.fiche.write_bytes(b"%PDF-1.4 x")
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(core_paths, "user_data_roots", return_value=[self.racine])
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(settings_router, "_stream_load_sse", _stub_stream)
        s.start()
        self.addCleanup(s.stop)
        d = mock.patch.object(docs_router, "_stream_docload", _stub_stream)
        d.start()
        self.addCleanup(d.stop)

    def test_files_load_accepte_un_chemin_sous_une_racine(self):
        res = self.client.post("/files/load", json={"paths": [str(self.fiche)]},
                               headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)

    def test_files_load_refuse_le_fichier_du_token(self):
        res = self.client.post("/files/load", json={"paths": [str(_INSTANCE_CONFIG)]},
                               headers=self.headers)
        self.assertEqual(res.status_code, 403, res.text)

    def test_files_load_refuse_le_lot_entier(self):
        """Un seul chemin hors périmètre suffit à refuser la requête : sinon il
        suffit de le glisser au milieu de chemins légitimes."""
        res = self.client.post(
            "/files/load",
            json={"paths": [str(self.fiche), str(_INSTANCE_CONFIG)]},
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 403, res.text)

    def test_docanalysis_load_refuse_un_chemin_hors_perimetre(self):
        res = self.client.post("/docanalysis/load", json={"path": str(_INSTANCE_CONFIG)},
                               headers=self.headers)
        self.assertEqual(res.status_code, 403, res.text)

    def test_docanalysis_load_accepte_un_chemin_legitime(self):
        res = self.client.post("/docanalysis/load", json={"path": str(self.fiche)},
                               headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)


if __name__ == "__main__":
    unittest.main()
