"""Modèle vocal téléchargé à la demande : chemin portable, intégrité, dégradation.

Le `.onnx` de Piper (76 Mo) a quitté le dépôt : il est récupéré au premier usage
de la synthèse. Trois choses doivent tenir, et aucune n'est vérifiable par
relecture du code :

1. **`resolve_models_dir()` lit l'environnement à chaque appel.** Même forme que
   `test_data_dir.py`, et pour la même raison : la variable est posée **après**
   les imports, puis on constate où le moteur écrit réellement. `PiperEngine`
   recevait auparavant `models_dir="piper_models"`, un chemin relatif au cwd —
   qui ne fonctionnait que parce qu'`epure_tray.py` lance uvicorn depuis
   `backend/`.

2. **Un téléchargement corrompu ou interrompu ne laisse rien de valide.** La
   cible n'est écrite qu'après vérification du sha256, par un renommage
   atomique. Sans ça, une coupure réseau laisse un fichier tronqué qui *existe*,
   que le démarrage suivant croit valide et ne retélécharge jamais : une panne
   passagère devenue définitive.

3. **Sans réseau, seule la voix tombe.** Épure est local-first, la voix y est
   optionnelle. On monte l'app pour de vrai et on constate que `/voice/synthesize`
   répond 503 avec un message explicite pendant que le reste répond normalement.

Le réseau n'est jamais touché : `urllib.request.urlopen` est remplacé dans
`core.voice` par une fausse réponse. Un test qui téléchargerait vraiment 76 Mo
serait lent, dépendant du réseau, et ne prouverait pas grand-chose de plus que
la sonde faite à la main avant d'épingler l'URL.

Usage :
    python test_models_dir.py
"""

import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les arbres AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core import voice as core_voice  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.paths import resolve_models_dir  # noqa: E402
from core.voice import PiperEngine, VoiceModelUnavailable, etat_modele_vocal  # noqa: E402

VOIX = "fr_FR-upmc-medium"


def _client() -> TestClient:
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 54321))


class _FausseReponse:
    """Réponse HTTP minimale : ce que `_telecharger` consomme, et rien de plus."""

    def __init__(self, contenu: bytes, taille_annoncee=None):
        self._flux = io.BytesIO(contenu)
        annonce = len(contenu) if taille_annoncee is None else taille_annoncee
        self.headers = {"Content-Length": str(annonce)}

    def read(self, n=-1):
        return self._flux.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _servir(contenu_par_nom: dict[str, bytes]):
    """Remplaçant d'`urlopen` : sert un contenu selon le nom de fichier de l'URL."""

    def faux_urlopen(url, timeout=None):
        nom = url.rsplit("/", 1)[-1]
        if nom not in contenu_par_nom:
            raise OSError(f"404 simulé pour {nom}")
        return _FausseReponse(contenu_par_nom[nom])

    return faux_urlopen


def _reseau_coupe(*_a, **_kw):
    """Ce que donne urllib sans réseau : URLError, qui dérive d'OSError."""
    import urllib.error
    raise urllib.error.URLError("getaddrinfo failed (simulé)")


class _DossierModeles(unittest.TestCase):
    """Pose EPURE_MODELS_DIR sur un neuf, APRÈS les imports ci-dessus."""

    def setUp(self):
        self._prev = os.environ.get("EPURE_MODELS_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-modeles-"))
        os.environ["EPURE_MODELS_DIR"] = str(self.tmp)
        self.addCleanup(self._restaurer)

    def _restaurer(self):
        if self._prev is None:
            os.environ.pop("EPURE_MODELS_DIR", None)
        else:
            os.environ["EPURE_MODELS_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)


class ResolutionTest(_DossierModeles):
    def test_lit_la_variable_a_chaque_appel(self):
        """La preuve de la liaison tardive : la variable change, le résultat suit."""
        self.assertEqual(resolve_models_dir(), self.tmp.resolve())

        autre = Path(tempfile.mkdtemp(prefix="epure-modeles2-"))
        try:
            os.environ["EPURE_MODELS_DIR"] = str(autre)
            self.assertEqual(
                resolve_models_dir(), autre.resolve(),
                "resolve_models_dir() doit relire l'environnement, pas servir un cache",
            )
        finally:
            shutil.rmtree(autre, ignore_errors=True)

    def test_defaut_sans_variable(self):
        os.environ.pop("EPURE_MODELS_DIR", None)
        attendu = (Path(core_voice.__file__).resolve().parent.parent / "piper_models").resolve()
        self.assertEqual(resolve_models_dir(), attendu)

    def test_toujours_resolu(self):
        self.assertTrue(resolve_models_dir().is_absolute())

    def test_le_moteur_suit_la_variable_posee_apres_import(self):
        """Le cas qui a mordu ailleurs : un chemin figé dans un défaut d'argument.

        `PiperEngine(models_dir="piper_models")` était un défaut évalué à
        l'import ET relatif au cwd. Ici la variable est posée par `setUp`, donc
        bien après l'import de `core.voice` : si le moteur figeait quoi que ce
        soit, il écrirait ailleurs et l'assertion tomberait.
        """
        contenus = {f"{VOIX}.onnx": b"faux modele", f"{VOIX}.onnx.json": b"{}"}
        with mock.patch.object(core_voice.urllib.request, "urlopen", _servir(contenus)), \
             mock.patch.object(PiperEngine, "_SHA256", {}), \
             mock.patch.object(PiperEngine, "_load", lambda self: object()):
            moteur = PiperEngine(voice=VOIX)
        self.assertEqual(moteur._models_dir, self.tmp)
        self.assertTrue((self.tmp / f"{VOIX}.onnx").is_file())
        self.assertTrue((self.tmp / f"{VOIX}.onnx.json").is_file())


class IntegriteTest(_DossierModeles):
    """Le sha256 est la seule chose qui autorise la cible à exister."""

    def _sha(self, donnees: bytes) -> str:
        return hashlib.sha256(donnees).hexdigest()

    def test_telechargement_conforme_ecrit_la_cible(self):
        onnx, cfg = b"contenu modele", b'{"sample_rate": 22050}'
        empreintes = {f"{VOIX}.onnx": self._sha(onnx), f"{VOIX}.onnx.json": self._sha(cfg)}
        with mock.patch.object(core_voice.urllib.request, "urlopen",
                               _servir({f"{VOIX}.onnx": onnx, f"{VOIX}.onnx.json": cfg})), \
             mock.patch.object(PiperEngine, "_SHA256", empreintes), \
             mock.patch.object(PiperEngine, "_load", lambda self: object()):
            PiperEngine(voice=VOIX)
        self.assertEqual((self.tmp / f"{VOIX}.onnx").read_bytes(), onnx)
        self.assertEqual((self.tmp / f"{VOIX}.onnx.json").read_bytes(), cfg)

    def test_empreinte_divergente_supprime_et_leve(self):
        faux = {f"{VOIX}.onnx": b"contenu altere", f"{VOIX}.onnx.json": b"{}"}
        empreintes = {f"{VOIX}.onnx": "0" * 64, f"{VOIX}.onnx.json": "0" * 64}
        with mock.patch.object(core_voice.urllib.request, "urlopen", _servir(faux)), \
             mock.patch.object(PiperEngine, "_SHA256", empreintes):
            with self.assertRaises(VoiceModelUnavailable) as ctx:
                PiperEngine(voice=VOIX)
        self.assertIn("mpreinte", str(ctx.exception))
        self.assertFalse(
            (self.tmp / f"{VOIX}.onnx").exists(),
            "un modèle dont l'empreinte diverge doit être supprimé, pas conservé",
        )

    def test_rien_ne_survit_a_une_coupure(self):
        """Ni la cible, ni le `.part` : sinon la panne devient définitive."""
        with mock.patch.object(core_voice.urllib.request, "urlopen", _reseau_coupe):
            with self.assertRaises(VoiceModelUnavailable):
                PiperEngine(voice=VOIX)
        restes = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(restes, [], f"fichiers laissés derrière : {restes}")

    def test_la_cible_n_existe_jamais_incomplete(self):
        """Un `.part` d'une tentative précédente ne doit pas être pris pour la cible."""
        (self.tmp / f"{VOIX}.onnx.part").write_bytes(b"tronque")
        onnx, cfg = b"contenu complet", b"{}"
        empreintes = {f"{VOIX}.onnx": self._sha(onnx), f"{VOIX}.onnx.json": self._sha(cfg)}
        with mock.patch.object(core_voice.urllib.request, "urlopen",
                               _servir({f"{VOIX}.onnx": onnx, f"{VOIX}.onnx.json": cfg})), \
             mock.patch.object(PiperEngine, "_SHA256", empreintes), \
             mock.patch.object(PiperEngine, "_load", lambda self: object()):
            PiperEngine(voice=VOIX)
        self.assertEqual((self.tmp / f"{VOIX}.onnx").read_bytes(), onnx)
        self.assertFalse((self.tmp / f"{VOIX}.onnx.part").exists())

    def test_voix_deja_presente_ne_retelecharge_pas(self):
        (self.tmp / f"{VOIX}.onnx").write_bytes(b"deja la")
        (self.tmp / f"{VOIX}.onnx.json").write_bytes(b"{}")

        def interdit(*_a, **_kw):
            raise AssertionError("le modèle est déjà là : aucun appel réseau attendu")

        with mock.patch.object(core_voice.urllib.request, "urlopen", interdit), \
             mock.patch.object(PiperEngine, "_load", lambda self: object()):
            PiperEngine(voice=VOIX)

    def test_nom_de_voix_invalide_refuse_avant_le_reseau(self):
        def interdit(*_a, **_kw):
            raise AssertionError("un nom de voix invalide ne doit rien télécharger")

        with mock.patch.object(core_voice.urllib.request, "urlopen", interdit):
            for mauvais in ("frFR-upmc-medium", "fr_FR-upmc", "", "../../evil"):
                with self.subTest(voix=mauvais), self.assertRaises(VoiceModelUnavailable):
                    PiperEngine(voice=mauvais)


class EtatModeleTest(_DossierModeles):
    """`GET /voice/model` doit répondre SANS construire le moteur."""

    def test_absent_puis_present(self):
        etat = etat_modele_vocal(VOIX)
        self.assertFalse(etat["présent"])
        self.assertFalse(etat["téléchargement_en_cours"])
        self.assertGreater(etat["taille_attendue_mo"], 50)

        (self.tmp / f"{VOIX}.onnx").write_bytes(b"x")
        (self.tmp / f"{VOIX}.onnx.json").write_bytes(b"{}")
        self.assertTrue(etat_modele_vocal(VOIX)["présent"])

    def test_un_seul_des_deux_fichiers_ne_suffit_pas(self):
        """Le .onnx et son .json sont une paire — un décalage serait silencieux."""
        (self.tmp / f"{VOIX}.onnx").write_bytes(b"x")
        self.assertFalse(etat_modele_vocal(VOIX)["présent"])


class DegradationTest(_DossierModeles):
    """Sans réseau, la voix tombe — et rien d'autre.

    L'app est montée pour de vrai : c'est la seule façon de vérifier « rien
    d'autre ne casse » plutôt que de le supposer.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def test_synthese_indisponible_repond_503_avec_un_message(self):
        with mock.patch.object(core_voice.urllib.request, "urlopen", _reseau_coupe):
            res = self.client.post("/voice/synthesize", json={"text": "bonjour"},
                                   headers=self.headers)
        self.assertEqual(res.status_code, 503, res.text)
        detail = res.json()["detail"]
        self.assertIn("modèle vocal", detail.lower())
        self.assertNotEqual(detail, "Erreur synthèse vocale")

    def test_le_reste_de_l_app_repond_normalement(self):
        with mock.patch.object(core_voice.urllib.request, "urlopen", _reseau_coupe):
            self.client.post("/voice/synthesize", json={"text": "bonjour"},
                             headers=self.headers)
            for chemin in ("/health", "/modules", "/instance/config"):
                with self.subTest(chemin=chemin):
                    res = self.client.get(chemin, headers=self.headers)
                    self.assertEqual(res.status_code, 200, res.text)

    def test_l_etat_du_modele_reste_interrogeable_sans_reseau(self):
        with mock.patch.object(core_voice.urllib.request, "urlopen", _reseau_coupe):
            res = self.client.get("/voice/model", headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("présent", res.json())

    def test_un_echec_ne_condamne_pas_les_tentatives_suivantes(self):
        """`_LazyEngine` ne mémorise pas un échec : le réseau revenu, ça remarche.

        Vérifié parce que c'est ce qui distingue « voix indisponible aujourd'hui »
        de « voix perdue jusqu'au prochain redémarrage ».
        """
        from core.runtime import piper
        self.assertIsNone(
            piper._engine,
            "prérequis : la voix n'a pas encore été construite dans ce process",
        )
        with mock.patch.object(core_voice.urllib.request, "urlopen", _reseau_coupe):
            with self.assertRaises(VoiceModelUnavailable):
                piper.synthesize("bonjour")
        self.assertIsNone(
            piper._engine,
            "un échec de construction ne doit pas être mis en cache par _LazyEngine",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
