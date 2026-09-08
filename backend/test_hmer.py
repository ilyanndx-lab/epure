#!/usr/bin/env python3
"""Tests de ``core.hmer`` — transcription d'une page manuscrite (module `encre`).

**AUCUN de ces tests ne charge le modèle, et c'est la contrainte qui a dessiné le
fichier.** Charger `pix2text-mfr` veut dire importer `optimum.onnxruntime`, donc
`torch` — 16,7 s à chaud et 54,2 s à froid, mesurés sur ce poste — puis
télécharger 117,7 Mo de poids. En CI c'est pire qu'un ralentissement : le job
rapide n'installe délibérément aucune de ces dépendances (cf. l'en-tête de
`ci.yml`), donc un test qui les toucherait n'échouerait pas sur une assertion, il
échouerait à la COLLECTE.

Ce qui rend ça possible sans rien perdre d'utile : la partie du moteur qui
mérite vraiment un test n'est pas l'appel au modèle — c'est **le rendu**, et
c'est déjà lui qui a coûté la mesure la plus chère de la phase 0 (0 % d'ExpRate
au lieu de 24 % pour une image mal cadrée). `_bitmap` et `_recadrer` sont des
fonctions pures sur des images Pillow ; le reste est mocké.

Deux garde-fous portent cette isolation, et il faut connaître les deux :

* ``EPURE_HMER_AUTOINSTALL=0``, posée par `_test_env` pour toute la suite —
  jumelle d'``EPURE_EMBEDDING_AUTOINSTALL``. `HmerEngine.__init__` lève alors
  `HmerIndisponible` au lieu de télécharger, ce qui est un état que le routeur
  doit de toute façon savoir servir ;
* ``EPURE_HMER_DIR``, posée sur un temporaire VIDE — même régime que
  ``EPURE_MODELS_DIR`` : détourné pour qu'aucun test n'écrive dans le vrai cache,
  **non surveillé** parce que ce sont des poids reconstructibles et non des
  données utilisateur. L'encre, elle, est surveillée. Les deux appartiennent au
  même module et n'ont pas le même régime ; c'est le tableau des chemins de
  `docs/module-encre.md` qui tranche, pas la parenté des noms.

⚠️ **Pourquoi `_recadrer` est éprouvé sur une image FABRIQUÉE et non de bout en
bout.** `_bitmap` dimensionne déjà le canevas sur le bounding box des points :
sur une page réelle, le recadrage qui suit ne retire donc presque rien, et une
assertion de bout en bout passerait tout aussi bien avec un `_recadrer` qui ne
ferait RIEN. On l'éprouve donc sur une image à larges marges blanches, la
situation qu'il existe pour rattraper — et celle qui se présentera le jour où
l'entrée viendra d'ailleurs (photo, PNG importé).

Usage :
    python test_hmer.py
"""

import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_HMER_DIR AVANT tout import de core.*

from core import hmer  # noqa: E402
from core.hmer import (  # noqa: E402
    HmerEngine, HmerIndisponible, PageSansEncre, points_de_page, poids_manquants,
)
from core.paths import resolve_hmer_dir  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402


def page(*traits) -> dict:
    """Une page d'encre minimale. ``traits`` : des listes de ``(x, y[, pression])``."""
    return {"strokes": [
        {"couleur": "#111827", "taille": 4,
         "points": [{"x": p[0], "y": p[1],
                     "pression": p[2] if len(p) > 2 else 0.5, "t": i}
                    for i, p in enumerate(points)]}
        for points in traits
    ]}


def boite_encre(image) -> tuple:
    """Bounding box de ce qui n'est pas blanc — la mesure, pas le calcul testé."""
    return image.convert("L").point(lambda v: 255 if v < 250 else 0).getbbox()


class PointsDePageTest(unittest.TestCase):
    """`points_de_page` : la tolérance est le comportement, pas un relâchement.

    Le routeur l'appelle AVANT de construire le moteur, pour répondre 400 sur une
    page vide sans payer 118 Mo de poids. Elle doit donc être à la fois solide et
    sans dépendance lourde.
    """

    def test_traits_normaux(self):
        traits = points_de_page(page([(0, 0), (10, 10)], [(5, 5), (6, 6), (7, 7)]))
        self.assertEqual([2, 3], [len(t) for t in traits])
        # (x, y, épaisseur) — l'épaisseur est calculée, pas recopiée : taille 4 et
        # pression 0,5 donnent 4 * (0,5 + 0,5).
        self.assertEqual((0.0, 0.0, 4.0), traits[0][0])

    def test_la_pression_module_l_epaisseur(self):
        traits = points_de_page(page([(0, 0, 0.0), (1, 1, 1.0)]))
        self.assertEqual(2.0, traits[0][0][2])
        self.assertEqual(6.0, traits[0][1][2])

    def test_un_trait_d_un_seul_point_est_ecarte(self):
        """Il ne dessine aucun segment, et un point isolé n'apprend rien au modèle."""
        self.assertEqual([], points_de_page(page([(3, 3)])))

    def test_page_vide_ou_sans_strokes(self):
        for entree in ({}, {"strokes": []}, {"strokes": None}, {"strokes": "pas une liste"}):
            with self.subTest(entree=entree):
                self.assertEqual([], points_de_page(entree))

    def test_les_points_mal_formes_sont_ignores_pas_fatals(self):
        """Un point cassé ne doit pas rendre une page entière intranscriptible.

        Même esprit que `EncreEngine._traits` : l'encre est la donnée
        irremplaçable, la punir d'un défaut de client serait le mauvais arbitrage.
        """
        brute = {"strokes": [{"taille": 4, "points": [
            {"x": 0, "y": 0, "pression": 0.5},
            {"x": None, "y": 5},                 # coordonnée absente
            "pas un point",                      # pas un dict
            {"x": float("nan"), "y": 1},         # non fini
            {"x": True, "y": 2},                 # un booléen n'est pas une abscisse
            {"x": 10, "y": 10, "pression": 9},   # pression hors bornes → 0,5
        ]}]}
        traits = points_de_page(brute)
        self.assertEqual(1, len(traits))
        self.assertEqual([(0.0, 0.0, 4.0), (10.0, 10.0, 4.0)], traits[0])

    def test_un_trait_qui_n_est_pas_un_dict_est_ignore(self):
        self.assertEqual([], points_de_page({"strokes": ["x", 3, None]}))


class BitmapTest(unittest.TestCase):
    """Le rendu : dimensions, contenu, refus."""

    def test_refuse_une_page_sans_trace(self):
        """Explicitement, plutôt qu'une image blanche envoyée au modèle.

        Le modèle répondrait quelque chose — il répond toujours — et ce quelque
        chose serait indexé comme le contenu d'une page vide.
        """
        with self.assertRaises(PageSansEncre):
            HmerEngine._bitmap({"strokes": []})

    def test_les_dimensions_suivent_la_boite_des_points(self):
        """100×50 de tracé, épaisseur 4 → marge 3 de chaque côté → 106×56.

        C'est l'assertion qui dit que le canevas **ne dépend pas** de la taille
        logique de la page côté frontend (1240×1754 aujourd'hui). Une page A4
        rendue en entier donnerait au modèle une image vide à 98 %, c'est-à-dire
        le défaut de la phase 0 reproduit à la source.
        """
        image = HmerEngine._bitmap(page([(10, 10), (110, 60)]))
        self.assertEqual((106, 56), image.size)

    def test_la_position_absolue_ne_change_pas_la_taille(self):
        """Deux pages au même tracé, placé ailleurs, rendent la même image.

        Corollaire direct du test précédent, et la propriété qui compte
        réellement : c'est ce qui rend une formule écrite en bas de page aussi
        lisible qu'une écrite en haut.
        """
        a = HmerEngine._bitmap(page([(10, 10), (110, 60)]))
        b = HmerEngine._bitmap(page([(910, 1610), (1010, 1660)]))
        self.assertEqual(a.size, b.size)
        self.assertEqual(a.tobytes(), b.tobytes())

    def test_encre_sombre_sur_papier_blanc(self):
        image = HmerEngine._bitmap(page([(10, 10), (110, 60)]))
        valeurs = image.getdata()
        self.assertEqual(hmer._ENCRE, min(valeurs))
        self.assertEqual(hmer._PAPIER, max(valeurs))
        # Le coin est hors de la diagonale : il doit rester du papier.
        self.assertEqual(hmer._PAPIER, image.getpixel((0, image.height - 1)))

    def test_le_trace_n_est_pas_tronque_par_les_bords(self):
        """La marge vaut la demi-épaisseur : sans elle un trait posé sur le bord
        serait coupé dans sa moitié, et le modèle lirait un symbole tronqué.

        L'assertion porte sur l'ÉTENDUE de l'encre et non sur l'existence d'une
        bande blanche : le canevas fait `étendue + 2 × marge` pixels, donc à un
        pixel près la marge de droite est exactement consommée par le rayon —
        vrai et sans conséquence. Ce qui compte est que le disque des extrémités
        soit entier des deux côtés : 100 d'étendue plus deux rayons de 2 plus le
        pixel du centre, soit 105.
        """
        image = HmerEngine._bitmap(page([(10, 10), (110, 60)]))
        x0, y0, x1, y1 = boite_encre(image)
        self.assertEqual(105, x1 - x0)      # 100 d'étendue + 2 × rayon + 1
        self.assertEqual(55, y1 - y0)       # 50 d'étendue + 2 × rayon + 1
        self.assertLessEqual(x1, image.width)
        self.assertLessEqual(y1, image.height)

    def test_une_page_demesuree_est_mise_a_l_echelle_pas_refusee(self):
        """`core/encre.py` ne regarde JAMAIS à l'intérieur d'un trait — c'est sa
        décision documentée — donc un `x` absurde peut arriver jusqu'ici. Sans
        borne, `Image.new` tenterait l'allocation et le backend mourrait sur un
        MemoryError. Une page démesurée reste une page : on met à l'échelle.
        """
        image = HmerEngine._bitmap(page([(0, 0), (10_000_000, 10)]))
        self.assertLessEqual(max(image.size), hmer._COTE_MAX)
        self.assertGreaterEqual(min(image.size), 1)

    def test_un_trait_sans_taille_prend_le_repli(self):
        """Un trait sans `taille` vient d'un client plus ancien, pas d'un choix."""
        brute = {"strokes": [{"points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}]}]}
        traits = points_de_page(brute)
        self.assertEqual(hmer._TAILLE_TRAIT * 1.0, traits[0][0][2])
        self.assertIsNotNone(HmerEngine._bitmap(brute))


class RecadrageTest(unittest.TestCase):
    """Le geste de la phase 0 : sans lui, 0 % d'ExpRate au lieu de 24 %.

    Éprouvé sur une image FABRIQUÉE à larges marges — cf. l'en-tête du fichier :
    de bout en bout il ne retirerait presque rien et l'assertion ne prouverait
    pas qu'il fait quelque chose.
    """

    @staticmethod
    def _avec_marges(taille=(400, 200), boite=(160, 80, 240, 120)):
        image = Image.new("L", taille, 255)
        ImageDraw.Draw(image).rectangle(boite, fill=0)
        return image

    def test_recadre_vraiment(self):
        source = self._avec_marges()
        recadree = HmerEngine._recadrer(source)
        self.assertLess(recadree.width, source.width)
        self.assertLess(recadree.height, source.height)

    def test_le_contenu_est_entierement_conserve(self):
        """Recadrer ne doit jamais AMPUTER : le pire résultat possible ici serait
        une image plus petite à laquelle il manque un exposant."""
        recadree = HmerEngine._recadrer(self._avec_marges())
        x0, y0, x1, y1 = boite_encre(recadree)
        self.assertEqual(81, x1 - x0)      # 160..240 inclus
        self.assertEqual(41, y1 - y0)

    def test_la_marge_vaut_bien_dix_pour_cent_de_la_boite(self):
        """Proportionnelle et non fixe : une marge absolue serait énorme sur trois
        symboles et nulle sur une ligne entière."""
        recadree = HmerEngine._recadrer(self._avec_marges())
        # Boîte de contenu 81×41 → 10 % tronqué à l'entier = 8 et 4, des DEUX côtés.
        self.assertEqual((81 + 16, 41 + 8), recadree.size)

    def test_la_marge_est_clippee_aux_bords(self):
        """Un `crop` hors cadre remplit de NOIR chez Pillow, c'est-à-dire d'encre :
        le modèle lirait une bande sombre collée au symbole."""
        source = self._avec_marges(taille=(120, 60), boite=(0, 0, 100, 50))
        recadree = HmerEngine._recadrer(source)
        self.assertEqual(0, boite_encre(recadree)[0])
        self.assertEqual(0, boite_encre(recadree)[1])
        self.assertLessEqual(recadree.width, source.width)
        self.assertLessEqual(recadree.height, source.height)

    def test_une_image_blanche_est_rendue_telle_quelle(self):
        """Il n'y a pas de boîte, et lever punirait un cas que `_bitmap` ne peut
        pas produire — il refuse avant (`PageSansEncre`)."""
        blanche = Image.new("L", (40, 20), 255)
        self.assertEqual(blanche.size, HmerEngine._recadrer(blanche).size)

    def test_le_seuil_ignore_l_antialiasing(self):
        """251-254 est du blanc-cassé de bord, pas du contenu. Le compter rendrait
        le recadrage inopérant sur une image pourtant nette."""
        image = Image.new("L", (100, 50), 255)
        ImageDraw.Draw(image).rectangle((0, 0, 99, 49), outline=252)
        ImageDraw.Draw(image).rectangle((40, 20, 60, 30), fill=0)
        self.assertLess(HmerEngine._recadrer(image).width, 100)


class TranscrireTest(unittest.TestCase):
    """`transcrire` avec un modèle bouchonné : l'enchaînement, pas l'inférence."""

    def setUp(self):
        # `object.__new__` plutôt que `__init__` : construire pour de vrai
        # téléchargerait 118 Mo et importerait torch. On ne teste pas ce que la
        # construction fait — `ConstructionTest` s'en charge — mais ce que
        # `transcrire` enchaîne une fois le moteur là.
        self.moteur = object.__new__(HmerEngine)
        self.moteur._verrou = __import__("threading").Lock()
        self.vues = []

        essai = self

        class FauxProcesseur:
            def __call__(self, images, return_tensors=None):
                essai.vues.append(images)
                return type("Entrees", (), {"pixel_values": "pixels"})()

            @staticmethod
            def batch_decode(sortie, skip_special_tokens=False):
                return ["  x^{2} + 1  "]

        class FauxModele:
            @staticmethod
            def generate(entrees, max_new_tokens=None):
                return [[1, 2, 3]]

        self.moteur._processeur = FauxProcesseur()
        self.moteur._modele = FauxModele()

    def test_rend_texte_modele_et_version(self):
        resultat = self.moteur.transcrire(page([(10, 10), (110, 60)]))
        self.assertEqual({"texte", "modele", "version"}, set(resultat))
        self.assertEqual("x^{2} + 1", resultat["texte"])   # espaces retirés
        self.assertEqual(hmer.MODELE, resultat["modele"])
        self.assertEqual(hmer.VERSION, resultat["version"])

    def test_l_image_donnee_au_modele_est_recadree(self):
        """Le rendu ET le recadrage sont sur le chemin, pas seulement le rendu."""
        self.moteur.transcrire(page([(10, 10), (110, 60)]))
        self.assertEqual(1, len(self.vues))
        image = self.vues[0]
        self.assertLessEqual(image.width, 106)
        self.assertGreater(image.width, 0)

    def test_l_image_est_convertie_en_RGB_pour_le_modele(self):
        """RÉGRESSION, trouvée par un essai de bout en bout et par rien d'autre.

        `_bitmap` rend une image en niveaux de gris (`L`), et c'est le bon choix :
        un canal au lieu de trois pour de l'encre noire, et le seuil de
        `_recadrer` raisonne en luminance. Mais `DeiTImageProcessor` — celui que
        `preprocessor_config.json` désigne — appelle
        `infer_channel_dimension_format`, qui lève `ValueError: Unsupported number
        of image dimensions: 2` sur une image à un canal.

        Aucun test de rendu ne pouvait voir ça : ils vérifient tous des pixels
        gris, et le processeur réel n'est justement pas là. Ce test-ci fige la
        conversion à la frontière du modèle, le seul endroit où l'exigence existe.
        """
        self.moteur.transcrire(page([(10, 10), (110, 60)]))
        self.assertEqual("RGB", self.vues[0].mode)

    def test_une_page_vide_leve_avant_tout_appel_au_modele(self):
        with self.assertRaises(PageSansEncre):
            self.moteur.transcrire({"strokes": []})
        self.assertEqual([], self.vues)


class VersionTest(unittest.TestCase):
    """`modele` + `version` doivent identifier le pipeline, pas seulement le modèle."""

    def test_la_version_porte_la_revision_epinglee(self):
        """Épinglée et non `main` : la baseline de la phase 0 (24,0 %) a été
        mesurée sur CES poids, et « évaluer contre la baseline gelée » (phase 4)
        n'a de sens que si elle l'est."""
        self.assertIn(hmer._REVISION[:12], hmer.VERSION)
        self.assertIn(hmer._REVISION, hmer._BASE_URL)

    def test_la_version_porte_aussi_le_rendu(self):
        """La même encre rendue autrement donne un autre LaTeX. Une version qui ne
        porterait que la révision amont laisserait croire à l'identité de deux
        transcriptions qui n'ont rien en commun."""
        self.assertIn(f"rendu{hmer._VERSION_RENDU}", hmer.VERSION)

    def test_la_taille_annoncee_est_derivee_des_tailles_reelles(self):
        """Écrite à la main, elle finit par mentir — `core/embedding_install.py`
        annonçait 2000 Mo pour 198 Mo de wheels."""
        self.assertEqual(
            round(sum(t for _, t in hmer._FICHIERS.values()) / 1e6),
            hmer.TAILLE_ESTIMEE_MO)
        self.assertGreater(hmer.TAILLE_ESTIMEE_MO, 100)

    def test_les_empreintes_sont_des_sha256_plausibles(self):
        """Garde-fou du garde-fou : un `""` recopié rendrait la vérification
        vraie pour n'importe quel contenu."""
        self.assertEqual(8, len(hmer._FICHIERS))
        for nom, (sha, taille) in hmer._FICHIERS.items():
            with self.subTest(fichier=nom):
                self.assertRegex(sha, r"^[0-9a-f]{64}$")
                self.assertGreater(taille, 0)


class IsolationTest(unittest.TestCase):
    """Ce que `_test_env` doit avoir posé — sinon toute la suite est en danger."""

    def test_le_telechargement_est_coupe_pendant_la_suite(self):
        self.assertEqual("0", os.environ.get("EPURE_HMER_AUTOINSTALL"))

    def test_le_cache_est_detourne_hors_du_vrai_dossier(self):
        from core.paths import BACKEND_DIR  # noqa: PLC0415
        self.assertNotEqual(BACKEND_DIR / "hmer_model", resolve_hmer_dir())
        self.assertEqual(Path(os.environ["EPURE_HMER_DIR"]).resolve(),
                         resolve_hmer_dir())

    def test_le_defaut_suit_la_variable(self):
        """Résolue à CHAQUE APPEL, jamais figée dans une constante de module
        (CLAUDE.md §3.5) : la figer rendrait `$EPURE_HMER_DIR` sans effet."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"EPURE_HMER_DIR": tmp}):
                self.assertEqual(Path(tmp).resolve(), resolve_hmer_dir())

    def test_aucun_import_lourd_au_niveau_module(self):
        """`core/runtime.py` importe ce module au niveau module, et le job rapide
        de la CI n'installe ni `optimum` ni `torch` : un import en tête de fichier
        ferait échouer à la COLLECTE tout test qui monte l'app — l'incident
        `readability-lxml` rejoué (CLAUDE.md §8).

        Vérifié sur le SOURCE et non sur `sys.modules` : `torch` peut être chargé
        par un autre test, et l'assertion deviendrait alors fausse pour une raison
        qui n'a rien à voir. Ce qu'on veut interdire est une LIGNE.
        """
        source = Path(hmer.__file__).read_text(encoding="utf-8")
        # Le docstring du module est retiré AVANT de chercher : il cite les
        # imports interdits pour dire de ne pas les mettre là, et un test qui
        # tomberait sur sa propre explication serait rouge pour rien.
        tete = source.split(chr(34) * 3, 2)[2].split("class HmerIndisponible")[0]
        for interdit in ("import torch", "from optimum", "import optimum",
                         "from transformers", "from PIL", "import PIL"):
            with self.subTest(import_=interdit):
                self.assertNotIn(interdit, tete)


class ConstructionTest(unittest.TestCase):
    """`__init__` : refus propre quand rien n'est là, téléchargement vérifié sinon."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_poids_manquants_les_liste_tous(self):
        self.assertEqual(sorted(hmer._FICHIERS), sorted(poids_manquants(self.dir)))

    def test_refuse_sans_telecharger_quand_la_variable_le_coupe(self):
        """L'état d'une instance qui n'a jamais transcrit — celui que le routeur
        doit savoir servir en 500 avec un message lisible, jamais en 200 vide."""
        with patch.object(hmer.urllib.request, "urlopen") as faux:
            with self.assertRaises(HmerIndisponible) as ctx:
                HmerEngine(self.dir)
        faux.assert_not_called()
        self.assertIn("EPURE_HMER_AUTOINSTALL", str(ctx.exception))

    def test_absence_de_la_pile_python_est_dite_et_non_plantee(self):
        """Poids présents mais `optimum`/`transformers` absents : c'est le cas d'un
        `pip install` incomplet, et il doit se lire dans le message.

        Le paquet distribué est dans cette configuration EXACTE et pour toujours
        (`HORS_PAQUET_PIP`) : `core/hmer.py` y est livré et n'y fonctionnera
        jamais. Personne ne peut l'appeler — le module `encre` n'est pas livré —
        mais si quelqu'un y arrivait, il doit lire pourquoi.
        """
        for nom in hmer._FICHIERS:
            (self.dir / nom).write_bytes(b"x")
        moteur = object.__new__(HmerEngine)
        moteur._dir = self.dir
        with patch.dict(sys.modules, {"optimum.onnxruntime": None, "optimum": None}):
            with self.assertRaises(HmerIndisponible) as ctx:
                moteur._charger()
        self.assertIn("optimum-onnx", str(ctx.exception))

    # ── Téléchargement ────────────────────────────────────────────────────────

    @staticmethod
    def _reponse(contenu: bytes):
        """Un objet qui se comporte comme le retour d'`urlopen` : contexte + read(n)."""
        class Faux(io.BytesIO):
            headers = {"Content-Length": str(len(contenu))}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False
        return Faux(contenu)

    def _telecharger_bouchonne(self, contenu: bytes, sha: str):
        moteur = object.__new__(HmerEngine)
        moteur._dir = self.dir
        with patch.dict(hmer._FICHIERS, {"config.json": (sha, len(contenu))}, clear=True):
            with patch.object(hmer.urllib.request, "urlopen",
                              return_value=self._reponse(contenu)):
                with patch.dict(os.environ, {"EPURE_HMER_AUTOINSTALL": "1"}):
                    moteur._assurer_poids()

    def test_telecharge_verifie_et_renomme(self):
        contenu = b'{"vrai": true}'
        self._telecharger_bouchonne(contenu, hashlib.sha256(contenu).hexdigest())
        self.assertEqual(contenu, (self.dir / "config.json").read_bytes())
        self.assertFalse((self.dir / "config.json.part").exists())

    def test_une_empreinte_fausse_ne_laisse_aucun_fichier(self):
        """L'ordre `.part` → vérification → renommage est ce qui garantit ça, et
        il n'est pas décoratif : un fichier tronqué qui EXISTE serait cru valide
        au démarrage suivant (`poids_manquants` ne teste que la présence) et
        `onnxruntime` planterait au chargement sans jamais retenter — une panne
        définitive née d'une coupure passagère.
        """
        with self.assertRaises(HmerIndisponible) as ctx:
            self._telecharger_bouchonne(b"tronque", "00" * 32)
        self.assertIn("Empreinte incorrecte", str(ctx.exception))
        self.assertFalse((self.dir / "config.json").exists())
        self.assertFalse((self.dir / "config.json.part").exists())

    def test_une_panne_reseau_ne_laisse_aucun_fichier_non_plus(self):
        """`URLError` dérive d'`OSError`, comme les erreurs disque : un seul filet,
        et le message doit dire lequel c'était."""
        moteur = object.__new__(HmerEngine)
        moteur._dir = self.dir
        with patch.dict(hmer._FICHIERS, {"config.json": ("00" * 32, 10)}, clear=True):
            with patch.object(hmer.urllib.request, "urlopen",
                              side_effect=OSError("getaddrinfo failed")):
                with patch.dict(os.environ, {"EPURE_HMER_AUTOINSTALL": "1"}):
                    with self.assertRaises(HmerIndisponible) as ctx:
                        moteur._assurer_poids()
        self.assertIn("getaddrinfo failed", str(ctx.exception))
        self.assertFalse(list(self.dir.iterdir()))

    def test_un_fichier_deja_present_n_est_pas_retelecharge(self):
        """`poids_manquants` ne relit pas 118 Mo pour reconfirmer un sha256 déjà
        vérifié avant le renommage — ce serait une seconde par transcription."""
        for nom in hmer._FICHIERS:
            (self.dir / nom).write_bytes(b"x")
        moteur = object.__new__(HmerEngine)
        moteur._dir = self.dir
        with patch.object(hmer.urllib.request, "urlopen") as faux:
            moteur._assurer_poids()
        faux.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
