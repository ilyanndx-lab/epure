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

import json
import os
import shutil
import sys
import tempfile
import unittest
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


class ExigencesTest(unittest.TestCase):
    def test_sentence_transformers_est_retire_mais_reste_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            cible = paquet._exigences_sans_torch(Path(tmp) / "req.txt")
            texte = cible.read_text(encoding="utf-8")
        lignes_actives = [l for l in texte.splitlines()
                         if l.strip() and not l.lstrip().startswith("#")]
        self.assertFalse(any(l.lower().startswith("sentence-transformers")
                            for l in lignes_actives),
                        "sentence-transformers serait installé (torch dans le paquet)")
        # Retiré en commentaire et non supprimé : sinon la prochaine relecture du
        # fichier dérivé ne dit pas pourquoi il diffère de requirements.txt.
        self.assertIn("RETIRÉ DU PAQUET", texte)
        for garde in ("fastapi", "chromadb", "faster-whisper", "piper-tts"):
            with self.subTest(paquet_garde=garde):
                self.assertTrue(any(l.lower().startswith(garde) for l in lignes_actives))

    def test_les_commentaires_de_requirements_sont_conserves(self):
        """Ils portent la raison de chaque épinglage (cf. requirements.txt)."""
        with tempfile.TemporaryDirectory() as tmp:
            texte = paquet._exigences_sans_torch(Path(tmp) / "req.txt").read_text(encoding="utf-8")
        self.assertIn("starlette", texte)
        self.assertGreater(sum(1 for l in texte.splitlines() if l.lstrip().startswith("#")), 10)

    def test_kubernetes_est_purge_du_resultat_et_non_de_l_installation(self):
        """Il n'est pas retirable par pip : c'est une dépendance de chromadb.

        Mesuré (§0) : importé seulement par
        `chromadb/segment/impl/distributed/segment_directory.py`, jamais chargé par
        un `PersistentClient` — le seul client que le dépôt construise.
        """
        self.assertIn("kubernetes", paquet.PURGE_SITE_PACKAGES)
        self.assertNotIn("kubernetes", paquet.HORS_PAQUET_PIP)

    def test_google_generativeai_est_retire_de_l_installation(self):
        """Décision 4 : contrairement à `kubernetes`, il PEUT être retiré à
        l'install — rien d'autre dans requirements.txt ne dépend de lui, donc
        toute sa chaîne transitive (googleapiclient, google-api-core,
        google-auth, google-ai-generativelanguage…) disparaît avec lui.
        """
        self.assertIn("google-generativeai", paquet.HORS_PAQUET_PIP)

    def test_grpcio_et_otel_grpc_sont_des_dependances_directes_de_chromadb_donc_purgees_apres(self):
        """Contrairement à `google-generativeai`, ceux-là ne peuvent pas être
        retirés de `requirements.txt` : chromadb les déclare lui-même
        (`Requires-Dist: grpcio>=1.58.0`, `...otlp-proto-grpc>=1.2.0`), donc
        `pip` les réinstallerait quand même. D'où `PURGE_DISTRIBUTIONS`, pas
        `HORS_PAQUET_PIP`.
        """
        for nom in ("grpcio", "opentelemetry-exporter-otlp-proto-grpc",
                    "googleapis-common-protos"):
            with self.subTest(nom=nom):
                self.assertIn(nom, paquet.PURGE_DISTRIBUTIONS)
                self.assertNotIn(nom, paquet.HORS_PAQUET_PIP)


class PurgeDistributionsTest(unittest.TestCase):
    """`_purger_distribution` sur un `site-packages` synthétique.

    Ni `grpcio` ni `opentelemetry-exporter-otlp-proto-grpc` ne s'installent
    comme un simple dossier `site-packages/<nom>/` (cf. `PURGE_DISTRIBUTIONS`) :
    `grpcio` pose `grpc/`, et l'exporter OTLP pose
    `opentelemetry/exporter/otlp/proto/grpc/`, un espace de noms PARTAGÉ avec
    `opentelemetry-exporter-otlp-proto-common`. Le risque n'est pas de ne rien
    retirer — c'est de retirer trop, et d'emporter `common/` avec `grpc/`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="epure-test-purge-")
        self.sp = Path(self._tmp.name) / "site-packages"
        self.sp.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _poser_distribution(self, nom_dist, nom_dossier_info, fichiers):
        """Pose une fausse distribution : ses fichiers + son `.dist-info` avec RECORD."""
        for relatif, contenu in fichiers.items():
            chemin = self.sp / relatif
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_text(contenu, encoding="utf-8")
        info = self.sp / f"{nom_dossier_info}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {nom_dist}\nVersion: 0\n",
                                       encoding="utf-8")
        lignes = [f"{r},," for r in fichiers] + [
            f"{nom_dossier_info}.dist-info/METADATA,,",
            f"{nom_dossier_info}.dist-info/RECORD,,",
        ]
        (info / "RECORD").write_text("\n".join(lignes) + "\n", encoding="utf-8")
        return info

    def test_grpcio_retire_grpc_pas_grpcio(self):
        """Le dossier posé par `grpcio` s'appelle `grpc/`, jamais `grpcio/`."""
        self._poser_distribution("grpcio", "grpcio-1.80.0", {
            "grpc/__init__.py": "# grpc",
            "grpc/_cython/cygrpc.pyd": "binaire",
        })
        mo = paquet._purger_distribution(self.sp, "grpcio", journal=lambda *_: None)
        self.assertIsNotNone(mo, "None signifierait « pas installée » — elle l'était")
        self.assertFalse((self.sp / "grpc").exists())
        self.assertEqual(list(self.sp.glob("grpcio-*.dist-info")), [])

    def test_otel_grpc_retire_sans_emporter_le_namespace_partage(self):
        """`common/` (une autre distribution, dans le même dossier `proto/`) doit survivre."""
        self._poser_distribution("opentelemetry-exporter-otlp-proto-common",
                                 "opentelemetry_exporter_otlp_proto_common-1.42.1", {
            "opentelemetry/exporter/otlp/proto/common/__init__.py": "# common",
        })
        self._poser_distribution("opentelemetry-exporter-otlp-proto-grpc",
                                 "opentelemetry_exporter_otlp_proto_grpc-1.42.1", {
            "opentelemetry/exporter/otlp/proto/grpc/__init__.py": "# grpc",
            "opentelemetry/exporter/otlp/proto/grpc/trace_exporter/__init__.py":
                "class OTLPSpanExporter: ...",
        })

        paquet._purger_distribution(self.sp, "opentelemetry-exporter-otlp-proto-grpc",
                                    journal=lambda *_: None)

        self.assertFalse(
            (self.sp / "opentelemetry" / "exporter" / "otlp" / "proto" / "grpc").exists())
        commun = self.sp / "opentelemetry" / "exporter" / "otlp" / "proto" / "common"
        self.assertTrue(commun.is_dir(), "la distribution voisine ne doit pas disparaître")
        self.assertTrue((commun / "__init__.py").is_file())

    def test_une_distribution_absente_ne_leve_pas(self):
        """`--sauter-python`, ou une distribution déjà purgée : silence, pas d'erreur."""
        self.assertIsNone(paquet._purger_distribution(self.sp, "grpcio", journal=lambda *_: None))

    def test_purger_site_packages_couvre_les_deux_mecanismes(self):
        """Bout en bout : `PURGE_SITE_PACKAGES` (dossier direct) ET
        `PURGE_DISTRIBUTIONS` (RECORD) sont appliqués par le même appel."""
        racine = Path(self._tmp.name)
        sp = racine / "Lib" / "site-packages"
        (sp / "pip").mkdir(parents=True)
        (sp / "pip" / "__init__.py").write_text("# pip", encoding="utf-8")
        self.sp = sp
        self._poser_distribution("grpcio", "grpcio-1.80.0", {"grpc/__init__.py": "# grpc"})

        gagne = paquet.purger_site_packages(racine, journal=lambda *_: None)

        self.assertFalse((sp / "pip").exists())
        self.assertFalse((sp / "grpc").exists())
        self.assertIn("pip", gagne)
        self.assertIn("grpcio", gagne)


class SitecustomizeTest(unittest.TestCase):
    """`sitecustomize.py` doit exister ET faire exactement ce pour quoi il est posé.

    Le stub existe pour être IMPORTÉ (chromadb en a besoin au chargement),
    jamais pour fonctionner (chroma_otel_granularity="none" par défaut fait
    que la classe n'est jamais construite en usage réel) — donc le test qui
    compte est que l'import réussit et que la classe existe, pas qu'elle
    marche.
    """

    def test_pose_un_fichier_qui_stub_exactement_le_bon_chemin_d_import(self):
        with tempfile.TemporaryDirectory(prefix="epure-test-sitecustomize-") as tmp:
            racine = Path(tmp)
            chemin = paquet.poser_sitecustomize(racine, journal=lambda *_: None)
            self.assertTrue(chemin.is_file())
            self.assertEqual(chemin, racine / "Lib" / "site-packages" / "sitecustomize.py")

    def test_le_stub_rend_l_import_de_chromadb_possible_sans_grpc(self):
        """Le test qui compte vraiment : reproduit ce que fait `chromadb.telemetry.opentelemetry`.

        Le poste de dev peut avoir le vrai paquet installé (il l'a, tant que
        `google-generativeai` n'est pas retiré de `requirements.txt`) — donc on
        retire d'abord tout module déjà en cache pour ce nom, sinon le test
        vérifierait la vraie classe et non le stub.
        """
        import sys
        nom = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
        original = sys.modules.pop(nom, None)
        try:
            namespace: dict = {}
            exec(compile(paquet.SITECUSTOMIZE, "sitecustomize.py", "exec"), namespace)
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            self.assertTrue(callable(OTLPSpanExporter))
            with self.assertRaises(RuntimeError):
                OTLPSpanExporter()
        finally:
            sys.modules.pop(nom, None)
            if original is not None:
                sys.modules[nom] = original


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
        """Le contrôle de bout en bout, sur l'arborescence réellement zippée."""
        paquet.assembler(self.staging, self.dist, [], None, journal=lambda *_: None)
        fuites = [p.relative_to(self.staging).as_posix()
                  for p in self.staging.rglob("*") if p.is_file()
                  and p.name != ".env.example"
                  and (p.name.startswith(".env") or p.suffix in (".log", ".pyc")
                       or p.name.startswith(("test_", "integration_", "_test_env")))]
        self.assertEqual(fuites, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
