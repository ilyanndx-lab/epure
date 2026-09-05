#!/usr/bin/env python3
"""L'arbre de modules vu par la suite ne dépend PAS de ce poste.

Ce fichier tient une frontière, comme `test_versions_epinglees.py` : il ne
corrige rien, il empêche un retour en arrière que rien d'autre ne verrait.

L'INCIDENT. `test_catalogue.InstallationTest` affirme « aucun installable ne
doit l'être par défaut » et échouait **en permanence sur le poste de dev**,
jamais en CI. Le diagnostic naturel — « ça dépend de l'ordre d'exécution » —
était faux : le test échoue seul, dans les deux sens d'une paire, et dans la
découverte complète. Il ne dépendait pas de l'ordre mais de la MACHINE.

La cause : `_test_env` copie le vrai `backend/modules/` dans son temporaire, et
sur un poste de dev cet arbre contient les modules réellement installés depuis
le catalogue ou générés par l'Atelier. Le module `code` y était installé, donc
`installé: True` était la **bonne réponse à une question posée au mauvais
arbre**. En CI, où le clone n'a que les modules versionnés (`_atelier`, `admin`,
`chat`, `hello`, `settings` — les autres sont gitignorés), le même test passait.

Ce que ça coûte, au-delà de ce test : un échec qui ne se reproduit que chez son
auteur finit par se lire comme du bruit, et c'est ainsi qu'il a survécu. Toute
la suite était concernée — `installed_ids()` ne rendait pas le même ensemble
ici et là-bas —, même si un seul test en mourait.

CE QUE CE FICHIER GARDE, dans cet ordre :

  1. la résolution reste À CHAUD (§3.5) — un chemin figé à l'import
     réintroduirait la classe de bug d'origine, celle des neuf modules qui
     calculaient leur dossier au chargement ;
  2. l'arbre isolé ne contient AUCUN module d'origine `catalogue` ou
     `workshop` — la propriété qui rend le résultat de la suite le même sur ce
     poste et en CI ;
  3. la règle est DÉRIVÉE des manifestes et non écrite en dur, sans quoi elle
     divergerait en silence du jour où `modules-catalogue/` bouge.

Usage :
    python test_arbre_modules_deterministe.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: E402  — pose les variables AVANT tout import de core.*

from core.paths import resolve_generated_dir, resolve_modules_dir  # noqa: E402

#: Les seules origines qu'un clone frais peut porter. `builtin` = module du
#: dépôt ; l'absence de manifeste couvre `_atelier` (support de l'Atelier,
#: versionné) et les dossiers vides laissés par une désinstallation.
_ORIGINES_DU_DEPOT = {"builtin", None}


def _origines(racine: Path) -> dict:
    """`{id: origin}` pour chaque manifeste trouvé sous `racine`."""
    trouve = {}
    if not racine.is_dir():
        return trouve
    for sub in sorted(racine.iterdir()):
        mf = sub / "manifest.json"
        if not mf.is_file():
            continue
        try:
            trouve[sub.name] = json.loads(mf.read_text(encoding="utf-8-sig")).get("origin")
        except Exception:
            trouve[sub.name] = "(manifeste illisible)"
    return trouve


class ResolutionAChaudTest(unittest.TestCase):
    """`resolve_modules_dir()` relit l'environnement à chaque appel.

    Vérifié par l'EXÉCUTION et non par relecture du code : on change la
    variable après que tout est importé, et on constate que le résultat suit.
    Un `MODULES_DIR = ...` figé en tête de module ferait échouer ce test — et
    c'est bien la forme qu'avait le bug d'origine ailleurs dans le dépôt.
    """

    def setUp(self):
        self._avant = os.environ.get("EPURE_MODULES_DIR")

    def tearDown(self):
        if self._avant is None:
            os.environ.pop("EPURE_MODULES_DIR", None)
        else:
            os.environ["EPURE_MODULES_DIR"] = self._avant

    def test_le_resolveur_suit_un_changement_tardif(self):
        autre = Path(tempfile.mkdtemp(prefix="epure-modules-sonde-")).resolve()
        try:
            os.environ["EPURE_MODULES_DIR"] = str(autre)
            self.assertEqual(
                resolve_modules_dir(), autre,
                "resolve_modules_dir() doit relire l'environnement, pas servir "
                "un chemin figé à l'import",
            )
        finally:
            import shutil
            shutil.rmtree(autre, ignore_errors=True)

    def test_l_arbre_de_test_n_est_pas_le_vrai(self):
        """Le préalable de tout le reste : si cette égalité devenait vraie, la
        suite écrirait dans les modules de l'utilisateur — et
        `DELETE /settings/modules/{id}` y ferait son `rmtree`."""
        self.assertNotEqual(resolve_modules_dir(), _test_env.REAL_MODULES_DIR)
        self.assertEqual(resolve_modules_dir(), _test_env.MODULES_DIR.resolve())


class ArbreDeterministeTest(unittest.TestCase):
    """L'arbre isolé est celui d'un clone frais, pas celui de ce poste."""

    def test_aucun_module_de_catalogue_ou_d_atelier_dans_l_arbre(self):
        intrus = {
            mid: origine
            for mid, origine in _origines(resolve_modules_dir()).items()
            if origine not in _ORIGINES_DU_DEPOT
        }
        self.assertEqual(
            intrus, {},
            "l'arbre de test contient des modules installés sur CE poste : la "
            "suite ne mesure alors pas la même chose ici et en CI",
        )

    def test_la_moitie_frontend_est_assainie_pareil(self):
        """`catalogue.install()` écrit les deux côtés ensemble et `uninstall()`
        les retire ensemble. N'assainir que le backend laisserait un
        `generated/<id>` du poste face à un `modules/` de clone frais — un état
        que ni ce poste ni la CI n'ont jamais eu."""
        genere = resolve_generated_dir()
        presents = {p.name for p in genere.iterdir() if p.is_dir()} if genere.is_dir() else set()
        self.assertEqual(
            presents & _test_env.MODULES_DU_POSTE, set(),
            "des composants de modules installés sur ce poste ont été copiés",
        )

    def test_les_modules_du_coeur_sont_bien_la(self):
        """Le pendant : assainir ne doit pas vider l'arbre. Les tests existants
        s'appuient sur un arbre RÉALISTE (`module_exists("chat")`, le `hello`
        de référence lu par l'Atelier) — un dossier vide les ferait tomber pour
        de mauvaises raisons."""
        presents = set(_origines(resolve_modules_dir()))
        for mid in ("admin", "chat", "hello", "settings"):
            with self.subTest(module=mid):
                self.assertIn(mid, presents)
        self.assertTrue((resolve_modules_dir() / "_atelier").is_dir(),
                        "_atelier n'a pas de manifeste et doit être copié quand même")


class RegleDeriveeTest(unittest.TestCase):
    """La règle se lit dans les manifestes, elle n'est pas écrite en dur.

    Une liste figée d'ids divergerait dès que `modules-catalogue/` bouge, et
    elle divergerait EN SILENCE : le seul symptôme serait le retour du test de
    catalogue, chez une seule personne.
    """

    def test_les_ecartes_sont_exactement_ceux_du_poste(self):
        attendu = {
            mid for mid, origine in _origines(_test_env.REAL_MODULES_DIR).items()
            if origine in ("catalogue", "workshop")
        }
        self.assertEqual(set(_test_env.MODULES_DU_POSTE), attendu)

    def test_rien_a_ecarter_est_un_resultat_valide(self):
        """Sur un clone frais — la CI — l'ensemble est vide, et ce fichier doit
        rester vert. Un test qui exigerait « au moins un module écarté »
        passerait ici et échouerait là-bas : l'erreur exacte qu'on corrige."""
        self.assertIsInstance(_test_env.MODULES_DU_POSTE, frozenset)


if __name__ == "__main__":
    unittest.main(verbosity=2)
