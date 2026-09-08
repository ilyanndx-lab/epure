#!/usr/bin/env python3
"""Tests de ``core.encre`` — magasin des pages manuscrites du module « encre ».

Deux choses à garder, et elles n'ont pas le même poids.

**1. Le CRUD.** Banal, sauf sur un point qui l'est moins : la liste ne doit
jamais rendre les tracés. C'est ce qui fait tenir le choix « un fichier par page,
pas d'index central » (cf. l'en-tête de ``core/encre.py``) — sans ça, afficher la
liste des pages transférerait toute l'encre de toutes les pages.

**2. Le confinement de chemin.** Un identifiant vient du client sur
``GET``/``PUT``/``DELETE /encre/pages/{id}``, et le dernier finit en ``unlink()``.
Les identifiants sont pourtant fabriqués côté serveur (``uuid4().hex``) : la
garde est une ceinture, pas un correctif d'une faille atteignable aujourd'hui —
Starlette refuse déjà un ``/`` dans un paramètre de chemin, même percent-encodé.
Ce que ces tests vérifient, c'est que le confinement est vrai **par
construction** dans le moteur, indépendamment d'une propriété du routage qui
n'est écrite nulle part dans ``core/encre.py``.

⚠️ **Ce que ces tests ne peuvent PAS affirmer de façon portable** : qu'un
antislash est refusé. Sous Windows, ``..\\x`` est une évasion (deux segments) ;
sous POSIX c'est un nom de fichier d'un seul segment, parfaitement confiné et
inoffensif. Une assertion « refusé » passerait sur ce poste et échouerait en CI —
c'est le piège que ``core/history.py`` documente déjà. Les cas éprouvés ici sont
donc ceux qui ont le MÊME verdict sur les deux plateformes, plus une
vérification structurelle qui vaut partout : après une tentative, rien n'a été
écrit ni lu hors du dossier d'encre.

Usage :
    python test_encre_store.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_ENCRE_DIR AVANT tout import de core.*

from core.encre import TITRE_DEFAUT, EncreEngine  # noqa: E402
from core.jsonstore import read_json  # noqa: E402
from core.paths import PathOutsideDataError, resolve_encre_dir  # noqa: E402

#: Un trait plausible : une liste de points, chacun portant sa pression et son
#: horodatage relatif. Le moteur n'en lit RIEN — il vérifie seulement que
#: ``strokes`` est une liste — et c'est précisément ce que
#: ``test_les_traits_sont_rendus_tels_quels`` affirme. La forme est recopiée de
#: ce qu'émet le composant pour que ce fichier ne teste pas une donnée que
#: personne n'envoie.
TRAIT = {
    "couleur": "#111827",
    "taille": 3,
    "points": [
        {"x": 10.5, "y": 20.25, "pression": 0.42, "t": 0},
        {"x": 11.0, "y": 21.75, "pression": 0.51, "t": 8},
    ],
}


class EncreStoreTest(unittest.TestCase):
    """CRUD sur un dossier temporaire propre à chaque test.

    Le moteur est construit avec un ``dossier`` explicite plutôt qu'en posant
    ``$EPURE_ENCRE_DIR`` : la variable est déjà posée pour toute la suite par
    ``_test_env`` (sur un temporaire unique et partagé), donc la modifier ici
    ferait dépendre les autres fichiers de test de l'ordre de découverte. Le
    paramètre existe pour ça — et ``test_le_defaut_suit_la_variable``
    ci-dessous vérifie séparément que le défaut, lui, passe bien par
    ``resolve_encre_dir()``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.moteur = EncreEngine(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    # ── Création ──────────────────────────────────────────────────────────────

    def test_creation_rend_la_page_entiere(self):
        page = self.moteur.create_page("Cours de méca", [TRAIT])
        self.assertEqual(page["titre"], "Cours de méca")
        self.assertEqual(page["strokes"], [TRAIT])
        self.assertTrue(page["id"])
        self.assertEqual(page["date_creation"], page["date_modification"])

    def test_creation_un_fichier_par_page_et_pas_d_index(self):
        """Le choix de structure du module, vérifié au lieu d'être commenté.

        Trois pages → trois fichiers, et RIEN d'autre. Un quatrième fichier
        signifierait qu'un index central est réapparu — c'est-à-dire une seconde
        source de vérité à côté du disque, exactement ce que le §3.3 interdit
        pour l'état des modules et ce que ce module refuse pour ses pages.
        """
        ids = {self.moteur.create_page(f"p{i}", [])["id"] for i in range(3)}
        fichiers = {p.stem for p in self.dir.glob("*")}
        self.assertEqual(fichiers, ids)

    def test_creation_sans_titre_prend_le_defaut(self):
        self.assertEqual(self.moteur.create_page()["titre"], TITRE_DEFAUT)

    def test_titre_vide_est_conserve(self):
        """Une chaîne vide n'est pas une absence — cf. ``EncreEngine._titre``."""
        self.assertEqual(self.moteur.create_page("", [])["titre"], "")

    def test_strokes_absent_ou_mal_forme_donne_une_page_vide(self):
        """Un client qui envoie n'importe quoi ne doit pas produire un fichier
        dont la relecture plantera. Même règle que ``liste()`` côté frontend."""
        for valeur in (None, "pas une liste", {"traits": []}, 42):
            with self.subTest(valeur=valeur):
                page = self.moteur.create_page("x", valeur)
                self.assertEqual(page["strokes"], [])

    # ── Lecture ───────────────────────────────────────────────────────────────

    def test_relecture_rend_exactement_ce_qui_a_ete_ecrit(self):
        pid = self.moteur.create_page("Thermo", [TRAIT, TRAIT])["id"]
        page = self.moteur.get_page(pid)
        self.assertEqual(page["strokes"], [TRAIT, TRAIT])
        self.assertEqual(page["titre"], "Thermo")
        self.assertEqual(page["id"], pid)

    def test_les_traits_sont_rendus_tels_quels(self):
        """Le moteur est OPAQUE aux tracés : ce qu'il reçoit revient à l'identique.

        C'est ce qui rend la phase 2 possible sans migration — un champ ajouté au
        point (ou au trait) traverse le magasin sans que personne ne l'ait
        déclaré. Le cas éprouvé est délibérément absurde pour le module d'encre
        (une clé inconnue, un point sans coordonnées) : si un jour une validation
        de forme s'installe dans ``core/encre.py``, c'est ce test qui doit tomber.
        """
        exotique = [{"champ_de_la_phase_2": True, "points": [{"n_importe_quoi": 1}]}]
        pid = self.moteur.create_page("x", exotique)["id"]
        self.assertEqual(self.moteur.get_page(pid)["strokes"], exotique)

    def test_page_absente_rend_none(self):
        self.assertIsNone(self.moteur.get_page("0" * 32))

    def test_fichier_corrompu_rend_none_sans_lever(self):
        (self.dir / "abime.json").write_text("pas du json {", encoding="utf-8")
        self.assertIsNone(self.moteur.get_page("abime"))

    # ── Liste ─────────────────────────────────────────────────────────────────

    def test_la_liste_ne_contient_jamais_les_traits(self):
        """LE test de ce fichier côté performance, et la raison d'être de la
        distinction entre ``GET /encre/pages`` et ``GET /encre/pages/{id}``."""
        self.moteur.create_page("Grosse page", [TRAIT] * 50)
        entrees = self.moteur.list_pages()
        self.assertEqual(len(entrees), 1)
        self.assertNotIn("strokes", entrees[0])
        self.assertEqual(entrees[0]["n_traits"], 50)

    def test_la_liste_est_derivee_du_disque(self):
        """Aucun index à tenir : un fichier retiré à la main disparaît de la
        liste, un fichier posé à la main y apparaît."""
        pid = self.moteur.create_page("A", [])["id"]
        self.assertEqual([e["id"] for e in self.moteur.list_pages()], [pid])
        (self.dir / f"{pid}.json").unlink()
        self.assertEqual(self.moteur.list_pages(), [])

    def test_la_liste_ignore_un_fichier_illisible(self):
        """Une page corrompue ne doit pas rendre les autres inaccessibles."""
        pid = self.moteur.create_page("saine", [])["id"]
        (self.dir / "abime.json").write_text("{{{", encoding="utf-8")
        self.assertEqual([e["id"] for e in self.moteur.list_pages()], [pid])

    def test_dossier_vide_rend_une_liste_vide(self):
        self.assertEqual(self.moteur.list_pages(), [])

    def test_la_liste_est_ordonnee_et_stable(self):
        """Plus récemment modifiée d'abord. Les trois pages sont créées dans la
        même seconde — l'horodatage n'a pas de sous-seconde — donc c'est le
        départage par id qui est réellement éprouvé ici : deux appels successifs
        doivent rendre le même ordre, sinon la liste sautille à l'écran."""
        for i in range(3):
            self.moteur.create_page(f"p{i}", [])
        premier = [e["id"] for e in self.moteur.list_pages()]
        self.assertEqual(premier, [e["id"] for e in self.moteur.list_pages()])
        self.assertEqual(len(premier), 3)

    # ── Mise à jour ───────────────────────────────────────────────────────────

    def test_mise_a_jour_remplace_titre_et_traits(self):
        pid = self.moteur.create_page("avant", [])["id"]
        page = self.moteur.update_page(pid, "après", [TRAIT])
        self.assertEqual(page["titre"], "après")
        self.assertEqual(page["strokes"], [TRAIT])
        self.assertEqual(self.moteur.get_page(pid)["titre"], "après")

    def test_mise_a_jour_partielle_ne_touche_pas_l_autre_champ(self):
        """``None`` veut dire « ne touche pas », jamais « efface ».

        C'est ce qui permet de renommer une page sans lui renvoyer ses tracés —
        et l'inverse, sauvegarder l'encre sans réécrire un titre que
        l'utilisateur est peut-être en train de modifier dans un autre champ.
        """
        pid = self.moteur.create_page("titre", [TRAIT])["id"]
        self.moteur.update_page(pid, titre="renommée")
        self.assertEqual(self.moteur.get_page(pid)["strokes"], [TRAIT])
        self.moteur.update_page(pid, strokes=[])
        self.assertEqual(self.moteur.get_page(pid)["titre"], "renommée")

    def test_une_liste_vide_efface_bien_les_traits(self):
        """``[]`` est une valeur, pas une absence : la gomme doit pouvoir vider
        une page. C'est le pendant du test précédent, et les confondre reviendrait
        à rendre l'effacement impossible."""
        pid = self.moteur.create_page("x", [TRAIT])["id"]
        self.moteur.update_page(pid, strokes=[])
        self.assertEqual(self.moteur.get_page(pid)["strokes"], [])

    def test_mise_a_jour_conserve_la_date_de_creation(self):
        pid = self.moteur.create_page("x", [])["id"]
        creation = self.moteur.get_page(pid)["date_creation"]
        self.assertEqual(self.moteur.update_page(pid, "y")["date_creation"], creation)

    def test_mise_a_jour_d_une_page_absente_rend_none_sans_rien_creer(self):
        self.assertIsNone(self.moteur.update_page("0" * 32, "x", []))
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_mise_a_jour_d_un_fichier_corrompu_n_ecrase_rien(self):
        """Le point de ``_PageAbsente`` : un fichier illisible n'est pas remplacé
        par une page vide au moment où on essaie de le modifier.

        ``transaction`` sur un JSON corrompu n'échoue pas — ``read_json`` loggue
        et rend le défaut — donc sans la sentinelle, le corps du ``with``
        écrirait une page neuve par-dessus le contenu d'origine. Sur de l'encre
        manuscrite, ce contenu est irrécupérable.
        """
        brut = "{ ceci n'est pas du json"
        (self.dir / "abime.json").write_text(brut, encoding="utf-8")
        self.assertIsNone(self.moteur.update_page("abime", "x", [TRAIT]))
        self.assertEqual((self.dir / "abime.json").read_text(encoding="utf-8"), brut)

    # ── Suppression ───────────────────────────────────────────────────────────

    # ── Transcription (phase 2) ───────────────────────────────────────────────

    def test_transcription_ajoute_le_bloc_sans_migration(self):
        """Un champ NEUF sur une page écrite avant qu'il existe, sans rien migrer.

        C'est la propriété que l'en-tête de `core/encre.py` promet — les tracés
        sont opaques, la page est un dict libre — et elle ne vaut que si on la
        vérifie sur une page créée par l'ancien chemin, ce que fait ce test.
        """
        page = self.moteur.create_page("Cours de méca", [TRAIT])
        mise = self.moteur.set_transcription(page["id"], "x^{2}", "pix2text-mfr", "v1")
        self.assertEqual({"texte", "modele", "version", "date"},
                         set(mise["transcription"]))
        self.assertEqual("x^{2}", mise["transcription"]["texte"])
        self.assertEqual("pix2text-mfr", mise["transcription"]["modele"])
        self.assertEqual("v1", mise["transcription"]["version"])
        self.assertTrue(mise["transcription"]["date"])

    def test_transcription_ne_touche_a_rien_d_autre(self):
        """Titre, tracés ET LES DEUX DATES intacts.

        `date_modification` est le point qui se déduit mal, et il est délibéré :
        transcrire ne MODIFIE pas la page. Cette date trie la liste (« sur quoi
        ai-je travaillé en dernier ? ») et décide si l'enregistrement automatique
        a quelque chose à envoyer ; la faire avancer sur un traitement dérivé
        remonterait la page en tête de liste sans qu'un trait ait bougé.
        """
        page = self.moteur.create_page("Cours de méca", [TRAIT])
        mise = self.moteur.set_transcription(page["id"], "x", "m", "v")
        for champ in ("titre", "strokes", "date_creation", "date_modification"):
            with self.subTest(champ=champ):
                self.assertEqual(page[champ], mise[champ])

    def test_transcription_relue_depuis_le_disque(self):
        """Écrite pour de bon, pas seulement rendue à l'appelant."""
        page = self.moteur.create_page("T", [TRAIT])
        self.moteur.set_transcription(page["id"], r"\\alpha", "m", "v")
        relue = self.moteur.get_page(page["id"])
        self.assertEqual(r"\\alpha", relue["transcription"]["texte"])

    def test_une_seconde_transcription_remplace_la_premiere(self):
        """Retranscrire ne doit pas empiler : c'est le geste prévu par
        `docs/module-encre.md` le jour où le modèle change."""
        page = self.moteur.create_page("T", [TRAIT])
        self.moteur.set_transcription(page["id"], "ancien", "m", "v1")
        mise = self.moteur.set_transcription(page["id"], "nouveau", "m", "v2")
        self.assertEqual("nouveau", mise["transcription"]["texte"])
        self.assertEqual("v2", mise["transcription"]["version"])

    def test_un_texte_vide_est_conserve_tel_quel(self):
        """Le modèle peut n'avoir rien lu ; c'est un résultat, pas une erreur. Le
        moteur n'a pas à inventer, ni à refuser d'écrire."""
        page = self.moteur.create_page("T", [TRAIT])
        mise = self.moteur.set_transcription(page["id"], "", "m", "v")
        self.assertEqual("", mise["transcription"]["texte"])

    def test_transcription_d_une_page_absente_rend_none_sans_rien_creer(self):
        """Même contrat que `update_page` : « cette page n'existe pas » est une
        réponse normale, que le routeur traduit en 404."""
        self.assertIsNone(self.moteur.set_transcription("inexistante", "x", "m", "v"))
        self.assertEqual([], list(self.dir.glob("*.json")))

    def test_transcription_d_un_fichier_corrompu_n_ecrase_rien(self):
        """`transaction(chemin, None)` sur un fichier illisible n'échoue PAS :
        `read_json` loggue et rend le défaut. Sans la sentinelle `_PageAbsente`,
        le corps du `with` reconstruirait une page par-dessus — et ce qui serait
        perdu ici est de l'encre manuscrite, que rien ne réécrit.
        """
        chemin = self.dir / "abimee.json"
        chemin.write_text("{ pas du json", encoding="utf-8")
        self.assertIsNone(self.moteur.set_transcription("abimee", "x", "m", "v"))
        self.assertEqual("{ pas du json", chemin.read_text(encoding="utf-8"))

    def test_suppression(self):
        pid = self.moteur.create_page("x", [])["id"]
        self.assertTrue(self.moteur.delete_page(pid))
        self.assertIsNone(self.moteur.get_page(pid))
        self.assertFalse(self.moteur.delete_page(pid))

    # ── Encodage (le piège maison) ────────────────────────────────────────────

    def test_ecriture_sans_bom_et_lecture_tolerante(self):
        """``core/jsonstore.py`` et non ``json.dump`` — CLAUDE.md §8.

        Vérifié dans les deux sens sur un fichier de page, pas seulement dans
        ``test_jsonstore.py`` : ce moteur est un nouvel appelant, et l'oubli se
        rattrape ici ou jamais. Un BOM posé par un outil Windows sur une page
        rendrait son encre invisible, puis la ferait écraser à l'écriture
        suivante.
        """
        pid = self.moteur.create_page("café ↦ é", [TRAIT])["id"]
        chemin = self.dir / f"{pid}.json"
        self.assertFalse(chemin.read_bytes().startswith(b"\xef\xbb\xbf"))
        chemin.write_bytes(b"\xef\xbb\xbf" + chemin.read_bytes())
        self.assertEqual(self.moteur.get_page(pid)["titre"], "café ↦ é")

    def test_le_fichier_est_du_json_relisible_par_jsonstore(self):
        pid = self.moteur.create_page("x", [TRAIT])["id"]
        self.assertEqual(read_json(self.dir / f"{pid}.json", None)["strokes"], [TRAIT])


class ConfinementTest(unittest.TestCase):
    """Un identifiant venu du client ne doit jamais désigner un fichier hors du
    dossier d'encre — ni pour le lire, ni pour l'écrire, ni pour le supprimer.

    Le dossier de test est un sous-dossier d'un temporaire qui contient un
    fichier TÉMOIN à côté : c'est le seul montage qui rend l'assertion
    intéressante. Sans voisin à atteindre, « rien n'a fui » ne prouve rien.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self._tmp.name)
        self.temoin = self.racine / "secret.json"
        self.temoin.write_text('{"secret": true}', encoding="utf-8")
        self.dir = self.racine / "encre"
        self.moteur = EncreEngine(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    #: Identifiants dont le verdict est le MÊME sous Windows et sous POSIX.
    #: L'antislash en est délibérément absent (cf. l'en-tête du module) : il est
    #: une évasion sur l'une des deux plateformes et un nom de fichier sur
    #: l'autre, donc une assertion commune y serait fausse quelque part.
    HOSTILES = (
        "../secret",
        "../../etc/passwd",
        "sous-dossier/page",       # confiné, mais créerait une arborescence
        "..",
        ".",
        "",
    )

    def test_get_refuse(self):
        for pid in self.HOSTILES:
            with self.subTest(pid=pid):
                with self.assertRaises(PathOutsideDataError):
                    self.moteur.get_page(pid)

    def test_update_refuse(self):
        for pid in self.HOSTILES:
            with self.subTest(pid=pid):
                with self.assertRaises(PathOutsideDataError):
                    self.moteur.update_page(pid, "pirate", [TRAIT])

    def test_delete_refuse(self):
        for pid in self.HOSTILES:
            with self.subTest(pid=pid):
                with self.assertRaises(PathOutsideDataError):
                    self.moteur.delete_page(pid)

    def test_rien_n_a_ete_ecrit_ni_supprime_hors_du_dossier(self):
        """L'assertion qui vaut sur les deux plateformes, et la seule qui dise
        vraiment quelque chose : après toutes les tentatives ci-dessus, le voisin
        est intact et le dossier d'encre est vide.

        Le percent-encoding (``..%2f..%2fsecret``) est joint à la liste ici plutôt
        que dans ``HOSTILES`` : c'est un identifiant que Starlette n'assemblerait
        jamais ainsi — il décode avant de router — donc ce qui l'atteint est un
        nom de fichier littéral, confiné et sans danger. Le tester comme un refus
        décrirait une menace qui n'existe pas ; le tester comme « n'a rien fui »
        décrit exactement ce qu'on veut.
        """
        for pid in (*self.HOSTILES, "..%2f..%2fsecret", "..%5c..%5csecret"):
            for appel in (self.moteur.get_page,
                          lambda p: self.moteur.update_page(p, "pirate", [TRAIT]),
                          self.moteur.delete_page):
                try:
                    appel(pid)
                except PathOutsideDataError:
                    pass
        self.assertTrue(self.temoin.is_file(), "le fichier voisin a été supprimé")
        self.assertEqual(self.temoin.read_text(encoding="utf-8"), '{"secret": true}')
        self.assertEqual(
            sorted(p.name for p in self.racine.rglob("*") if p.is_file()),
            ["secret.json"],
            "un fichier a été créé en dehors du dossier d'encre",
        )

    def test_un_identifiant_legitime_passe(self):
        """Contrôle du contrôle : la garde ne refuse pas tout.

        Sans lui, un ``_page_path`` qui lèverait systématiquement rendrait les
        trois tests ci-dessus verts pour la pire des raisons.
        """
        page = self.moteur.create_page("légitime", [TRAIT])
        self.assertEqual(self.moteur.get_page(page["id"])["titre"], "légitime")


class DefautDeCheminTest(unittest.TestCase):
    """Le défaut du moteur suit ``resolve_encre_dir()``, donc ``$EPURE_ENCRE_DIR``.

    Ce test est la moitié qui manque à tous les autres : ils passent un dossier
    explicite, donc aucun d'eux ne verrait un ``EncreEngine`` construit sur un
    chemin figé à l'import. C'est précisément la faute que le §3.5 décrit et que
    ``_test_env`` existe pour rattraper — un moteur qui figerait son dossier
    écrirait dans les vraies notes de l'utilisateur pendant la suite.
    """

    def test_le_defaut_suit_la_variable(self):
        moteur = EncreEngine()
        self.assertEqual(moteur._dir, resolve_encre_dir())

    def test_la_variable_pointe_ailleurs_que_le_vrai_dossier(self):
        self.assertNotEqual(resolve_encre_dir(), _test_env.REAL_ENCRE_DIR.resolve())
        self.assertFalse(resolve_encre_dir().is_relative_to(_test_env._REPO))


if __name__ == "__main__":
    unittest.main(verbosity=2)
