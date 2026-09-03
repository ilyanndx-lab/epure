"""Hook de sauvegarde des figures matplotlib — importé AUTOMATIQUEMENT par
l'interpréteur Python au démarrage (mécanisme standard du module `site` de la
stdlib : tout `sitecustomize.py` trouvable sur `sys.path` est importé une
fois, sans rien à demander explicitement).

Ce dossier est posé en TÊTE de `PYTHONPATH` par `core.codeagent.execute_code`
pour CHAQUE script `.py` exécuté par le module Code (à l'exclusion des
vraies libs GUI interactives, routées vers `_launch_gui` — tkinter, pygame,
etc.) — jamais autrement, et JAMAIS dans `WORKSPACE` : ce fichier ne doit
apparaître ni dans l'arbre de fichiers de l'utilisateur, ni être éditable
depuis l'UI du module Code.

Objectif : quand un script utilise `matplotlib.pyplot` et laisse des figures
ouvertes à la fin (`plt.plot(...)` sans `plt.savefig()` explicite — le cas le
plus courant), les sauvegarder en PNG dans le répertoire désigné par
`EPURE_PLOT_OUTPUT_DIR`, pour un rendu INLINE côté frontend au lieu d'une
fenêtre externe. `MPLBACKEND=Agg`, posé par le même appelant, garantit déjà
qu'aucune fenêtre n'est tentée — `plt.show()` sous ce backend ne bloque pas
et ne lève pas.

IMPÉRATIF — n'importe JAMAIS matplotlib lui-même. Ce fichier est importé à
CHAQUE script `.py` du module Code, y compris ceux qui n'ont rien à voir avec
des graphiques : forcer l'import de matplotlib ici ajouterait son coût de
démarrage à tous, pour un bénéfice nul dans l'immense majorité des cas. Tout
ce qui suit ne fait que VÉRIFIER `sys.modules` — zéro effet de bord si le
script utilisateur n'importe jamais matplotlib.

── Piège mesuré : l'ORDRE d'enregistrement `atexit` ─────────────────────────

Un simple `atexit.register(_sauver_figures_matplotlib)` posé ICI, au chargement
de ce fichier (donc AVANT que le script utilisateur importe quoi que ce soit),
ne suffit PAS — mesuré en écrivant cette phase : `plt.get_fignums()` valait
`[]` dans le hook alors que le script venait de tracer une figure au premier
plan.

Cause : `matplotlib.pyplot`, à SON import (par le script utilisateur, donc
APRÈS ce fichier), enregistre lui-même un hook `atexit`
(`matplotlib._pylab_helpers.Gcf.destroy_all`, qui détruit toutes les figures
ouvertes). `atexit` exécute ses callbacks en LIFO — le DERNIER enregistré part
EN PREMIER. Un `atexit.register` posé ici, avant l'import de pyplot par le
script, s'exécuterait donc TOUJOURS après `Gcf.destroy_all` : les figures
seraient déjà détruites quand notre hook s'exécute.

Correctif : on n'enregistre notre hook qu'APRÈS que `matplotlib.pyplot` a fini
de s'importer (donc après que `Gcf.destroy_all` soit lui-même enregistré) — un
enregistrement tardif passe alors AVANT lui en LIFO. On ne peut pas se
contenter d'observer `sys.modules` une seule fois (l'import peut survenir à
n'importe quel moment du script) : `builtins.__import__` est donc enveloppé
pour détecter la FIN du tout premier import touchant `matplotlib`, quelle que
soit sa forme (`import matplotlib.pyplot`, `from matplotlib import pyplot`,
un import indirect par un paquet tiers) — et se retire lui-même dès que le
hook est posé, pour ne laisser aucune trace après coup.
"""

import atexit
import builtins
import os
import sys

_hook_enregistre = False


def _sauver_figures_matplotlib() -> None:
    """Hook `atexit` — protégé de bout en bout par un `try/except` large :
    une figure impossible à sauvegarder (backend cassé, disque plein, figure
    déjà fermée entre `get_fignums()` et `savefig()`...) ne doit JAMAIS faire
    échouer le script utilisateur ni changer son code de sortie."""
    try:
        if "matplotlib.pyplot" not in sys.modules:
            return
        out_dir = os.environ.get("EPURE_PLOT_OUTPUT_DIR", "").strip()
        if not out_dir:
            return

        plt = sys.modules["matplotlib.pyplot"]
        fignums = sorted(plt.get_fignums())
        if not fignums:
            # Aucune figure encore ouverte (plt.close() explicite, ou aucun
            # plot réellement tracé) — comportement normal, rien à faire.
            return

        os.makedirs(out_dir, exist_ok=True)
        for i, num in enumerate(fignums, start=1):
            try:
                fig = plt.figure(num)
                fig.savefig(os.path.join(out_dir, f"figure_{i}.png"))
            except Exception:
                continue
    except Exception:
        pass


def _enregistrer_si_pret() -> None:
    """Enregistre notre hook `atexit` (une seule fois) et désinstalle
    `_import_intercepte` — appelée uniquement au retour de l'appel `__import__`
    de PROFONDEUR 0 (cf. `_import_intercepte`), jamais depuis un import
    imbriqué : c'est ce qui garantit que TOUT ce que `matplotlib.pyplot`
    importe pour son propre compte (donc `Gcf.destroy_all`, cf. docstring du
    module) a déjà fini de s'enregistrer avant nous."""
    global _hook_enregistre
    if _hook_enregistre or "matplotlib.pyplot" not in sys.modules:
        return
    _hook_enregistre = True
    atexit.register(_sauver_figures_matplotlib)
    builtins.__import__ = _import_original  # plus besoin d'intercepter


_import_original = builtins.__import__
_profondeur_import = 0


def _import_intercepte(name, *args, **kwargs):
    """Enveloppe `__import__` le temps de repérer la fin du TOUT PREMIER appel
    `import matplotlib[...]` émis par le code utilisateur — pas un import
    imbriqué déclenché depuis l'intérieur de matplotlib lui-même.

    Un simple test « `matplotlib.pyplot` est dans `sys.modules` » après CHAQUE
    appel intercepté serait trop tôt : Python enregistre un module dans
    `sys.modules` AVANT d'exécuter son corps (pour les imports circulaires),
    donc `sys.modules["matplotlib.pyplot"]` existe déjà (auto-référence, en
    cours d'exécution) dès qu'un import imbriqué a lieu DEPUIS le corps de
    pyplot lui-même — avant que ce corps ait fini d'importer
    `matplotlib._pylab_helpers` et d'y enregistrer `Gcf.destroy_all`. Mesuré :
    sans ce compteur de profondeur, notre hook s'enregistrait AVANT
    `Gcf.destroy_all`, qui tournait donc en premier en LIFO et détruisait les
    figures avant nous. Ne vérifier qu'au retour de l'appel de profondeur 0
    (la ligne `import` du script utilisateur elle-même) garantit que toute la
    chaîne d'imports internes de matplotlib s'est déjà déroulée.
    """
    global _profondeur_import
    _profondeur_import += 1
    try:
        return _import_original(name, *args, **kwargs)
    finally:
        _profondeur_import -= 1
        if _profondeur_import == 0 and not _hook_enregistre and (
            name == "matplotlib" or name.startswith("matplotlib.")
        ):
            _enregistrer_si_pret()


builtins.__import__ = _import_intercepte
