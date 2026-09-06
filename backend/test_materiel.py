"""Détection matérielle et verdicts de faisabilité — `core/materiel.py`.

**AUCUN test d'ici ne touche le matériel réel de la machine qui l'exécute.**
C'est la contrainte structurante du fichier, pas une précaution : `dxdiag` coûte
16,3 s (mesuré sur le poste de dev), et un test qui lirait la vraie carte
graphique serait vert ici et rouge en CI — ou l'inverse, sans qu'aucun des deux
ne soit un bug. Tout part par `subprocess.run` mocké, et les analyseurs de
sortie sont des fonctions PURES éprouvées sur des chaînes.

Les chaînes en question ne sont pas inventées : l'extrait dxdiag reproduit le
rapport RÉEL de ce poste (Lenovo Yoga, AMD Radeon 840M), y compris la seconde
ligne `Dedicated Memory: 0 MB` qui vit 143 lignes plus bas dans un autre bloc et
qu'une recherche non bornée ramasserait à la place de la bonne.

Le cas qui tourne pour de bon en CI a sa classe : runner Linux, ni `nvidia-smi`
ni `dxdiag`, `SystemRoot` absent — le module doit rendre « inconnu » sans lever.

**Depuis le 2026-09-06, deux durées de vie se testent séparément** : ce qui est
figé (RAM totale, GPU) et ce qui est relu à chaque verdict (mémoire libre,
joignabilité de FLM). `ModeleResidentTest` rejoue la mesure qui a motivé le
chantier — `qwen2.5:7b` résident, un second modèle proposé — et `CacheTest`
vérifie qu'aucune valeur périssable ne s'est glissée dans le cache, ce qui est
la seule façon d'empêcher le bug de revenir sous un autre nom de champ.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _test_env  # noqa: F401,E402  — AVANT tout import de core.*

from core import materiel  # noqa: E402


# ── Fragments de sortie RÉELS, capturés sur du matériel ──────────────────────

#: Extrait fidèle du rapport de ce poste. Les deux blocs sont conservés : le
#: second porte le leurre `Dedicated Memory: 0 MB`, qui n'est PAS celui de la
#: carte et se lirait « pas de mémoire » s'il était ramassé.
DXDIAG_IGPU = """\
------------------
System Information
------------------
      Available OS Memory: 32074MB RAM

---------------
Display Devices
---------------
           Card name: AMD Radeon(TM) 840M Graphics
        Manufacturer: Advanced Micro Devices, Inc.
           Chip type: AMD Radeon Graphics Processor (0x1902)
      Display Memory: 16548 MB
    Dedicated Memory: 512 MB
       Shared Memory: 16036 MB
        Current Mode: 2880 x 1800 (32 bit) (60Hz)
        Driver Model: WDDM 3.2

-------------
Sound Devices
-------------
    Device Manufacturer: AMD
       Dedicated Memory: 0 MB
   Shared System Memory: 16036 MB
"""

#: Forme d'une carte discrète : la mémoire dédiée domine, mais Windows annonce
#: quand même de la mémoire partagée. C'est pour ça que `nvidia-smi` — et lui
#: seul — sert de preuve qu'un pool séparé existe.
DXDIAG_DISCRET = """\
---------------
Display Devices
---------------
           Card name: NVIDIA GeForce RTX 4070 Laptop GPU
      Display Memory: 24276 MB
    Dedicated Memory: 8188 MB
       Shared Memory: 16088 MB
"""

#: TROIS colonnes depuis le 2026-09-06 : `memory.free` part dans la même
#: requête que `memory.total`, et chaque appelant garde la sienne (le total est
#: mis en cache, le libre est jeté aussitôt lu).
NVIDIA_SMI_OK = (
    "name, memory.total [MiB], memory.free [MiB]\n"
    "NVIDIA GeForce RTX 4070 Laptop GPU, 8188 MiB, 6120 MiB\n"
)
NVIDIA_SMI_DEUX = (
    "name, memory.total [MiB], memory.free [MiB]\n"
    "NVIDIA GeForce RTX 4090, 24564 MiB, 20000 MiB\n"
    "NVIDIA GeForce GTX 1050, 4096 MiB, 4000 MiB\n"
)
NVIDIA_SMI_NA = (
    "name, memory.total [MiB], memory.free [MiB]\n"
    "NVIDIA GeForce RTX 4070 Laptop GPU, [N/A], [N/A]\n"
)

#: Un `nvidia-smi` plus ancien, qui ne connaîtrait pas `memory.free` : deux
#: colonnes. Le total reste lisible, le libre est `None` — donc `inconnu`, et
#: surtout pas un repli sur le total.
NVIDIA_SMI_SANS_LIBRE = (
    "name, memory.total [MiB]\nNVIDIA GeForce RTX 4070 Laptop GPU, 8188 MiB\n"
)

GIO = 1024 ** 3
MO = 1024 * 1024

# ── Les chiffres RÉELS du 2026-09-06 sur ce poste ────────────────────────────
#
# Relevés, pas inventés : `ullTotalPhys` / `ullAvailPhys` avant et après un
# chargement de `qwen2.5:7b` par Ollama, et les tailles de `/api/tags`. Ils
# servent à `ModeleResidentTest`, qui rejoue exactement ce cas — écrire des
# nombres ronds à la place ferait un test qui prouve l'arithmétique plutôt que
# le bug.
RAM_TOTALE = 33631817728          # 31,32 Gio — ullTotalPhys
RAM_LIBRE_AU_REPOS = 16808464384  # 15,65 Gio — ullAvailPhys, aucun modèle résident
RAM_LIBRE_AVEC_QWEN = 10166943744 # 9,47 Gio  — après chargement (-6,18 Gio)
TAILLE_QWEN25_7B = 4683087332     # 4,36 Gio sur disque, 6,44 Gio résident
TAILLE_MISTRAL_24B = 14333921662  # 13,35 Gio — le modèle qui bascule


def _vider_cache() -> None:
    """`materiel()` mémorise pour la vie du process — chaque test repart à zéro."""
    materiel._cache = None


class _SondesActives:
    """Réactive les sondes que `_test_env` coupe, le temps d'un test.

    `EPURE_MATERIEL_SONDE=0` est le défaut de toute la suite : sans ce contexte,
    `detecter()` court-circuite avant le moindre mock et les tests de sonde
    seraient verts par vacuité — exactement le défaut que ce dépôt a déjà payé
    avec `tsc --noEmit`.
    """

    def __enter__(self):
        self._avant = os.environ.get("EPURE_MATERIEL_SONDE")
        os.environ["EPURE_MATERIEL_SONDE"] = "1"
        _vider_cache()
        return self

    def __exit__(self, *exc):
        if self._avant is None:
            os.environ.pop("EPURE_MATERIEL_SONDE", None)
        else:
            os.environ["EPURE_MATERIEL_SONDE"] = self._avant
        _vider_cache()
        return False


class AnalyseNvidiaSmiTest(unittest.TestCase):
    """La sortie CSV de `nvidia-smi`, sans lancer `nvidia-smi`."""

    def test_carte_et_vram(self):
        gpu = materiel.analyser_nvidia_smi(NVIDIA_SMI_OK)
        self.assertEqual(gpu["nom"], "NVIDIA GeForce RTX 4070 Laptop GPU")
        self.assertEqual(gpu["vram_octets"], 8188 * MO)
        self.assertEqual(gpu["source"], "nvidia-smi")

    def test_pool_separe_affirme(self):
        """Le SEUL chemin qui a le droit d'affirmer « pas partagé » — c'est lui
        qui autorise la VRAM comme dénominateur."""
        self.assertIs(materiel.analyser_nvidia_smi(NVIDIA_SMI_OK)["partage_la_ram"], False)

    def test_premiere_carte_et_non_la_plus_grosse(self):
        gpu = materiel.analyser_nvidia_smi(NVIDIA_SMI_DEUX)
        self.assertEqual(gpu["nom"], "NVIDIA GeForce RTX 4090")

    def test_memoire_indisponible_garde_le_nom(self):
        """`[N/A]` : on sait qu'il y a une carte, on ignore sa mémoire. Les deux
        informations sont indépendantes et ne doivent pas se perdre ensemble."""
        gpu = materiel.analyser_nvidia_smi(NVIDIA_SMI_NA)
        self.assertEqual(gpu["nom"], "NVIDIA GeForce RTX 4070 Laptop GPU")
        self.assertIsNone(gpu["vram_octets"])

    def test_sortie_vide_ou_entete_seul(self):
        self.assertIsNone(materiel.analyser_nvidia_smi(""))
        self.assertIsNone(materiel.analyser_nvidia_smi("name, memory.total [MiB]\n"))

    def test_sortie_incoherente(self):
        self.assertIsNone(materiel.analyser_nvidia_smi("bash: nvidia-smi: not found"))

    def test_aucune_valeur_libre_dans_le_dict(self):
        """**Le garde-fou structurel du correctif.** Ce dict part dans le cache
        de `materiel()` : y laisser entrer `memory.free` figerait pour la vie
        du process une valeur qui change à chaque chargement de modèle — le bug
        de 2026-09-06 rejoué sous un nouveau nom de champ, invisible."""
        gpu = materiel.analyser_nvidia_smi(NVIDIA_SMI_OK)
        for cle in gpu:
            self.assertNotIn("libre", cle)
            self.assertNotIn("free", cle)


class AnalyseNvidiaSmiLibreTest(unittest.TestCase):
    """La TROISIÈME colonne, lue à part et sur la même sortie."""

    def test_colonne_libre(self):
        self.assertEqual(materiel.analyser_nvidia_smi_libre(NVIDIA_SMI_OK), 6120 * MO)

    def test_premiere_carte(self):
        """Même carte que `analyser_nvidia_smi` — les deux lecteurs doivent
        parler du même GPU, sinon le verdict compare la VRAM libre d'une carte
        au pool d'une autre."""
        self.assertEqual(materiel.analyser_nvidia_smi_libre(NVIDIA_SMI_DEUX), 20000 * MO)

    def test_colonne_absente(self):
        """`nvidia-smi` sans `memory.free` : `None`, jamais le total. Le total
        est exactement la valeur fausse que ce chantier a retirée du calcul."""
        self.assertIsNone(materiel.analyser_nvidia_smi_libre(NVIDIA_SMI_SANS_LIBRE))

    def test_na(self):
        self.assertIsNone(materiel.analyser_nvidia_smi_libre(NVIDIA_SMI_NA))

    def test_sortie_vide(self):
        self.assertIsNone(materiel.analyser_nvidia_smi_libre(""))
        self.assertIsNone(materiel.analyser_nvidia_smi_libre("bash: not found"))


class AnalyseDxdiagTest(unittest.TestCase):
    """Le rapport dxdiag, sur les chaînes réelles capturées ici."""

    def test_igpu_les_deux_nombres_separes(self):
        """512 Mo dédiés ET 16036 Mo partagés — jamais fondus en un seul.

        Les fondre (ou publier `Display Memory`, qui est leur somme) annoncerait
        49 Go à quelqu'un qui a 32 Go de RAM : le même octet compté deux fois.
        """
        gpu = materiel.analyser_dxdiag(DXDIAG_IGPU)
        self.assertEqual(gpu["nom"], "AMD Radeon(TM) 840M Graphics")
        self.assertEqual(gpu["vram_octets"], 512 * MO)
        self.assertEqual(gpu["vram_partagee_octets"], 16036 * MO)
        self.assertIs(gpu["partage_la_ram"], True)
        self.assertEqual(gpu["source"], "dxdiag")

    def test_le_leurre_plus_bas_nest_pas_ramasse(self):
        """`Dedicated Memory: 0 MB` du bloc audio ne doit pas gagner.

        C'est le bug qu'une recherche non bornée produirait, et son symptôme
        serait « carte sans mémoire » — indiscernable d'un vrai problème.
        """
        self.assertEqual(materiel.analyser_dxdiag(DXDIAG_IGPU)["vram_octets"], 512 * MO)

    def test_carte_discrete(self):
        gpu = materiel.analyser_dxdiag(DXDIAG_DISCRET)
        self.assertEqual(gpu["vram_octets"], 8188 * MO)
        self.assertEqual(gpu["vram_partagee_octets"], 16088 * MO)

    def test_aucune_carte_nommee(self):
        self.assertIsNone(materiel.analyser_dxdiag("Display Devices\n---\n"))
        self.assertIsNone(materiel.analyser_dxdiag(""))

    def test_carte_sans_ligne_memoire(self):
        """« GPU vu, mémoire inconnue » n'est pas « pas de GPU ». Trois états
        jusqu'au bout : `partage_la_ram` reste `None`, pas `False`."""
        gpu = materiel.analyser_dxdiag("           Card name: Machin Graphics\n")
        self.assertEqual(gpu["nom"], "Machin Graphics")
        self.assertIsNone(gpu["vram_octets"])
        self.assertIsNone(gpu["partage_la_ram"])

    def test_sortie_tronquee_ou_binaire(self):
        """Un rapport illisible ne doit pas lever — au pire, il ne dit rien."""
        self.assertIsNone(materiel.analyser_dxdiag("\x00\x01\x02 rapport tronqu"))


class AnalyseMeminfoTest(unittest.TestCase):
    def test_memtotal(self):
        self.assertEqual(
            materiel.analyser_meminfo("MemTotal:       32851028 kB\nMemFree: 12 kB\n"),
            32851028 * 1024,
        )

    def test_absent(self):
        self.assertIsNone(materiel.analyser_meminfo("MemFree:  12 kB\n"))

    def test_memavailable(self):
        texte = "MemTotal: 32851028 kB\nMemFree: 812345 kB\nMemAvailable: 9123456 kB\n"
        self.assertEqual(materiel.analyser_meminfo_libre(texte), 9123456 * 1024)

    def test_memavailable_ne_lit_pas_memfree(self):
        """`MemFree` ignore le cache réclamable : le prendre pour `MemAvailable`
        sous-estimerait massivement ce qu'un modèle peut prendre, et ferait
        sortir `ne_tiendra_pas` sur des modèles qui se chargent sans peine."""
        texte = "MemTotal: 32851028 kB\nMemFree: 812345 kB\nMemAvailable: 9123456 kB\n"
        self.assertNotEqual(materiel.analyser_meminfo_libre(texte), 812345 * 1024)

    def test_memavailable_absent_ne_retombe_pas_sur_memfree(self):
        """Noyau antérieur à 3.14 : `None`, donc `inconnu`. Remplacer une
        ignorance par un chiffre faux serait pire que se taire."""
        self.assertIsNone(materiel.analyser_meminfo_libre("MemTotal: 1 kB\nMemFree: 12 kB\n"))


class SondeNvidiaSmiTest(unittest.TestCase):
    """L'orchestration autour du sous-processus — absent, en échec, ou correct."""

    def test_binaire_absent(self):
        """Le cas NORMAL d'une machine sans carte NVIDIA : `None`, pas une erreur."""
        with mock.patch("core.materiel.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(materiel._sonder_nvidia_smi())

    def test_code_de_retour_non_nul(self):
        faux = mock.Mock(returncode=9, stdout=b"", stderr=b"NVIDIA-SMI has failed")
        with mock.patch("core.materiel.subprocess.run", return_value=faux):
            self.assertIsNone(materiel._sonder_nvidia_smi())

    def test_timeout(self):
        boom = subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10)
        with mock.patch("core.materiel.subprocess.run", side_effect=boom):
            self.assertIsNone(materiel._sonder_nvidia_smi())

    def test_succes(self):
        faux = mock.Mock(returncode=0, stdout=NVIDIA_SMI_OK.encode("utf-8"), stderr=b"")
        with mock.patch("core.materiel.subprocess.run", return_value=faux):
            self.assertEqual(materiel._sonder_nvidia_smi()["vram_octets"], 8188 * MO)

    def test_une_seule_requete_pour_les_deux_colonnes(self):
        """Total et libre partent ENSEMBLE. Deux commandes distinctes
        divergeraient le jour où l'une gagne un champ, et paieraient deux
        sous-processus pour une sortie qui les porte tous les deux."""
        faux = mock.Mock(returncode=0, stdout=NVIDIA_SMI_OK.encode("utf-8"), stderr=b"")
        with mock.patch("core.materiel.subprocess.run", return_value=faux) as run:
            materiel._sonder_nvidia_smi()
        argv = run.call_args[0][0]
        requete = next(a for a in argv if a.startswith("--query-gpu="))
        self.assertIn("memory.total", requete)
        self.assertIn("memory.free", requete)


class SondeVramLibreTest(unittest.TestCase):
    """`_sonder_vram_libre` — la moitié périssable, relue à chaque verdict."""

    def test_succes(self):
        faux = mock.Mock(returncode=0, stdout=NVIDIA_SMI_OK.encode("utf-8"), stderr=b"")
        with mock.patch("core.materiel.subprocess.run", return_value=faux):
            self.assertEqual(materiel._sonder_vram_libre(), 6120 * MO)

    def test_binaire_absent(self):
        with mock.patch("core.materiel.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(materiel._sonder_vram_libre())

    def test_timeout(self):
        boom = subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10)
        with mock.patch("core.materiel.subprocess.run", side_effect=boom):
            self.assertIsNone(materiel._sonder_vram_libre())

    def test_relance_la_commande(self):
        """Et ne réutilise PAS la sortie de la détection : celle-là date du
        démarrage du process, c'est-à-dire précisément ce qu'on corrige."""
        faux = mock.Mock(returncode=0, stdout=NVIDIA_SMI_OK.encode("utf-8"), stderr=b"")
        with mock.patch("core.materiel.subprocess.run", return_value=faux) as run:
            materiel._sonder_vram_libre()
            materiel._sonder_vram_libre()
        self.assertEqual(run.call_count, 2)


class SondeDxdiagTest(unittest.TestCase):
    """dxdiag : absent, en échec, incohérent, et enfin correct.

    Un faux `dxdiag.exe` est posé dans un `SystemRoot` temporaire, et
    `sys.platform` est forcé — sans quoi ces cas ne s'exécuteraient jamais en CI
    (runner Linux), c'est-à-dire là où ils comptent le plus.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="epure-test-sysroot-")
        self.racine = Path(self._tmp.name)
        (self.racine / "System32").mkdir()
        self.exe = self.racine / "System32" / "dxdiag.exe"
        self.exe.write_bytes(b"")
        self._patchs = [
            mock.patch("core.materiel.sys.platform", "win32"),
            mock.patch.dict(os.environ, {"SystemRoot": str(self.racine)}),
        ]
        for p in self._patchs:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patchs:
            self.addCleanup(p.stop)

    def _ecrire(self, contenu: str, code: int = 0):
        """Faux dxdiag : écrit `contenu` là où l'argv le demande, en cp1252."""
        def _run(argv, **_):
            if code == 0:
                Path(argv[2]).write_bytes(contenu.encode("cp1252", "replace"))
            return mock.Mock(returncode=code, stdout=b"", stderr=b"")
        return _run

    def test_executable_absent(self):
        self.exe.unlink()
        with mock.patch("core.materiel.subprocess.run") as run:
            self.assertIsNone(materiel._sonder_dxdiag())
        run.assert_not_called()

    def test_systemroot_absent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SystemRoot", None)
            with mock.patch("core.materiel.subprocess.run") as run:
                self.assertIsNone(materiel._sonder_dxdiag())
            run.assert_not_called()

    def test_hors_windows(self):
        """Le cas de la CI. dxdiag n'existe pas sous Linux — aucun appel."""
        with mock.patch("core.materiel.sys.platform", "linux"), \
                mock.patch("core.materiel.subprocess.run") as run:
            self.assertIsNone(materiel._sonder_dxdiag())
        run.assert_not_called()

    def test_echec_code_retour(self):
        with mock.patch("core.materiel.subprocess.run",
                        side_effect=self._ecrire("", code=1)):
            self.assertIsNone(materiel._sonder_dxdiag())

    def test_timeout(self):
        boom = subprocess.TimeoutExpired(cmd="dxdiag", timeout=90)
        with mock.patch("core.materiel.subprocess.run", side_effect=boom):
            self.assertIsNone(materiel._sonder_dxdiag())

    def test_succes_sans_rapport_ecrit(self):
        """dxdiag répond 0 et n'écrit rien : « ok » ne prouve pas un résultat.

        Même méfiance que `core/ollama_memoire.py` face au 200 d'une éjection
        qui ne décharge rien — un code de retour n'est pas un état.
        """
        with mock.patch("core.materiel.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=b"", stderr=b"")):
            self.assertIsNone(materiel._sonder_dxdiag())

    def test_rapport_incoherent(self):
        with mock.patch("core.materiel.subprocess.run",
                        side_effect=self._ecrire("rien d'utile ici\n")):
            self.assertIsNone(materiel._sonder_dxdiag())

    def test_succes(self):
        with mock.patch("core.materiel.subprocess.run",
                        side_effect=self._ecrire(DXDIAG_IGPU)):
            gpu = materiel._sonder_dxdiag()
        self.assertEqual(gpu["vram_octets"], 512 * MO)
        self.assertEqual(gpu["source"], "dxdiag")

    def test_le_rapport_ne_survit_pas(self):
        """120 ko de rapport par appel : le temporaire est nettoyé, et il n'est
        surtout pas sous `backend/` (`test_zz_donnees_reelles` parle d'autre
        chose, mais tomberait quand même)."""
        vus: list[Path] = []
        def _run(argv, **_):
            vus.append(Path(argv[2]))
            Path(argv[2]).write_bytes(DXDIAG_IGPU.encode("cp1252"))
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch("core.materiel.subprocess.run", side_effect=_run):
            materiel._sonder_dxdiag()
        self.assertEqual(len(vus), 1)
        self.assertFalse(vus[0].exists())
        self.assertFalse(str(vus[0]).startswith(str(Path(__file__).resolve().parent)))


class OrdreDesSondesTest(unittest.TestCase):
    """nvidia-smi d'abord, dxdiag en repli, `inconnu` si les deux se taisent."""

    def test_nvidia_gagne(self):
        with mock.patch("core.materiel._sonder_nvidia_smi",
                        return_value={"nom": "RTX", "vram_octets": 8 * GIO,
                                      "vram_partagee_octets": None,
                                      "partage_la_ram": False, "source": "nvidia-smi"}), \
                mock.patch("core.materiel._sonder_dxdiag") as dx:
            self.assertEqual(materiel._sonder_gpu()["source"], "nvidia-smi")
        dx.assert_not_called()

    def test_repli_dxdiag(self):
        with mock.patch("core.materiel._sonder_nvidia_smi", return_value=None), \
                mock.patch("core.materiel._sonder_dxdiag",
                           return_value=materiel.analyser_dxdiag(DXDIAG_IGPU)):
            self.assertEqual(materiel._sonder_gpu()["source"], "dxdiag")

    def test_les_deux_muettes(self):
        with mock.patch("core.materiel._sonder_nvidia_smi", return_value=None), \
                mock.patch("core.materiel._sonder_dxdiag", return_value=None):
            gpu = materiel._sonder_gpu()
        self.assertEqual(gpu["source"], "inconnu")
        self.assertIsNone(gpu["vram_octets"])
        self.assertIsNone(gpu["partage_la_ram"])

    def test_une_sonde_qui_leve_ne_tue_pas_lautre(self):
        with mock.patch("core.materiel._sonder_nvidia_smi", side_effect=RuntimeError), \
                mock.patch("core.materiel._sonder_dxdiag",
                           return_value=materiel.analyser_dxdiag(DXDIAG_IGPU)):
            self.assertEqual(materiel._sonder_gpu()["source"], "dxdiag")


class RessourceTotaleTest(unittest.TestCase):
    """Le POOL dans lequel se joue le verdict — c'est ici que se joue l'écart
    assumé à la règle naïve « VRAM si connue, sinon RAM » (cf. en-tête de
    core/materiel.py).

    Ce n'est plus le dénominateur depuis le 2026-09-06 (`ressource_libre` l'est),
    mais le choix du pool reste le même et c'est lui qui décide QUELLE sonde
    fraîche est interrogée ensuite."""

    def test_carte_discrete_la_vram_gagne(self):
        gpu = materiel.analyser_nvidia_smi(NVIDIA_SMI_OK)
        r = materiel.ressource_totale(32 * GIO, gpu)
        self.assertEqual(r["origine"], "vram")
        self.assertEqual(r["octets"], 8188 * MO)

    def test_igpu_la_ram_gagne(self):
        """La mémoire « partagée » EST la RAM : la compter à part la compterait
        deux fois, et son total (16,5 Go ici) déclarerait impossible un modèle
        de 17,7 Gio qui tourne réellement sur ce poste."""
        gpu = materiel.analyser_dxdiag(DXDIAG_IGPU)
        r = materiel.ressource_totale(32 * GIO, gpu)
        self.assertEqual(r["origine"], "ram")
        self.assertEqual(r["octets"], 32 * GIO)

    def test_partage_inconnu_retombe_sur_la_ram(self):
        """`None` n'est pas `False`. Ne pas savoir si la mémoire est partagée
        n'autorise pas à affirmer un second pool."""
        gpu = materiel.analyser_dxdiag("           Card name: Machin\n    Dedicated Memory: 4096 MB\n")
        self.assertIsNone(gpu["partage_la_ram"])
        self.assertEqual(materiel.ressource_totale(32 * GIO, gpu)["origine"], "ram")

    def test_aucun_gpu(self):
        r = materiel.ressource_totale(16 * GIO, materiel._gpu_inconnu())
        self.assertEqual(r["origine"], "ram")
        self.assertEqual(r["octets"], 16 * GIO)

    def test_rien_du_tout(self):
        r = materiel.ressource_totale(None, materiel._gpu_inconnu())
        self.assertEqual(r["origine"], "inconnu")
        self.assertIsNone(r["octets"])


class RessourceLibreTest(unittest.TestCase):
    """Le VRAI dénominateur : relu à chaque appel, jamais mis en cache."""

    def test_ram_sous_windows(self):
        with _SondesActives(), \
                mock.patch("core.materiel.sys.platform", "win32"), \
                mock.patch("core.materiel._memoire_windows",
                           return_value=(RAM_TOTALE, RAM_LIBRE_AVEC_QWEN)):
            self.assertEqual(materiel.ressource_libre("ram"), RAM_LIBRE_AVEC_QWEN)

    def test_ram_sous_linux(self):
        with _SondesActives(), \
                mock.patch("core.materiel.sys.platform", "linux"), \
                mock.patch("core.materiel.Path.read_text",
                           return_value="MemTotal: 16384000 kB\nMemAvailable: 4096000 kB\n"):
            self.assertEqual(materiel.ressource_libre("ram"), 4096000 * 1024)

    def test_vram_passe_par_nvidia_smi(self):
        """Le pool décide de la sonde, pas la plateforme : lire la RAM alors
        que le verdict se joue en VRAM donnerait un chiffre juste pour la
        mauvaise question."""
        faux = mock.Mock(returncode=0, stdout=NVIDIA_SMI_OK.encode("utf-8"), stderr=b"")
        with _SondesActives(), mock.patch("core.materiel.subprocess.run", return_value=faux):
            self.assertEqual(materiel.ressource_libre("vram"), 6120 * MO)

    def test_origine_inconnue(self):
        with _SondesActives(), mock.patch("core.materiel.subprocess.run") as run:
            self.assertIsNone(materiel.ressource_libre("inconnu"))
        run.assert_not_called()

    def test_sondes_coupees(self):
        """`EPURE_MATERIEL_SONDE=0` : aucune sonde, y compris fraîche. Sans
        cette porte, toute la suite lirait la mémoire du poste qui l'exécute et
        les verdicts changeraient d'une machine à l'autre."""
        with mock.patch("core.materiel.subprocess.run") as run, \
                mock.patch("core.materiel._memoire_windows") as win:
            self.assertIsNone(materiel.ressource_libre("ram"))
            self.assertIsNone(materiel.ressource_libre("vram"))
        run.assert_not_called()
        win.assert_not_called()


class RessourceFraicheTest(unittest.TestCase):
    """Le dict `{octets, origine}` du moment — et son refus de se replier."""

    @staticmethod
    def _etat(origine, octets=RAM_TOTALE):
        return {"ressource": {"octets": octets, "origine": origine}}

    def test_garde_l_origine_du_pool(self):
        with _SondesActives(), \
                mock.patch("core.materiel.ressource_libre", return_value=RAM_LIBRE_AVEC_QWEN):
            r = materiel.ressource_fraiche(self._etat("ram"))
        self.assertEqual(r, {"octets": RAM_LIBRE_AVEC_QWEN, "origine": "ram"})

    def test_lecture_ratee_ne_retombe_pas_sur_le_total(self):
        """**La règle du fichier.** Le total est précisément la valeur qui
        donnait la mauvaise réponse ; y revenir en silence rejouerait le bug en
        le faisant passer pour une dégradation prudente."""
        with _SondesActives(), mock.patch("core.materiel.ressource_libre", return_value=None):
            r = materiel.ressource_fraiche(self._etat("ram"))
        self.assertEqual(r, {"octets": None, "origine": "inconnu"})
        self.assertNotEqual(r["octets"], RAM_TOTALE)

    def test_zero_ne_vaut_pas_une_lecture(self):
        with _SondesActives(), mock.patch("core.materiel.ressource_libre", return_value=0):
            self.assertIsNone(materiel.ressource_fraiche(self._etat("ram"))["octets"])

    def test_materiel_inconnu(self):
        with _SondesActives():
            r = materiel.ressource_fraiche({"ressource": {"octets": None, "origine": "inconnu"}})
        self.assertEqual(r["origine"], "inconnu")

    def test_etat_sans_ressource(self):
        with _SondesActives():
            self.assertEqual(materiel.ressource_fraiche({}),
                             {"octets": None, "origine": "inconnu"})


class NpuJoignableTest(unittest.TestCase):
    """La joignabilité de FLM, relue et non mémorisée."""

    def test_suit_check_flm(self):
        for joignable in (True, False):
            with self.subTest(flm=joignable), _SondesActives(), \
                    mock.patch("core.materiel.check_flm", return_value=joignable):
                self.assertIs(materiel.npu_joignable(), joignable)

    def test_relue_a_chaque_appel(self):
        """Le bug jumeau de la mémoire libre : un FLM démarré APRÈS
        l'application restait « éteint » pour la vie du process."""
        with _SondesActives(), \
                mock.patch("core.materiel.check_flm", side_effect=[False, True]):
            self.assertFalse(materiel.npu_joignable())
            self.assertTrue(materiel.npu_joignable())

    def test_une_sonde_qui_leve_ne_tue_rien(self):
        with _SondesActives(), mock.patch("core.materiel.check_flm", side_effect=RuntimeError):
            self.assertFalse(materiel.npu_joignable())

    def test_sondes_coupees(self):
        with mock.patch("core.materiel.check_flm") as flm:
            self.assertFalse(materiel.npu_joignable())
        flm.assert_not_called()


class VerdictMemoireTest(unittest.TestCase):
    """Les quatre états, et surtout : `inconnu` n'est le repli d'aucun autre."""

    def test_tient(self):
        self.assertEqual(materiel.verdict_memoire(4 * GIO, 32 * GIO), "tient")

    def test_pile_sur_la_marge_tient(self):
        """80 % exactement est du côté confortable — l'inégalité est large."""
        self.assertEqual(materiel.verdict_memoire(8 * GIO, 10 * GIO), "tient")

    def test_juste_au_dessus_de_la_marge(self):
        self.assertEqual(materiel.verdict_memoire(8 * GIO + 1, 10 * GIO), "limite")

    def test_pile_a_cent_pour_cent(self):
        self.assertEqual(materiel.verdict_memoire(10 * GIO, 10 * GIO), "limite")

    def test_au_dessus(self):
        self.assertEqual(materiel.verdict_memoire(10 * GIO + 1, 10 * GIO), "ne_tiendra_pas")

    def test_taille_inconnue(self):
        self.assertEqual(materiel.verdict_memoire(None, 32 * GIO), "inconnu")

    def test_ressource_inconnue(self):
        self.assertEqual(materiel.verdict_memoire(4 * GIO, None), "inconnu")

    def test_zero_ne_vaut_pas_illimite(self):
        """Une ressource à 0 est une lecture ratée, pas une machine infinie."""
        self.assertEqual(materiel.verdict_memoire(4 * GIO, 0), "inconnu")
        self.assertEqual(materiel.verdict_memoire(0, 32 * GIO), "inconnu")


class VerdictFlmTest(unittest.TestCase):
    """FLM ne passe pas par le calcul mémoire : il gère sa mémoire NPU seul."""

    def test_les_trois_conditions(self):
        self.assertEqual(materiel.verdict_flm(True, True, True), "disponible")

    def test_serveur_injoignable(self):
        self.assertEqual(materiel.verdict_flm(False, True, True), "indisponible")

    def test_modele_non_installe(self):
        """Joignable ne suffit pas : c'est déjà la règle du champ `disponible`
        de `/models`, reprise telle quelle pour ne pas diverger."""
        self.assertEqual(materiel.verdict_flm(True, False, True), "indisponible")

    def test_absent_du_catalogue_du_serveur(self):
        self.assertEqual(materiel.verdict_flm(True, True, False), "indisponible")


class VerdictsModelesTest(unittest.TestCase):
    """Les trois backends locaux, avec leurs trois qualités d'information."""

    #: La forme que rend `etat_frais()` : les faits figés du cache PLUS les deux
    #: valeurs relues à chaque requête. `verdicts_modeles` lit `ressource_libre`
    #: comme dénominateur — `ressource` n'est là que pour l'affichage.
    ETAT = {
        "ram_octets": 32 * GIO,
        "gpu": materiel._gpu_inconnu(),
        "npu": {"disponible": False},
        "ressource": {"octets": 32 * GIO, "origine": "ram"},
        "ressource_libre": {"octets": 32 * GIO, "origine": "ram"},
    }

    def _verdicts(self, ollama=None, lmstudio=None, npu=False, flm_installes=None,
                  flm_live=None, libre=None):
        etat = dict(self.ETAT, npu={"disponible": npu})
        if libre is not None:
            etat["ressource_libre"] = {"octets": libre, "origine": "ram"}
        with mock.patch("core.materiel._tailles_ollama", return_value=ollama), \
                mock.patch("core.materiel.get_lmstudio_installed", return_value=lmstudio), \
                mock.patch("core.materiel.get_flm_installed",
                           return_value=flm_installes or set()), \
                mock.patch("core.materiel.flm_model_ids", return_value=flm_live):
            return {m["id"]: m for m in materiel.verdicts_modeles(etat)}

    def test_ollama_taille_lue(self):
        v = self._verdicts(ollama={"qwen2.5:7b": 4 * GIO, "geant:70b": 40 * GIO})
        self.assertEqual(v["qwen2.5:7b"]["verdict"], "tient")
        self.assertEqual(v["qwen2.5:7b"]["taille_octets"], 4 * GIO)
        self.assertEqual(v["geant:70b"]["verdict"], "ne_tiendra_pas")

    def test_ollama_injoignable(self):
        """`None` (pas de serveur) et `{}` (serveur vide) donnent tous deux zéro
        ligne Ollama — mais aucun ne doit lever."""
        self.assertEqual(self._verdicts(ollama=None), self._verdicts(ollama={}))

    def test_lmstudio_taille_inconnue(self):
        """MESURÉ : ni `/v1/models` ni `/api/v0/models` ne rendent d'octets.
        Donc `inconnu` — jamais une taille devinée depuis le nom."""
        v = self._verdicts(lmstudio=["qwen/qwen3.8-27b"])
        self.assertIsNone(v["lmstudio:qwen/qwen3.8-27b"]["taille_octets"])
        self.assertEqual(v["lmstudio:qwen/qwen3.8-27b"]["verdict"], "inconnu")

    def test_flm_npu_absent(self):
        v = self._verdicts(npu=False)
        self.assertTrue(v)
        for m in v.values():
            self.assertEqual(m["verdict"], "indisponible")
            self.assertIsNone(m["taille_octets"])

    def test_flm_npu_present(self):
        v = self._verdicts(npu=True, flm_installes={"qwen3:4b"}, flm_live={"qwen3:4b"})
        self.assertEqual(v["flm:qwen3:4b"]["verdict"], "disponible")
        self.assertEqual(v["flm:qwen3:8b"]["verdict"], "indisponible")

    def test_flm_catalogue_illisible_ne_punit_pas(self):
        """FLM joignable mais son `/v1/models` muet : on ne retire pas un modèle
        installé pour une lecture ratée. Même tolérance que
        `premier_modele_vision_disponible()`."""
        v = self._verdicts(npu=True, flm_installes={"qwen3:4b"}, flm_live=None)
        self.assertEqual(v["flm:qwen3:4b"]["verdict"], "disponible")

    def test_aucun_verdict_hors_du_vocabulaire(self):
        v = self._verdicts(ollama={"a:1b": GIO}, lmstudio=["b"], npu=True,
                           flm_installes={"qwen3:4b"}, flm_live={"qwen3:4b"})
        for m in v.values():
            self.assertIn(m["verdict"], materiel.VERDICTS)

    def test_aucun_modele_cloud(self):
        """La mémoire de cette machine ne dit rien de ce qui tourne chez Groq."""
        v = self._verdicts(ollama={"qwen2.5:7b": 4 * GIO})
        self.assertEqual({m["provider"] for m in v.values()}, {"ollama", "flm"})

    def test_le_denominateur_est_le_libre_pas_le_total(self):
        """`ressource` reste à 32 Gio, `ressource_libre` tombe à 5 Gio : c'est
        le second qui décide. Lire le premier était le bug du 2026-09-06."""
        v = self._verdicts(ollama={"gros:20b": 10 * GIO}, libre=5 * GIO)
        self.assertEqual(v["gros:20b"]["verdict"], "ne_tiendra_pas")

    def test_etat_sans_ressource_libre_ne_retombe_pas_sur_le_total(self):
        """Un appelant qui ne passe pas par `etat_frais()` obtient `inconnu`,
        jamais un verdict calculé sur le total — la dégradation doit se voir."""
        etat = {k: v for k, v in self.ETAT.items() if k != "ressource_libre"}
        with mock.patch("core.materiel._tailles_ollama", return_value={"a:1b": GIO}), \
                mock.patch("core.materiel.get_lmstudio_installed", return_value=[]), \
                mock.patch("core.materiel.get_flm_installed", return_value=set()), \
                mock.patch("core.materiel.flm_model_ids", return_value=None):
            v = {m["id"]: m for m in materiel.verdicts_modeles(etat)}
        self.assertEqual(v["a:1b"]["verdict"], "inconnu")

    def test_aucune_sonde_par_modele(self):
        """Le dénominateur est LU dans l'état, jamais sondé ici : sept modèles
        installés ne doivent pas donner sept `nvidia-smi`."""
        with mock.patch("core.materiel.subprocess.run") as run, \
                mock.patch("core.materiel.ressource_libre") as libre:
            self._verdicts(ollama={"a:1b": GIO, "b:2b": 2 * GIO, "c:3b": 3 * GIO})
        run.assert_not_called()
        libre.assert_not_called()


class ModeleResidentTest(unittest.TestCase):
    """**Le cas MESURÉ qui a motivé le chantier**, rejoué de bout en bout.

    2026-09-06 sur ce poste : `qwen2.5:7b` chargé par Ollama, `ullAvailPhys`
    tombé de 15,65 à 9,47 Gio pour 31,32 Gio de RAM totale. Un second modèle
    proposé — `mistral-small:24b`, 13,35 Gio, réellement installé ici — tenait
    largement contre le total (43 %) et ne tenait pas du tout contre ce qui
    restait (141 %).

    Les DEUX directions sont affirmées dans le même test : sans la première,
    on ne prouverait pas que le verdict a changé, seulement qu'il est sévère.
    """

    def _verdicts(self, libre):
        etat = {
            "ram_octets": RAM_TOTALE,
            "gpu": materiel._gpu_inconnu(),
            "npu": {"disponible": False},
            "ressource": {"octets": RAM_TOTALE, "origine": "ram"},
            "ressource_libre": {"octets": libre, "origine": "ram"},
        }
        tailles = {"qwen2.5:7b": TAILLE_QWEN25_7B, "mistral-small:24b": TAILLE_MISTRAL_24B}
        with mock.patch("core.materiel._tailles_ollama", return_value=tailles), \
                mock.patch("core.materiel.get_lmstudio_installed", return_value=[]), \
                mock.patch("core.materiel.get_flm_installed", return_value=set()), \
                mock.patch("core.materiel.flm_model_ids", return_value=None):
            return {m["id"]: m["verdict"] for m in materiel.verdicts_modeles(etat)}

    def test_machine_au_repos_le_second_modele_tient(self):
        """Rien de résident : 13,35 Gio sur 15,65 Gio libres — c'est limite, et
        le dire « limite » est déjà plus juste que le « tient » d'avant."""
        v = self._verdicts(RAM_LIBRE_AU_REPOS)
        self.assertEqual(v["mistral-small:24b"], "limite")

    def test_avec_qwen_resident_le_second_ne_tient_plus(self):
        """Le cœur du correctif : 13,35 Gio réclamés, 9,47 Gio libres."""
        v = self._verdicts(RAM_LIBRE_AVEC_QWEN)
        self.assertEqual(v["mistral-small:24b"], "ne_tiendra_pas")
        # Et le petit modèle continue de tenir : le correctif ne rend pas tout
        # rouge, il rend le verdict vrai.
        self.assertEqual(v["qwen2.5:7b"], "tient")

    def test_l_ancien_calcul_disait_l_inverse(self):
        """La preuve que le test mesure le CHANGEMENT et pas l'arithmétique :
        contre la mémoire TOTALE — l'ancien dénominateur — le même modèle, dans
        le même état de la machine, sortait « tient »."""
        self.assertEqual(
            materiel.verdict_memoire(TAILLE_MISTRAL_24B, RAM_TOTALE), "tient")
        self.assertEqual(
            materiel.verdict_memoire(TAILLE_MISTRAL_24B, RAM_LIBRE_AVEC_QWEN),
            "ne_tiendra_pas")


class SondesCoupeesTest(unittest.TestCase):
    """`EPURE_MATERIEL_SONDE=0` — le régime de toute la suite."""

    def setUp(self):
        _vider_cache()
        self.addCleanup(_vider_cache)

    def test_aucun_sous_processus(self):
        with mock.patch("core.materiel.subprocess.run") as run:
            etat = materiel.detecter()
        run.assert_not_called()
        self.assertEqual(etat["gpu"]["source"], "inconnu")
        self.assertIsNone(etat["ram_octets"])
        self.assertEqual(etat["ressource"]["origine"], "inconnu")

    def test_prechauffage_ne_lance_aucun_fil(self):
        with mock.patch("core.materiel.threading.Thread") as fil:
            materiel.prechauffer()
        fil.assert_not_called()

    def test_verdicts_tous_inconnus(self):
        """Le matériel muet ne rend pas des modèles « qui tiennent » par défaut.

        **Et la sonde FRAÎCHE est coupée elle aussi** : sans cette porte, la
        RAM libre du poste qui exécute la suite entrerait dans le calcul, `a:1b`
        sortirait « tient » ici et « inconnu » en CI, et le test mesurerait la
        machine plutôt que le code.
        """
        with mock.patch("core.materiel._tailles_ollama", return_value={"a:1b": GIO}), \
                mock.patch("core.materiel.get_lmstudio_installed", return_value=[]), \
                mock.patch("core.materiel.get_flm_installed", return_value=set()), \
                mock.patch("core.materiel.flm_model_ids", return_value=None):
            corps = materiel.etat_complet()
        ollama = [m for m in corps["modeles"] if m["provider"] == "ollama"]
        self.assertEqual([m["verdict"] for m in ollama], ["inconnu"])
        self.assertEqual(corps["materiel"]["ressource_libre"]["origine"], "inconnu")

    def test_aucun_sous_processus_sur_le_chemin_complet(self):
        """`etat_complet()` ajoute deux sondes fraîches au chemin de `detecter()`
        — elles passent par la même porte."""
        with mock.patch("core.materiel.subprocess.run") as run, \
                mock.patch("core.materiel.check_flm") as flm, \
                mock.patch("core.materiel._tailles_ollama", return_value=None), \
                mock.patch("core.materiel.get_lmstudio_installed", return_value=None), \
                mock.patch("core.materiel.get_flm_installed", return_value=set()), \
                mock.patch("core.materiel.flm_model_ids", return_value=None):
            materiel.etat_complet()
        run.assert_not_called()
        flm.assert_not_called()


class DetectionCompleteTest(unittest.TestCase):
    """`detecter()` sondes actives, tout mocké — dont le cas réel de la CI.

    **Ne rend QUE des faits figés** : ce qu'elle produit part droit dans un
    cache qui vit aussi longtemps que le process. Le NPU et la mémoire libre
    sont ailleurs (`etat_frais`), et leur absence ici est vérifiée."""

    def test_linux_sans_rien(self):
        """Le runner de la CI : pas de `nvidia-smi`, pas de dxdiag, `/proc` lu.

        Doit rendre un état complet et cohérent, sans lever. C'est le seul de
        ces tests qui décrit une machine que la CI exécute vraiment.
        """
        with _SondesActives(), \
                mock.patch("core.materiel.sys.platform", "linux"), \
                mock.patch("core.materiel.subprocess.run", side_effect=FileNotFoundError), \
                mock.patch("core.materiel.Path.read_text",
                           return_value="MemTotal:       16384000 kB\n"), \
                mock.patch("core.materiel.check_flm", return_value=False):
            etat = materiel.detecter()
        self.assertEqual(etat["ram_octets"], 16384000 * 1024)
        self.assertEqual(etat["gpu"]["source"], "inconnu")
        self.assertEqual(etat["ressource"]["origine"], "ram")

    def test_aucune_valeur_perissable(self):
        """Le garde-fou structurel, côté détection : ni NPU ni mémoire libre
        dans ce que le cache va garder pour la vie du process."""
        with _SondesActives(), \
                mock.patch("core.materiel.sys.platform", "linux"), \
                mock.patch("core.materiel.subprocess.run", side_effect=FileNotFoundError), \
                mock.patch("core.materiel.Path.read_text",
                           return_value="MemTotal: 16384000 kB\nMemAvailable: 999 kB\n"), \
                mock.patch("core.materiel.check_flm", return_value=True) as flm:
            etat = materiel.detecter()
        self.assertEqual(set(etat), {"ram_octets", "gpu", "ressource"})
        flm.assert_not_called()

    def test_ram_illisible(self):
        with _SondesActives(), \
                mock.patch("core.materiel.sys.platform", "linux"), \
                mock.patch("core.materiel.subprocess.run", side_effect=FileNotFoundError), \
                mock.patch("core.materiel.Path.read_text", side_effect=OSError), \
                mock.patch("core.materiel.check_flm", return_value=False):
            etat = materiel.detecter()
        self.assertIsNone(etat["ram_octets"])
        self.assertEqual(etat["ressource"]["origine"], "inconnu")


class EtatFraisTest(unittest.TestCase):
    """Là où les deux durées de vie se rencontrent — une fois par requête."""

    FIGE = {
        "ram_octets": RAM_TOTALE,
        "gpu": materiel._gpu_inconnu(),
        "ressource": {"octets": RAM_TOTALE, "origine": "ram"},
    }

    def setUp(self):
        _vider_cache()
        self.addCleanup(_vider_cache)

    def test_assemble_les_deux_durees_de_vie(self):
        with _SondesActives(), \
                mock.patch("core.materiel.detecter", return_value=dict(self.FIGE)), \
                mock.patch("core.materiel.ressource_libre", return_value=RAM_LIBRE_AVEC_QWEN), \
                mock.patch("core.materiel.check_flm", return_value=True):
            etat = materiel.etat_frais()
        self.assertEqual(etat["ressource"]["octets"], RAM_TOTALE)
        self.assertEqual(etat["ressource_libre"]["octets"], RAM_LIBRE_AVEC_QWEN)
        self.assertTrue(etat["npu"]["disponible"])

    def test_npu_suit_flm(self):
        for joignable in (True, False):
            with self.subTest(flm=joignable), _SondesActives(), \
                    mock.patch("core.materiel.detecter", return_value=dict(self.FIGE)), \
                    mock.patch("core.materiel.ressource_libre", return_value=None), \
                    mock.patch("core.materiel.check_flm", return_value=joignable):
                self.assertIs(materiel.etat_frais()["npu"]["disponible"], joignable)
            _vider_cache()

    def test_flm_qui_leve_ne_tue_pas_l_assemblage(self):
        with _SondesActives(), \
                mock.patch("core.materiel.detecter", return_value=dict(self.FIGE)), \
                mock.patch("core.materiel.ressource_libre", return_value=None), \
                mock.patch("core.materiel.check_flm", side_effect=RuntimeError):
            etat = materiel.etat_frais()
        self.assertFalse(etat["npu"]["disponible"])
        self.assertEqual(etat["ressource"]["octets"], RAM_TOTALE)

    def test_ne_mute_jamais_le_cache(self):
        """**Le piège que ce dict rend possible.** `materiel()` rend toujours le
        MÊME objet et le garde pour la vie du process : un `etat["npu"] = …`
        y figerait la valeur fraîche pour toutes les requêtes suivantes,
        c'est-à-dire reproduirait très exactement le bug corrigé."""
        with _SondesActives(), \
                mock.patch("core.materiel.detecter", return_value=dict(self.FIGE)), \
                mock.patch("core.materiel.ressource_libre", return_value=RAM_LIBRE_AVEC_QWEN), \
                mock.patch("core.materiel.check_flm", return_value=True):
            frais = materiel.etat_frais()
            cache = materiel.materiel()
        self.assertIsNot(frais, cache)
        self.assertNotIn("npu", cache)
        self.assertNotIn("ressource_libre", cache)

    def test_une_seule_mesure_par_appel(self):
        """Les verdicts et la ligne qui les motive doivent parler du MÊME
        instant : deux lectures afficheraient « calculés sur 9,5 Gio » à côté
        de verdicts calculés sur autre chose."""
        with _SondesActives(), \
                mock.patch("core.materiel.detecter", return_value=dict(self.FIGE)), \
                mock.patch("core.materiel.ressource_libre",
                           return_value=RAM_LIBRE_AVEC_QWEN) as libre, \
                mock.patch("core.materiel.check_flm", return_value=False) as flm, \
                mock.patch("core.materiel._tailles_ollama", return_value={"a:1b": GIO}), \
                mock.patch("core.materiel.get_lmstudio_installed", return_value=[]), \
                mock.patch("core.materiel.get_flm_installed", return_value=set()), \
                mock.patch("core.materiel.flm_model_ids", return_value=None):
            corps = materiel.etat_complet()
        self.assertEqual(libre.call_count, 1)
        self.assertEqual(flm.call_count, 1)
        self.assertEqual(corps["materiel"]["ressource_libre"]["octets"], RAM_LIBRE_AVEC_QWEN)


class CacheTest(unittest.TestCase):
    """Une seule détection pour la vie du process — 16 s de dxdiag ne se paient
    pas à chaque ouverture de panneau."""

    def setUp(self):
        _vider_cache()
        self.addCleanup(_vider_cache)

    def test_detection_unique(self):
        faux = {"ram_octets": 1, "gpu": materiel._gpu_inconnu(),
                "ressource": {"octets": 1, "origine": "ram"}}
        with mock.patch("core.materiel.detecter", return_value=faux) as det:
            premier = materiel.materiel()
            second = materiel.materiel()
        self.assertEqual(det.call_count, 1)
        self.assertIs(premier, second)

    def test_rien_de_perissable_dans_le_cache(self):
        """**L'invariant qui empêche le bug de revenir sous un autre nom.** Un
        futur champ dynamique posé dans `detecter()` serait figé pour la vie du
        process sans que personne ne le remarque — c'est exactement ce qui est
        arrivé au NPU et à la mémoire libre."""
        with _SondesActives(), \
                mock.patch("core.materiel.sys.platform", "linux"), \
                mock.patch("core.materiel.subprocess.run", side_effect=FileNotFoundError), \
                mock.patch("core.materiel.Path.read_text",
                           return_value="MemTotal: 16384000 kB\nMemAvailable: 4096000 kB\n"), \
                mock.patch("core.materiel.check_flm", return_value=True):
            cache = materiel.materiel()
        self.assertEqual(set(cache), {"ram_octets", "gpu", "ressource"})
        for cle in cache["gpu"]:
            self.assertNotIn("libre", cle)


class EndpointMaterielTest(unittest.TestCase):
    """`GET /models/materiel` — la forme du corps, et son refus de lever."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient  # noqa: PLC0415
        from core.auth import get_api_token  # noqa: PLC0415
        import main  # noqa: PLC0415
        # `base_url="http://localhost"` et non le defaut `testserver` :
        # TrustedHostMiddleware refuse ce Host-la et repond 400 sur TOUTES les
        # routes, y compris celle qu'on croit tester (meme piege que
        # test_auth_surface._client).
        cls.client = TestClient(main.app, base_url="http://localhost")
        cls.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        _vider_cache()
        self.addCleanup(_vider_cache)

    def test_corps(self):
        with mock.patch("core.materiel._tailles_ollama", return_value=None), \
                mock.patch("core.materiel.get_lmstudio_installed", return_value=None), \
                mock.patch("core.materiel.get_flm_installed", return_value=set()), \
                mock.patch("core.materiel.flm_model_ids", return_value=None):
            r = self.client.get("/models/materiel", headers=self.entetes)
        self.assertEqual(r.status_code, 200)
        corps = r.json()
        self.assertIn("materiel", corps)
        self.assertIn("modeles", corps)
        # Sondes coupées par `_test_env` : l'état est honnêtement vide, et les
        # clés sont TOUTES émises — une clé absente arrive `undefined` côté
        # TypeScript et les normaliseurs du frontend l'écraseraient (§8).
        for cle in ("ram_octets", "gpu", "npu", "ressource", "ressource_libre"):
            self.assertIn(cle, corps["materiel"])
        # Les DEUX nombres sont servis : le libre est le dénominateur, le total
        # est ce qui le rend lisible — « 9,5 Gio » seul ne dit pas si la machine
        # est petite ou simplement occupée.
        for cle in ("octets", "origine"):
            self.assertIn(cle, corps["materiel"]["ressource_libre"])
        for cle in ("nom", "vram_octets", "vram_partagee_octets", "partage_la_ram", "source"):
            self.assertIn(cle, corps["materiel"]["gpu"])
        for m in corps["modeles"]:
            self.assertEqual(set(m), {"id", "provider", "taille_octets", "verdict"})
            self.assertIn(m["verdict"], materiel.VERDICTS)

    def test_token_exige(self):
        self.assertEqual(self.client.get("/models/materiel").status_code, 401)


if __name__ == "__main__":
    unittest.main()
