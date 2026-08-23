#!/usr/bin/env python3
"""uvicorn ne parle WebSocket que si on le lui installe — et tout `/ws/*` en dépend.

**L'incident, pas la règle.** Chez un destinataire du paquet (ARM64, mais la
cause n'a rien d'architectural — voir plus bas), le backend démarrait sur
« ``WARNING: No supported WebSocket library detected`` » puis répondait **401 sur
/ws/chat**. Le chat, l'Atelier et la dictée passent tous par un WebSocket : le
paquet démarrait sans rien servir.

La chaîne complète, parce que le 401 envoie chercher au mauvais endroit :

1. ``uvicorn`` seul ne sait pas parler WebSocket. Il ne le sait que si
   ``websockets`` **ou** ``wsproto`` est importable
   (``uvicorn/protocols/websockets/auto.py``, essayés dans cet ordre) ; sinon
   ``AutoWebSocketsProtocol`` vaut ``None``.
2. ``ws_protocol_class`` étant ``None``, ``_should_upgrade()`` renvoie False :
   la requête d'upgrade n'est **pas** promue, elle est servie comme un GET HTTP
   ordinaire.
3. Le token du WebSocket voyage en query param, faute d'en-tête possible sur
   ``new WebSocket()`` (CLAUDE.md §3.6). Sur le chemin HTTP c'est donc
   ``_require_api_token`` qui répond, et il ne lit que ``Authorization:
   Bearer`` — d'où le 401. Le token n'était pas en cause, ni l'appairage.

**La cause était une ligne absente de requirements.txt, et elle n'a jamais
existé.** ``chromadb`` déclarait ``uvicorn[standard]>=0.18.3``, dont l'extra
``standard`` tire ``websockets>=10.4`` : l'implémentation arrivait en transitif,
par une dépendance qui n'avait rien à voir avec les WebSockets. Son retrait le
2026-08-13 (docs/remplacement-vectoriel.md) l'a emportée avec la grappe —
``tools/contraintes-paquet.txt`` la liste parmi les 49 départs, à côté de
``watchfiles``, du même extra. Celui-là était du poids mort ; celle-ci non, et
rien ne les distinguait.

**Le trou n'est pas propre à ARM64** : sur le poste de dev (x64), plus rien
d'installé ne réclame ``websockets`` — ``openai`` ne le déclare que sous son
extra ``realtime``, jamais installé. Il n'y survit qu'en orphelin de l'époque
chromadb, ce qui est *exactement* pourquoi le poste de dev ne pouvait pas voir
le bug.

Ce fichier garde en deux couches, et la première est celle qui aurait attrapé
le bug :

**1. La déclaration.** Le bug n'était pas dans le code mais dans la liste des
paquets : c'est la liste qu'il faut lire. Aucun ``TestClient`` ne pouvait le
voir — ``fastapi.testclient`` parle ASGI en mémoire, sans serveur HTTP, donc
sans jamais demander d'upgrade à uvicorn. ``test_auth_surface.py`` teste bien
``/ws/chat`` et restait vert pendant que le paquet livré ne servait aucun
WebSocket. Prouvé aveugle, donc insuffisant.

**2. L'environnement.** ``find_spec`` sur l'implémentation déclarée, puis le
verdict d'uvicorn lui-même (``AutoWebSocketsProtocol``), qui est la condition
exacte derrière le WARNING. Cette couche échoue dans un environnement
incomplet, et c'est voulu : une implémentation WebSocket est une **précondition
d'installation**, pas une option — même statut que ``piper-tts`` (CLAUDE.md
§2). D'où sa déclaration dans ``requirements.txt`` **et** dans les deps du job
``backend`` de la CI ; l'omettre de la CI rendrait ce garde-fou vert par
vacuité, ce que ce dépôt a déjà payé une fois avec ``tsc --noEmit``.

Usage :
    python test_websocket_dependance.py
"""

import importlib.util
import re
import unittest

import _test_env  # noqa: F401  — avant tout import de core.* (CLAUDE.md §3.5)

import uvicorn.config

from core.paths import BACKEND_DIR, REPO_ROOT

REQUIREMENTS = BACKEND_DIR / "requirements.txt"
CONTRAINTES = REPO_ROOT / "tools" / "contraintes-paquet.txt"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Les deux seules implémentations qu'uvicorn sait utiliser, dans son ordre de
#: préférence (``uvicorn/protocols/websockets/auto.py``). Le nom de distribution
#: est aussi la clé de ``WS_PROTOCOLS``, ce dont un test profite ci-dessous pour
#: que cette liste ne puisse pas se désynchroniser d'uvicorn en silence.
IMPLEMENTATIONS = ("websockets", "wsproto")

CONSIGNE = (
    "aucune implémentation WebSocket déclarée : uvicorn démarre en annonçant "
    "« No supported WebSocket library detected » et tout /ws/* répond 401 (la "
    "requête d'upgrade retombe en GET HTTP, où le token de query param n'est "
    "pas lu). Déclarer `wsproto` — Python pur, aucune wheel d'architecture à "
    "vérifier, aucun transitif nouveau."
)


def _declarations(texte: str) -> dict[str, str]:
    """``{paquet: version}`` des lignes ``paquet==version`` d'un fichier pip.

    Tolère les commentaires et les lignes vides ; ignore tout ce qui n'est pas
    un épinglage exact, ce qui suffit ici — les deux fichiers lus n'en
    contiennent pas d'autre forme.
    """
    trouve: dict[str, str] = {}
    for ligne in texte.splitlines():
        ligne = ligne.split("#", 1)[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)", ligne)
        if m:
            trouve[m.group(1).lower().replace("_", "-")] = m.group(2)
    return trouve


def _bloc_pip_install_ci(texte: str) -> str:
    """Le ``pip install`` du job ``backend`` de ci.yml, scalaire replié compris.

    Lu plutôt que deviné : c'est une commande sur plusieurs lignes (``run: >``),
    et un `in` sur tout le fichier accepterait une simple mention en
    commentaire — or c'est justement un commentaire qui explique pourquoi
    ``wsproto`` figure dans cette liste.
    """
    lignes = texte.splitlines()
    debut = next(
        (i for i, l in enumerate(lignes) if "pip install fastapi==" in l), None
    )
    assert debut is not None, "commande d'installation du job backend introuvable"
    bloc = [lignes[debut]]
    indent = len(lignes[debut]) - len(lignes[debut].lstrip())
    for ligne in lignes[debut + 1:]:
        nu = ligne.strip()
        if not nu or nu.startswith("- ") or nu.startswith("#"):
            break
        if len(ligne) - len(ligne.lstrip()) < indent:
            break
        bloc.append(ligne)
    return " ".join(l.strip() for l in bloc)


class DeclarationTest(unittest.TestCase):
    """Couche 1 — la liste des paquets. C'est elle qui portait le bug."""

    def setUp(self):
        self.requis = _declarations(REQUIREMENTS.read_text(encoding="utf-8"))

    def test_requirements_declare_une_implementation(self):
        declarees = [i for i in IMPLEMENTATIONS if i in self.requis]
        self.assertTrue(
            declarees,
            "backend/requirements.txt ne déclare ni "
            f"{' ni '.join(IMPLEMENTATIONS)} — {CONSIGNE}",
        )

    def test_uvicorn_sait_encore_utiliser_ce_qui_est_declare(self):
        """Une montée de version d'uvicorn ne doit pas retirer notre implémentation.

        Le nom de distribution est aussi la clé de ``WS_PROTOCOLS`` : le jour où
        uvicorn abandonne ``wsproto``, la clé disparaît et ce test le dit, au
        lieu de laisser un paquet déclaré que plus personne ne consomme.
        """
        for impl in (i for i in IMPLEMENTATIONS if i in self.requis):
            with self.subTest(impl=impl):
                self.assertIn(
                    impl,
                    uvicorn.config.WS_PROTOCOLS,
                    f"uvicorn {uvicorn.__version__} ne connaît plus « {impl} » : "
                    "choisir l'implémentation qu'il supporte encore.",
                )

    def test_contraintes_du_paquet_epinglent_la_meme_version(self):
        """Le paquet livré est construit avec ``-c tools/contraintes-paquet.txt``.

        Une implémentation déclarée dans requirements.txt mais absente des
        contraintes s'installerait dans la version publiée du jour : le paquet
        redeviendrait non reproductible sur la dépendance même dont l'absence a
        cassé /ws/*.
        """
        gel = _declarations(CONTRAINTES.read_text(encoding="utf-8"))
        for impl in (i for i in IMPLEMENTATIONS if i in self.requis):
            with self.subTest(impl=impl):
                self.assertIn(
                    impl, gel, f"« {impl} » absent de tools/contraintes-paquet.txt"
                )
                self.assertEqual(
                    gel[impl],
                    self.requis[impl],
                    f"« {impl} » épinglé en {gel[impl]} dans les contraintes du "
                    f"paquet et en {self.requis[impl]} dans requirements.txt",
                )

    def test_la_ci_installe_de_quoi_executer_la_couche_2(self):
        """Sans ça, les tests d'environnement ci-dessous seraient verts par vacuité.

        Le job ``backend`` n'installe pas requirements.txt mais un jeu réduit
        (en-tête de ci.yml) : l'implémentation doit y être nommée explicitement,
        sinon la CI ne vérifie plus rien de ce fichier.
        """
        commande = _bloc_pip_install_ci(CI_YML.read_text(encoding="utf-8"))
        installees = [i for i in IMPLEMENTATIONS if re.search(rf"\b{i}\b", commande)]
        self.assertTrue(
            installees,
            "le job backend de la CI n'installe aucune implémentation WebSocket : "
            "les tests d'environnement de ce fichier ne prouveraient plus rien.",
        )


class EnvironnementTest(unittest.TestCase):
    """Couche 2 — ce que l'installation courante permet réellement.

    Échouer ici est une réponse correcte : ça veut dire que cet environnement ne
    peut servir aucun WebSocket, donc ni chat, ni Atelier, ni dictée.
    """

    def test_une_implementation_est_importable(self):
        presentes = [i for i in IMPLEMENTATIONS if importlib.util.find_spec(i)]
        self.assertTrue(
            presentes,
            f"aucune des implémentations {IMPLEMENTATIONS} n'est importable dans "
            f"cet environnement — {CONSIGNE}",
        )

    def test_uvicorn_resout_un_protocole_websocket(self):
        """Le verdict d'uvicorn lui-même, et non une déduction.

        ``AutoWebSocketsProtocol is None`` EST la condition que teste
        ``_should_upgrade_to_ws()`` avant d'émettre « No supported WebSocket
        library detected ». On interroge donc la même valeur que le serveur.
        """
        from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol

        self.assertIsNotNone(
            AutoWebSocketsProtocol,
            "uvicorn ne résout aucun protocole WebSocket : c'est mot pour mot "
            "l'état qui a livré un paquet où /ws/chat répondait 401.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
