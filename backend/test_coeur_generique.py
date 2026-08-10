"""Le cœur d'Épure ne doit présumer aucune filière ni aucune matière.

Principe : ce qui spécialise une instance, ce sont ses **modules** et sa
configuration. Le cœur — `backend/core/`, `backend/main.py`, les modules du
cœur, et `frontend/src/` hors `generated/` — reste générique.

Ce test existe parce que l'inverse s'était installé sans que rien ne le signale :
le profil élève naissait en « PTSI2 » (`core/memory.py`), la config surveillait
`Maths / Physique-Chimie / SI` (`core/instance.py`), le tri de PDF n'acceptait
que ces trois matières (`core/admin.py`), et trois prompts de `core/` parlaient
d'un « étudiant en prépa ». Quiconque installait Épure héritait de la filière de
son auteur.

Aucun import de `core.*` : le test lit des fichiers, il n'exécute pas
l'application. Rien n'est écrit, donc rien n'atteint `backend/memory/`.
"""

import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
REPO = BACKEND.parent

#: Marqueurs de filière ou de matière. Choisis pour ne pas produire de faux
#: positifs : « prepa » seul attraperait `prepare()` et « Préparation du
#: staging », qui n'ont rien à voir.
_TERMES = [
    "PTSI",
    "MPSI",
    "PCSI",
    "BCPST",
    "Physique-Chimie",
    "classe préparatoire",
    "prépa scientifique",
    "étudiant en prépa",
]

#: Occurrences tolérées : des explications historiques, pas du comportement.
#: Une docstring qui dit « ceci ÉTAIT figé à Maths/Physique-Chimie/SI » est
#: précisément ce que la convention du dépôt demande d'écrire (CLAUDE.md §9).
#: Toute occurrence hors de cette liste fait échouer le test.
_TOLERE = {
    ("backend/core/admin.py", "Physique-Chimie"),
    ("backend/core/admin.py", "prépa scientifique"),
    ("backend/core/memory.py", "PTSI"),
}


def _fichiers_du_coeur() -> list[Path]:
    """Le périmètre exact du « cœur » : ce qui est livré à tout le monde."""
    fichiers: list[Path] = [BACKEND / "main.py"]
    fichiers += sorted((BACKEND / "core").glob("*.py"))
    for module in sorted((BACKEND / "modules").iterdir()):
        if module.is_dir() and not module.name.startswith("_"):
            fichiers += sorted(module.glob("*.py"))
            fichiers += sorted(module.glob("*.json"))
    src = REPO / "frontend" / "src"
    for motif in ("**/*.ts", "**/*.tsx"):
        fichiers += [
            f for f in sorted(src.glob(motif))
            # `generated/` reçoit les modules installés : c'est le lieu du
            # spécifique, et il est ignoré par git.
            if "generated" not in f.relative_to(src).parts
        ]
    return fichiers


class TestCoeurGenerique(unittest.TestCase):

    def test_aucune_filiere_ni_matiere_dans_le_coeur(self):
        fautes: list[str] = []
        for fichier in _fichiers_du_coeur():
            try:
                lignes = fichier.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            rel = fichier.relative_to(REPO).as_posix()
            for terme in _TERMES:
                if (rel, terme) in _TOLERE:
                    continue
                for numero, ligne in enumerate(lignes, 1):
                    if terme.lower() in ligne.lower():
                        fautes.append(f"{rel}:{numero} — « {terme} » : {ligne.strip()[:90]}")
        self.assertEqual(
            fautes, [],
            "Le cœur d'Épure présume une filière ou une matière :\n  "
            + "\n  ".join(fautes)
            + "\n\nCe qui spécialise une instance, ce sont ses modules et sa "
              "configuration. Si l'occurrence est une explication historique, "
              "ajoute-la à _TOLERE avec sa justification.",
        )

    def test_le_perimetre_scanne_est_reel(self):
        """Un garde-fou qui ne lit rien passerait toujours.

        Le test précédent est vert par vacuité si la collecte de fichiers casse
        — un renommage de dossier suffirait. On vérifie donc qu'elle rend bien
        les deux moitiés du cœur, et un volume plausible.
        """
        fichiers = _fichiers_du_coeur()
        rels = {f.relative_to(REPO).as_posix() for f in fichiers}
        self.assertIn("backend/core/instance.py", rels)
        self.assertIn("backend/core/admin.py", rels)
        self.assertIn("backend/main.py", rels)
        self.assertTrue(
            any(r.startswith("frontend/src/") for r in rels),
            "aucun fichier frontend scanné — le périmètre a glissé",
        )
        self.assertGreater(len(fichiers), 40, f"seulement {len(fichiers)} fichiers scannés")

    def test_les_tolerances_sont_toutes_utiles(self):
        """Une tolérance qui ne correspond plus à rien doit être retirée.

        Sans ça la liste enfle et finit par autoriser des choses qui n'existent
        plus — un cliquet qui ne descend jamais.
        """
        vivantes = set()
        for fichier in _fichiers_du_coeur():
            try:
                contenu = fichier.read_text(encoding="utf-8").lower()
            except (OSError, UnicodeDecodeError):
                continue
            rel = fichier.relative_to(REPO).as_posix()
            for rel_tol, terme in _TOLERE:
                if rel == rel_tol and terme.lower() in contenu:
                    vivantes.add((rel_tol, terme))
        self.assertEqual(
            _TOLERE - vivantes, set(),
            "tolérances devenues inutiles, à supprimer de _TOLERE",
        )

    def test_les_defauts_ne_sont_pas_prepa(self):
        """Les deux valeurs par défaut qui servaient la filière de l'auteur.

        Contrôle du contenu, pas seulement du vocabulaire : `_TERMES` ne
        verrait pas un `watch_folders` repeuplé de « Maths » et « SI », qui ne
        sont pas des marqueurs de filière pris isolément.
        """
        instance = (BACKEND / "core" / "instance.py").read_text(encoding="utf-8")
        self.assertIn('"watch_folders": [],', instance,
                      "watch_folders par défaut doit rester vide")
        memory = (BACKEND / "core" / "memory.py").read_text(encoding="utf-8")
        self.assertIn('"niveau": ""', memory,
                      "le niveau par défaut doit rester vide")


if __name__ == "__main__":
    unittest.main()
