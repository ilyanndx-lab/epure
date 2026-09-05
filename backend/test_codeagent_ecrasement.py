#!/usr/bin/env python3
"""Le repli « aucun tool appelé » du CodeAgent n'écrase plus le fichier actif
sans confirmation — correctif du 2026-09-05.

L'incident : `CodeAgent.run_turn` (`core/codeagent.py`) parse la réponse du
modèle avec `parse_tool_calls`. Quand celui-ci ne rend RIEN et qu'un bloc
```` ``` ```` traîne dans la réponse, le repli écrivait le PREMIER bloc sur le
fichier actif via `create_file` — donc `write_text`, écrasement complet — puis
émettait l'avertissement « Tool non utilisé » APRÈS coup.

Le cas qui casse est le cas NORMAL, pas un cas tordu : « explique-moi ce que
fait ce fichier », « montre-moi la fonction X ». Un modèle qui explique cite un
fragment dans un bloc de code ; le fichier entier était remplacé par ce
fragment. Et `workspace/` est gitignoré (`.gitignore`) : aucun historique git
ne rattrape la version perdue.

Le correctif reprend le mécanisme de `execute_code`, qui est déjà derrière une
confirmation (`needs_confirm` → `execute_request` → bouton → `execute_confirm`)
plutôt que d'inventer une heuristique « est-ce un fragment ? » qui se tromperait
un jour : le repli émet un `write_request` et **n'écrit pas**. L'écriture n'a
lieu qu'au retour de l'utilisateur (`write_confirm` sur `/ws/code`), et elle
sauvegarde d'abord la version précédente hors du workspace.

Deux trous restaient ouverts après ce premier correctif, fermés le même jour :

**Le filet ne couvrait qu'un chemin.** Seul `appliquer_ecriture` sauvegardait,
donc seul le repli CONFIRMÉ était protégé. Le tool `create_file` du modèle
écrit, lui, sans rien demander (`dispatch_tool`), tout comme `generate_tests`
et le `POST /code/file` de l'éditeur : tous écrasaient sans laisser de trace.
La sauvegarde est descendue dans `create_file`, par où passent les quatre — et
si elle échoue, l'écriture n'a pas lieu (`SauvegardeError`), même règle et même
raison que sur le chemin confirmé.

**Une conversion perdait l'intention du modèle.** `parse_tool_calls`
transformait `**edit_file** \\`path\\`` + bloc markdown en `create_file` à
contenu complet : le modèle demandait de remplacer QUELQUES LIGNES, on
remplaçait le FICHIER ENTIER par le fragment, sans confirmation. Le markdown ne
portant ni `old` ni `new`, l'édition demandée est inexécutable — ce cas rejoint
donc le `write_request` du repli, et l'utilisateur tranche. `**create_file**`
en markdown annonce bien un fichier complet : il reste une écriture directe.

Ce que ce test verrouille, dans cet ordre d'importance :
  1. le fichier sur le DISQUE est inchangé (pas seulement « un événement a été
     émis » — une assertion sur les seuls événements passerait encore si un
     autre chemin écrivait) ;
  2. la confirmation est proposée, avec le contenu, qu'on écrase ou qu'on crée ;
  3. tout écrasement laisse un `.bak`, quel que soit le chemin d'appel, et une
     sauvegarde impossible annule l'écriture ;
  4. le chemin normal `parse_tool_calls` → `dispatch_tool` reste intact ;
  5. l'écriture confirmée sauvegarde la version précédente.

Usage :
    python test_codeagent_ecrasement.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Permettre d'importer le package `core` depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.*

from core import codeagent


_REPO = Path(__file__).resolve().parent.parent


class _StubLLM:
    """Modèle bouchonné : rend une réponse fixée, sans réseau ni Ollama.

    `run_turn` rappelle `stream` pour la phrase de conclusion — d'où le
    compteur, qui sert aussi à vérifier qu'aucune phase de réflexion ne part
    (elle est neutralisée en laissant `reflection_model`/`pipeline` à None).
    """

    def __init__(self, reponse: str):
        self.reponse = reponse
        self.appels = 0

    def stream(self, messages, model=None, max_tokens=None, **kwargs):
        self.appels += 1
        yield self.reponse

    def generate(self, messages, model=None, **kwargs):
        return "✓ Code OK"


class _WorkspaceTest(unittest.TestCase):
    """Racine de travail confinée à un temporaire — même patron que
    `test_safe_path.SafePathTest` et `test_codeagent_plots._WorkspaceTest`,
    pour ne jamais toucher au vrai workspace de l'utilisateur."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_workspace = codeagent.WORKSPACE
        codeagent.WORKSPACE = Path(self._tmp.name).resolve()

    def tearDown(self):
        codeagent.WORKSPACE = self._orig_workspace
        self._tmp.cleanup()

    def _ecrire(self, nom: str, contenu: str) -> str:
        cible = codeagent.WORKSPACE / nom
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(contenu, encoding="utf-8")
        return nom

    def _lire(self, nom: str) -> str:
        return (codeagent.WORKSPACE / nom).read_text(encoding="utf-8")

    @staticmethod
    def _contexte(nom: str, contenu: str) -> str:
        """Reproduit le `file_context` que construit le frontend :
        `<chemin>\\n``` \\n<contenu>\\n``` ` — la PREMIÈRE ligne est le chemin,
        c'est elle que lit le repli (`file_context.split("\\n")[0]`)."""
        return f"{nom}\n```\n{contenu}\n```"

    @staticmethod
    def _tour(agent, message, contexte):
        return list(agent.run_turn(message, contexte))


# Contenu réel du fichier ouvert : ce qui doit survivre à une simple explication.
_FICHIER_ORIGINAL = '''\
"""Utilitaires de calcul."""


def additionner(a, b):
    return a + b


def multiplier(a, b):
    return a * b


def diviser(a, b):
    if b == 0:
        raise ZeroDivisionError("division par zéro")
    return a / b
'''

# Réponse typique d'un modèle à qui on demande une EXPLICATION : de la prose,
# un fragment cité dans un bloc de code, aucune balise <tool>.
_REPONSE_EXPLICATION = """\
Ce fichier expose trois fonctions de calcul. La plus intéressante est
`diviser`, qui garde le cas du diviseur nul :

```python
def diviser(a, b):
    if b == 0:
        raise ZeroDivisionError("division par zéro")
    return a / b
```

Les deux autres sont des enveloppes directes des opérateurs Python.
"""


class ReplipasDEcrasementTest(_WorkspaceTest):
    """Le cœur du correctif : un bloc de code sans balise d'outil ne touche
    plus au disque."""

    def test_explication_avec_bloc_de_code_n_ecrase_pas_le_fichier_actif(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_EXPLICATION))

        self._tour(agent, "explique-moi ce que fait ce fichier",
                   self._contexte(nom, _FICHIER_ORIGINAL))

        # L'assertion qui compte : le CONTENU, pas les événements.
        self.assertEqual(
            self._lire(nom), _FICHIER_ORIGINAL,
            "le fichier actif a été écrasé par le fragment cité dans la réponse",
        )

    def test_une_confirmation_est_proposee_avec_le_contenu(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_EXPLICATION))

        events = self._tour(agent, "montre-moi juste diviser",
                            self._contexte(nom, _FICHIER_ORIGINAL))

        demandes = [e for e in events if e.get("type") == "write_request"]
        self.assertEqual(len(demandes), 1,
                         "le repli doit proposer l'écriture, pas la faire")
        self.assertEqual(demandes[0]["path"], nom)
        self.assertIn("def diviser", demandes[0]["content"])
        self.assertTrue(demandes[0]["existant"],
                        "le fichier existe : la confirmation doit dire « écraser »")

    def test_aucun_ecrit_annonce_tant_que_rien_n_est_confirme(self):
        """Aucun `tool_result` de création, donc aucune conclusion « j'ai créé »
        pour un fichier que personne n'a écrit."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_EXPLICATION))

        events = self._tour(agent, "explique ce fichier",
                            self._contexte(nom, _FICHIER_ORIGINAL))

        ecritures = [e for e in events
                     if e.get("type") == "tool_result" and e.get("tool") == "create_file"]
        self.assertEqual(ecritures, [])
        self.assertEqual([e for e in events if e.get("type") == "conclusion"], [])

    def test_fichier_actif_inexistant_demande_aussi_confirmation(self):
        """Créer n'est pas écraser, mais ça reste une écriture non demandée :
        la carte s'affiche quand même, avec `existant` faux."""
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_EXPLICATION))

        events = self._tour(agent, "explique", self._contexte("nouveau.py", ""))

        demandes = [e for e in events if e.get("type") == "write_request"]
        self.assertEqual(len(demandes), 1)
        self.assertFalse(demandes[0]["existant"])
        self.assertFalse((codeagent.WORKSPACE / "nouveau.py").exists())

    def test_sans_fichier_actif_rien_ne_se_passe(self):
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_EXPLICATION))
        events = self._tour(agent, "explique", "")
        self.assertEqual([e for e in events if e.get("type") == "write_request"], [])


class CheminNormalIntactTest(_WorkspaceTest):
    """Garde-fou de non-régression : le correctif ne doit rien changer au
    chemin `parse_tool_calls` → `dispatch_tool`, qui est correct."""

    def test_balise_create_file_ecrit_toujours_sans_confirmation(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        reponse = (
            "Je remplace le fichier.\n\n"
            "<tool>create_file</tool><path>calculs.py</path><content>\n"
            "def additionner(a, b):\n    return a + b\n"
            "</content>"
        )
        agent = codeagent.CodeAgent(_StubLLM(reponse))

        events = self._tour(agent, "réécris ce fichier",
                            self._contexte(nom, _FICHIER_ORIGINAL))

        self.assertIn("def additionner", self._lire(nom))
        self.assertNotIn("multiplier", self._lire(nom))
        self.assertEqual([e for e in events if e.get("type") == "write_request"], [])

    def test_execute_code_reste_derriere_confirmation(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        agent = codeagent.CodeAgent(
            _StubLLM("<tool>execute_code</tool><path>calculs.py</path>")
        )

        events = self._tour(agent, "lance-le", self._contexte(nom, _FICHIER_ORIGINAL))

        self.assertEqual(
            [e["type"] for e in events if e.get("type") == "execute_request"],
            ["execute_request"],
        )


class EcritureConfirmeeTest(_WorkspaceTest):
    """La confirmation applique l'écriture ET sauvegarde la version d'avant.

    La sauvegarde vit HORS du workspace (`resolve_data_dir()/code_backups/`) :
    dans le workspace elle apparaîtrait dans l'arborescence rendue au frontend
    (`get_tree` ne filtre rien) et serait effaçable par le modèle lui-même via
    `_safe_path`.
    """

    def test_ecriture_confirmee_sauvegarde_la_version_precedente(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)

        res = codeagent.appliquer_ecriture(nom, "print('remplacé')\n")

        self.assertEqual(res["status"], "success")
        self.assertEqual(self._lire(nom), "print('remplacé')\n")
        sauvegarde = Path(res["sauvegarde"])
        self.assertTrue(sauvegarde.exists(), "aucune sauvegarde écrite")
        self.assertEqual(sauvegarde.read_text(encoding="utf-8"), _FICHIER_ORIGINAL)
        self.assertFalse(
            sauvegarde.is_relative_to(codeagent.WORKSPACE),
            "la sauvegarde ne doit pas vivre dans le workspace",
        )

    def test_creation_confirmee_sans_sauvegarde(self):
        res = codeagent.appliquer_ecriture("neuf.py", "print(1)\n")
        self.assertEqual(res["status"], "success")
        self.assertIsNone(res["sauvegarde"])
        self.assertEqual(self._lire("neuf.py"), "print(1)\n")

    def test_sauvegardes_successives_ne_s_ecrasent_pas(self):
        nom = self._ecrire("calculs.py", "v1\n")
        s1 = Path(codeagent.appliquer_ecriture(nom, "v2\n")["sauvegarde"])
        s2 = Path(codeagent.appliquer_ecriture(nom, "v3\n")["sauvegarde"])
        self.assertNotEqual(s1, s2)
        self.assertEqual(s1.read_text(encoding="utf-8"), "v1\n")
        self.assertEqual(s2.read_text(encoding="utf-8"), "v2\n")

    def test_sortie_du_workspace_refusee(self):
        res = codeagent.appliquer_ecriture("../evade.py", "x")
        self.assertEqual(res["status"], "error")
        self.assertFalse((codeagent.WORKSPACE.parent / "evade.py").exists())


class _SauvegardesTest(_WorkspaceTest):
    """Fixture commune aux deux classes ci-dessous — aucun test ici.

    `_dossier_sauvegardes` est détourné vers un temporaire PROPRE À CHAQUE
    TEST : les sauvegardes s'accumulent sinon dans le `EPURE_DATA_DIR` partagé
    par toute la session (`_test_env`), et compter « combien de .bak » y
    deviendrait dépendant de l'ordre d'exécution.
    """

    def setUp(self):
        super().setUp()
        self._tmp_bak = tempfile.TemporaryDirectory()
        self._bak = Path(self._tmp_bak.name).resolve()
        self._orig_dossier = codeagent._dossier_sauvegardes
        codeagent._dossier_sauvegardes = lambda: self._bak
        # Compteur d'échecs : état de MODULE, donc il traverserait les tests.
        codeagent._echecs_sauvegarde.clear()

    def tearDown(self):
        codeagent._dossier_sauvegardes = self._orig_dossier
        codeagent._echecs_sauvegarde.clear()
        self._tmp_bak.cleanup()
        super().tearDown()

    def _sauvegardes(self, nom: str, origine: str = "*") -> list:
        """Sauvegardes de `nom`, triées — l'horodatage `%Y%m%d-%H%M%S-%f` étant
        de largeur fixe, l'ordre lexicographique EST l'ordre chronologique.
        `origine` par défaut à `*` : toutes origines confondues."""
        return sorted(self._bak.rglob(f"{Path(nom).name}.*.{origine}.bak"))

    def _bloquer_les_sauvegardes(self) -> None:
        """Rend le dossier de sauvegarde impossible à créer, en posant un
        FICHIER régulier là où `sauvegarder_version` veut une arborescence.

        Le blocage porte sur le DOSSIER, pas sur `sauvegarder_version` qu'on
        aurait pu bouchonner : le test reste alors vrai quelle que soit la
        façon dont l'écriture appelle la sauvegarde.
        """
        bloqueur = self._bak / "bloqueur"
        bloqueur.write_text("je ne suis pas un dossier", encoding="utf-8")
        codeagent._dossier_sauvegardes = lambda: bloqueur / "backups"

    def _debloquer_les_sauvegardes(self) -> None:
        codeagent._dossier_sauvegardes = lambda: self._bak


class SauvegardeSystematiqueTest(_SauvegardesTest):
    """Le filet est dans `create_file`, donc sous TOUS les chemins d'écriture.

    Avant, seul `appliquer_ecriture` sauvegardait : le chemin confirmé était
    protégé, et le tool `create_file` du modèle — celui qui écrit SANS
    confirmation, via `dispatch_tool` — écrasait sans laisser de trace, tout
    comme `generate_tests` et le `POST /code/file` de l'éditeur. La sauvegarde
    ayant migré dans `create_file`, ces tests s'écrivent au niveau de la
    fonction : ils valent pour les appelants d'aujourd'hui comme pour ceux de
    demain, qui n'ont rien à savoir du filet.
    """

    def test_create_file_sur_un_fichier_existant_sauvegarde_la_version_d_avant(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)

        codeagent.create_file(nom, "print('remplacé')\n",
                              origine=codeagent.ORIGINE_MODELE)

        copies = self._sauvegardes(nom)
        self.assertEqual(len(copies), 1, "aucune sauvegarde avant écrasement")
        self.assertEqual(copies[0].read_text(encoding="utf-8"), _FICHIER_ORIGINAL)
        self.assertEqual(self._lire(nom), "print('remplacé')\n")

    def test_create_file_dans_un_sous_dossier_sauvegarde_aussi(self):
        """L'arborescence relative est conservée dans les sauvegardes — deux
        `main.py` de dossiers différents ne doivent pas se recouvrir."""
        nom = self._ecrire("src/main.py", "v1\n")

        codeagent.create_file(nom, "v2\n", origine=codeagent.ORIGINE_MODELE)

        copies = self._sauvegardes(nom)
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].parent, self._bak / "src")
        self.assertEqual(copies[0].read_text(encoding="utf-8"), "v1\n")

    def test_create_file_sur_un_fichier_absent_ne_sauvegarde_rien(self):
        """Créer n'est pas écraser : rien à sauvegarder, et l'écriture est
        normale."""
        codeagent.create_file("neuf.py", "print(1)\n",
                              origine=codeagent.ORIGINE_MODELE)

        self.assertEqual(self._sauvegardes("neuf.py"), [])
        self.assertEqual(self._lire("neuf.py"), "print(1)\n")
        self.assertFalse(self._bak.exists() and any(self._bak.rglob("*.bak")))

    def test_edit_file_sauvegarde_aussi_sans_changer_son_message(self):
        """`edit_file` écrit lui aussi dans le workspace, par un chemin qui ne
        passe pas par `create_file` — il était donc resté hors du filet. Une
        édition partielle ratée fait perdre le texte remplacé, exactement comme
        un écrasement complet. Le message rendu reste « modifié », pas
        « créé » : c'est ce que l'utilisateur lit dans le fil."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)

        res = codeagent.edit_file(nom, "a + b", "a - b")

        self.assertIn("modifié", res)
        copies = self._sauvegardes(nom, codeagent.ORIGINE_MODELE)
        self.assertEqual(len(copies), 1, "édition partielle sans sauvegarde")
        self.assertEqual(copies[0].read_text(encoding="utf-8"), _FICHIER_ORIGINAL)
        self.assertIn("a - b", self._lire(nom))

    def test_sauvegarde_impossible_annule_l_ecriture(self):
        """Fail-closed : sans copie de secours, on ne touche pas au fichier."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        self._bloquer_les_sauvegardes()

        # `assertLogs` sert deux fois : l'échec doit laisser une trace (sinon
        # l'écriture refusée serait inexplicable), et il évite de déverser la
        # pile d'appels dans la sortie de la suite.
        with self.assertLogs(codeagent.logger, level="ERROR"):
            with self.assertRaises(codeagent.SauvegardeError):
                codeagent.create_file(nom, "print('remplacé')\n",
                                      origine=codeagent.ORIGINE_MODELE)

        self.assertEqual(
            self._lire(nom), _FICHIER_ORIGINAL,
            "le fichier a été écrasé alors que la sauvegarde avait échoué",
        )

    def test_l_agent_remonte_l_echec_au_lieu_de_planter(self):
        """`dispatch_tool` transforme la `SauvegardeError` en résultat d'outil
        en erreur — le tour continue, l'utilisateur voit pourquoi."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        self._bloquer_les_sauvegardes()

        with self.assertLogs(codeagent.logger, level="ERROR"):
            res = codeagent.dispatch_tool(
                {"tool": "create_file", "path": nom, "content": "x"})

        self.assertEqual(res["status"], "error")
        self.assertIn("sauvegarder", res["result"])
        self.assertEqual(self._lire(nom), _FICHIER_ORIGINAL)

    def test_l_origine_doit_etre_passee_explicitement(self):
        """`create_file` est l'entrée COMMUNE aux chemins modèle et éditeur :
        elle ne peut pas deviner lequel l'appelle, et une valeur par défaut
        serait précisément cette devinette. L'oubli échoue donc bruyamment —
        et sans rien écrire, ce qui est le bon sens de l'échec."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)

        with self.assertRaises(TypeError):
            codeagent.create_file(nom, "x")  # type: ignore[call-arg]

        self.assertEqual(self._lire(nom), _FICHIER_ORIGINAL)

    def test_une_origine_inconnue_est_refusee(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)

        with self.assertRaises(ValueError):
            codeagent.create_file(nom, "x", origine="autre")

        self.assertEqual(self._lire(nom), _FICHIER_ORIGINAL)


class BruitDeLogTest(_SauvegardesTest):
    """Un échec DURABLE ne doit pas noyer les logs sous une trace par tentative.

    Le contexte est celui de l'auto-save : le réessai à chaque pause de frappe
    est le bon comportement — un auto-save qui abandonne laisserait le contenu
    dans le seul éditeur — mais avec une cause persistante (dossier de
    sauvegarde inaccessible), c'est une trace complète toutes les 1,5 s pour
    UNE seule cause racine. La trace utile est la PREMIÈRE ; les suivantes
    répètent la même pile.

    « Identique » = **chemin + type de la cause**, jamais le message complet.
    Le message porte le chemin de la copie visée, qui contient un horodatage à
    la microseconde : deux tentatives n'ont donc JAMAIS le même message, et une
    déduplication sur le message ne dédupliquerait rien du tout.
    """

    def _echecs(self, cm) -> list:
        """Ne garde que les enregistrements de l'échec de sauvegarde : la purge
        et d'autres chemins loguent aussi en WARNING."""
        return [r for r in cm.records if "sauvegarde impossible" in r.getMessage()]

    def test_trois_echecs_de_suite_ne_donnent_qu_une_trace(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        self._bloquer_les_sauvegardes()

        with self.assertLogs(codeagent.logger, level="DEBUG") as cm:
            for _ in range(3):
                with self.assertRaises(codeagent.SauvegardeError):
                    codeagent.create_file(nom, "x", origine=codeagent.ORIGINE_EDITEUR)

        echecs = self._echecs(cm)
        self.assertEqual(len(echecs), 3, "chaque tentative doit rester visible")
        niveaux = [r.levelname for r in echecs]
        self.assertEqual(niveaux, ["ERROR", "WARNING", "WARNING"])
        self.assertTrue(echecs[0].exc_info, "la première doit porter la pile")
        self.assertFalse(echecs[1].exc_info, "les suivantes répètent la même pile")
        # Le compteur est dans le message : « ça échoue ENCORE », lisible sans
        # remonter le fichier de log.
        self.assertIn("3", echecs[2].getMessage())

    def test_un_succes_remet_le_compteur_a_zero(self):
        """Une cause transitoire ne doit pas faire taire la trace de la
        suivante : après un enregistrement réussi, le prochain échec est de
        nouveau une première occurrence."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)

        with self.assertLogs(codeagent.logger, level="DEBUG") as cm:
            self._bloquer_les_sauvegardes()
            with self.assertRaises(codeagent.SauvegardeError):
                codeagent.create_file(nom, "a", origine=codeagent.ORIGINE_EDITEUR)
            self._debloquer_les_sauvegardes()
            codeagent.create_file(nom, "b", origine=codeagent.ORIGINE_EDITEUR)
            self._bloquer_les_sauvegardes()
            with self.assertRaises(codeagent.SauvegardeError):
                codeagent.create_file(nom, "c", origine=codeagent.ORIGINE_EDITEUR)

        self.assertEqual([r.levelname for r in self._echecs(cm)], ["ERROR", "ERROR"])
        self.assertEqual(self._lire(nom), "b", "l'écriture du milieu a bien eu lieu")

    def test_la_pile_est_re_emise_a_intervalles_croissants(self):
        """Dédupliquer sans jamais ré-échantillonner cache une cause NEUVE.

        La clé est (chemin, type) : une seconde cause racine du MÊME type sur
        le même chemin n'aurait plus jamais de pile — et `PermissionError`
        couvre aussi bien un dossier bloqué qu'un fichier verrouillé par un
        autre process. On ré-émet donc la pile aux puissances de dix.
        """
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        self._bloquer_les_sauvegardes()

        with self.assertLogs(codeagent.logger, level="DEBUG") as cm:
            for _ in range(105):
                with self.assertRaises(codeagent.SauvegardeError):
                    codeagent.create_file(nom, "x", origine=codeagent.ORIGINE_EDITEUR)

        echecs = self._echecs(cm)
        self.assertEqual(len(echecs), 105)
        rangs = [i + 1 for i, r in enumerate(echecs) if r.exc_info]
        self.assertEqual(rangs, [1, 10, 100],
                         "les piles ne tombent pas aux rangs annoncés")

    def test_le_warning_annonce_le_rang_de_la_prochaine_pile(self):
        """Sans ça, l'opérateur ne sait pas s'il doit attendre la prochaine
        pile ou provoquer lui-même la reproduction."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        self._bloquer_les_sauvegardes()

        with self.assertLogs(codeagent.logger, level="DEBUG") as cm:
            for _ in range(12):
                with self.assertRaises(codeagent.SauvegardeError):
                    codeagent.create_file(nom, "x", origine=codeagent.ORIGINE_EDITEUR)

        echecs = self._echecs(cm)
        self.assertIn("10", echecs[1].getMessage(),
                      "la 2e tentative doit annoncer la pile de la 10e")
        self.assertIn("100", echecs[10].getMessage(),
                      "après la pile de la 10e, la suivante est la 100e")

    def test_le_compteur_est_par_chemin(self):
        """Deux fichiers qui échouent sont deux causes à diagnostiquer : le
        second ne doit pas être masqué par le premier."""
        a = self._ecrire("a.py", "v0\n")
        b = self._ecrire("b.py", "v0\n")
        self._bloquer_les_sauvegardes()

        with self.assertLogs(codeagent.logger, level="DEBUG") as cm:
            for nom in (a, b, a, b):
                with self.assertRaises(codeagent.SauvegardeError):
                    codeagent.create_file(nom, "x", origine=codeagent.ORIGINE_EDITEUR)

        self.assertEqual([r.levelname for r in self._echecs(cm)],
                         ["ERROR", "ERROR", "WARNING", "WARNING"])


class RetentionSauvegardesTest(_SauvegardesTest):
    """Rétention : garder la sauvegarde qui COMPTE trouvable.

    Le coût des instantanés d'auto-sauvegarde n'est pas le disque (du texte,
    négligeable) mais la **findabilité** : la copie d'avant un écrasement par le
    modèle — la seule raison d'être du dispositif — se noierait sous une copie
    par pause de frappe. D'où deux rétentions distinctes, et une origine
    étiquetée dans le nom du fichier pour les séparer.

    **Les sauvegardes d'origine MODÈLE ne sont jamais purgées.** L'invariant
    demandé (« une purge ne supprime jamais une sauvegarde modèle tant qu'il
    reste une sauvegarde éditeur ») est donc vrai par CONSTRUCTION, pas défendu
    par la logique de purge : celle-ci ne regarde que les fichiers portant
    l'origine éditeur. Un futur lecteur qui voudrait plafonner aussi le côté
    modèle doit savoir que c'est cette structure-là qu'il retire.
    """

    def _saves_editeur(self, nom: str, n: int, depart: int = 0) -> None:
        for i in range(depart, depart + n):
            codeagent.create_file(nom, f"editeur {i}\n",
                                  origine=codeagent.ORIGINE_EDITEUR)

    def test_l_origine_est_inscrite_dans_le_nom_de_la_sauvegarde(self):
        """Étiquette dans le NOM, pas dans un fichier annexe : rien à tenir
        synchronisé, et l'origine reste lisible à l'œil dans le dossier."""
        nom = self._ecrire("calculs.py", "v1\n")

        codeagent.create_file(nom, "v2\n", origine=codeagent.ORIGINE_MODELE)
        codeagent.create_file(nom, "v3\n", origine=codeagent.ORIGINE_EDITEUR)

        self.assertEqual(len(self._sauvegardes(nom, codeagent.ORIGINE_MODELE)), 1)
        self.assertEqual(len(self._sauvegardes(nom, codeagent.ORIGINE_EDITEUR)), 1)

    def test_les_sauvegardes_editeur_sont_plafonnees(self):
        nom = self._ecrire("calculs.py", "v0\n")

        self._saves_editeur(nom, codeagent.RETENTION_EDITEUR + 5)

        copies = self._sauvegardes(nom, codeagent.ORIGINE_EDITEUR)
        self.assertEqual(len(copies), codeagent.RETENTION_EDITEUR)

    def test_la_purge_garde_les_plus_RECENTES(self):
        """Un plafond qui garderait les plus anciennes serait pire que pas de
        plafond : on veut revenir en arrière de quelques pas, pas au début."""
        nom = self._ecrire("calculs.py", "v0\n")

        self._saves_editeur(nom, codeagent.RETENTION_EDITEUR + 3)

        contenus = [c.read_text(encoding="utf-8")
                    for c in self._sauvegardes(nom, codeagent.ORIGINE_EDITEUR)]
        # La i-ème sauvegarde contient l'état d'AVANT la i-ème écriture.
        attendu = [f"editeur {i}\n"
                   for i in range(2, codeagent.RETENTION_EDITEUR + 2)]
        self.assertEqual(contenus, attendu)

    def test_aucune_purge_ne_touche_une_sauvegarde_du_MODELE(self):
        """L'invariant du dispositif. Le modèle écrit trois fois, noyé sous
        beaucoup d'enregistrements d'éditeur : ses trois copies restent."""
        nom = self._ecrire("calculs.py", "v0\n")
        attendues = []
        for tour in range(3):
            self._saves_editeur(nom, codeagent.RETENTION_EDITEUR, depart=tour * 100)
            codeagent.create_file(nom, f"modele {tour}\n",
                                  origine=codeagent.ORIGINE_MODELE)
            attendues.append(self._sauvegardes(nom, codeagent.ORIGINE_MODELE)[-1])
        self._saves_editeur(nom, codeagent.RETENTION_EDITEUR * 2, depart=900)

        survivantes = self._sauvegardes(nom, codeagent.ORIGINE_MODELE)
        self.assertEqual(survivantes, attendues,
                         "une purge a emporté une sauvegarde d'origine modèle")
        self.assertEqual(len(self._sauvegardes(nom, codeagent.ORIGINE_EDITEUR)),
                         codeagent.RETENTION_EDITEUR)

    def test_le_plafond_est_par_fichier(self):
        """Deux fichiers édités en alternance ne se volent pas leurs pas de
        recul."""
        a = self._ecrire("a.py", "v0\n")
        b = self._ecrire("b.py", "v0\n")

        self._saves_editeur(a, codeagent.RETENTION_EDITEUR + 5)
        self._saves_editeur(b, 3)

        self.assertEqual(len(self._sauvegardes(a, codeagent.ORIGINE_EDITEUR)),
                         codeagent.RETENTION_EDITEUR)
        self.assertEqual(len(self._sauvegardes(b, codeagent.ORIGINE_EDITEUR)), 3)

    def test_les_sauvegardes_sans_origine_survivent(self):
        """Celles d'avant ce changement (`<nom>.<horodatage>.bak`, sans
        segment d'origine) sont déjà sur le disque d'Ilyann. Elles ne matchent
        aucun motif de purge : conservées, délibérément — on ne sait pas d'où
        elles viennent, donc on les traite comme les plus précieuses."""
        nom = self._ecrire("calculs.py", "v0\n")
        ancienne = self._bak / "calculs.py.20260101-000000-000000.bak"
        ancienne.parent.mkdir(parents=True, exist_ok=True)
        ancienne.write_text("version d'avant l'étiquetage\n", encoding="utf-8")

        self._saves_editeur(nom, codeagent.RETENTION_EDITEUR + 5)

        self.assertTrue(ancienne.exists())
        self.assertEqual(ancienne.read_text(encoding="utf-8"),
                         "version d'avant l'étiquetage\n")

    def test_une_purge_impossible_ne_fait_pas_echouer_l_ecriture(self):
        """La purge est du ménage, pas une condition de l'écriture : à
        l'inverse exact de la sauvegarde, qui est fail-closed. Si une copie ne
        peut pas être supprimée, le fichier est quand même enregistré."""
        nom = self._ecrire("calculs.py", "v0\n")
        self._saves_editeur(nom, codeagent.RETENTION_EDITEUR + 1)
        indesirable = self._sauvegardes(nom, codeagent.ORIGINE_EDITEUR)[0]

        original_unlink = Path.unlink

        def _unlink_qui_echoue(chemin, *a, **kw):
            if chemin == indesirable:
                raise PermissionError("fichier verrouillé")
            return original_unlink(chemin, *a, **kw)

        with mock.patch.object(Path, "unlink", _unlink_qui_echoue):
            with self.assertLogs(codeagent.logger, level="WARNING"):
                codeagent.create_file(nom, "malgre tout\n",
                                      origine=codeagent.ORIGINE_EDITEUR)

        self.assertEqual(self._lire(nom), "malgre tout\n")
        self.assertTrue(indesirable.exists())


# Réponse d'un modèle qui annonce son outil en markdown au lieu de la syntaxe
# XML : il demande une édition PARTIELLE (`edit_file`) mais ne fournit qu'un
# fragment, sans `old`/`new`. L'édition demandée est donc inexécutable.
_REPONSE_MD_EDIT = '''\
Le garde-fou de division est inutile ici, je le retire :

**edit_file** `calculs.py`

```python
def diviser(a, b):
    return a / b
```
'''

# Même syntaxe markdown, mais le modèle annonce un FICHIER COMPLET : là, la
# conversion en écriture directe dit bien ce que le modèle voulait.
_REPONSE_MD_CREATE = '''\
Voici le nouveau module :

**create_file** `outils.py`

```python
def carre(x):
    return x * x
```
'''


class MarkdownEditFileTest(_WorkspaceTest):
    """`**edit_file**` en markdown ne se convertit plus en écrasement complet.

    La conversion faisait perdre l'intention : le modèle demandait « remplace
    ces lignes », on exécutait « remplace tout le fichier par ces lignes », sans
    confirmation. Le markdown ne portant ni `old` ni `new`, la vraie édition est
    impossible — le seul choix honnête est de proposer et de laisser trancher.
    """

    def test_edit_file_markdown_n_ecrit_pas_et_demande_confirmation(self):
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_MD_EDIT))

        events = self._tour(agent, "retire le garde-fou",
                            self._contexte(nom, _FICHIER_ORIGINAL))

        self.assertEqual(
            self._lire(nom), _FICHIER_ORIGINAL,
            "le fichier a été remplacé par le fragment d'une édition partielle",
        )
        demandes = [e for e in events if e.get("type") == "write_request"]
        self.assertEqual(len(demandes), 1)
        self.assertEqual(demandes[0]["path"], nom)
        self.assertIn("def diviser", demandes[0]["content"])
        self.assertTrue(demandes[0]["existant"])

    def test_aucun_outil_n_est_annonce_pour_une_proposition(self):
        """Rien n'a été exécuté : ni `tool_call`, ni `tool_result`, ni la
        conclusion « j'ai modifié… » qui en découle."""
        nom = self._ecrire("calculs.py", _FICHIER_ORIGINAL)
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_MD_EDIT))

        events = self._tour(agent, "retire le garde-fou",
                            self._contexte(nom, _FICHIER_ORIGINAL))

        types = {e.get("type") for e in events}
        self.assertNotIn("tool_call", types)
        self.assertNotIn("tool_result", types)
        self.assertNotIn("conclusion", types)

    def test_le_pseudo_outil_ne_fuit_pas_vers_le_dispatch(self):
        """Garde-fou de conception : `dispatch_tool` ne connaît pas le
        pseudo-outil et le refuse — un appelant futur qui oublierait de
        l'intercepter n'écrirait toujours rien."""
        calls = codeagent.parse_tool_calls(_REPONSE_MD_EDIT)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(calls[0]["tool"], ("create_file", "edit_file"))
        res = codeagent.dispatch_tool(calls[0])
        self.assertEqual(res["status"], "error")

    def test_un_chemin_hors_workspace_est_propose_puis_refuse_au_clic(self):
        """Choix délibéré, à ne pas « corriger » en abandon silencieux : une
        proposition sur un chemin hors workspace est quand même affichée, et
        c'est `appliquer_ecriture` qui refuse au moment du clic — avec un
        message. Faire disparaître la carte laisserait l'utilisateur devant une
        réponse du modèle sans aucune explication.
        """
        reponse = _REPONSE_MD_EDIT.replace("`calculs.py`", "`../evade.py`")
        agent = codeagent.CodeAgent(_StubLLM(reponse))

        events = self._tour(agent, "modifie", "")

        demandes = [e for e in events if e.get("type") == "write_request"]
        self.assertEqual(len(demandes), 1)
        self.assertEqual(demandes[0]["path"], "../evade.py")
        self.assertFalse(demandes[0]["existant"])
        self.assertFalse((codeagent.WORKSPACE.parent / "evade.py").exists())
        # Le refus n'arrive qu'ici, à la confirmation.
        self.assertEqual(
            codeagent.appliquer_ecriture("../evade.py", "x")["status"], "error")
        self.assertFalse((codeagent.WORKSPACE.parent / "evade.py").exists())

    def test_create_file_markdown_ecrit_toujours_directement(self):
        """Non-régression : `**create_file**` annonce un fichier complet, il
        reste un `create_file`, cohérent avec le chemin XML."""
        agent = codeagent.CodeAgent(_StubLLM(_REPONSE_MD_CREATE))

        events = self._tour(agent, "crée un module d'outils", "")

        self.assertIn("def carre", self._lire("outils.py"))
        self.assertEqual([e for e in events if e.get("type") == "write_request"], [])
        ecrits = [e for e in events
                  if e.get("type") == "tool_result" and e.get("tool") == "create_file"]
        self.assertEqual(len(ecrits), 1)
        self.assertEqual(ecrits[0]["status"], "success")


class PariteCatalogueTest(unittest.TestCase):
    """`modules-catalogue/code/` est la SOURCE du module installable (§3.3) —
    et la SEULE des deux copies qui soit versionnée : `backend/modules/*/` et
    `frontend/src/modules/generated/*/` sont gitignorés (lignes 89 et 101 du
    `.gitignore`), donc absents d'un clone de CI.

    Conséquence pour ce test, et c'est tout son intérêt : corriger l'instance
    installée sur ce poste ne laisse AUCUNE trace dans le dépôt, et le prochain
    `POST /settings/catalogue/code/install` chez un destinataire réinstallerait
    le bug. Le côté catalogue est donc vérifié inconditionnellement ; l'instance
    locale n'est comparée que si elle existe.

    **Ce que ces marqueurs valent, et ce qu'ils ne valent pas.** Ils remplacent
    un test de composant qui ne peut pas exister : le `Component.tsx` du module
    Code ne vit, côté `frontend/src/`, que dans `generated/`, gitignoré — un
    `*.test.tsx` posé là serait invisible du dépôt, et vitest ne collecte que
    `src/**/*.test.tsx`, jamais `modules-catalogue/`. Un marqueur prouve donc
    UNIQUEMENT que la copie versionnée contient encore le mot ; il ne prouve
    ni qu'un `beforeunload` se déclenche, ni qu'un compteur de tentatives
    s'incrémente, ni qu'un « il y a 12 s » avance à l'écran. Ces trois-là ne
    sont pas couverts, et il ne faut pas lire l'inverse dans un test vert :
    c'est un garde-fou anti-régression-par-copie, pas une vérification de
    comportement.
    """

    CIBLES = [
        ("modules-catalogue/code/router.py",
         "backend/modules/code/router.py",
         # confirmation d'écriture ; origine de sauvegarde côté éditeur ;
         # le 409 qui porte le message de SauvegardeError jusqu'à l'interface.
         ("write_confirm", "_ORIGINE_EDITEUR", "409")),
        ("modules-catalogue/code/Component.tsx",
         "frontend/src/modules/generated/code/Component.tsx",
         # carte de confirmation ; contrôle de statut HTTP (apiFetch ne lève
         # pas) ; message d'erreur affiché au lieu d'un faux succès ; fraîcheur
         # du bandeau (horodatage + tentatives consécutives) ; garde de
         # fermeture tant qu'un onglet est sale.
         ("write_request", "res.ok", "signalerEchec", "tentatives", "dernier",
          "beforeunload", "persistanceOnglets")),
    ]

    def test_le_catalogue_porte_les_controles_d_ecriture(self):
        for source, _installe, marqueurs in self.CIBLES:
            texte = (_REPO / source).read_text(encoding="utf-8")
            for marqueur in marqueurs:
                with self.subTest(fichier=source, marqueur=marqueur):
                    self.assertIn(marqueur, texte,
                                  "un contrôle d'écriture manque dans le catalogue — "
                                  "seule copie versionnée, donc la seule qui parte "
                                  "chez un destinataire")

    def test_l_instance_installee_ne_diverge_pas(self):
        for source, installe, _marqueurs in self.CIBLES:
            chemin = _REPO / installe
            if not chemin.is_file():
                continue  # module non installé sur ce poste (cas de la CI)
            with self.subTest(fichier=installe):
                self.assertEqual(
                    chemin.read_text(encoding="utf-8"),
                    (_REPO / source).read_text(encoding="utf-8"),
                    f"{installe} a divergé de {source}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
