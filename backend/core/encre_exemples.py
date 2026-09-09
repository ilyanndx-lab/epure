"""Magasin des exemples d'entraînement du module `encre` — phase 3.

Feuille de route dans `docs/module-encre.md` (§2, phase 3). Ce module stocke des
paires **(tracés, LaTeX vérité)** destinées à un futur fine-tuning de
`pix2text-mfr` (phase 4, conditionnelle, non construite) — c'est une collection
de données, pas un moteur d'inférence, et il n'importe donc rien de plus lourd
que la bibliothèque standard et `core.hmer.points_de_page`, elle-même sans
dépendance lourde.

**Chaque exemple est INDÉPENDANT des pages d'encre existantes.** Il porte sa
propre copie des tracés plutôt qu'une référence vers une page
(`core/encre.py`) : si la page d'origine est supprimée plus tard — l'utilisateur
fait le ménage dans ses notes, une page de brouillon n'a plus sa place —
l'exemple doit rester exploitable pour l'entraînement. Une référence par id
casserait précisément dans ce cas, et silencieusement : l'exemple resterait
listé mais son `get_page` rendrait `None`.

Deux sources, deux façons d'obtenir la paire :

* ``"correction"`` — une page en mode "maths" déjà transcrite par `pix2text-mfr`
  (`core/hmer.py`), dont l'utilisateur valide ou corrige le LaTeX. `texte_modele`
  porte alors la sortie BRUTE du modèle, conservée pour comparaison — jamais
  écrasée par la correction ;
* ``"dictee_inversee"`` — une expression tirée de `core.banque_dictee_encre`,
  affichée rendue, que l'utilisateur recopie au stylet sur un canvas vierge.
  Aucun modèle n'est appelé sur ce chemin : `texte_modele` vaut ``None``, pas une
  chaîne vide — les deux ne veulent pas dire la même chose (« aucun modèle
  consulté » contre « le modèle a lu du vide »).

Même choix de structure que `core/encre.py`, pour la même raison : **un fichier
JSON par exemple, aucun index central.** L'état « quels exemples existent » se
lit en scannant `resolve_encre_dataset_dir()`. `tools/export_dataset_encre.py`
en dépend directement — c'est un script MANUEL et occasionnel, pas un chemin
chaud, donc le coût d'ouvrir chaque fichier n'a pas la même conséquence qu'un
index de pages consulté à chaque rendu de la barre latérale.

⚠️ **Les tracés sont opaques ici aussi**, exactement pour la raison écrite en
tête de `core/encre.py` : ce moteur vérifie que `strokes` est une liste, jamais
ce qu'il y a dedans.
"""

import hashlib
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.hmer import points_de_page
from core.jsonstore import read_json, transaction, write_json
from core.paths import PathOutsideDataError, resolve_data_dir, resolve_encre_dataset_dir
from core.banque_dictee_encre import choisir_expression, expressions

logger = logging.getLogger(__name__)

#: Les deux seules sources connues. Contrairement à `EncreEngine._mode`, une
#: valeur invalide ici n'a pas de repli silencieux : `source` n'est jamais tapée
#: par un client au clavier, elle est choisie par LE SITE D'APPEL du routeur
#: (deux endpoints distincts, un par brique) — une valeur inattendue signale un
#: bug de ce côté-ci, pas une entrée utilisateur malformée à tolérer.
SOURCE_CORRECTION = "correction"
SOURCE_DICTEE_INVERSEE = "dictee_inversee"
_SOURCES_VALIDES = frozenset({SOURCE_CORRECTION, SOURCE_DICTEE_INVERSEE})

#: Nom du fichier de progression de la dictée inversée, sous `resolve_data_dir()`
#: (`memory/`) — un état applicatif reconstruit sans drame en cas de perte (au
#: pire, quelques répétitions plus tôt que prévu), donc le régime de `memory/` et
#: non celui, irremplaçable, de `resolve_encre_dataset_dir()`.
_FICHIER_PROGRES = "dictee_encre_progres.json"


def _horodatage() -> str:
    """Recopié de `core/encre.py`, pour la même raison qu'il l'est déjà de
    `core/history.py` : une convention de format du dépôt, pas une fonction à
    partager entre des moteurs par ailleurs indépendants."""
    return datetime.now().isoformat(timespec="seconds")


def _traits(strokes) -> list:
    """`strokes` ramené à une liste, sans jamais regarder à l'intérieur.

    Recopié de `EncreEngine._traits` : même contrat, même raison (cf. l'en-tête
    de `core/encre.py`), et ce fichier n'a pas besoin d'en faire une dépendance
    partagée pour trois lignes.
    """
    return strokes if isinstance(strokes, list) else []


class ExemplesEncreEngine:
    """Lecture/écriture des exemples d'entraînement, et sélection de la dictée.

    Construit tôt (`core/runtime.py`, à côté de `encre_engine`) et non derrière
    un `_LazyEngine` : comme `EncreEngine`, il n'y a rien de coûteux à
    construire — un `mkdir`, rien de plus.
    """

    def __init__(self, dossier: Optional[Path] = None, dossier_progres: Optional[Path] = None):
        self._dir = Path(dossier).resolve() if dossier else resolve_encre_dataset_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        # Résolu ici et non en défaut d'argument (CLAUDE.md §3.5) : figer
        # `resolve_data_dir()` dans une constante de module rendrait
        # `$EPURE_DATA_DIR` sans effet pour ce fichier.
        base_progres = Path(dossier_progres).resolve() if dossier_progres else resolve_data_dir()
        self._progres_path = base_progres / _FICHIER_PROGRES

    # ── Chemins ───────────────────────────────────────────────────────────────

    def _exemple_path(self, exemple_id: str) -> Path:
        """Fichier d'un exemple, confiné au dossier du dataset — ou refus explicite.

        Recopié du patron de `EncreEngine._page_path`, dont le docstring détaille
        les trois raisons (segments dégénérés, `resolve()` + comparaison de
        chemins plutôt que `startswith`, enfant DIRECT plutôt que
        `is_relative_to`). Les identifiants sont fabriqués ICI
        (`uuid4().hex`) : comme pour les pages d'encre, c'est une ceinture posée
        avant le premier incident, pas le correctif d'une faille atteinte —
        aucun endpoint n'accepte aujourd'hui un `exemple_id` venu du client.
        """
        if not exemple_id or exemple_id in (".", ".."):
            raise PathOutsideDataError(f"Identifiant d'exemple invalide : {exemple_id!r}")
        racine = self._dir.resolve()
        try:
            cible = (racine / f"{exemple_id}.json").resolve()
        except OSError as exc:
            raise PathOutsideDataError(f"Identifiant d'exemple invalide : {exemple_id!r}") from exc
        if cible.parent != racine or cible == racine:
            raise PathOutsideDataError(f"Identifiant d'exemple invalide : {exemple_id!r}")
        return cible

    # ── Exemples ──────────────────────────────────────────────────────────────

    def create_exemple(self, source: str, strokes, texte_verite: str,
                        texte_modele: Optional[str] = None) -> dict:
        """Crée un exemple et rend son contenu complet.

        `texte_verite` est écrit tel quel s'il est une chaîne, sinon vide — un
        exemple sans texte de vérité n'a aucun sens pour l'entraînement, mais ce
        n'est pas à ce moteur de le refuser : c'est au routeur de ne jamais
        appeler cette méthode dans ce cas (défense en profondeur, pas une
        validation métier dupliquée ici).

        `texte_modele` reste `None` s'il n'est pas fourni, jamais une chaîne
        vide : « aucun modèle consulté » (dictée inversée) et « le modèle a lu du
        vide » (correction sur une transcription vide) sont deux états
        différents, cf. l'en-tête du module.
        """
        if source not in _SOURCES_VALIDES:
            raise ValueError(f"Source d'exemple invalide : {source!r}")
        exemple_id = uuid.uuid4().hex
        exemple = {
            "id": exemple_id,
            "source": source,
            "date": _horodatage(),
            "strokes": _traits(strokes),
            "texte_verite": texte_verite if isinstance(texte_verite, str) else "",
            "texte_modele": texte_modele if isinstance(texte_modele, str) else None,
        }
        write_json(self._exemple_path(exemple_id), exemple)
        return exemple

    def get_exemple(self, exemple_id: str) -> Optional[dict]:
        """L'exemple complet, tracés compris — ou `None` s'il n'existe pas."""
        chemin = self._exemple_path(exemple_id)
        if not chemin.is_file():
            return None
        exemple = read_json(chemin, None)
        if not isinstance(exemple, dict):
            return None
        exemple["id"] = chemin.stem
        return exemple

    def list_exemples(self) -> list[dict]:
        """Tous les exemples, tracés compris.

        Contrairement à `EncreEngine.list_pages()`, aucune version allégée n'a de
        raison d'exister : rien dans ce lot n'affiche une liste d'exemples dans
        l'interface (ce n'est pas ce que demande la phase 3), et le seul
        consommateur, `tools/export_dataset_encre.py`, a justement besoin des
        tracés pour produire les images. Un fichier illisible est ignoré avec un
        avertissement, comme `EncreEngine.list_pages()`.
        """
        if not self._dir.is_dir():
            return []
        exemples: list[dict] = []
        for chemin in self._dir.glob("*.json"):
            if not chemin.is_file():
                continue
            exemple = read_json(chemin, None)
            if not isinstance(exemple, dict):
                logger.warning("Exemple d'entraînement illisible, ignoré : %s", chemin.name)
                continue
            exemple["id"] = chemin.stem
            exemples.append(exemple)
        exemples.sort(key=lambda e: (e.get("date") or "", e["id"]))
        return exemples

    # ── Dictée inversée ───────────────────────────────────────────────────────

    def _progres(self) -> set:
        """Identifiants déjà faits, lus depuis le disque. `{}` si le fichier n'existe pas."""
        return set(read_json(self._progres_path, {}).get("faites", []) or [])

    def expression_a_copier(self) -> dict:
        """Une expression à recopier — LECTURE PURE, aucun effet de bord.

        Le reset de la progression, quand la banque vient d'être épuisée, vit
        dans :meth:`valider_dictee` et non ici : un `GET` qui écrirait sur le
        disque serait surprenant pour l'appelant (rejouer la requête ne devrait
        rien changer d'autre que le tirage), et ferait courir une course entre
        deux `GET` rapprochés qui constateraient chacun l'épuisement et
        réinitialiseraient deux fois — inoffensif ici, mais un effet de bord
        caché dans une lecture est le genre de choix qui finit par surprendre
        ailleurs.
        """
        return choisir_expression(self._progres())

    def valider_dictee(self, expression_id: str, strokes) -> dict:
        """Crée l'exemple de dictée inversée et marque l'expression comme faite.

        `expression_id` est résolu contre la banque CÔTÉ SERVEUR — jamais un
        `texte_verite` envoyé par le client : sans ça, un appel direct à l'API
        pourrait enregistrer n'importe quel texte sous couvert de « dictée
        inversée », faussant le dataset sans qu'aucune trace ne le distingue
        d'une vraie paire recopiée.

        Lève `ValueError` sur un identifiant inconnu (banque modifiée entre
        l'affichage et la validation — cas manuel et rare, cf.
        `core.banque_dictee_encre`) ou sur des tracés vides : le routeur les
        traduit en 400, jamais en 500, ce sont des refus de requête, pas des
        pannes du serveur.
        """
        expr = next((e for e in expressions() if e["id"] == expression_id), None)
        if expr is None:
            raise ValueError(f"Expression inconnue : {expression_id!r}")
        traits_normalises = _traits(strokes)
        if not points_de_page({"strokes": traits_normalises}):
            raise ValueError("Aucun tracé exploitable — rien à enregistrer.")

        exemple = self.create_exemple(
            SOURCE_DICTEE_INVERSEE, traits_normalises, texte_verite=expr["latex"],
        )

        with transaction(self._progres_path, {}) as doc:
            faites = set(doc.get("faites") or [])
            faites.add(expression_id)
            toutes = {e["id"] for e in expressions()}
            # Épuisement constaté ICI, au moment où la validation vient de le
            # provoquer : cf. le docstring d'`expression_a_copier` sur pourquoi
            # ce n'est pas là-bas que ça se décide.
            doc["faites"] = [] if faites >= toutes else sorted(faites)

        return exemple
