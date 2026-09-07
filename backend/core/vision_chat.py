"""Analyse vision CIBLÉE d'une image attachée, dans un tour de chat.

Ce module existe parce que le mécanisme d'import ne suffisait pas, et le
constat est une mesure, pas une intuition : une image attachée ne passait que
par `RAGEngine._texte_image` **à l'indexation**, avec le prompt générique de
`core.llm.prompt_vision()` sans question (« Décris cette image et transcris
tout texte visible »). Sur un énoncé mathématique dense, cette légende ne
permet pas de répondre à une vraie question sur l'image — elle dit qu'il y a un
triangle et des symboles. Et le chat, lui, **ne voyait jamais l'image** : au
mieux ce résumé, recopié dans `[CONTEXTE ACTIF]`.

── Ce que ce module ajoute, et ce qu'il ne touche pas ────────────────────────

Il ajoute une SECONDE analyse, déclenchée par la question réellement posée,
gardée par fichier, et injectée dans le prompt des tours suivants. Il ne
remplace rien : `_texte_image`, `résumé_contexte` et le résumé d'import restent
en place et gardent leur rôle d'aperçu léger, disponible sans coût au moment
où l'on n'a pas encore de question.

── Les trois bornes, et pourquoi elles ne sont pas décoratives ───────────────

`config.yaml` pose ``n_ctx: 4096``. Une analyse d'image dense fait facilement
un millier de tokens, et elle est réinjectée à CHAQUE tour tant que l'image
reste attachée. Sans borne, deux images suffisent à saturer la fenêtre en
quelques échanges — c'est-à-dire à reproduire, par le remède, la panne qu'on
soigne. D'où :

* `MAX_CARACTERES_ANALYSE` — tronquée **à l'écriture**, pas à l'injection : le
  disque et le prompt doivent dire la même chose, sinon un jour quelqu'un lit
  le fichier de conversation et croit à un contexte que le modèle n'a jamais
  vu ;
* `MAX_IMAGES_PAR_TOUR` — combien d'analyses un SEUL tour déclenche. La borne
  est de latence, pas de mémoire : 6 à 26 s par image mesurés (§3.3 bis de
  CLAUDE.md), donc cinq images attachées feraient deux minutes de silence dans
  une conversation active. Ce qui dépasse n'est pas perdu, c'est **reporté** :
  le tour suivant prend les suivantes, et `restantes()` permet de le dire à
  l'utilisateur au lieu de le laisser deviner ;
* `MAX_CARACTERES_CONTEXTE` — le total injecté par tour, toutes images
  confondues. Au-delà, les analyses excédentaires sont omises et l'omission
  est **écrite dans le bloc** : un contexte tronqué en silence est exactement
  la classe de panne que ce dépôt paie le plus cher.

── Ce que ce module ne fait PAS ──────────────────────────────────────────────

Aucune heuristique d'intention. La règle de déclenchement est celle demandée
par le chantier, et elle tient en une ligne : **image attachée + pas encore
d'analyse pour ce fichier = on analyse**. Pas de détection de « cette question
a-t-elle besoin de l'image ? » — se tromper coûterait soit 26 s de latence pour
rien, soit une réponse à côté sur la seule question qui avait besoin de l'œil,
et rien ne permet de trancher à moindre coût que d'essayer.

La réanalyse, elle, n'est **jamais** automatique : une image dont l'analyse
existe n'est plus jamais renvoyée à un modèle vision, quelle que soit la
question suivante. C'est l'`@image` du chat qui la force
(``force=True`` ici), et lui seul.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.paths import cle_chemin
from core.rag import _IMAGE_EXTENSIONS

logger = logging.getLogger(__name__)

#: Taille maximale du texte d'analyse CONSERVÉ pour une image, en caractères.
#:
#: ~2 000 caractères ≈ 500 tokens, soit un huitième de la fenêtre de 4 096 de
#: `config.yaml`, pour un texte réinjecté à chaque tour. Assez pour la
#: transcription d'un énoncé dense (l'exemple qui a lancé ce chantier tient
#: largement dedans) ; pas assez pour qu'une image bavarde monopolise le
#: contexte d'une conversation entière.
MAX_CARACTERES_ANALYSE = 2000

#: Nombre d'images analysées par UN tour de chat. Borne de LATENCE.
#:
#: Mesuré sur ce poste (§3.3 bis) : 6 à 19 s par image sur `flm:qwen3vl-it:4b`,
#: ~2 s sur `moondream` chaud, 26 s au premier appel après chargement du modèle.
#: Deux images = un pire cas d'environ une minute, déjà long dans une
#: conversation ; cinq seraient inacceptables. Le reste part au tour suivant.
MAX_IMAGES_PAR_TOUR = 2

#: Total injecté par tour, toutes analyses confondues.
#:
#: 4 000 caractères ≈ 1 000 tokens ≈ un quart de la fenêtre. Le plafond mord à
#: partir de trois images analysées ; au-delà, l'omission est annoncée dans le
#: bloc plutôt que silencieuse.
MAX_CARACTERES_CONTEXTE = 4000

#: Étiquette du bloc injecté dans le prompt système. Distincte de
#: `[CONTEXTE ACTIF]` (le résumé d'import) : les deux peuvent coexister sur une
#: conversation, et le modèle doit pouvoir les distinguer — l'un est une
#: légende générique, l'autre une lecture faite pour une question précise.
ETIQUETTE = "[ANALYSE D'IMAGE]"


def est_image(chemin: str) -> bool:
    """Extension d'image reconnue par le RAG ?

    `_IMAGE_EXTENSIONS` de `core.rag` est la source, importée et non recopiée —
    même discipline que `modules/settings/router.py`, qui l'importe déjà sous
    ce nom privé. Une seconde liste divergerait, et l'oubli le plus probable
    (une extension ajoutée d'un côté seulement) produirait le pire symptôme :
    une image attachée qui ne déclenche jamais d'analyse, sans un mot.
    """
    return Path(chemin).suffix.lower() in _IMAGE_EXTENSIONS


def images_attachees(fichiers) -> list[str]:
    """Les images de la liste des fichiers attachés, dans l'ordre d'attachement."""
    return [str(f) for f in (fichiers or []) if est_image(str(f))]


def analyse_de(chemin: str, cache: dict) -> Optional[dict]:
    """L'analyse déjà faite pour ce fichier, ou ``None``.

    Clé `cle_chemin` (`normcase` + `normpath`) et pas la chaîne brute : sous
    Windows, ``C:/Users/…`` et ``C:\\Users\\…`` désignent le même fichier, et
    la même image attachée deux fois par deux chemins différents serait
    analysée deux fois — au prix mesuré ci-dessus.
    """
    entree = (cache or {}).get(cle_chemin(chemin))
    return entree if isinstance(entree, dict) and entree.get("texte") else None


def images_a_analyser(fichiers, cache: dict, force: bool = False) -> list[str]:
    """Les images que CE tour doit analyser, bornées par `MAX_IMAGES_PAR_TOUR`.

    ``force=False`` (le cas normal, tous les tours) : uniquement celles qui
    n'ont pas encore d'analyse. Une fois analysée, une image ne repart jamais
    vers un modèle vision — c'est tout l'objet du cache, et c'est ce que le
    chantier demande explicitement (« NE PAS la refaire tant qu'aucun besoin
    explicite ne le demande »).

    ``force=True`` (l'override ``@image``) : **toutes** les images attachées,
    y compris celles déjà analysées. Rejouer toutes les images du fil et pas
    seulement la dernière est le comportement voulu : l'utilisateur qui tape
    ``@image`` demande à ce que la question qu'il pose soit reposée à l'image,
    et il n'a aucun moyen de désigner un fichier dans le chat.
    """
    images = images_attachees(fichiers)
    if not force:
        images = [p for p in images if analyse_de(p, cache) is None]
    return images[:MAX_IMAGES_PAR_TOUR]


def restantes(fichiers, cache: dict, force: bool = False) -> int:
    """Combien d'images le plafond de ce tour laisse pour les tours suivants."""
    images = images_attachees(fichiers)
    if not force:
        images = [p for p in images if analyse_de(p, cache) is None]
    return max(0, len(images) - MAX_IMAGES_PAR_TOUR)


def analyser(llm, chemin: str, question: str, modele: str) -> Optional[dict]:
    """Analyse ciblée d'UNE image. Rend l'enregistrement à conserver, ou ``None``.

    ``None`` couvre tous les échecs, et **aucun ne lève** : même convention que
    `RAGEngine._texte_image` (§3.3 bis de CLAUDE.md), et pour une raison de plus
    ici — cet appel est sur le chemin d'un message. Un modèle vision en panne
    ne doit pas empêcher le modèle de chat de répondre avec ce qu'il a.

    Les trois cas dégradés SANS exception sont logués séparément, comme dans
    `_texte_image` : c'est ce qui a fini par expliquer un cas réel là-bas
    (« content vide » indiscernable d'un « aucun modèle disponible »).
    """
    if llm is None:
        logger.warning("Aucun LLM — analyse ciblée impossible pour %s", chemin)
        return None
    if not modele:
        logger.warning("Aucun modèle vision — analyse ciblée impossible pour %s", chemin)
        return None
    # `stats` rempli sur place par `describe_image` : c'est ce qui permet à
    # l'appelant de COMPTER un appel cloud dans `usage_tracker`. Un appel
    # payant non compté est pire qu'un quota absent — il donne confiance dans
    # un chiffre faux. Le dict est créé même pour un modèle local (le tracker
    # ignore les providers locaux, cf. `_LOCAL_PROVIDERS`) : une branche de
    # moins, et les tokens restent une information de diagnostic utile
    # (`eval_count` proche de 0 est LE discriminant d'une réponse vide, §3.3
    # bis de CLAUDE.md).
    stats: dict = {}
    try:
        texte = llm.describe_image(chemin, modele, question=question, stats=stats)
    except Exception:
        logger.exception("Échec analyse ciblée de %s (modèle %s)", chemin, modele)
        return None
    texte = (texte or "").strip()
    if not texte:
        logger.warning(
            "Analyse ciblée vide pour %s (modèle %s) — rien de conservé, "
            "l'image restera à analyser", chemin, modele,
        )
        return None
    return {
        "chemin": str(chemin),
        # La question est CONSERVÉE et renommée dans le bloc injecté. Sans
        # elle, un tour suivant qui porte une question sans rapport lirait
        # l'analyse comme si elle y répondait — le modèle n'a aucun moyen de
        # savoir qu'elle a été faite pour autre chose.
        "question": (question or "").strip()[:MAX_CARACTERES_ANALYSE],
        "texte": texte[:MAX_CARACTERES_ANALYSE],
        "modèle": modele,
        "horodatage": datetime.now().isoformat(timespec="seconds"),
        # `tronquée` plutôt qu'une comparaison de longueurs à la relecture :
        # le lecteur du fichier de conversation doit pouvoir distinguer une
        # analyse courte d'une analyse coupée.
        "tronquée": len(texte) > MAX_CARACTERES_ANALYSE,
        "tokens": dict(stats),
    }


def bloc_contexte(fichiers, cache: dict) -> str:
    """Le bloc à injecter dans le prompt système, ou une chaîne vide.

    Une section par image attachée POSSÉDANT une analyse, dans l'ordre
    d'attachement, sous le plafond `MAX_CARACTERES_CONTEXTE`. Une image sans
    analyse n'apparaît pas — c'est vrai, et c'est mieux qu'une mention
    « analyse indisponible » qui occuperait de la place pour ne rien dire.

    L'omission par plafond, elle, est ÉCRITE : le modèle doit savoir qu'il lui
    manque quelque chose plutôt que raisonner sur un contexte qu'il croit
    complet.
    """
    sections: list[str] = []
    budget = MAX_CARACTERES_CONTEXTE
    omises = 0
    for chemin in images_attachees(fichiers):
        entree = analyse_de(chemin, cache)
        if entree is None:
            continue
        question = str(entree.get("question") or "").strip()
        entete = f"— {Path(chemin).name}"
        if question:
            entete += f" (analyse réalisée pour la question : « {question} »)"
        section = f"{entete}\n{entree['texte']}"
        if len(section) > budget:
            omises += 1
            continue
        sections.append(section)
        budget -= len(section)
    if not sections:
        return ""
    bloc = f"{ETIQUETTE}\n" + "\n\n".join(sections)
    if omises:
        bloc += (
            f"\n\n({omises} autre(s) image(s) analysée(s) non reprise(s) ici, "
            "faute de place dans le contexte.)"
        )
    return bloc


def chunk_redondant(chunk: dict, cache: dict) -> bool:
    """Ce chunk RAG est-il la légende générique d'une image déjà analysée ?

    Le piège qu'il ferme, et il est facile à ne pas voir : une image indexée à
    l'import a pour texte de chunk ``"Image : nom\\n\\n<légende générique>"``.
    Elle est dans `fichiers_attachés`, donc `rag.query_filtered_avec_sources`
    la fait remonter comme n'importe quel document. Un tour portant une analyse
    ciblée mettrait donc **les deux descriptions de la même image** dans le même
    prompt — deux fois payées sur une fenêtre de 4 096, et surtout deux textes
    concurrents dont le plus faible est le plus long à lire. C'est exactement
    ce que le critère de succès du chantier interdit.

    Filtré ICI, sur les chunks rendus, et pas seulement en retirant le fichier
    de la liste passée au RAG : le mode ``@cours`` (`rag_override == "all"`)
    interroge tout le corpus sans liste de fichiers, donc une exclusion en
    amont ne le couvrirait pas.
    """
    source = (chunk or {}).get("source")
    if not source or not est_image(str(source)):
        return False
    return analyse_de(str(source), cache) is not None
