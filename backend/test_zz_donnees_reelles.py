"""Garde-fou : la suite ne doit rien écrire sous le vrai ``backend/memory/``.

⚠️ **LE NOM DE CE FICHIER PORTE SON ORDRE D'EXÉCUTION. NE PAS LE RENOMMER.**

``unittest discover`` charge les modules dans l'ordre alphabétique de leur nom
de fichier, et les exécute dans cet ordre. Le préfixe ``test_zz_`` place donc ce
module **en dernier**, après ``test_workshop_paths``. C'est la seule raison
d'être du ``zz`` : un contrôle qui vérifie qu'aucun test n'a sali les données
réelles n'a de valeur que s'il passe **après tous les autres**.

Ce n'est pas théorique. Le contrôle vivait auparavant dans ``test_data_dir.py``,
découvert en 3e position sur 12 : un fichier écrit dans le vrai dossier par
``test_workshop_paths`` (12e) laissait la suite verte. Mesuré, pas supposé —
179 tests OK avec un fichier intrus bien présent sur le disque.

INVARIANT À TENIR : ce module doit rester le dernier découvert. Tout nouveau
fichier de test doit trier **avant** ``test_zz_donnees_reelles`` — ce qui est le
cas de tout nom ne commençant pas par ``test_z``. Si un jour un test doit
vraiment passer après, c'est ce garde-fou qu'il faut renommer plus loin, pas
l'inverse.

CE QUE CE GARDE-FOU NE COUVRE PAS, et il faut le savoir :

* un ``tearDownModule`` / ``tearDownClass`` d'un autre module qui s'exécuterait
  après lui — l'ordre inter-modules place bien nos tests en dernier, mais rien
  n'empêche un démontage tardif d'écrire ensuite ;
* les ``atexit`` et les threads démons, qui tournent après la fin de la suite
  (``QuotaTracker`` en lance un) ;
* un ``tearDown`` de ce module lui-même.

Autrement dit : il prouve qu'aucun **test** n'a écrit, pas qu'aucune **ligne de
code** n'écrira jamais. Le contrôle complémentaire est la comparaison d'empreinte
faite à la main après un passage, et la règle d'import de ``_test_env``
(CLAUDE.md §3.5).

L'empreinte témoin est prise par ``_test_env`` au tout premier import, donc
avant le moindre ``core.*`` — c'est important, parce que les écritures les plus
dangereuses sont celles de la phase d'import (``main.py`` lance la migration des
modules au chargement) et que ``discover`` importe TOUS les modules avant
d'exécuter le premier test.

Usage :
    python test_zz_donnees_reelles.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core.paths import resolve_data_dir  # noqa: E402


class RealDataUntouchedTest(unittest.TestCase):
    """Le vrai ``backend/memory/`` doit être bit-à-bit identique à l'empreinte."""

    def test_le_vrai_dossier_de_donnees_est_intact(self):
        actuel = _test_env._instantaner(_test_env.REAL_DATA_DIR)
        avant = _test_env.REAL_SNAPSHOT

        crees = sorted(set(actuel) - set(avant))
        supprimes = sorted(set(avant) - set(actuel))
        modifies = sorted(f for f in set(avant) & set(actuel) if avant[f] != actuel[f])

        self.assertEqual(
            (crees, supprimes, modifies), ([], [], []),
            f"\nLa suite a touché {_test_env.REAL_DATA_DIR} :"
            f"\n  créés     : {crees}"
            f"\n  supprimés : {supprimes}"
            f"\n  modifiés  : {modifies}"
            "\nUn test importe core.* ou main sans passer par `import _test_env` "
            "en premier, ou un chemin de données est encore codé en dur "
            "(cf. core.paths.resolve_data_dir, CLAUDE.md §3.5).",
        )

    def test_la_suite_ecrit_bien_ailleurs(self):
        """Contrôle du contrôle : EPURE_DATA_DIR est posé et pointe ailleurs.

        Sans ça, un garde-fou vert pourrait simplement signifier que la variable
        n'a jamais été posée et que tout le monde écrit… ailleurs par hasard.
        """
        courant = resolve_data_dir()
        self.assertNotEqual(courant, _test_env.REAL_DATA_DIR.resolve())
        self.assertEqual(courant, _test_env.DATA_DIR.resolve())

    def test_ce_module_est_bien_le_dernier_decouvert(self):
        """L'invariant qui rend les deux tests ci-dessus utiles.

        Vérifié plutôt que commenté : renommer un fichier de test en
        ``test_zzz_…`` casserait silencieusement le garde-fou sans ce contrôle.
        """
        racine = os.path.dirname(os.path.abspath(__file__))
        modules = sorted(
            f[:-3] for f in os.listdir(racine)
            if f.startswith("test_") and f.endswith(".py")
        )
        self.assertEqual(
            modules[-1], "test_zz_donnees_reelles",
            f"\n{modules[-1]} est découvert après le garde-fou, qui ne le "
            "surveille donc pas. Renommer ce fichier pour qu'il trie avant "
            "test_zz_donnees_reelles.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
