"""Tests de `lanceur.py` — la logique du tray, sans icône.

Ces tests existent parce que trois bugs du lanceur ont été trouvés À LA MAIN,
un par un, en relançant le tray et en lisant son journal. Aucun n'était
couvert : la logique vivait dans `epure_tray.py`, à côté de `pystray.Icon`, donc
inimportable sur le runner Linux sans display de la CI. Le découpage en
`lanceur.py` sert d'abord à ça.

Le fichier tourne dans la découverte automatique (CLAUDE.md §2), sans job ni
display supplémentaire : il n'importe ni pystray, ni PIL, ni `core.*` — donc pas
de `_test_env` à poser, rien n'écrit dans `backend/memory/`.

Les trois bugs verrouillés ici :

1. **L'URL dans un global.** Une affectation sans `global` en avait fait une
   variable locale : le navigateur s'ouvrait sur le bon port, mais « Ouvrir
   Épure » rouvrait l'ancien — donc l'ancien frontend.
2. **La sonde de disponibilité passait par /health.** Elle expirait avant que le
   backend soit joignable dès qu'Ollama était éteint, parce que /health attend
   alors le timeout de connexion du client Ollama.
3. **L'occupant de 5173 n'était pas détecté.** Vite basculait sur 5174 et le
   tray ouvrait 5173 quand même.
"""

import os
import socket
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# lanceur.py vit à la racine du dépôt, à côté de epure_tray.py : c'est un outil
# de poste, pas du code applicatif du backend.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lanceur  # noqa: E402


def _port_libre() -> int:
    """Un port qui n'écoute pas : on en réserve un puis on le relâche."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestIsolationDuModule(unittest.TestCase):
    """Ce qui garantit que ce fichier tourne en CI, sur Linux, sans display."""

    def test_aucun_import_d_interface(self):
        source = Path(lanceur.__file__).read_text(encoding="utf-8")
        for interdit in ("import pystray", "from pystray", "import PIL", "from PIL"):
            self.assertNotIn(
                interdit, source,
                f"{interdit} dans lanceur.py : le module redevient inimportable "
                "sur un runner sans serveur X, et test_lanceur.py avec lui",
            )

    def test_aucun_import_de_core(self):
        source = Path(lanceur.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import core", source)
        self.assertNotIn("from core", source)

    def test_fenetre_masquee_sans_startupinfo(self):
        """Hors Windows, pas de STARTUPINFO : la fonction rend None, elle ne lève pas.

        C'est la raison d'être de la FONCTION `fenetre_masquee` : la même valeur
        en constante de module était évaluée à l'import et levait un
        AttributeError sur Linux.
        """
        faux = types.SimpleNamespace()  # aucun attribut STARTUPINFO
        with mock.patch.object(lanceur, "subprocess", faux):
            self.assertIsNone(lanceur.fenetre_masquee())

    def test_fenetre_masquee_sur_cette_plateforme(self):
        resultat = lanceur.fenetre_masquee()
        if hasattr(lanceur.subprocess, "STARTUPINFO"):
            self.assertIsNotNone(resultat)
        else:
            self.assertIsNone(resultat)


class TestEtatLanceur(unittest.TestCase):
    """Bug 1 — l'URL servie doit suivre le port réellement retenu."""

    def test_url_par_defaut(self):
        self.assertEqual(lanceur.EtatLanceur().url, "http://localhost:5173")

    def test_definir_port_interface_met_a_jour_l_url(self):
        etat = lanceur.EtatLanceur()
        change = etat.definir_port_interface(5174)
        self.assertTrue(change, "un port différent doit être signalé à l'appelant")
        self.assertEqual(etat.url, "http://localhost:5174")
        self.assertEqual(etat.port_interface, 5174)

    def test_port_attendu_ne_signale_rien(self):
        etat = lanceur.EtatLanceur()
        self.assertFalse(etat.definir_port_interface(lanceur.PORT_VITE))
        self.assertEqual(etat.url, "http://localhost:5173")

    def test_la_mise_a_jour_est_vue_par_le_menu(self):
        """LE bug : l'orchestration met à jour, le menu doit lire la même chose.

        Reproduit la forme exacte de l'incident. `_demarrer()` affectait `_url`
        sans `global` : la valeur partait dans une locale, `webbrowser.open`
        ouvrait le bon port juste après, et « Ouvrir Épure » relisait le global
        resté à 5173. Passer par un attribut d'objet rend l'erreur impossible —
        une affectation d'attribut ne peut pas créer de portée locale.
        """
        etat = lanceur.EtatLanceur()

        def orchestration(e):        # ce que fait _demarrer()
            e.definir_port_interface(5174)

        def menu(e):                 # ce que lit _on_open()
            return e.url

        orchestration(etat)
        self.assertEqual(menu(etat), "http://localhost:5174")

    def test_infobulle_normale_et_degradee(self):
        etat = lanceur.EtatLanceur()
        self.assertEqual(etat.infobulle(), "Épure")
        etat.ajouter_incident("Ollama introuvable")
        self.assertIn("dégradé", etat.infobulle())
        self.assertIn("Ollama introuvable", etat.infobulle())

    def test_infobulle_tronquee(self):
        """Windows coupe au-delà de 127 caractères : on coupe nous-mêmes, plus court."""
        etat = lanceur.EtatLanceur()
        for i in range(8):
            etat.ajouter_incident(f"incident numero {i} avec un texte deliberement long")
        bulle = etat.infobulle()
        self.assertLessEqual(len(bulle), 125)
        self.assertTrue(bulle.endswith("…"))

    def test_reinitialiser_vide_les_incidents(self):
        etat = lanceur.EtatLanceur()
        etat.ajouter_incident("x")
        etat.reinitialiser()
        self.assertEqual(etat.incidents, [])
        self.assertEqual(etat.infobulle(), "Épure")


def _netstat(lignes: str):
    """Remplace subprocess.run par un netstat en boîte."""
    return mock.patch.object(
        lanceur.subprocess, "run",
        return_value=types.SimpleNamespace(stdout=lignes, returncode=0),
    )


class TestPortOccupant(unittest.TestCase):
    """Bug 3, première moitié — savoir QUI tient un port, sans se tromper."""

    SORTIE = (
        "\r\n"
        "Connexions actives\r\n"
        "\r\n"
        "  Proto  Adresse locale         Adresse distante       État           PID\r\n"
        "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       4242\r\n"
        "  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       777\r\n"
        "  TCP    [::1]:11434            [::]:0                 LISTENING       999\r\n"
    )

    def test_trouve_le_pid_qui_ecoute(self):
        with _netstat(self.SORTIE):
            self.assertEqual(lanceur.port_occupant(8000), 4242)
            self.assertEqual(lanceur.port_occupant(5173), 777)

    def test_fonctionne_en_ipv6(self):
        with _netstat(self.SORTIE):
            self.assertEqual(lanceur.port_occupant(11434), 999)

    def test_port_libre(self):
        with _netstat(self.SORTIE):
            self.assertIsNone(lanceur.port_occupant(9999))

    def test_ne_confond_pas_un_port_dont_8000_est_un_suffixe(self):
        """`":8000" in ligne` acceptait 18000 et 48000. Le découpage en colonnes, non."""
        sortie = (
            "  TCP    127.0.0.1:18000        0.0.0.0:0              LISTENING       11\r\n"
            "  TCP    127.0.0.1:48000        0.0.0.0:0              LISTENING       22\r\n"
        )
        with _netstat(sortie):
            self.assertIsNone(lanceur.port_occupant(8000))

    def test_ignore_l_adresse_distante(self):
        """Un port qui n'apparaît qu'en colonne DISTANTE n'est pas un occupant local."""
        sortie = (
            "  TCP    127.0.0.1:52000        127.0.0.1:5173         ESTABLISHED     33\r\n"
        )
        with _netstat(sortie):
            self.assertIsNone(lanceur.port_occupant(5173))

    def test_ignore_les_connexions_non_listening(self):
        sortie = (
            "  TCP    127.0.0.1:8000         127.0.0.1:52001        ESTABLISHED     44\r\n"
        )
        with _netstat(sortie):
            self.assertIsNone(lanceur.port_occupant(8000))

    def test_netstat_absent_ne_leve_pas(self):
        """Sur un système sans netstat — un runner Linux, par exemple."""
        with mock.patch.object(lanceur.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(lanceur.port_occupant(8000))


class TestNomProcessus(unittest.TestCase):
    def test_lit_le_nom_dans_la_sortie_csv(self):
        csv = '"python.exe","13768","Console","1","52 000 Ko"\r\n'
        with mock.patch.object(
            lanceur.subprocess, "run",
            return_value=types.SimpleNamespace(stdout=csv, returncode=0),
        ):
            self.assertEqual(lanceur.nom_processus(13768), "python.exe")

    def test_sortie_vide_donne_inconnu(self):
        with mock.patch.object(
            lanceur.subprocess, "run",
            return_value=types.SimpleNamespace(stdout="", returncode=1),
        ):
            self.assertEqual(lanceur.nom_processus(1), "inconnu")


class TestTuerArbre(unittest.TestCase):
    def test_taskkill_avec_T_et_le_bon_pid(self):
        """`/T` : sans lui, tuer le shell de npm laissait node vivant et le port pris."""
        with mock.patch.object(lanceur.subprocess, "run") as run:
            lanceur.tuer_arbre(4242)
        args = run.call_args[0][0]
        self.assertIn("/T", args)
        self.assertIn("4242", args)
        self.assertEqual(args[0], "taskkill")


class TestIdentificationDuBackend(unittest.TestCase):
    """On ne tue que ce qu'on a identifié — sinon c'est le kill aveugle de start.ps1."""

    def test_forme_de_health_reconnue(self):
        with mock.patch.object(
            lanceur, "http_json",
            return_value={"ollama": True, "model": "qwen2.5:7b", "models": [], "flm": False},
        ):
            self.assertTrue(lanceur.backend_epure_repond(8000))

    def test_un_autre_service_json_n_est_pas_epure(self):
        with mock.patch.object(lanceur, "http_json", return_value={"status": "ok"}):
            self.assertFalse(lanceur.backend_epure_repond(8000))

    def test_pas_de_reponse(self):
        with mock.patch.object(lanceur, "http_json", return_value=None):
            self.assertFalse(lanceur.backend_epure_repond(8000))

    def test_ollama_reconnu_par_api_tags(self):
        with mock.patch.object(lanceur, "http_json", return_value={"models": []}):
            self.assertTrue(lanceur.ollama_repond(11434))
        with mock.patch.object(lanceur, "http_json", return_value=None):
            self.assertFalse(lanceur.ollama_repond(11434))

    def test_http_json_avale_les_erreurs(self):
        with mock.patch.object(
            lanceur.urllib.request, "urlopen", side_effect=OSError("refus")
        ):
            self.assertIsNone(lanceur.http_json("http://127.0.0.1:1/health"))


class TestAttendreBackend(unittest.TestCase):
    """Bug 2 — la disponibilité se mesure sur le port, pas sur /health."""

    def test_port_qui_ecoute(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(lanceur.attendre_backend(port, timeout=2.0, pause=0.05))
        finally:
            srv.close()

    def test_port_muet_expire_et_le_dit(self):
        debut = time.monotonic()
        self.assertFalse(
            lanceur.attendre_backend(_port_libre(), timeout=0.4, pause=0.05)
        )
        self.assertLess(time.monotonic() - debut, 5.0)

    def test_ne_depend_pas_de_health(self):
        """LE bug : uvicorn écoutait, /health était lent, la sonde concluait « muet ».

        Ollama éteint, /health attend le timeout de connexion du client Ollama
        (core/llm.py, connect=5 s) et dépassait la sonde. On simule ici le pire
        cas — /health ne répond jamais — et la disponibilité doit rester vraie,
        parce qu'uvicorn ne se lie au port qu'après son démarrage applicatif.
        """
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            with mock.patch.object(lanceur, "http_json", return_value=None) as sonde:
                self.assertTrue(lanceur.attendre_backend(port, timeout=2.0, pause=0.05))
                sonde.assert_not_called()
        finally:
            srv.close()

    def test_port_accepte_sur_port_ferme(self):
        self.assertFalse(lanceur.port_accepte(_port_libre(), timeout=0.3))


class TestLirePortVite(unittest.TestCase):
    """Bug 3, seconde moitié — ouvrir le port que Vite a vraiment pris."""

    SORTIE_5174 = (
        "\n> frontend@0.0.0 dev\n> vite\n\n"
        "Port 5173 is in use, trying another one...\n\n"
        "  VITE v8.0.14  ready in 828 ms\n\n"
        "  ➜  Local:   http://localhost:5174/\n"
        "  ➜  Network: http://10.220.18.70:5174/\n"
    )

    def _journal(self, contenu: str) -> str:
        fd, chemin = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contenu)
        self.addCleanup(lambda: os.path.exists(chemin) and os.unlink(chemin))
        return chemin

    def test_lit_le_port_de_repli(self):
        journal = self._journal(self.SORTIE_5174)
        self.assertEqual(
            lanceur.lire_port_vite(journal, offset=0, timeout=0.3, pause=0.05), 5174
        )

    def test_lit_le_port_nominal(self):
        journal = self._journal("  ➜  Local:   http://localhost:5173/\n")
        self.assertEqual(
            lanceur.lire_port_vite(journal, offset=0, timeout=0.3, pause=0.05), 5173
        )

    def test_l_offset_ignore_le_demarrage_precedent(self):
        """Sans offset, on relirait le port d'un lancement antérieur.

        La détection mentirait au lieu de corriger : elle rendrait 5173 — la
        valeur même qu'on cherche à ne plus supposer — avec l'autorité d'une
        mesure.
        """
        ancien = "  ➜  Local:   http://localhost:5173/\n"
        journal = self._journal(ancien + self.SORTIE_5174)
        offset = len(ancien.encode("utf-8"))
        self.assertEqual(
            lanceur.lire_port_vite(journal, offset=offset, timeout=0.3, pause=0.05),
            5174,
        )

    def test_repli_sur_le_defaut_si_vite_ne_dit_rien(self):
        journal = self._journal("npm ne dit rien d'utile\n")
        self.assertEqual(
            lanceur.lire_port_vite(journal, offset=0, timeout=0.2, pause=0.05), 5173
        )

    def test_journal_absent(self):
        self.assertEqual(
            lanceur.lire_port_vite(
                os.path.join(tempfile.gettempdir(), "journal-qui-n-existe-pas.log"),
                offset=0, timeout=0.2, pause=0.05,
            ),
            5173,
        )

    def test_ignore_l_adresse_reseau(self):
        """Vite affiche aussi une URL « Network » sur une IP : seul localhost compte."""
        journal = self._journal(
            "  ➜  Network: http://10.220.18.70:4321/\n"
            "  ➜  Local:   http://localhost:5174/\n"
        )
        self.assertEqual(
            lanceur.lire_port_vite(journal, offset=0, timeout=0.3, pause=0.05), 5174
        )

    def test_taille_journal(self):
        journal = self._journal("douze octets")
        self.assertEqual(lanceur.taille_journal(journal), len("douze octets"))
        self.assertEqual(lanceur.taille_journal("/aucun/fichier/ici.log"), 0)


if __name__ == "__main__":
    unittest.main()
