"""Épinglage de fastapi/starlette : requirements.txt fait foi, la CI s'y aligne.

Pourquoi ce fichier existe — l'incident, pas la règle.

La CI installait `fastapi` sans épingle, résolvait donc la dernière version
publiée à chaque push, et testait autre chose que le poste de dev : c'est ainsi
qu'un fastapi ≥ 0.137 est entré en CI et a fait échouer les deux tests de route
fantôme (`AssertionError: 200 != 404`) pendant que le poste, en 0.136.3, les
passait. Le diagnostic est parti sur le catalogue — la cause était le résolveur
de pip. C'est la version VALIDÉE qui fait foi, pas la dernière publiée.

**Ce qui a changé le 2026-09-23.** Ce fichier gardait aussi une FRONTIÈRE :
fastapi ne devait pas dépasser 0.136, parce que le démontage de routes à chaud
(`_drop_module_routes`) filtrait `app.router.routes` et que 0.137 a changé cet
interne (`docs/limite-demontage.md`). Le démontage n'existe plus — les modules
ne se chargent qu'au démarrage (`docs/demontage-option-d.md`), et
`test_redemarrage_modules.py` interdit d'y revenir. La borne sur
`fastapi.__version__` est donc retirée ; l'accord requirements.txt/CI et la
correspondance de la mineure installée restent, parce qu'ils étaient vrais
indépendamment d'elle.

Les deux paquets restent épinglés EXACTEMENT (`==`) : monter l'un ou l'autre
est une décision, prise en changeant requirements.txt et ci.yml ensemble, pas
un effet du résolveur.
"""
import re
import unittest

import _test_env  # noqa: F401  — avant tout import de core.* (CLAUDE.md §3.5)

import fastapi

from core.paths import BACKEND_DIR, REPO_ROOT

REQUIREMENTS = BACKEND_DIR / "requirements.txt"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Épinglés exactement : la version validée fait foi (cf. l'en-tête). Les autres
# paquets de la CI flottent délibérément (cf. en-tête de ci.yml).
PAQUETS_EPINGLES = ("fastapi", "starlette")


def _version(txt: str) -> tuple[int, ...]:
    """(majeur, mineur, correctif) depuis « 0.136.3 », « 0.137.0rc1 »…"""
    m = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", txt)
    if not m:
        raise AssertionError(f"version illisible : {txt!r}")
    return tuple(int(g) for g in m.groups() if g is not None)


def _epingles(texte: str, paquet: str) -> list[str]:
    """Toutes les versions épinglées pour `paquet` dans un texte (`paquet==X`).

    Rend la liste et non la première : deux épingles divergentes du même paquet
    dans le même fichier est exactement le genre de dérive à faire échouer.
    """
    return re.findall(rf"(?<![\w.-]){re.escape(paquet)}==([0-9][^\s'\"#,;]*)", texte)


class EpinglageTest(unittest.TestCase):
    """requirements.txt fait foi ; la CI doit s'y aligner, jamais l'inverse."""

    @classmethod
    def setUpClass(cls):
        cls.requirements = REQUIREMENTS.read_text(encoding="utf-8")
        cls.ci = CI_YML.read_text(encoding="utf-8") if CI_YML.is_file() else None

    def test_requirements_epingle_les_deux_paquets(self):
        for paquet in PAQUETS_EPINGLES:
            with self.subTest(paquet=paquet):
                trouve = _epingles(self.requirements, paquet)
                self.assertEqual(
                    len(trouve), 1,
                    f"{paquet} doit être épinglé exactement une fois dans "
                    f"backend/requirements.txt (trouvé : {trouve}). starlette y "
                    f"est déclaré explicitement bien qu'il arrive en transitif : "
                    f"fastapi le déclare sans borne haute, sa version serait "
                    f"subie au lieu d'être choisie.",
                )

    def test_la_ci_epingle_les_memes_versions_que_requirements(self):
        if self.ci is None:
            self.skipTest(f"{CI_YML} absent (checkout partiel ?)")
        for paquet in PAQUETS_EPINGLES:
            with self.subTest(paquet=paquet):
                attendu = _epingles(self.requirements, paquet)
                dans_ci = _epingles(self.ci, paquet)
                self.assertTrue(
                    dans_ci,
                    f"la CI installe {paquet} sans épingle. Sans elle, pip "
                    f"résout la dernière version publiée à chaque push : c'est "
                    f"ainsi qu'un fastapi ≥ 0.137 est entré et a fait échouer "
                    f"les tests de route fantôme (docs/limite-demontage.md §2).",
                )
                self.assertEqual(
                    set(dans_ci), set(attendu),
                    f"{paquet} : la CI épingle {sorted(set(dans_ci))} et "
                    f"requirements.txt {sorted(set(attendu))}. C'est la version "
                    f"validée qui fait foi — aligner la CI sur "
                    f"requirements.txt, pas le contraire.",
                )

    def test_la_mineure_installee_est_celle_de_requirements(self):
        """Sinon la plage validée ne dit rien de ce qui tourne ici.

        Compare la MINEURE seule, pas le correctif : faire échouer un poste sur
        un écart de correctif ferait du bruit sans rien protéger, alors qu'une
        mineure d'écart est le cas qui a déjà coûté un diagnostic.
        """
        attendu = _epingles(self.requirements, "fastapi")[0]
        self.assertEqual(
            _version(fastapi.__version__)[:2], _version(attendu)[:2],
            f"fastapi installé : {fastapi.__version__} ; épinglé dans "
            f"requirements.txt : {attendu}. L'environnement a dérivé du fichier "
            f"d'une mineure — réinstaller backend/requirements.txt.",
        )


if __name__ == "__main__":
    unittest.main()
