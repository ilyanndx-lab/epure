"""Magasin des pages d'encre manuscrite du module ``encre`` (phase 1).

**Un fichier JSON par page, et aucun index central.** C'est le seul choix de
structure de ce fichier, et il est délibéré : l'état « quelles pages existent »
se lit en scannant ``resolve_encre_dir()``, jamais dans un second fichier tenu à
jour en parallèle. C'est la règle du §3.3 de CLAUDE.md appliquée ailleurs que
sur les modules — deux stockages pour une même notion divergent mécaniquement,
et ce dépôt l'a déjà payé (``modules_state.json`` face à ``modules_activés`` :
9 entrées sur 11 pointaient des modules effacés). Un index de pages serait le
même piège : une page écrite sans son entrée d'index devient invisible alors que
son contenu est intact sur le disque — exactement le mode d'échec qu'on ne veut
pas sur des notes manuscrites.

Le coût de ce choix est connu et assumé : ``list_pages()`` ouvre chaque fichier.
Il ne rend PAS les tracés pour autant — l'entrée de liste est construite sans le
champ ``strokes``, qui pèse l'essentiel du fichier (quelques centaines de points
par trait, quelques centaines de traits par page). C'est pour ça que
``GET /encre/pages`` et ``GET /encre/pages/{id}`` ne rendent pas la même chose.

⚠️ **Les tracés sont opaques pour ce moteur.** Il vérifie que ``strokes`` est une
LISTE, et rien de plus profond. Deux raisons, pas une :

* ``docs/module-encre.md`` fixe l'encre brute comme la donnée irremplaçable et
  le reste (rendu bitmap, transcription de la phase 2) comme dérivé. Un moteur
  qui validerait la forme d'un point déciderait à la place du client ce qu'est
  un point, et le ferait au moment précis où plus rien ne se reconstruit ;
* la phase 2 ajoutera des champs (``transcrite``, ``modèle``, ``version``), et
  peut-être un champ par point. Une validation profonde ici obligerait à migrer
  les fichiers de l'utilisateur pour ajouter un champ facultatif.

Ce que le moteur garantit, en revanche, c'est le confinement de chemin. Voir
:meth:`EncreEngine._page_path`.
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.jsonstore import read_json, transaction, write_json
from core.paths import PathOutsideDataError, resolve_encre_dir

logger = logging.getLogger(__name__)

#: Titre d'une page créée sans titre. Constante et non littéral : le routeur et
#: les tests le lisent, et une page « Page sans titre » n'est pas la même chose
#: qu'une page dont le titre est la chaîne vide (le second cas dit
#: « l'utilisateur a effacé le titre », et on le conserve tel quel).
TITRE_DEFAUT = "Page sans titre"


class _PageAbsente(Exception):
    """Sentinelle interne : abandonne une ``transaction`` SANS rien écrire.

    Jumelle de ``core/history.py:_ConversationAbsente``, et le danger écarté est
    le même — à ceci près qu'ici le contenu perdu serait de l'encre manuscrite,
    que rien ne réécrit. ``transaction(chemin, None)`` sur un fichier corrompu
    n'échoue pas : ``read_json`` loggue, rend le défaut, et le corps du ``with``
    reconstruirait alors une page par-dessus. Lever depuis ce corps est le seul
    moyen correct de sortir sans écrire, ``transaction`` n'ayant pas de
    ``finally``.
    """


def _horodatage() -> str:
    """Instant courant à la seconde, local et sans fuseau.

    Recopié de ``core/history.py`` plutôt qu'importé : c'est une convention de
    format du dépôt, pas une fonction partagée, et ``HistoryEngine`` n'a aucune
    raison de devenir une dépendance de ce module.
    """
    return datetime.now().isoformat(timespec="seconds")


class EncreEngine:
    """Lecture/écriture des pages d'encre. Aucun LLM, aucun modèle, aucun réseau.

    Construit tôt (``core/runtime.py``, à côté de ``flashcards_engine``) et non
    derrière un ``_LazyEngine`` : il n'y a rien de coûteux à construire ici — pas
    d'embedding, pas de poids à télécharger — donc la paresse n'achèterait rien
    et ajouterait une indirection.

    Le dossier est résolu DANS ``__init__``, jamais au niveau module
    (CLAUDE.md §3.5) : figer ``resolve_encre_dir()`` dans une constante rendrait
    ``$EPURE_ENCRE_DIR`` sans effet, donc la suite de tests écrirait dans les
    vraies notes de l'utilisateur.
    """

    def __init__(self, dossier: Optional[Path] = None):
        self._dir = Path(dossier).resolve() if dossier else resolve_encre_dir()
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── Chemins ───────────────────────────────────────────────────────────────

    def _page_path(self, page_id: str) -> Path:
        """Fichier d'une page, confiné au dossier d'encre — ou refus explicite.

        Les identifiants sont fabriqués ICI (``uuid4().hex``), donc en usage
        normal aucun d'eux ne peut sortir du dossier. Cette garde n'en est pas
        moins obligatoire : ``get``/``update``/``delete`` reçoivent l'identifiant
        du CLIENT, et ``delete`` finit en ``unlink()``. Défense en profondeur,
        posée avant la première lecture plutôt qu'après le premier incident.

        Confinement par ``resolve()`` puis comparaison de chemins, jamais par
        ``startswith`` de chaîne (CLAUDE.md §6) — un dossier frère ``encre-bis/``
        passerait le second.

        ``cible.parent != racine`` et NON ``is_relative_to(racine)``, exactement
        comme ``HistoryEngine._conv_path`` et pour la raison qui y est écrite :
        ``is_relative_to`` accepte encore ``sub/x`` → ``<encre>/sub/x.json``,
        confiné mais créant une arborescence au premier ``write_json``, qui fait
        un ``mkdir(parents=True)``. Un identifiant de page est un segment nu ; on
        exige donc un enfant DIRECT, et on refuse au lieu de nettoyer en silence.

        ⚠️ Ce qui compte comme séparateur DÉPEND DE LA PLATEFORME, et il faut le
        savoir avant d'écrire un test : un antislash est une évasion sous Windows
        (deux segments) et un simple caractère de nom de fichier sous POSIX (un
        segment). Les deux comportements sont corrects — sous POSIX le fichier
        reste bien dans le dossier d'encre et ne menace rien — mais une assertion
        qui exigerait un refus des deux côtés passerait ici et échouerait en CI.
        """
        # Les trois identifiants dégénérés d'abord, parce que la garde de
        # confinement ci-dessous les LAISSE PASSER et a raison de le faire : le
        # suffixe `.json` est concaténé, donc `..` donne `<encre>/...json` et la
        # chaîne vide `<encre>/.json` — des noms de fichiers bizarres, mais bel
        # et bien dans le dossier. Ce n'est donc pas une évasion qu'on refuse
        # ici, c'est un ALLER-RETOUR CASSÉ : `list_pages()` rend l'identifiant
        # d'une page par `chemin.stem`, et `Path(".json").stem` vaut `.json` et
        # non la chaîne vide. Une page atteignable sous un identifiant qui n'est
        # pas celui que la liste affiche est une page qu'on croit avoir perdue.
        # Refus explicite plutôt que nettoyage silencieux, comme
        # `core.paths.safe_upload_name`.
        if not page_id or page_id in (".", ".."):
            raise PathOutsideDataError(f"Identifiant de page invalide : {page_id!r}")
        racine = self._dir.resolve()
        try:
            cible = (racine / f"{page_id}.json").resolve()
        except OSError as exc:      # nom impossible pour le système de fichiers
            raise PathOutsideDataError(
                f"Identifiant de page invalide : {page_id!r}") from exc
        if cible.parent != racine or cible == racine:
            raise PathOutsideDataError(f"Identifiant de page invalide : {page_id!r}")
        return cible

    # ── Normalisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _traits(strokes) -> list:
        """``strokes`` ramené à une liste, sans jamais regarder à l'intérieur.

        C'est ``Array.isArray`` côté serveur, et pour le même motif qu'en
        frontend (``src/normaliser.ts``) : un ``None``, un objet ou une chaîne
        arrivant d'un client qui n'a pas la bonne version doit produire une page
        vide, pas un fichier dont la relecture plantera. Ce qu'il y a DANS la
        liste n'est pas notre affaire (cf. l'en-tête du module).
        """
        return strokes if isinstance(strokes, list) else []

    @staticmethod
    def _titre(titre) -> str:
        """Titre nettoyé. Une chaîne vide est conservée, ``None`` ne l'est pas.

        Un titre effacé par l'utilisateur est une information — l'interface
        affiche alors un repli — alors qu'un champ absent vient d'un client qui
        ne connaît pas ce champ, à qui on doit un défaut.
        """
        return titre if isinstance(titre, str) else TITRE_DEFAUT

    def _resume(self, page: dict, page_id: str) -> dict:
        """Entrée de liste : tout SAUF les tracés.

        Le seul endroit qui décide ce que voit ``GET /encre/pages``. Le nombre de
        traits est calculé ici plutôt que le contenu renvoyé brut : c'est ce que
        l'interface affiche, et le faire côté client obligerait à transférer
        l'encre entière rien que pour rendre une liste.
        """
        return {
            "id": page_id,
            "titre": self._titre(page.get("titre")),
            "date_creation": page.get("date_creation") or "",
            "date_modification": page.get("date_modification") or "",
            "n_traits": len(self._traits(page.get("strokes"))),
        }

    # ── API publique ──────────────────────────────────────────────────────────

    def list_pages(self) -> list[dict]:
        """Les pages du dossier, la plus récemment modifiée d'abord, sans tracés.

        L'identifiant retenu est le NOM DU FICHIER, pas le champ ``id`` du
        contenu. Les deux coïncident en usage normal ; s'ils divergeaient (un
        fichier copié à la main, une écriture interrompue), c'est le nom qui
        permet de rouvrir la page — un ``id`` interne faux rendrait l'entrée
        cliquable et introuvable.

        Un fichier illisible est ignoré avec un avertissement plutôt que de faire
        échouer la liste entière : une page corrompue ne doit pas rendre les
        autres inaccessibles. ``read_json`` a déjà logué la cause.
        """
        if not self._dir.is_dir():
            return []
        pages: list[dict] = []
        for chemin in self._dir.glob("*.json"):
            if not chemin.is_file():
                continue
            page = read_json(chemin, None)
            if not isinstance(page, dict):
                logger.warning("Page d'encre illisible, ignorée : %s", chemin.name)
                continue
            pages.append(self._resume(page, chemin.stem))
        # Tri sur la date de modification puis sur l'id : l'horodatage est à la
        # seconde, donc deux pages écrites dans la même seconde doivent garder un
        # ordre stable d'un appel à l'autre, sinon la liste sautille.
        pages.sort(key=lambda p: (p["date_modification"], p["id"]), reverse=True)
        return pages

    def get_page(self, page_id: str) -> Optional[dict]:
        """La page complète, tracés compris — ou ``None`` si elle n'existe pas.

        ``None`` et non une exception : « cette page n'existe pas » est une
        réponse normale (un onglet resté ouvert sur une page supprimée), que le
        routeur traduit en 404. Un identifiant hors du dossier, lui, lève : c'est
        une tentative, pas un état.
        """
        chemin = self._page_path(page_id)
        if not chemin.is_file():
            return None
        page = read_json(chemin, None)
        if not isinstance(page, dict):
            return None
        page["id"] = chemin.stem
        page["titre"] = self._titre(page.get("titre"))
        page["strokes"] = self._traits(page.get("strokes"))
        return page

    def create_page(self, titre=None, strokes=None) -> dict:
        """Crée une page et rend son contenu complet.

        L'identifiant est fabriqué ici (``uuid4().hex``) et jamais accepté du
        client : c'est un nom de fichier, et laisser le client le choisir
        rouvrirait toute la surface que ``_page_path`` referme.

        Rend la page ENTIÈRE et pas seulement son id : le client vient de la
        créer, il a besoin des deux dates pour afficher son état sans un second
        aller-retour.
        """
        page_id = uuid.uuid4().hex
        maintenant = _horodatage()
        page = {
            "id": page_id,
            "titre": self._titre(titre) if titre is not None else TITRE_DEFAUT,
            "date_creation": maintenant,
            "date_modification": maintenant,
            "strokes": self._traits(strokes),
        }
        write_json(self._page_path(page_id), page)
        return page

    def update_page(self, page_id: str, titre=None, strokes=None) -> Optional[dict]:
        """Remplace titre et/ou tracés d'une page existante. ``None`` si absente.

        Read-modify-write sous ``transaction`` et non ``read_json`` +
        ``write_json`` (CLAUDE.md §3.4) : la sauvegarde automatique du canvas et
        un renommage de page partent du même client à quelques millisecondes
        d'écart, sur deux threads du pool FastAPI. Sans verrou, le second écrase
        la modification du premier.

        ``None`` laisse le champ INCHANGÉ, il ne l'efface pas — c'est ce qui rend
        possible le « renomme sans me faire remonter 3 Mo d'encre ». Un
        ``strokes`` explicitement vide (``[]``) efface bien la page : c'est une
        liste, pas une absence.
        """
        chemin = self._page_path(page_id)
        if not chemin.is_file():
            return None
        try:
            with transaction(chemin, None) as page:
                if not isinstance(page, dict):
                    raise _PageAbsente
                page["id"] = page_id
                if titre is not None:
                    page["titre"] = self._titre(titre)
                if strokes is not None:
                    page["strokes"] = self._traits(strokes)
                page.setdefault("date_creation", _horodatage())
                page["date_modification"] = _horodatage()
                # Copie prise DANS le ``with`` : après la sortie, l'objet cédé a
                # déjà été écrit et rien ne garantit qu'on puisse encore le lire
                # sans reprendre le verrou.
                resultat = dict(page)
        except _PageAbsente:
            logger.warning("Page d'encre illisible, non modifiée : %s", chemin.name)
            return None
        return resultat

    def delete_page(self, page_id: str) -> bool:
        """Supprime une page. ``False`` si elle n'existait pas.

        ``unlink()`` puis ``FileNotFoundError`` attrapé, plutôt qu'un
        ``exists()`` suivi d'un ``unlink()`` : entre les deux appels, un autre
        thread peut avoir supprimé le fichier, et l'endpoint remonterait alors en
        500 sur une opération dont le résultat voulu est déjà atteint.
        """
        chemin = self._page_path(page_id)
        try:
            chemin.unlink()
        except FileNotFoundError:
            return False
        return True
