#!/usr/bin/env python3
"""Tests du script d'assemblage de paquet — `tools/faire_paquet.py`, étape B.

`docs/distribution-empaquetee.md`. Le sujet de ce fichier n'est pas « le script
marche » : c'est **ce qui ne doit pas sortir du poste d'Ilyann**. Un paquet est
une archive envoyée à quelqu'un d'autre ; s'il emporte `backend/.env`, il emporte
toutes les clés d'API cloud, et il n'existe aucun moyen de le reprendre. C'est le
genre d'erreur qui ne se commet qu'une fois et qu'on paie longtemps.

D'où la forme des tests : :func:`doit_exclure` est une fonction pure interrogée
directement, ET la copie réelle est vérifiée après coup. Les deux, parce qu'une
règle correcte peut être contournée par un parcours qui ne l'applique pas au bon
chemin relatif — c'est exactement le genre d'écart qu'un test sur la seule
fonction laisserait passer.

Ce que ce fichier NE fait pas : construire le frontend. La suite Python ne lance
pas npm (la CI a un job `frontend` pour ça). Les invariants côté build sont donc
testés sur la FORME du code — que `ATELIER_PRESENT` reste une comparaison
pliable en constante, et que l'alias de `vite.config.ts` existe. Ce ne sont pas
des tests de substitution mais les invariants réels : c'est en ajoutant un
`?.trim()` que l'Atelier s'est retrouvé dans le paquet, chunk émis et lisible,
alors que l'écran était bien caché.

**Aucun test ici ne touche l'arbre réel du dépôt.** `generated_restreint`
renomme `frontend/src/modules/generated/`, qui est surveillé par
`test_zz_donnees_reelles` (REAL_FRONTEND_MODULES) : l'exercer pour de vrai
salirait les données de l'utilisateur ET ferait tomber le garde-fou. Les globals
du script sont donc redirigés vers des copies temporaires.

Usage :
    python test_paquet.py
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent
sys.path.insert(0, str(_REPO / "tools"))

import faire_paquet as paquet  # noqa: E402


class DoitExclureTest(unittest.TestCase):
    """La règle, interrogée directement. Un échec ici est une fuite potentielle."""

    def test_les_donnees_de_l_utilisateur_ne_partent_jamais(self):
        for chemin in [
            ".env",
            ".env.local",
            "memory/instance_config.json",
            "memory/context_session.json",
            "history/2026-08-10/conv.json",
            "chroma_db/chroma.sqlite3",
            "doc_uploads/cours.pdf",
            "piper_models/fr_FR-upmc-medium.onnx",
            "epure_tray.log",
            "modules/_backups/chat-20260810/router.py",
            "modules/_staging/brouillon/router.py",
            "modules/_atelier/CONVENTIONS.md",
        ]:
            with self.subTest(chemin=chemin):
                self.assertTrue(paquet.doit_exclure(Path(chemin)),
                                f"{chemin} partirait dans le paquet")

    def test_le_code_necessaire_part_bien(self):
        """Le pendant : trop exclure casse le paquet, silencieusement.

        `module_workshop.py` et `module_validate.py` sont ici volontairement :
        `catalogue.py` importe le premier, qui importe le second au niveau
        module. Les retirer casserait l'écran Réglages du destinataire — pas
        l'Atelier.
        """
        for chemin in [
            "main.py",
            "config.yaml",
            "requirements.txt",
            "core/paths.py",
            "core/runtime.py",
            "core/logs.py",
            "core/catalogue.py",
            "core/module_workshop.py",
            "core/module_validate.py",
            "core/codeagent.py",
            "modules/chat/router.py",
            "modules/settings/router.py",
            "modules/admin/router.py",
            "modules/history/router.py",
        ]:
            with self.subTest(chemin=chemin):
                self.assertFalse(paquet.doit_exclure(Path(chemin)),
                                 f"{chemin} manquerait au paquet")

    def test_le_code_de_test_et_l_outillage_restent_ici(self):
        for chemin in ["test_paquet.py", "test_auth_surface.py", "_test_env.py",
                       "integration_modules_mount.py", "core/__pycache__/paths.cpython-312.pyc"]:
            with self.subTest(chemin=chemin):
                self.assertTrue(paquet.doit_exclure(Path(chemin)))

    def test_seuls_les_modules_du_coeur_sont_copies_depuis_l_arbre_installe(self):
        """Les autres viennent du catalogue, pour ne pas emporter ceux d'autrui."""
        for mid in sorted(paquet.MODULES_COEUR):
            with self.subTest(module=mid, coeur=True):
                self.assertFalse(paquet.doit_exclure(Path(f"modules/{mid}/router.py")))
        for mid in ("hello", "code", "kholle", "reviseur", "flashcards"):
            with self.subTest(module=mid, coeur=False):
                self.assertTrue(paquet.doit_exclure(Path(f"modules/{mid}/router.py")))

    def test_les_fichiers_atelier_seuls_sont_retires_de_core(self):
        self.assertTrue(paquet.doit_exclure(Path("core/smoke_runner.py")))
        self.assertTrue(paquet.doit_exclure(Path("core/module_worker.py")))


class CopieReelleTest(unittest.TestCase):
    """La règle appliquée pour de vrai, sur le VRAI `backend/`.

    Lecture seule sur le dépôt, écriture dans un temporaire : c'est la copie qu'on
    inspecte. Ce test est le seul qui prouve que le parcours de
    :func:`copier_backend` passe bien à `doit_exclure` un chemin relatif à
    `backend/` — une erreur d'un cran dans le `relative_to` rendrait toutes les
    règles inopérantes sans qu'aucun test de la classe précédente ne bouge.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="epure-test-paquet-")
        cls.cible = Path(cls._tmp.name) / "backend"
        cls.nb = paquet.copier_backend(cls.cible, journal=lambda *_: None)
        cls.copies = {p.relative_to(cls.cible).as_posix()
                      for p in cls.cible.rglob("*") if p.is_file()}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_la_copie_n_est_pas_vide(self):
        self.assertGreater(self.nb, 30, "copie suspecte : trop peu de fichiers")

    #: `.env.example` PART, et c'est voulu : il ne contient que des clés vides et
    #: c'est ce qui explique au destinataire comment renseigner les siennes. Il est
    #: nommé ici plutôt que couvert par un `startswith('.env')` inversé, pour que
    #: la liste des fichiers `.env*` autorisés reste explicite et courte.
    ENV_AUTORISES = frozenset({".env.example"})

    def test_aucun_env_aucune_donnee_dans_la_copie(self):
        interdits = [c for c in self.copies if c not in self.ENV_AUTORISES and (
            c.startswith((".env", "memory/", "history/", "chroma_db/",
                          "doc_uploads/", "piper_models/"))
            or c.endswith((".log", ".pyc", ".onnx"))
            or "__pycache__" in c
            or "/_backups/" in c or "/_staging/" in c or "/_atelier/" in c
        )]
        self.assertEqual(interdits, [], f"le paquet emporterait : {interdits}")

    def test_l_exemple_de_env_part_bien_et_ne_contient_aucune_valeur(self):
        """Il documente les clés cloud ; il ne doit jamais en porter une vraie."""
        self.assertIn(".env.example", self.copies)
        texte = (self.cible / ".env.example").read_text(encoding="utf-8")
        renseignees = [
            l for l in texte.splitlines()
            if "=" in l and not l.lstrip().startswith("#") and l.split("=", 1)[1].strip()
        ]
        self.assertEqual(renseignees, [],
                         f"des valeurs sont renseignées dans .env.example : {renseignees}")

    def test_le_env_reel_existe_bien_donc_le_test_precedent_a_du_sens(self):
        """Sans ce contrôle, l'assertion ci-dessus passerait sur un dépôt sans .env.

        Un test de non-fuite qui ne peut pas échouer est plus dangereux que pas de
        test : il dit « aucune clé ne part » alors qu'il n'y avait aucune clé.
        """
        if not (_BACKEND / ".env").is_file():
            self.skipTest("pas de backend/.env sur ce poste (CI) — non-fuite non prouvée ici")
        self.assertNotIn(".env", self.copies)

    def test_aucun_test_dans_la_copie(self):
        restes = [c for c in self.copies
                  if Path(c).name.startswith(("test_", "integration_", "_test_env"))]
        self.assertEqual(restes, [])

    def test_le_coeur_est_bien_la(self):
        for attendu in ("main.py", "config.yaml", "requirements.txt",
                        "core/runtime.py", "core/catalogue.py",
                        "core/module_workshop.py", "core/module_validate.py",
                        "modules/settings/router.py", "modules/chat/router.py"):
            with self.subTest(attendu=attendu):
                self.assertIn(attendu, self.copies)

    def test_le_script_d_assemblage_ne_part_pas(self):
        """`tools/` n'est pas dans `backend/`, donc rien ne peut l'y faire entrer.

        Affirmé quand même : c'est une exigence explicite du plan (« le script vit
        hors de ce qui est livré »), et la façon la plus probable de la casser
        serait de déplacer le script dans `backend/` pour qu'il importe `core.*`.
        """
        self.assertFalse((_BACKEND / "faire_paquet.py").exists())
        self.assertTrue((_REPO / "tools" / "faire_paquet.py").is_file())
        self.assertEqual([c for c in self.copies if "faire_paquet" in c], [])


class ValidationModulesTest(unittest.TestCase):
    def test_un_id_inconnu_est_refuse_en_listant_les_possibles(self):
        with self.assertRaises(paquet.ErreurPaquet) as ctx:
            paquet.valider_modules(["inexistant"])
        message = str(ctx.exception)
        self.assertIn("inexistant", message)
        self.assertIn("Disponibles", message)

    def test_les_modules_du_catalogue_sont_acceptes(self):
        dispo = sorted(paquet.modules_disponibles())
        if not dispo:
            self.skipTest("catalogue vide")
        manifestes = paquet.valider_modules(dispo[:2])
        self.assertEqual([m["id"] for m in manifestes], dispo[:2])

    def test_une_liste_vide_est_valide(self):
        """Un paquet sans module ajouté reste un paquet (cœur seul)."""
        self.assertEqual(paquet.valider_modules([]), [])


class GeneratedRestreintTest(unittest.TestCase):
    """Le filtre qui empêche le paquet d'emporter les modules faits pour d'autres.

    Travaille sur une COPIE : `generated_restreint` renomme un dossier surveillé
    par `test_zz_donnees_reelles`, et l'exercer sur le vrai arbre le salirait.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="epure-test-gen-")
        racine = Path(self._tmp.name)
        self.front = racine / "frontend"
        self.gen = self.front / "src" / "modules" / "generated"
        self.gen.mkdir(parents=True)
        for mid in ("pour_alice", "pour_bob"):
            (self.gen / mid).mkdir()
            (self.gen / mid / "Component.tsx").write_text(f"// {mid}", encoding="utf-8")

        self.catalogue = racine / "modules-catalogue"
        for mid in ("flashcards", "reviseur"):
            (self.catalogue / mid).mkdir(parents=True)
            (self.catalogue / mid / "Component.tsx").write_text(f"// {mid}", encoding="utf-8")

        self._front_original = paquet.FRONTEND
        self._cat_original = paquet.CATALOGUE
        paquet.FRONTEND = self.front
        paquet.CATALOGUE = self.catalogue

    def tearDown(self):
        paquet.FRONTEND = self._front_original
        paquet.CATALOGUE = self._cat_original
        self._tmp.cleanup()

    def _contenu(self) -> set[str]:
        return {p.relative_to(self.gen).as_posix()
                for p in self.gen.rglob("*") if p.is_file()}

    def test_pendant_le_build_seuls_les_modules_choisis_sont_visibles(self):
        avant = self._contenu()
        with paquet.generated_restreint(["flashcards"]):
            pendant = self._contenu()
        self.assertEqual(pendant, {"flashcards/Component.tsx"})
        self.assertNotIn("pour_alice/Component.tsx", pendant)
        self.assertNotIn("pour_bob/Component.tsx", pendant)
        self.assertEqual(self._contenu(), avant, "l'arbre d'origine n'a pas été restauré")

    def test_l_arbre_est_restaure_meme_sur_exception(self):
        avant = self._contenu()
        with self.assertRaises(ZeroDivisionError):
            with paquet.generated_restreint(["flashcards"]):
                raise ZeroDivisionError("build interrompu")
        self.assertEqual(self._contenu(), avant)

    def test_un_dossier_de_garde_residuel_fait_refuser(self):
        """Un build interrompu brutalement laisse la garde : ne pas écraser.

        Écraser ferait perdre les composants installés d'Ilyann, définitivement.
        """
        (self.gen.parent / "_generated_hors_paquet").mkdir()
        with self.assertRaises(paquet.ErreurPaquet) as ctx:
            with paquet.generated_restreint(["flashcards"]):
                pass
        self.assertIn("interrompu", str(ctx.exception))


def _exigences(arch: str = "amd64") -> tuple[str, list[str]]:
    """Texte du requirements généré et ses lignes ACTIVES (hors commentaires)."""
    with tempfile.TemporaryDirectory() as tmp:
        texte = paquet._exigences_du_paquet(
            Path(tmp) / "req.txt", arch).read_text(encoding="utf-8")
    actives = [l for l in texte.splitlines()
               if l.strip() and not l.lstrip().startswith("#")]
    return texte, actives


class ExigencesTest(unittest.TestCase):
    def test_la_pile_d_embedding_part_dans_le_paquet(self):
        """RENVERSÉ le 2026-08-26, et c'est le point du chantier.

        Ce test s'appelait `test_sentence_transformers_est_retire_mais_reste_visible`
        et vérifiait l'inverse : que la pile d'embedding N'était PAS installée,
        parce qu'elle pesait 198 Mo de wheels (torch) et se reportait « au premier
        usage ». Elle pèse maintenant 13,7 Mo — `onnxruntime` — donc elle part
        avec le reste, et il n'y a plus rien à reporter côté paquets. Ce qui reste
        différé, ce sont les 90 Mo de POIDS du modèle, qui ne sont pas une
        dépendance pip et n'apparaissent donc pas dans ce fichier.

        Le retirer par confusion est le risque réel : `onnxruntime` arrivait
        jusqu'ici par `faster-whisper` et `piper-tts`, tous deux exclus sur ARM64.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cible = paquet._exigences_du_paquet(Path(tmp) / "req.txt")
            texte = cible.read_text(encoding="utf-8")
        lignes_actives = [l for l in texte.splitlines()
                         if l.strip() and not l.lstrip().startswith("#")]
        self.assertTrue(any(l.lower().startswith("onnxruntime")
                            for l in lignes_actives),
                        "onnxruntime ne partirait pas : plus de moteur d'embedding")
        # L'ancienne pile ne doit pas revenir par la bande : `sentence-transformers`
        # réinstallerait scikit-learn, donc le binaire que Smart App Control bloque
        # sur la machine cible.
        self.assertFalse(any(l.lower().startswith(("sentence-transformers", "torch"))
                            for l in lignes_actives),
                        "l'ancienne pile d'embedding est revenue")
        # Retiré en commentaire et non supprimé : sinon la prochaine relecture du
        # fichier dérivé ne dit pas pourquoi il diffère de requirements.txt.
        self.assertIn("RETIRÉ DU PAQUET", texte)
        for garde in ("fastapi", "numpy", "faster-whisper", "piper-tts"):
            with self.subTest(paquet_garde=garde):
                self.assertTrue(any(l.lower().startswith(garde) for l in lignes_actives))

    def test_les_commentaires_de_requirements_sont_conserves(self):
        """Ils portent la raison de chaque épinglage (cf. requirements.txt)."""
        with tempfile.TemporaryDirectory() as tmp:
            texte = paquet._exigences_du_paquet(Path(tmp) / "req.txt").read_text(encoding="utf-8")
        self.assertIn("starlette", texte)
        self.assertGreater(sum(1 for l in texte.splitlines() if l.lstrip().startswith("#")), 10)

    def test_les_lecteurs_de_documents_partent_dans_le_paquet(self):
        """`python-docx`, `python-pptx`, `openpyxl` doivent être INSTALLÉS.

        Ce sont les lecteurs de `core/rag.py`. Les exclure par ressemblance avec
        `sentence-transformers` — « c'est du traitement de documents, ça doit être
        lourd » — coûterait cher et sans erreur au build : `_extract_text_from_path`
        rend une chaîne VIDE quand le paquet manque (dégradation volontaire), donc
        le destinataire déposerait un .pptx qui s'indexerait à zéro chunk **en
        silence**. C'est exactement le symptôme que ce lot corrige.

        Le poids ne justifie pas de les écarter : mesuré à **+6,6 Mo** sur
        `site-packages` pour les quatre paquets du lot (les trois lecteurs +
        `et-xmlfile` et `XlsxWriter`), **zéro transitif nouveau** — `lxml` et
        `Pillow` étaient déjà dans l'arbre. À comparer aux 97,9 Mo de
        `googleapiclient` que la décision 4 écarte, ou aux ~2 Go de torch.
        """
        for lecteur in ("python-docx", "python-pptx", "openpyxl"):
            with self.subTest(lecteur=lecteur):
                self.assertNotIn(lecteur, paquet.HORS_PAQUET_PIP)
                self.assertNotIn(lecteur, paquet.HORS_PAQUET_PIP_ARM64)
        with tempfile.TemporaryDirectory() as tmp:
            texte = paquet._exigences_du_paquet(Path(tmp) / "req.txt").read_text(encoding="utf-8")
        actives = [l for l in texte.splitlines()
                   if l.strip() and not l.lstrip().startswith("#")]
        for lecteur in ("python-docx", "python-pptx", "openpyxl"):
            with self.subTest(exigence=lecteur):
                self.assertTrue(any(l.lower().startswith(lecteur) for l in actives), lecteur)

    def test_les_lecteurs_de_documents_partent_aussi_sur_arm64(self):
        """Sur ARM64 aussi, et c'est le point qui mérite un test à part.

        `HORS_PAQUET_PIP_ARM64` retire ce qui n'a pas de wheel `win_arm64` — la
        voix. Les trois lecteurs publient tous une wheel `py3-none-any` sans
        aucune extension compilée (vérifié fichier par fichier), donc ils n'ont
        rien à faire dans cette liste. Les y glisser « au cas où » retirerait la
        lecture de documents d'un paquet ARM64 pour une raison inexistante.
        """
        with tempfile.TemporaryDirectory() as tmp:
            texte = paquet._exigences_du_paquet(Path(tmp) / "req.txt", "arm64").read_text(encoding="utf-8")
        actives = [l for l in texte.splitlines()
                   if l.strip() and not l.lstrip().startswith("#")]
        for lecteur in ("python-docx", "python-pptx", "openpyxl"):
            with self.subTest(exigence=lecteur):
                self.assertTrue(any(l.lower().startswith(lecteur) for l in actives), lecteur)
        # Contre-épreuve : la voix, elle, est bien retirée sur cette architecture.
        self.assertFalse(any(l.lower().startswith("piper-tts") for l in actives))

    def test_google_generativeai_est_retire_de_l_installation(self):
        """Décision 4 : rien d'autre dans requirements.txt ne dépend de lui, donc
        toute sa chaîne transitive (googleapiclient, google-api-core,
        google-auth, google-ai-generativelanguage…) disparaît avec lui.
        """
        self.assertIn("google-generativeai", paquet.HORS_PAQUET_PIP)

    def test_chromadb_et_sa_grappe_ne_sont_plus_installes_ni_purges(self):
        """Le retrait de chromadb (`docs/remplacement-vectoriel.md`, étape D) doit
        se voir aux DEUX bouts, sinon il n'est pas fait.

        Côté installation : plus aucune ligne active de `requirements.txt` ne le
        nomme. Côté paquet : plus aucune entrée de purge ne le vise — ni
        `kubernetes` (dépendance déclarée de chromadb), ni `grpcio` ou
        l'exporter OTLP, qui avaient exigé un second mécanisme de purge entier
        et un `sitecustomize.py` pour être contenus.

        Ce test échouerait aussi si quelqu'un remettait chromadb sans rétablir
        ces contournements — ce qui est le bon sens de l'échec : c'est le paquet
        qui casserait, silencieusement, à l'import.
        """
        exigences = (Path(paquet.BACKEND) / "requirements.txt").read_text(encoding="utf-8")
        actives = [l for l in exigences.splitlines()
                   if l.strip() and not l.lstrip().startswith("#")]
        self.assertFalse([l for l in actives if l.lower().startswith("chromadb")])
        self.assertNotIn("kubernetes", paquet.PURGE_SITE_PACKAGES)
        self.assertFalse(hasattr(paquet, "PURGE_DISTRIBUTIONS"))
        self.assertFalse(hasattr(paquet, "SITECUSTOMIZE"))
        self.assertFalse(hasattr(paquet, "poser_sitecustomize"))

    def test_les_scripts_de_migration_ne_partent_pas(self):
        """Ils exigent `chromadb`, qui n'est plus installé, et parlent d'un ancien
        index que le destinataire n'a jamais eu.

        Constaté en inspectant le paquet du 2026-08-13 : ils étaient bien partis.
        Les préfixes `test_`/`integration_` ne les attrapent pas — un script de
        maintenance ne se signale par aucune convention de nom, d'où la liste
        explicite. Vérifié aussi qu'ils ne sont exclus qu'à la RACINE : un module
        qui s'appellerait `modules/x/migrer_vectoriel.py` doit partir normalement.
        """
        for nom in ("migrer_vectoriel.py", "parite_vectorielle.py"):
            with self.subTest(nom=nom):
                self.assertTrue(paquet.doit_exclure(Path(nom)))
                self.assertFalse(paquet.doit_exclure(Path("modules") / "chat" / nom))

    def test_la_voix_est_retiree_du_paquet_arm64_et_seulement_la(self):
        """Décision du 2026-08-22 : voix déclarée indisponible sur Windows ARM64.

        Le blocage est à l'INSTALLATION, pas à l'usage : `ctranslate2` (dont dépend
        `faster-whisper`) ne publie aucune wheel `win_arm64` ni aucune sdist, donc
        `pip install -r requirements.txt` échoue avant que le backend démarre.

        Le « et seulement là » est la moitié qui compte. Deux façons de se tromper,
        toutes les deux vérifiées ici : retirer trop peu (le paquet ARM64 ne
        s'installe pas), ou retirer trop — `onnxruntime` et `torch` ont l'air liés
        à la voix mais ont tous les deux leur wheel ARM64, et les écarter coûterait
        le RAG, qui marche parfaitement sur cette architecture.
        """
        _, actives = _exigences("arm64")
        for retire in paquet.HORS_PAQUET_PIP_ARM64:
            with self.subTest(retire=retire):
                self.assertFalse(
                    any(l.lower().startswith(retire) for l in actives),
                    f"{retire} serait installé sur ARM64 — pip échouerait")
        for garde in ("fastapi", "numpy", "onnxruntime", "pypdf", "openai"):
            with self.subTest(arm64_garde=garde):
                # onnxruntime n'est PAS dans requirements.txt (transitif) : ne le
                # tester que s'il y est, sinon ce test affirmerait autre chose.
                if any(l.lower().startswith(garde) for l in _exigences("amd64")[1]):
                    self.assertTrue(any(l.lower().startswith(garde) for l in actives))

    def test_le_paquet_x64_garde_la_voix(self):
        """Le pendant strict : l'exclusion ne doit pas fuir vers l'architecture
        par défaut. C'est le paquet que reçoit tout le monde sauf sandr.
        """
        _, actives = _exigences("amd64")
        for garde in paquet.HORS_PAQUET_PIP_ARM64:
            with self.subTest(x64_garde=garde):
                self.assertTrue(
                    any(l.lower().startswith(garde) for l in actives),
                    f"{garde} a disparu du paquet x64 — la voix y est supportée")

    def test_l_exclusion_arm64_est_annoncee_comme_definitive(self):
        """Les deux motifs de retrait ne se lisent pas pareil, et le fichier livré
        doit le dire : `google-generativeai` est récupérable, la voix ARM64 est
        définitive. Un unique « RETIRÉ » laisserait le
        destinataire attendre une installation qui n'arrivera jamais.
        """
        texte, _ = _exigences("arm64")
        self.assertIn("RETIRÉ DU PAQUET ARM64", texte)
        self.assertIn("win_arm64", texte)
        self.assertIn("installé au premier usage", texte)   # l'autre motif, intact

    def test_une_architecture_inconnue_est_refusee(self):
        """Un `--arch` fantaisiste ne doit pas produire un paquet x64 en silence."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(paquet.ErreurPaquet):
                paquet._exigences_du_paquet(Path(tmp) / "req.txt", "i386")

    def test_l_arch_choisit_aussi_le_runtime_embeddable(self):
        """Exigences ARM64 + Python x64 = un paquet qui ne démarre pas chez le
        destinataire. Les deux suivent la même variable, ou aucune des deux.
        """
        for arch in paquet.ARCHS:
            with self.subTest(arch=arch):
                url = paquet.URL_EMBEDDABLE.format(v=paquet.VERSION_PYTHON, arch=arch)
                self.assertIn(f"embed-{arch}.zip", url)

    def test_l_arch_du_paquet_est_ecrite_dans_le_manifeste(self):
        """`PAQUET.json` doit dire ce que le destinataire n'a pas.

        Sans ça, un micro absent se lit comme une interface cassée — et personne,
        six mois plus tard, ne saura si ce paquet-là avait la voix.
        """
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<html></html>", encoding="utf-8")
            for arch, voix in (("arm64", False), ("amd64", True)):
                with self.subTest(arch=arch):
                    staging = Path(tmp) / f"staging-{arch}"
                    infos = paquet.assembler(staging, dist, [], None,
                                             journal=lambda *_: None, arch=arch)
                    self.assertEqual(infos["arch"], arch)
                    self.assertIs(infos["voix"], voix)

    def test_plus_aucun_contournement_arm64_a_tenir_pour_l_embedding(self):
        """REMPLACE `test_l_index_arm64_de_torch_est_indique_au_destinataire`.

        Ce test exigeait que `requirements.txt` porte la consigne
        `--index-url https://download.pytorch.org/whl/cpu`, parce que PyPI ne
        publie aucune wheel `win_arm64` pour torch et que sans elle
        `pip install -r requirements.txt` échouait sur ARM64. Le contournement a
        disparu avec torch : `onnxruntime` publie sa wheel `win_arm64` sur PyPI
        comme tout le monde (cp311 → cp314, vérifié).

        Ce qui est vérifié maintenant est le contraire, et il faut le vérifier :
        que la consigne ne traîne PAS, active, dans le fichier livré. Une
        instruction d'installer torch depuis un index tiers, dans un dépôt qui
        n'en dépend plus, ferait télécharger 2 Go à un destinataire qui suit ce
        qu'il lit.
        """
        texte = (Path(paquet.BACKEND) / "requirements.txt").read_text(encoding="utf-8")
        lignes_actives = [l for l in texte.splitlines()
                          if l.strip() and not l.lstrip().startswith("#")]
        self.assertFalse(any("download.pytorch.org" in l for l in lignes_actives),
                         "une consigne d'installation de torch subsiste, active")
        # `win_arm64` doit rester mentionné : c'est là que se lit ce qui a été
        # vérifié pour cette architecture, et c'est ce qui a coûté le plus cher.
        self.assertIn("win_arm64", texte)

    def test_le_store_vectoriel_est_exclu_du_paquet(self):
        """`vector_db/` contient le TEXTE des fiches et PDF indexés, pas seulement
        des vecteurs : il est aussi sensible que `history/` ou `doc_uploads/`.

        Il a remplacé `chroma_db/` sous un NOM NEUF — exactement la situation où
        une liste d'exclusion se fait distancer par le code sans que rien ne le
        signale, et où le paquet part avec les documents de son auteur.
        """
        self.assertIn("vector_db", paquet.EXCLUS_RACINE)
        self.assertIn("chroma_db", paquet.EXCLUS_RACINE)


class BuildCroiseTest(unittest.TestCase):
    """Un paquet se construit sur l'architecture qu'il vise — refus, pas avertissement.

    Le script a averti au lieu de refuser jusqu'au 2026-08-24, sur une affirmation
    fausse : « le zip embeddable et les wheels viennent de PyPI, donc l'archive est
    correcte, simplement non testable ici ». Vrai du téléchargement, faux de la
    suite — `preparer_python` EXÉCUTE le `python.exe` de la cible (`get-pip.py`,
    puis `pip install`), et Windows ne lance pas un binaire ARM64 sur un hôte x64
    (`OSError: [WinError 216]` ; l'émulation ne va que d'ARM64 vers x64).

    Ce qui est éprouvé ici, et qui est le cœur du bug : que l'échec arrive **avant**
    `construire_frontend`. Le mur physique est dans `preparer_python`, qui vient
    après plusieurs minutes de `npm run build` — un refus tardif serait correct et
    quand même une régression.
    """

    def setUp(self):
        self._hote = paquet.arch_hote
        self.addCleanup(setattr, paquet, "arch_hote", self._hote)

    def _poser_hote(self, arch: str) -> None:
        paquet.arch_hote = lambda: arch

    def _main_muet(self, argv: list[str]) -> int:
        """`main` écrit son journal de build sur stdout/stderr — une vingtaine de
        lignes par appel, qui noieraient la sortie de la suite."""
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return paquet.main(argv)

    def test_l_arch_de_l_hote_passe(self):
        """Contre-épreuve : le garde-fou ne doit pas bloquer le cas normal."""
        for arch in paquet.ARCHS:
            with self.subTest(arch=arch):
                self._poser_hote(arch)
                paquet.exiger_arch_native(arch)   # ne lève pas

    def test_une_autre_arch_est_refusee(self):
        self._poser_hote("amd64")
        with self.assertRaises(paquet.ErreurPaquet) as ctx:
            paquet.exiger_arch_native("arm64")
        message = str(ctx.exception)
        # Le message doit dire quoi FAIRE, pas seulement que c'est refusé : c'est
        # tout ce que l'ancien avertissement ne disait pas.
        self.assertIn("arm64", message)
        self.assertIn("amd64", message)
        self.assertIn("--sauter-python", message)

    def test_preparer_python_refuse_avant_de_telecharger(self):
        """La règle vit dans la fonction qui la subit, pas seulement dans `main`.

        Un futur appelant (script d'automatisation, test) qui court-circuiterait la
        ligne de commande buterait sur le même `WinError 216`. Et le refus doit
        précéder le téléchargement : sinon un build croisé coûte 11 Mo et deux
        minutes avant d'échouer.
        """
        appels = []
        ancien = paquet._telecharger
        paquet._telecharger = lambda *a, **k: appels.append(a) or Path("x")
        self.addCleanup(setattr, paquet, "_telecharger", ancien)
        self._poser_hote("amd64")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(paquet.ErreurPaquet):
                paquet.preparer_python(Path(tmp) / "python", None, None,
                                       journal=lambda *_: None, arch="arm64")
        self.assertEqual(appels, [])

    def test_main_refuse_sans_construire_le_frontend(self):
        appels = []
        ancien = paquet.construire_frontend
        paquet.construire_frontend = lambda *a, **k: appels.append(a)
        self.addCleanup(setattr, paquet, "construire_frontend", ancien)
        self._poser_hote("amd64")
        with tempfile.TemporaryDirectory() as tmp:
            code = self._main_muet(["--destinataire", "sandr", "--arch", "arm64",
                                    "--sortie", tmp])
        self.assertEqual(code, 1)
        self.assertEqual(appels, [], "npm run build lancé avant le refus")

    def test_sauter_python_est_la_derogation(self):
        """`--sauter-python` n'exécute jamais l'interpréteur cible : rien ne casse.

        C'est la seule façon d'éprouver l'assemblage d'un paquet ARM64 depuis x64 —
        et l'archive obtenue n'a pas de `python/`, donc n'est pas livrable. Le test
        vérifie les deux : que ça passe, et que le runtime est bien absent.
        """
        self._poser_hote("amd64")
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<html></html>", encoding="utf-8")
            ancien = paquet.construire_frontend
            paquet.construire_frontend = lambda *a, **k: dist
            self.addCleanup(setattr, paquet, "construire_frontend", ancien)
            sortie = Path(tmp) / "sortie"
            code = self._main_muet(["--destinataire", "sandr", "--arch", "arm64",
                                    "--sauter-python", "--sortie", str(sortie)])
            self.assertEqual(code, 0)
            archive = sortie / "epure-sandr.zip"
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as z:
                noms = z.namelist()
            self.assertFalse([n for n in noms if n.startswith("python/")],
                             "un runtime a été assemblé sans preparer_python")
            with zipfile.ZipFile(archive) as z:
                manifeste = json.loads(z.read("PAQUET.json").decode("utf-8"))
            self.assertEqual(manifeste["arch"], "arm64")
            self.assertIs(manifeste["voix"], False)


class PurgeSitePackagesTest(unittest.TestCase):
    """Le seul mécanisme de purge qui subsiste : par nom de dossier — et il ne
    vise plus rien.

    Il y en avait deux. Le second (`_purger_distribution`, par lecture du
    `RECORD` d'un `.dist-info`) existait parce que `grpcio` et
    `opentelemetry-exporter-otlp-proto-grpc` ne s'installent pas comme un simple
    dossier `site-packages/<nom>/` et qu'aucun des deux ne pouvait être retiré à
    l'installation — chromadb les déclarait en dépendances directes. Retiré avec
    chromadb (`docs/remplacement-vectoriel.md`, étape D) : plus rien à purger de
    cette façon, donc plus de mécanisme, donc plus de tests. Ce fichier rétrécit
    avec le script qu'il surveille, au lieu de garder en vie des tests qui
    passeraient encore parfaitement sur du code que personne n'appelle.

    Le premier, lui, a perdu sa cible le 2026-08-23 : `pip` et `setuptools`
    restent dans le paquet, parce que l'application installe elle-même sa pile
    d'embedding au premier usage de la recherche documentaire
    (`backend/core/embedding_install.py`). Ce qui est testé ici a donc changé de
    nature — ce n'est plus « la purge fait bien son travail » mais **« la purge
    ne remange pas `pip` »**, qui est l'invariant dont la violation rendrait la
    recherche documentaire irréparable chez le destinataire.
    """

    def test_pip_et_setuptools_ne_sont_pas_purges(self):
        """L'invariant qui compte, et le seul que la relecture ne garantit pas.

        Remettre `"pip"` dans cette liste livrerait un paquet qu'on ne peut pas
        réparer. C'était l'écart 3 de `docs/distribution-empaquetee.md` quand
        `HORS_PAQUET_PIP` reportait l'installation de `sentence-transformers` au
        premier usage — purger `pip` retirait l'outil qui seul pouvait la faire.
        La pile d'embedding est embarquée depuis le 2026-08-26, donc ce cas précis
        a disparu ; le motif reste : `core/embedding_install.py` DIT au
        destinataire de lancer `pip install -r requirements.txt` quand
        `onnxruntime` manque, et ce conseil doit rester suivable.

        Le test porte sur la LISTE et non sur un paquet construit : la suite ne
        construit aucun paquet (plusieurs minutes de `pip install`), et c'est la
        liste qui décide.
        """
        for nom in ("pip", "setuptools", "pkg_resources"):
            self.assertNotIn(nom, paquet.PURGE_SITE_PACKAGES)
        # Vide aujourd'hui. L'assertion suivante n'est pas un doublon : elle dit
        # qu'aucun AUTRE paquet n'y a été glissé sans passer par cette relecture.
        self.assertEqual(paquet.PURGE_SITE_PACKAGES, ())

    def test_retire_les_dossiers_listes_et_signale_ce_qu_il_a_gagne(self):
        """Le mécanisme lui-même, sur une liste posée par le test.

        La liste réelle étant vide, l'éprouver telle quelle ne testerait plus
        rien. On la remplace le temps du test : la mécanique reste en place pour
        qu'un besoin futur ait où s'écrire, et elle doit rester correcte.
        """
        original = paquet.PURGE_SITE_PACKAGES
        paquet.PURGE_SITE_PACKAGES = ("un_paquet_a_purger",)
        try:
            with tempfile.TemporaryDirectory(prefix="epure-test-purge-") as tmp:
                racine = Path(tmp)
                sp = racine / "Lib" / "site-packages"
                sp.mkdir(parents=True)
                (sp / "un_paquet_a_purger").mkdir()
                (sp / "un_paquet_a_purger" / "__init__.py").write_text("# x", encoding="utf-8")
                garde = sp / "fastapi"
                garde.mkdir()
                (garde / "__init__.py").write_text("# fastapi", encoding="utf-8")

                gagne = paquet.purger_site_packages(racine, journal=lambda *_: None)

                # Dans le `with` : hors du bloc, le dossier temporaire entier est
                # supprimé, donc `assertFalse(...exists())` passerait sans rien
                # prouver et `assertTrue(...)` sur ce qui devait SURVIVRE échouerait.
                self.assertFalse((sp / "un_paquet_a_purger").exists())
                self.assertIn("un_paquet_a_purger", gagne)
                # Ce qui n'est pas listé reste : une purge trop large est le vrai risque.
                self.assertTrue((garde / "__init__.py").exists())
        finally:
            paquet.PURGE_SITE_PACKAGES = original

    def test_pip_survit_a_la_purge_reelle(self):
        """Contre-épreuve, sur la liste RÉELLE : `pip/` est toujours là après.

        Le test précédent passe sur une liste fabriquée, donc il resterait vert si
        quelqu'un remettait `pip` dans la vraie. Celui-ci joue la purge telle
        qu'elle partira dans le paquet, sur un `site-packages` qui contient `pip`,
        et vérifie qu'il en ressort. C'est aussi ce qui rendrait visible un
        `purger_site_packages` qui se mettrait à purger par un autre chemin que la
        liste.
        """
        with tempfile.TemporaryDirectory(prefix="epure-test-purge-") as tmp:
            racine = Path(tmp)
            sp = racine / "Lib" / "site-packages"
            sp.mkdir(parents=True)
            for nom in ("pip", "setuptools", "pkg_resources"):
                (sp / nom).mkdir()
                (sp / nom / "__init__.py").write_text(f"# {nom}", encoding="utf-8")
            cache = sp / "pip" / "__pycache__"
            cache.mkdir()
            (cache / "x.cpython-312.pyc").write_bytes(b"\x00")

            gagne = paquet.purger_site_packages(racine, journal=lambda *_: None)

            self.assertEqual(gagne, {})
            for nom in ("pip", "setuptools", "pkg_resources"):
                self.assertTrue((sp / nom / "__init__.py").is_file(), nom)
            # Le geste qui reste : les `__pycache__` partent, eux.
            self.assertFalse(cache.exists())

    def test_un_dossier_absent_ne_leve_pas(self):
        """Un `site-packages` sans rien à purger n'est pas une erreur de build."""
        with tempfile.TemporaryDirectory(prefix="epure-test-purge-") as tmp:
            racine = Path(tmp)
            (racine / "Lib" / "site-packages").mkdir(parents=True)
            gagne = paquet.purger_site_packages(racine, journal=lambda *_: None)
        self.assertEqual(gagne, {})


class DrapeauxDeBuildTest(unittest.TestCase):
    """Invariants de forme côté frontend — la suite Python ne lance pas npm.

    Ce ne sont pas des tests par défaut : c'est précisément en changeant la FORME
    de ces deux expressions qu'on remet l'Atelier dans le paquet, sans que rien
    d'autre ne change.
    """

    @staticmethod
    def _code_seul(chemin: Path) -> str:
        """Le fichier sans ses commentaires.

        Nécessaire : le docstring d'`atelier.ts` CITE les formes interdites pour
        expliquer pourquoi elles le sont. Chercher dans le fichier entier faisait
        échouer le test sur sa propre explication.
        """
        lignes = []
        dans_bloc = False
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            nu = ligne.strip()
            if nu.startswith("/*"):
                dans_bloc = True
            if dans_bloc:
                if "*/" in nu:
                    dans_bloc = False
                continue
            if nu.startswith("//"):
                continue
            lignes.append(ligne)
        return "\n".join(lignes)

    def test_atelier_present_reste_une_comparaison_pliable(self):
        code = self._code_seul(_REPO / "frontend" / "src" / "atelier.ts")
        self.assertIn("import.meta.env.VITE_ATELIER !== '0'", code,
                      "ATELIER_PRESENT doit rester une comparaison DIRECTE : avec un "
                      "?.trim() ou un String(), rolldown ne plie plus la constante, "
                      "la branche morte reste atteignable, et le chunk Workshop-*.js "
                      "est émis avec le code de l'Atelier dedans (mesuré : 26,1 ko)")
        for interdit in ("?.trim()", "String(", "?? "):
            with self.subTest(interdit=interdit):
                self.assertNotIn(interdit, code)

    def test_l_alias_qui_vide_le_chunk_existe(self):
        config = (_REPO / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
        self.assertIn("AtelierAbsent", config)
        self.assertIn("VITE_ATELIER", config)
        self.assertTrue((_REPO / "frontend" / "src" / "components" / "AtelierAbsent.tsx").is_file())

    def test_le_script_pose_les_deux_drapeaux(self):
        source = (_REPO / "tools" / "faire_paquet.py").read_text(encoding="utf-8")
        self.assertIn('"VITE_API_URL": "/"', source,
                      "le mode paquet se règle avec '/', pas la chaîne vide : sous "
                      "Windows $env:VAR = '' supprime la variable")
        self.assertIn('"VITE_ATELIER": "0"', source)


class AssemblageTest(unittest.TestCase):
    """L'arborescence produite, sans runtime Python (long) ni npm."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="epure-test-asm-")
        racine = Path(self._tmp.name)
        self.staging = racine / "paquet"
        self.staging.mkdir()
        self.dist = racine / "dist"
        (self.dist / "_assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<html/>", encoding="utf-8")
        (self.dist / "_assets" / "index-A.js").write_text("//", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_arborescence_conforme_aux_defauts_de_core_paths(self):
        """`app/` tient le rôle de racine du dépôt — aucune variable à poser.

        Si cette disposition change, le lanceur du paquet devra poser cinq
        variables d'environnement, et un oubli ne se verra qu'à l'usage.
        """
        dispo = sorted(paquet.modules_disponibles())
        manifestes = paquet.valider_modules(dispo[:1]) if dispo else []
        infos = paquet.assembler(self.staging, self.dist, manifestes, None,
                                journal=lambda *_: None)

        self.assertTrue((self.staging / "app" / "backend" / "main.py").is_file())
        self.assertTrue((self.staging / "app" / "frontend" / "dist" / "index.html").is_file())
        self.assertFalse(infos["atelier"])
        self.assertEqual(infos["frontend"]["VITE_API_URL"], "/")

        if manifestes:
            mid = manifestes[0]["id"]
            dossier = self.staging / "app" / "backend" / "modules" / mid
            self.assertTrue((dossier / "manifest.json").is_file())
            self.assertTrue((dossier / "router.py").is_file())

    def test_le_catalogue_ne_part_pas(self):
        """Décision assumée : le destinataire active/désactive, il n'installe pas.

        Installer depuis le catalogue écrit un Component.tsx dans les sources du
        frontend, ce qui suppose un build. Sans catalogue livré,
        `GET /settings/catalogue` renvoie une liste vide et le bouton n'apparaît
        pas — l'incapacité est honnête plutôt que cassée.
        """
        paquet.assembler(self.staging, self.dist, [], None, journal=lambda *_: None)
        self.assertFalse((self.staging / "app" / "modules-catalogue").exists())
        self.assertEqual(list(self.staging.rglob("modules-catalogue")), [])

    def test_aucune_donnee_ni_env_dans_le_staging_complet(self):
        """Le contrôle de bout en bout, sur l'arborescence réellement zippée.

        Deux `.env` sont désormais attendus dans le paquet et aucun ne vient du
        disque d'Ilyann : `.env.example` (copié, sans valeur — vérifié par
        :meth:`test_l_exemple_de_env_part_bien_et_ne_contient_aucune_valeur`) et
        `app/backend/.env` (ÉCRIT à partir de :data:`ENV_PAQUET`). Les deux sont
        donc exemptés **par chemin exact**, jamais par motif : un `.env` ailleurs
        dans l'arbre reste une fuite, et c'est la seule forme de cette liste qui
        garde le test capable de dire non.
        """
        paquet.assembler(self.staging, self.dist, [], None, journal=lambda *_: None)
        attendus = {"app/backend/.env", "app/backend/.env.example"}
        fuites = [p.relative_to(self.staging).as_posix()
                  for p in self.staging.rglob("*") if p.is_file()
                  and p.relative_to(self.staging).as_posix() not in attendus
                  and (p.name.startswith(".env") or p.suffix in (".log", ".pyc")
                       or p.name.startswith(("test_", "integration_", "_test_env")))]
        self.assertEqual(fuites, [])

    def test_le_env_du_paquet_eteint_vraiment_l_atelier(self):
        """Le `.env` livré porte la ligne, et c'est ce qui coupe les routes.

        `VITE_ATELIER=0` sort l'Atelier du bundle donc de l'écran ; les ROUTES,
        elles, dépendent d'`EPURE_ATELIER`, que `main.py` lit avec **`"1"` pour
        défaut**. Le paquet n'ayant aucun lanceur, personne ne posait la variable :
        l'Atelier d'un paquet livré était invisible et joignable en HTTP.
        """
        paquet.assembler(self.staging, self.dist, [], None, journal=lambda *_: None)
        env = self.staging / "app" / "backend" / ".env"
        self.assertTrue(env.is_file(), "aucun .env écrit dans le paquet")
        lignes = [l.strip() for l in env.read_text(encoding="utf-8").splitlines()]
        self.assertIn("EPURE_ATELIER=0", lignes)

    def test_le_env_du_paquet_ne_contient_aucune_valeur_secrete(self):
        """Il porte le même nom que le `.env` d'Ilyann : il ne doit rien porter d'autre.

        `EXCLUS_FICHIERS` interdit de le COPIER ; ici on en ÉCRIT un. Les deux
        gestes coexistent tant que le contenu écrit reste connu et vide de secrets
        — donc ce test, qui refuse toute affectation non vide autre que celle
        qu'on a voulue.
        """
        paquet.assembler(self.staging, self.dist, [], None, journal=lambda *_: None)
        env = self.staging / "app" / "backend" / ".env"
        affectations = {}
        for ligne in env.read_text(encoding="utf-8").splitlines():
            nu = ligne.strip()
            if nu and not nu.startswith("#") and "=" in nu:
                cle, _, valeur = nu.partition("=")
                affectations[cle.strip()] = valeur.strip()
        self.assertEqual(affectations, {"EPURE_ATELIER": "0"},
                         "le .env du paquet ne doit porter QUE l'extinction de "
                         "l'Atelier — toute autre valeur est soit un secret, soit "
                         "un réglage que le destinataire n'a pas choisi")

    def test_le_manifeste_lit_l_etat_reel_au_lieu_de_l_affirmer(self):
        """`PAQUET.json` doit CONSTATER l'état, pas le déclarer.

        Le test qui compte : on efface le `.env` derrière `assembler()` et on
        vérifie que la règle relit `True`. Avec l'ancien `"atelier": False` en dur,
        un paquet sans extinction effective se serait décrit comme éteint — c'est
        exactement l'écart qui a existé, et une assertion sur le seul cas nominal
        ne l'aurait pas vu.
        """
        infos = paquet.assembler(self.staging, self.dist, [], None,
                                 journal=lambda *_: None)
        env = self.staging / "app" / "backend" / ".env"
        self.assertFalse(infos["atelier"])
        self.assertFalse(paquet.atelier_actif_selon(env))

        env.unlink()
        self.assertTrue(paquet.atelier_actif_selon(env),
                        "un .env absent doit rendre True (le défaut de main.py) : "
                        "ne jamais rendre une absence rassurante")

    def test_le_manifeste_suit_le_env_meme_quand_il_n_eteint_rien(self):
        """Le seul test que `"atelier": False` en dur ne peut pas passer.

        Mesuré : remettre la constante en dur laissait les 43 autres tests verts,
        y compris celui d'au-dessus — parce qu'il n'affirme `False` que dans le cas
        nominal, où les deux écritures coïncident. Tant qu'aucun test ne fait
        DIVERGER le fichier et le manifeste, « constater » et « déclarer » sont
        indiscernables, et c'est la déclaration qui finit par mentir.

        On force donc un `.env` qui n'éteint rien : le manifeste doit le dire.
        """
        d_origine = paquet.ENV_PAQUET
        paquet.ENV_PAQUET = "# rien ici n'éteint l'Atelier\n"
        try:
            infos = paquet.assembler(self.staging, self.dist, [], None,
                                     journal=lambda *_: None)
        finally:
            paquet.ENV_PAQUET = d_origine
        self.assertTrue(
            infos["atelier"],
            "PAQUET.json annonce l'Atelier éteint alors que le .env livré ne "
            "l'éteint pas — le champ est déclaré, pas mesuré",
        )

    def test_la_regle_de_lecture_est_bien_celle_de_main_py(self):
        """Même défaut, mêmes cas limites que `main.py:_ATELIER_ACTIF`.

        Si l'une des deux dérive, `PAQUET.json` décrira une instance qui n'est pas
        celle qui démarre — le mode de panne précis qu'on corrige ici.
        """
        source = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("EPURE_ATELIER", "1").strip() != "0"', source,
                      "la règle de main.py a changé : aligner atelier_actif_selon()")

        env = self.staging / "env-test"
        for contenu, actif in [
            ("EPURE_ATELIER=0\n", False),
            ("EPURE_ATELIER= 0 \n", False),      # dotenv strippe, main.py aussi
            ('EPURE_ATELIER="0"\n', False),      # guillemets : dotenv les retire
            ("EPURE_ATELIER=1\n", True),
            ("# EPURE_ATELIER=0\n", True),       # commenté = non posé
            ("", True),                          # vide = défaut de main.py
            ("AUTRE=0\n", True),
        ]:
            with self.subTest(contenu=contenu):
                env.write_text(contenu, encoding="utf-8")
                self.assertEqual(paquet.atelier_actif_selon(env), actif)


if __name__ == "__main__":
    unittest.main(verbosity=2)
