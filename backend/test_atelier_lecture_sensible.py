"""L'Atelier ne donne jamais à lire un fichier sensible, quelle que soit l'écriture du chemin.

L'INCIDENT (2026-09-25, audit des noms courts 8.3). ``module_workshop._read_is_safe``
décidait sur la CHAÎNE du chemin : une liste de motifs (``\\.env``, ``/memory/``,
``/chroma_db/``…) cherchés dans ``str(path).lower()``. Un chemin absolu n'était
jamais résolu avant le filtre. Mesuré sur le poste de dev :

    backend\\.env      -> refusé
    backend\\ENV~1     -> ACCEPTÉ   (le même fichier, par son nom court 8.3)

Le chemin accordé part ensuite à aider en ``--read``, donc dans le contexte du
moteur de l'Atelier — cloud compris. Les clés d'API de ``.env`` avec.

Ce fichier tente chaque écriture d'un même fichier (et d'un même dossier
sensible) que Windows accepte : nom court, casse, point et espace finaux, flux
ADS, préfixe ``\\\\?\\``, UNC administratif, jonction, et un fichier atteint par
``rglob`` sous un dossier désigné par son nom court. Chacune doit être refusée
par les deux portes — ``grant_read`` et ``_atelier_read_files`` — et un fichier
anodin doit rester accepté (sans quoi « tout refuser » passerait ce test).

L'arbre est un TEMPORAIRE (``REPO_ROOT`` du module rebranché dessus) : le test
ne touche jamais au vrai ``backend/.env``. Les variantes propres à Windows se
sautent ailleurs ; elles tournent dans verif-ci et dans le job Windows
``paquet-voix``.

Usage :
    python test_atelier_lecture_sensible.py
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401,E402  — isole les données AVANT tout import de core.*

from core import module_workshop  # noqa: E402

WINDOWS = os.name == "nt"


def _nom_court(chemin: Path):
    """Nom court 8.3 de ``chemin``, ou None si le volume n'en génère pas."""
    if not WINDOWS:
        return None
    import ctypes

    tampon = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(chemin), tampon, 1024)
    court = tampon.value if n else ""
    return court if court and court.lower() != str(chemin).lower() else None


class LectureSensibleTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="epure-lecture-")
        self.addCleanup(tmp.cleanup)
        # Résolu : la racine elle-même ne doit pas introduire d'alias (TEMP peut
        # être un nom court sur le runner) — seules les VARIANTES en introduisent.
        self.racine = Path(tmp.name).resolve()
        self.backend = self.racine / "backend"
        (self.backend / "memory").mkdir(parents=True)
        (self.backend / "chroma_db").mkdir()
        (self.backend / "src").mkdir()
        self.env = self.backend / ".env"
        self.env.write_text("CLE_API=secret-de-test\n", encoding="utf-8")
        (self.backend / "memory" / "notes.md").write_text("perso", encoding="utf-8")
        (self.backend / "chroma_db" / "index.md").write_text("perso", encoding="utf-8")
        self.anodin = self.backend / "src" / "outil.py"
        self.anodin.write_text("x = 1\n", encoding="utf-8")
        p = mock.patch.object(module_workshop, "REPO_ROOT", self.racine)
        p.start()
        self.addCleanup(p.stop)

    # -- outillage ---------------------------------------------------------

    def _sensible(self, chemin) -> bool:
        """Le chemin désigne-t-il `.env` ou un fichier sous memory/chroma_db ?"""
        try:
            r = Path(chemin).resolve(strict=True)
        except OSError:
            return False
        if os.path.samefile(r, self.env):
            return True
        for d in (self.backend / "memory", self.backend / "chroma_db"):
            if any(os.path.samefile(a, d) for a in (r, *r.parents) if a.exists()):
                return True
        return False

    def _refuse_partout(self, variante: str):
        """Les deux portes refusent `variante`, et rien de sensible n'en sort."""
        self.assertFalse(module_workshop._read_is_safe(Path(variante)),
                         f"_read_is_safe accepte {variante!r}")
        with mock.patch.object(module_workshop, "_read_meta", return_value={}), \
             mock.patch.object(module_workshop, "_write_meta"):
            self.assertFalse(module_workshop.grant_read("sonde", variante),
                             f"grant_read accorde {variante!r}")
        sortis = module_workshop._atelier_read_files(extra=[variante], minimal=True)
        fuites = [s for s in sortis if self._sensible(s)]
        self.assertEqual(fuites, [], f"_atelier_read_files laisse passer {fuites} via {variante!r}")

    # -- témoin ------------------------------------------------------------

    def test_un_fichier_anodin_reste_lisible(self):
        """Sans lui, un correctif qui refuse tout passerait ce fichier entier."""
        self.assertTrue(module_workshop._read_is_safe(self.anodin))
        with mock.patch.object(module_workshop, "_read_meta", return_value={}), \
             mock.patch.object(module_workshop, "_write_meta"):
            self.assertTrue(module_workshop.grant_read("sonde", str(self.anodin)))
        sortis = module_workshop._atelier_read_files(extra=[str(self.backend / "src")], minimal=True)
        self.assertIn(str(self.anodin), [str(Path(s).resolve()) for s in sortis])

    # -- écritures portables -------------------------------------------------

    def test_env_nom_long(self):
        self._refuse_partout(str(self.env))

    def test_env_relatif_a_la_racine(self):
        self._refuse_partout("backend/.env")

    def test_dossier_memory_sans_slash_final(self):
        self._refuse_partout(str(self.backend / "memory"))

    def test_fichier_sous_memory(self):
        self._refuse_partout(str(self.backend / "memory" / "notes.md"))

    # -- écritures propres à Windows ----------------------------------------

    def _windows(self):
        if not WINDOWS:
            self.skipTest("variante propre à Windows")

    def test_env_nom_court_8_3(self):
        self._windows()
        court = _nom_court(self.env)
        if not court:
            self.skipTest("ce volume ne génère pas de noms courts 8.3")
        self._refuse_partout(court)

    def test_env_casse(self):
        self._windows()
        for nom in (".ENV", ".Env"):
            with self.subTest(nom=nom):
                self._refuse_partout(str(self.backend / nom))

    def test_env_point_et_espace_finaux(self):
        self._windows()
        for nom in (".env.", ".env "):
            with self.subTest(nom=repr(nom)):
                self._refuse_partout(str(self.backend) + "\\" + nom)

    def test_env_flux_ads(self):
        self._windows()
        self._refuse_partout(str(self.env) + "::$DATA")

    def test_env_prefixe_etendu(self):
        self._windows()
        self._refuse_partout("\\\\?\\" + str(self.env))

    def test_env_unc_administratif(self):
        self._windows()
        lecteur, reste = os.path.splitdrive(str(self.env))
        unc = f"\\\\localhost\\{lecteur[0]}$" + reste
        if not os.path.exists(unc):
            self.skipTest(f"partage administratif inaccessible ici ({unc})")
        self._refuse_partout(unc)

    def test_env_par_une_jonction(self):
        self._windows()
        import _winapi

        lien = self.racine / "raccourci"
        _winapi.CreateJunction(str(self.backend), str(lien))
        self.addCleanup(lambda: os.rmdir(lien))  # retire la jonction, pas sa cible
        self._refuse_partout(str(lien / ".env"))
        self._refuse_partout(str(lien / "memory"))

    def test_rglob_sous_un_dossier_designe_par_son_nom_court(self):
        """``chroma_db`` a 9 caractères : Windows lui donne un alias ``CHROMA~1``,
        et ``rglob`` garde l'alias dans chaque chemin qu'il produit."""
        self._windows()
        court = _nom_court(self.backend / "chroma_db")
        if not court:
            self.skipTest("ce volume ne génère pas de noms courts 8.3")
        self._refuse_partout(court)


if __name__ == "__main__":
    unittest.main(verbosity=2)
