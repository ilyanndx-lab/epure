"""Frontière de version : fastapi ne franchit pas 0.136 en silence.

Pourquoi ce fichier existe — l'incident, pas la règle.

`_drop_module_routes` (core/module_workshop.py) retire les routes d'un module
de l'app vivante en filtrant `app.router.routes` à la main. Il n'existe aucune
API publique de démontage dans FastAPI ni dans Starlette : ce filtrage est le
seul moyen, et il dépend donc de la forme interne de cette liste.

Cette forme a changé. À partir de **fastapi 0.137.0**, `include_router`
n'aplatit plus les routes du router inclus dans `app.router.routes` : il y
ajoute UNE entrée `fastapi.routing._IncludedRouter`, sans `endpoint`, derrière
laquelle vivent les vraies routes (`original_router`) — servies via un cache de
correspondance invalidé par un compteur de version. Le filtre ne voit plus rien
à retirer, la route continue de répondre 200, et le module supprimé garde une
API fantôme.

Ce que ça a coûté : la CI installait `fastapi` sans épingle, résolvait donc une
version ≥ 0.137, et les deux tests de route fantôme échouaient en
`AssertionError: 200 != 404` là où le poste de dev, en 0.136.3, les passait. Le
diagnostic est parti sur le catalogue — la cause était le résolveur de pip.

Ce test est le seul point qui empêche l'oubli. Le bug **n'est pas corrigé** :
il n'y a pas de correctif dans le dépôt, seulement une frontière tenue ici. Le
jour où quelqu'un monte la version, il devient rouge et dit quoi faire.

Il vérifie aussi que la CI n'épingle pas autre chose que `requirements.txt` :
c'est la version validée qui fait foi, pas la dernière publiée.

**La limite est côté fastapi, pas côté starlette** — et ce test ne contraint
donc aucune version de starlette. Mesuré : fastapi 0.136.3 + starlette 1.6.0
passe les 207 tests, alors que fastapi 0.137.0 avec la même starlette échoue.
Si `starlette` est tout de même épinglé dans requirements.txt, c'est pour la
reproductibilité (fastapi le déclare `>=0.46.0` sans borne haute : sa version
est subie, jamais choisie), pas comme correctif. Ne pas partir chercher la
cause de son côté.
"""
import re
import unittest

import _test_env  # noqa: F401  — avant tout import de core.* (CLAUDE.md §3.5)

import fastapi

from core.paths import BACKEND_DIR, REPO_ROOT

# (majeur, mineur) validé. 0.136.3 est la dernière version qui démonte
# correctement ; 0.137.0 est la première qui ne le fait plus. Les deux bornes
# sont mesurées, pas déduites — cf. le tableau de bissection dans
# docs/limite-demontage.md.
PLAGE_VALIDEE = (0, 136)
PREMIERE_CASSEE = "0.137.0"

REQUIREMENTS = BACKEND_DIR / "requirements.txt"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Épinglés parce qu'un invariant du dépôt en dépend. Les autres paquets de la
# CI flottent délibérément (cf. en-tête de ci.yml).
PAQUETS_EPINGLES = ("fastapi", "starlette")

CONSIGNE = (
    "le démontage de routes (_drop_module_routes) ne fonctionne pas au-delà de "
    "0.136 — include_router n'aplatit plus, les routes vivent derrière "
    "_IncludedRouter.original_router avec un cache de correspondance versionné. "
    "Avant de monter la version, résoudre docs/limite-demontage.md."
)


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


class PlageValideeTest(unittest.TestCase):
    def test_fastapi_dans_la_plage_validee(self):
        installee = _version(fastapi.__version__)
        if installee[:2] > PLAGE_VALIDEE:
            self.fail(
                f"fastapi {fastapi.__version__} dépasse la plage validée "
                f"{PLAGE_VALIDEE[0]}.{PLAGE_VALIDEE[1]}.x — {CONSIGNE}\n"
                f"Première version cassée mesurée : {PREMIERE_CASSEE}. Les deux "
                f"tests qui le prouvent sont "
                f"test_catalogue.SuppressionOrdreTest."
                f"test_hello_ne_repond_plus_apres_suppression et "
                f"test_catalogue.CycleReinstallationTest."
                f"test_apres_reinstallation_c_est_la_nouvelle_version_qui_sert ; "
                f"ils échouent en « AssertionError: 200 != 404 », c'est-à-dire "
                f"une API qui répond encore après suppression du module."
            )
        if installee[:2] < PLAGE_VALIDEE:
            self.fail(
                f"fastapi {fastapi.__version__} est SOUS la plage validée "
                f"{PLAGE_VALIDEE[0]}.{PLAGE_VALIDEE[1]}.x. Rien n'a été mesuré "
                f"en dessous : ce n'est pas une version connue pour marcher, "
                f"c'est une version non testée. Installer "
                f"backend/requirements.txt."
            )


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
                    f"on dépend de ses internes, la version doit être choisie.",
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
                    f"ainsi qu'un fastapi ≥ {PREMIERE_CASSEE} est entré et a "
                    f"fait échouer les tests de route fantôme.",
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

        Compare la MINEURE seule, pas le correctif : l'invariant est au niveau
        de la mineure (0.136.1 comme 0.136.3 démontent correctement, mesuré), et
        faire échouer un poste sur un écart de correctif ferait du bruit sans
        rien protéger.
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
