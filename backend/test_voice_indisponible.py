"""La voix doit être ABSENTE proprement, pas seulement sans modèle.

Deux états qu'on confond facilement et qui n'ont pas la même issue :

* **modèle manquant** — le paquet est installé, le `.onnx` de 76 Mo n'est pas
  encore là. Récupérable : un téléchargement suffit, et c'est ce que
  `etat_modele_vocal` annonce avant de le lancer.
* **paquet manquant** — `faster-whisper` / `piper-tts` ne sont pas installés du
  tout. **Définitif**, et c'est le cas de Windows ARM64 : `ctranslate2` ne publie
  aucune wheel `win_arm64` ni aucune sdist, donc `tools/faire_paquet.py --arch
  arm64` ne les installe pas (décision du 2026-08-22,
  `docs/remplacement-vectoriel.md`). Rien ne s'installera en cliquant.

Le second cas n'était couvert par aucun test. Il l'est ici, en simulant vraiment
l'absence (`sys.modules[nom] = None`, qui fait lever l'import comme sur une
machine où le paquet n'existe pas) et non en supprimant un fichier de modèle.

Ce fichier vérifie aussi ce que la détection ne doit PAS faire : importer. Savoir
si un module existe et le charger sont deux choses, et la seconde coûte des
secondes plus des bibliothèques natives en mémoire — le coût caché qui a déjà été
payé une fois avec `sentence_transformers` (17,4 s, CLAUDE.md §3.4).

Usage :
    python test_voice_indisponible.py
"""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les arbres AVANT tout import de core.*

from core import voice  # noqa: E402
from core.voice import (  # noqa: E402
    PiperEngine,
    VoiceModelUnavailable,
    WhisperEngine,
    capacites_vocales,
)

_MODULES_VOCAUX = ("faster_whisper", "ctranslate2", "piper")


@contextmanager
def paquet_absent(*noms: str):
    """Rend `noms` non importables, comme sur une machine où ils manquent.

    `sys.modules[nom] = None` est le mécanisme d'origine de CPython : un `import`
    y voit une entrée nulle et lève `ImportError`, et `find_spec` lève
    `ValueError`. C'est plus fidèle qu'un `mock.patch` de l'import, parce que ça
    frappe le même point que l'absence réelle — et ça vaut donc pour les deux
    chemins qu'on teste ici, celui de l'import et celui de la détection.

    Restaure l'état exact à la sortie : ces modules peuvent très bien être
    installés sur le poste qui exécute la suite, et les laisser piégés casserait
    tous les tests suivants.
    """
    avant = {nom: sys.modules.get(nom, ...) for nom in noms}
    for nom in noms:
        sys.modules[nom] = None  # type: ignore[assignment]
    voice._capacites = None      # le cache ne doit pas survivre au changement
    try:
        yield
    finally:
        for nom, valeur in avant.items():
            if valeur is ...:
                sys.modules.pop(nom, None)
            else:
                sys.modules[nom] = valeur
        voice._capacites = None


class CapacitesTest(unittest.TestCase):
    def setUp(self):
        voice._capacites = None

    def tearDown(self):
        voice._capacites = None

    def test_paquets_absents_donne_indisponible_et_nomme_ce_qui_manque(self):
        with paquet_absent(*_MODULES_VOCAUX):
            caps = capacites_vocales()
        self.assertFalse(caps["transcription"]["disponible"])
        self.assertFalse(caps["synthèse"]["disponible"])
        # Le nom PyPI, pas le nom de module : c'est ce qu'on installe.
        self.assertIn("faster-whisper", caps["transcription"]["manquants"])
        self.assertIn("ctranslate2", caps["transcription"]["manquants"])
        self.assertIn("piper-tts", caps["synthèse"]["manquants"])
        self.assertIn("ctranslate2", caps["transcription"]["raison"])

    def test_ctranslate2_seul_absent_suffit_a_couper_la_transcription(self):
        """C'est LE cas ARM64 : `faster-whisper` est du Python pur et
        s'installerait, `ctranslate2` non. Ne tester que le premier annoncerait
        une capacité disponible qui échoue à l'import.
        """
        with paquet_absent("ctranslate2"):
            caps = capacites_vocales()
        self.assertFalse(caps["transcription"]["disponible"])
        self.assertEqual(caps["transcription"]["manquants"], ["ctranslate2"])

    def test_la_detection_n_importe_pas_les_modules(self):
        """`find_spec`, pas `import`. Un diagnostic ne doit pas charger onnxruntime
        ni une bibliothèque native pour répondre « oui ».

        Ne juge que les modules ABSENTS de `sys.modules` au départ : ceux qu'un
        autre test aurait déjà importés ne prouveraient rien.
        """
        candidats = [n for n in _MODULES_VOCAUX if n not in sys.modules]
        if not candidats:
            self.skipTest("tous les modules vocaux sont déjà importés dans ce process")
        capacites_vocales(rafraichir=True)
        for nom in candidats:
            with self.subTest(module=nom):
                self.assertNotIn(nom, sys.modules,
                                 f"capacites_vocales() a importé {nom}")

    def test_le_resultat_est_memorise(self):
        """Appelée à chaque affichage de l'interface : elle ne doit pas re-balayer
        le disque. Vérifié par le comportement (une seule construction), pas en
        relisant le code.
        """
        premier = capacites_vocales()
        self.assertIs(capacites_vocales(), premier)
        self.assertIsNot(capacites_vocales(rafraichir=True), premier)


class MoteursSansPaquetTest(unittest.TestCase):
    def tearDown(self):
        voice._capacites = None

    def test_whisper_sans_faster_whisper_leve_voice_model_unavailable(self):
        """Et pas un `ImportError` nu : l'endpoint distingue 503 (voix absente,
        état prévu) de 500 (panne). Un ImportError qui remonte donne un 500 avec
        trace de pile pour une dépendance simplement pas installée.
        """
        with paquet_absent("faster_whisper"):
            with self.assertRaises(VoiceModelUnavailable) as ctx:
                WhisperEngine(model_size="tiny")
        self.assertIn("faster-whisper", str(ctx.exception))

    def test_whisper_sans_ctranslate2_leve_aussi(self):
        """L'import de `faster_whisper` tire `ctranslate2` : l'ImportError vient
        d'un module plus profond, mais il doit être attrapé pareil.
        """
        with paquet_absent("ctranslate2"):
            with self.assertRaises(VoiceModelUnavailable):
                WhisperEngine(model_size="tiny")

    def test_piper_sans_le_paquet_leve_avant_de_telecharger_76_mo(self):
        """La garantie neuve, et elle porte sur l'ORDRE.

        `_load` levait déjà `VoiceModelUnavailable` quand piper-tts manque — mais
        il tourne après `_ensure_model`, donc après 76 Mo téléchargés pour un
        moteur qui ne peut pas se construire. Sur ARM64, où le paquet est absent
        par construction, c'était 76 Mo à chaque tentative de synthèse pour finir
        sur le même 503.

        Le test piège `_ensure_model` au lieu de surveiller le réseau : c'est le
        point de passage obligé, et un piège qui lève nomme la régression au lieu
        de la faire deviner.
        """
        def _interdit(self):
            raise AssertionError(
                "_ensure_model appelé alors que piper-tts est absent — "
                "76 Mo téléchargés pour rien")

        with tempfile.TemporaryDirectory() as tmp:
            original = PiperEngine._ensure_model
            PiperEngine._ensure_model = _interdit
            try:
                with paquet_absent("piper"):
                    with self.assertRaises(VoiceModelUnavailable) as ctx:
                        PiperEngine(models_dir=tmp)
            finally:
                PiperEngine._ensure_model = original
            self.assertIn("piper-tts", str(ctx.exception))
            # Rien sur le disque : ni .onnx, ni .part d'un téléchargement entamé.
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [])


class EndpointTest(unittest.TestCase):
    """`GET /voice/capabilities` vu depuis le réseau.

    L'appeler par HTTP et pas seulement la fonction : ce qui peut casser sans
    qu'aucun test de `capacites_vocales` ne bouge, c'est le MONTAGE — un chemin
    mal préfixé (CLAUDE.md §3.3), une route qui n'existe plus. C'est ce que
    l'interface consomme, donc c'est ce qu'il faut interroger.

    `main` s'importe ici et pas en tête de fichier : son import démarre une vraie
    instance (moteurs, préchauffage RAG dans un thread). Les tests ci-dessus n'en
    ont pas besoin et ne doivent pas le payer quand ce fichier tourne seul.
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        import main
        from core.auth import get_api_token

        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def tearDown(self):
        voice._capacites = None

    def test_la_route_repond_les_deux_capacites(self):
        voice._capacites = None
        r = self.client.get("/voice/capabilities", headers=self.entetes)
        self.assertEqual(r.status_code, 200, r.text)
        corps = r.json()
        self.assertEqual(sorted(corps), ["synthèse", "transcription"])
        for capacite in corps.values():
            self.assertIsInstance(capacite["disponible"], bool)

    def test_paquets_absents_donne_disponible_false_sur_le_reseau(self):
        with paquet_absent(*_MODULES_VOCAUX):
            corps = self.client.get("/voice/capabilities", headers=self.entetes).json()
        self.assertFalse(corps["transcription"]["disponible"])
        self.assertFalse(corps["synthèse"]["disponible"])
        self.assertIn("piper-tts", corps["synthèse"]["raison"])

    def test_la_route_exige_le_token(self):
        """Comme tout le reste sauf /health et /pair — la capacité vocale n'est pas
        une information à donner à une page web visitée par hasard (CLAUDE.md §6).
        """
        self.assertEqual(self.client.get("/voice/capabilities").status_code, 401)


_REPO = Path(__file__).resolve().parent.parent


class InterfaceTest(unittest.TestCase):
    """Les contrôles vocaux doivent être MASQUÉS, pas laissés à échouer.

    Tests de FORME, sur le modèle de ceux de `test_paquet.py` : la suite Python ne
    lance pas npm (la CI a un job `frontend` pour `tsc` et le build). Ce ne sont
    pas des tests de substitution — `tsc` vérifie que le code compile, pas qu'un
    bouton est conditionné.

    Le dernier test est le seul qui compte vraiment sur la durée : il attrape un
    NOUVEAU point d'entrée vocal ajouté sans garde, ce qu'aucune relecture des
    fichiers déjà connus ne ferait.
    """

    #: Les deux seuls fichiers autorisés à appeler la voix — et ils la gardent.
    #: `voix.ts` appelle `/voice/capabilities`, qui est la garde elle-même.
    _POINTS_ENTREE = {"App.tsx", "ModuleBar.tsx", "voix.ts"}

    def _lire(self, *parties: str) -> str:
        return (_REPO.joinpath(*parties)).read_text(encoding="utf-8")

    def test_le_store_de_capacites_existe_et_interroge_le_bon_endpoint(self):
        texte = self._lire("frontend", "src", "voix.ts")
        self.assertIn("/voice/capabilities", texte)
        # Et pas d'APPEL à /models : celui-là interroge quatre API distantes
        # (timeout 4 s chacune), un bouton micro ne doit pas attendre le réseau
        # pour s'afficher. Le motif vise la forme de l'appel (`${API}/models`) et
        # non la chaîne nue, que les commentaires du fichier mentionnent
        # légitimement pour expliquer ce choix.
        self.assertNotIn("API}/models", texte)

    def test_le_micro_est_conditionne_a_la_capacite(self):
        texte = self._lire("frontend", "src", "components", "ModuleBar.tsx")
        self.assertIn("useVoix", texte)
        self.assertIn("voix.transcription", texte)
        # `showMic` seul ne doit plus commander le rendu du bouton : c'est
        # l'intention du module, pas la capacité de la machine.
        self.assertNotIn("{showMic && (", texte)

    def test_la_lecture_a_voix_haute_est_coupee_a_la_source(self):
        """`playSpeech` omis suffit : chat et kholle le gardent déjà derrière un
        `playSpeech && …`. Couper à la source plutôt qu'ajouter une condition par
        bouton — un module ajouté plus tard hérite du bon comportement.
        """
        texte = self._lire("frontend", "src", "App.tsx")
        self.assertIn("useVoix", texte)
        self.assertIn("voix.synthese ? playSpeech : undefined", texte)
        self.assertIn("voix.synthese ?", texte)

    def test_aucun_nouveau_point_d_entree_vocal_sans_garde(self):
        """Le vrai garde-fou : personne n'appelle la voix ailleurs.

        Balaie `frontend/src` ET `modules-catalogue` (les modules installables, dont
        kholle, qui existent en double — source et copie installée). Un composant
        neuf qui appellerait `/voice/transcribe` en direct afficherait forcément un
        contrôle sans consulter la capacité : il échouerait au clic sur ARM64, et
        c'est ce test qui le dirait au lieu du destinataire.
        """
        arbres = [_REPO / "frontend" / "src", _REPO / "modules-catalogue"]
        coupables: list[str] = []
        for arbre in arbres:
            if not arbre.is_dir():
                continue
            for fichier in list(arbre.rglob("*.tsx")) + list(arbre.rglob("*.ts")):
                if fichier.name in self._POINTS_ENTREE:
                    continue
                texte = fichier.read_text(encoding="utf-8", errors="replace")
                for route in ("/voice/transcribe", "/voice/synthesize", "/voice/model"):
                    if route in texte:
                        coupables.append(f"{fichier.relative_to(_REPO)} → {route}")
        self.assertEqual(
            coupables, [],
            "point d'entrée vocal hors des fichiers qui gardent la capacité : "
            + ", ".join(coupables))


if __name__ == "__main__":
    unittest.main(verbosity=2)
