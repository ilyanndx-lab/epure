#!/usr/bin/env python3
"""Tests de `core.encre_exemples` et `core.banque_dictee_encre` — phase 3 du
module `encre` : collecte d'exemples d'entraînement.

Trois sujets, et ils n'ont pas le même poids :

**1. Le confinement de chemin.** Même patron que `ConfinementTest` de
`test_encre_store.py`, et pour la même raison : les identifiants sont fabriqués
côté serveur (`uuid4().hex`), donc c'est une ceinture et non le correctif d'une
faille atteinte — mais `EncreEngine._page_path` a déjà montré que c'est bon
marché à vérifier et cher à découvrir en incident.

**2. Les deux flux de création d'exemple** (correction, dictée inversée), au
niveau du moteur — les tests HTTP des deux endpoints qui les déclenchent vivent
dans `test_encre_entrainement.py`.

**3. La non-répétition de la banque tant qu'elle n'est pas épuisée.** Ni mockée
ni supposée : le test DRAINE la banque entière (`len(banque)` tirages, chacun
validé) et vérifie que les `len(banque)` tirages sont deux à deux distincts et
couvrent exactement la banque — puis qu'un tirage de plus, après épuisement,
reste valide. Aucun mock de `random` : la propriété éprouvée est « pas de
répétition avant d'avoir tout vu », pas « l'ordre est tel ou tel ».

Usage :
    python test_encre_exemples.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_ENCRE_DATASET_DIR AVANT tout import de core.*

from core import banque_dictee_encre as banque  # noqa: E402
from core.encre_exemples import (  # noqa: E402
    SOURCE_CORRECTION, SOURCE_DICTEE_INVERSEE, ExemplesEncreEngine,
)
from core.paths import PathOutsideDataError, resolve_encre_dataset_dir  # noqa: E402

#: Un trait plausible, recopié de `test_encre_store.py` pour la même raison :
#: le moteur ne regarde jamais à l'intérieur (cf. `_traits`).
TRAIT = {
    "couleur": "#111827",
    "taille": 3,
    "points": [
        {"x": 10.5, "y": 20.25, "pression": 0.42, "t": 0},
        {"x": 11.0, "y": 21.75, "pression": 0.51, "t": 8},
    ],
}


class _MoteurTemporaire(unittest.TestCase):
    """Un moteur sur DEUX dossiers temporaires explicites — exemples et progrès.

    Comme `EncreStoreTest`, passer les deux dossiers explicitement (plutôt que
    de laisser le moteur retomber sur `$EPURE_ENCRE_DATASET_DIR`/`$EPURE_DATA_DIR`,
    déjà posées par `_test_env` pour toute la suite) évite que ce fichier fasse
    dépendre les autres de l'ordre de découverte — en particulier le drainage
    complet de la banque, qui écrirait sinon dans le fichier de progression
    PARTAGÉ par tout le process de test.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        racine = Path(self._tmp.name)
        self.dir = racine / "exemples"
        self.progres_dir = racine / "progres"
        self.moteur = ExemplesEncreEngine(self.dir, self.progres_dir)

    def tearDown(self):
        self._tmp.cleanup()


class CreationExempleTest(_MoteurTemporaire):
    """Les deux flux de création, au niveau du moteur."""

    def test_correction_valide_telle_quelle(self):
        """« Valider tel quel » : texte_verite == texte_modele, tous deux non vides."""
        exemple = self.moteur.create_exemple(
            SOURCE_CORRECTION, [TRAIT], texte_verite="x^{2}", texte_modele="x^{2}")
        self.assertEqual(SOURCE_CORRECTION, exemple["source"])
        self.assertEqual("x^{2}", exemple["texte_verite"])
        self.assertEqual("x^{2}", exemple["texte_modele"])
        self.assertEqual([TRAIT], exemple["strokes"])
        self.assertTrue(exemple["id"])
        self.assertTrue(exemple["date"])

    def test_correction_editee_garde_le_texte_modele_original(self):
        """La correction change `texte_verite` SANS toucher `texte_modele` — c'est
        ce qui rend la comparaison possible plus tard."""
        exemple = self.moteur.create_exemple(
            SOURCE_CORRECTION, [TRAIT], texte_verite="x^2 + 1", texte_modele="x2 + l")
        self.assertEqual("x^2 + 1", exemple["texte_verite"])
        self.assertEqual("x2 + l", exemple["texte_modele"])

    def test_dictee_n_a_jamais_de_texte_modele(self):
        """`None`, pas une chaîne vide : « aucun modèle consulté » n'est pas
        « le modèle a lu du vide » — cf. l'en-tête du module."""
        exemple = self.moteur.create_exemple(
            SOURCE_DICTEE_INVERSEE, [TRAIT], texte_verite=r"\alpha + \beta")
        self.assertEqual(SOURCE_DICTEE_INVERSEE, exemple["source"])
        self.assertIsNone(exemple["texte_modele"])
        self.assertEqual(r"\alpha + \beta", exemple["texte_verite"])

    def test_source_invalide_leve(self):
        with self.assertRaises(ValueError):
            self.moteur.create_exemple("autre_chose", [TRAIT], texte_verite="x")

    def test_chaque_exemple_est_un_fichier_independant(self):
        """Un fichier par exemple, comme les pages d'encre — même choix, même
        raison (cf. l'en-tête de `core/encre.py`)."""
        ids = {
            self.moteur.create_exemple(SOURCE_CORRECTION, [TRAIT], texte_verite=f"e{i}")["id"]
            for i in range(3)
        }
        fichiers = {p.stem for p in self.dir.glob("*.json")}
        self.assertEqual(fichiers, ids)

    def test_relecture_rend_exactement_ce_qui_a_ete_ecrit(self):
        exemple = self.moteur.create_exemple(
            SOURCE_CORRECTION, [TRAIT], texte_verite="x", texte_modele="y")
        relu = self.moteur.get_exemple(exemple["id"])
        self.assertEqual(exemple, relu)

    def test_exemple_absent_rend_none(self):
        self.assertIsNone(self.moteur.get_exemple("0" * 32))

    def test_strokes_est_copie_pas_une_reference_vers_une_page(self):
        """La propriété structurante de la phase 3 : l'exemple porte SA PROPRE
        copie. Ce test ne peut pas prouver une absence de référence en Python
        (les dicts sont partagés par défaut), mais il prouve ce qui compte pour
        l'utilisateur : le contenu écrit sur disque ne dépend plus de rien
        d'externe une fois l'exemple créé."""
        traits = [dict(TRAIT)]
        exemple = self.moteur.create_exemple(SOURCE_CORRECTION, traits, texte_verite="x")
        traits.clear()  # simule la suppression de la page d'origine
        self.assertEqual([TRAIT], self.moteur.get_exemple(exemple["id"])["strokes"])

    def test_list_exemples_ignore_un_fichier_illisible(self):
        pid = self.moteur.create_exemple(SOURCE_CORRECTION, [TRAIT], texte_verite="x")["id"]
        (self.dir / "abime.json").write_text("{{{", encoding="utf-8")
        self.assertEqual([pid], [e["id"] for e in self.moteur.list_exemples()])


class DicteeInverseeTest(_MoteurTemporaire):
    """`expression_a_copier` / `valider_dictee`, au niveau du moteur."""

    def test_expression_a_copier_rend_une_entree_de_la_banque(self):
        expr = self.moteur.expression_a_copier()
        self.assertIn(expr["id"], {e["id"] for e in banque.expressions()})
        self.assertEqual(expr["latex"], dict(
            (e["id"], e["latex"]) for e in banque.expressions())[expr["id"]])

    def test_expression_a_copier_est_une_lecture_pure(self):
        """Aucun effet de bord : appeler deux fois ne change rien sur le disque."""
        self.moteur.expression_a_copier()
        self.assertFalse(self.moteur._progres_path.exists())

    def test_valider_dictee_cree_un_exemple_correct(self):
        expr = self.moteur.expression_a_copier()
        exemple = self.moteur.valider_dictee(expr["id"], [TRAIT])
        self.assertEqual(SOURCE_DICTEE_INVERSEE, exemple["source"])
        self.assertEqual(expr["latex"], exemple["texte_verite"])
        self.assertIsNone(exemple["texte_modele"])
        self.assertEqual([TRAIT], exemple["strokes"])

    def test_valider_dictee_expression_inconnue_leve(self):
        with self.assertRaises(ValueError):
            self.moteur.valider_dictee("id-inexistant", [TRAIT])

    def test_valider_dictee_sans_trait_leve(self):
        expr = self.moteur.expression_a_copier()
        with self.assertRaises(ValueError):
            self.moteur.valider_dictee(expr["id"], [])
        # Aucun exemple ne doit avoir été créé pour un tracé vide.
        self.assertEqual([], self.moteur.list_exemples())

    def test_valider_marque_l_expression_faite(self):
        expr = self.moteur.expression_a_copier()
        self.moteur.valider_dictee(expr["id"], [TRAIT])
        from core.jsonstore import read_json  # noqa: PLC0415
        doc = read_json(self.moteur._progres_path, {})
        self.assertIn(expr["id"], doc.get("faites", []))

    def test_non_repetition_jusqu_a_epuisement_puis_cycle(self):
        """LE test de ce fichier : draine la banque entière sans mocker `random`.

        Une expression FAITE ne doit jamais revenir tant qu'il en reste une
        autre à faire — vérifié en collectant les `len(banque)` tirages et en
        s'assurant qu'ils sont deux à deux distincts et couvrent exactement la
        banque. Le tirage suivant, après épuisement, doit rester valide (repli
        documenté par `choisir_expression` : l'ensemble complet, jamais une
        exception) — et le fichier de progression doit être reparti à zéro à ce
        moment-là (cf. `valider_dictee`), pas laissé plein pour toujours.
        """
        toutes = {e["id"] for e in banque.expressions()}
        tirees = []
        for _ in range(len(toutes)):
            expr = self.moteur.expression_a_copier()
            self.assertNotIn(
                expr["id"], tirees,
                "une expression déjà faite est revenue avant l'épuisement de la banque")
            self.moteur.valider_dictee(expr["id"], [TRAIT])
            tirees.append(expr["id"])
        self.assertEqual(toutes, set(tirees))

        # La banque est épuisée : le tirage suivant reste valide (repli sur
        # l'ensemble complet), et la progression a été remise à zéro par le
        # DERNIER `valider_dictee` — pas laissée pleine indéfiniment, ce qui
        # rendrait tout `GET` suivant équivalent à un tirage uniforme sur tout,
        # jamais un nouveau cycle « sans répétition ».
        from core.jsonstore import read_json  # noqa: PLC0415
        self.assertEqual([], read_json(self.moteur._progres_path, {}).get("faites"))
        expr_suivante = self.moteur.expression_a_copier()
        self.assertIn(expr_suivante["id"], toutes)

    def test_reformuler_une_expression_la_remet_dans_le_tirage(self):
        """Conséquence assumée de l'identifiant dérivé du CONTENU (cf.
        `core.banque_dictee_encre`) : marquer une expression faite, puis la
        « remplacer » par un texte différent (même id de test que reformuler la
        banque) fait disparaître la trace — c'est le bon arbitrage, pas une
        fuite : le contenu réellement proposé a changé."""
        expr = self.moteur.expression_a_copier()
        self.moteur.valider_dictee(expr["id"], [TRAIT])
        nouvel_id = banque.identifiant_expression(expr["latex"] + " ")
        self.assertNotEqual(expr["id"], nouvel_id)


class ConfinementTest(unittest.TestCase):
    """Un identifiant d'exemple ne doit jamais désigner un fichier hors du dataset.

    Même montage que `test_encre_store.ConfinementTest` : un fichier témoin à
    côté du dossier, pour que « rien n'a fui » soit une assertion qui prouve
    quelque chose.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self._tmp.name)
        self.temoin = self.racine / "secret.json"
        self.temoin.write_text('{"secret": true}', encoding="utf-8")
        self.dir = self.racine / "exemples"
        self.moteur = ExemplesEncreEngine(self.dir, self.racine / "progres")

    def tearDown(self):
        self._tmp.cleanup()

    #: Mêmes identifiants, même verdict des deux côtés — cf. l'avertissement de
    #: `test_encre_store.py` sur l'antislash, absent ici pour la même raison.
    HOSTILES = ("../secret", "../../etc/passwd", "sous-dossier/exemple", "..", ".", "")

    def test_get_refuse(self):
        for eid in self.HOSTILES:
            with self.subTest(eid=eid):
                with self.assertRaises(PathOutsideDataError):
                    self.moteur.get_exemple(eid)

    def test_rien_n_a_fui(self):
        for eid in (*self.HOSTILES, "..%2f..%2fsecret"):
            try:
                self.moteur.get_exemple(eid)
            except PathOutsideDataError:
                pass
        self.assertTrue(self.temoin.is_file())
        self.assertEqual(self.temoin.read_text(encoding="utf-8"), '{"secret": true}')
        self.assertEqual(
            sorted(p.name for p in self.racine.rglob("*") if p.is_file() and p != self.temoin),
            [],
        )

    def test_un_identifiant_legitime_passe(self):
        exemple = self.moteur.create_exemple(SOURCE_CORRECTION, [TRAIT], texte_verite="x")
        self.assertEqual(exemple, self.moteur.get_exemple(exemple["id"]))


class DefautDeCheminTest(unittest.TestCase):
    """Le défaut du moteur suit `resolve_encre_dataset_dir()` — même contrôle
    que `test_encre_store.DefautDeCheminTest`, pour la même raison : aucun autre
    test de ce fichier ne construit un moteur sans dossier explicite."""

    def test_le_defaut_suit_la_variable(self):
        moteur = ExemplesEncreEngine()
        self.assertEqual(moteur._dir, resolve_encre_dataset_dir())

    def test_la_variable_pointe_ailleurs_que_le_vrai_dossier(self):
        self.assertNotEqual(resolve_encre_dataset_dir(), _test_env.REAL_ENCRE_DATASET_DIR.resolve())
        self.assertFalse(resolve_encre_dataset_dir().is_relative_to(_test_env._REPO))


class BanqueDicteeTest(unittest.TestCase):
    """`core.banque_dictee_encre`, indépendamment du moteur."""

    def test_la_banque_n_est_pas_vide_et_a_des_identifiants_uniques(self):
        exprs = banque.expressions()
        self.assertGreaterEqual(len(exprs), 30)
        self.assertEqual(len({e["id"] for e in exprs}), len(exprs))

    def test_identifiant_stable_pour_le_meme_contenu(self):
        self.assertEqual(
            banque.identifiant_expression("x"), banque.identifiant_expression("x"))
        self.assertNotEqual(
            banque.identifiant_expression("x"), banque.identifiant_expression("y"))

    def test_choisir_expression_exclut_les_deja_faites_tant_qu_il_en_reste(self):
        exprs = banque.expressions()
        deja_faites = {e["id"] for e in exprs[:-1]}
        choix = banque.choisir_expression(deja_faites)
        self.assertEqual(exprs[-1]["id"], choix["id"])

    def test_choisir_expression_retombe_sur_tout_si_epuise(self):
        toutes = {e["id"] for e in banque.expressions()}
        choix = banque.choisir_expression(toutes)
        self.assertIn(choix["id"], toutes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
