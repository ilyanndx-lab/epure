"""Migration des données vectorielles : ``chroma_db/`` → ``vector_db/``.

Étape C de ``docs/remplacement-vectoriel.md``. Lit l'ancien index avec le vrai
``chromadb.PersistentClient`` et le réécrit dans ``core/vector_store.VectorStore``
(SQLite + numpy). À lancer une fois, à la main :

    cd backend
    python migrer_vectoriel.py              # migre vers le dossier par défaut
    python migrer_vectoriel.py --dest /tmp/essai   # ailleurs, pour un essai

**Exige `pip install chromadb`, qui n'est plus une dépendance du projet** (retiré à
l'étape D). Ce n'est pas un oubli : ce script ne peut pas se passer du moteur qu'il
migre, et il est destiné à ne tourner qu'une fois. Le garder installé dans
`requirements.txt` pour lui seul annulerait tout l'intérêt du remplacement.

**N'écrit jamais dans la source.** L'ancien ``chroma_db/`` doit rester intact et
interrogeable après coup : la comparaison de parité (``parite_vectorielle.py``)
fait tourner les deux stockages côte à côte sur les mêmes requêtes, et le plan
interdit de supprimer l'ancien avant d'avoir fait tourner l'application réelle
sur le nouveau.

Deux décisions prises sur mesure, pas par confort — elles changent ce que
contient le nouveau store :

1. **Les documents sont ré-embeddés, pas recopiés.** Recopier les vecteurs déjà
   calculés par chromadb était l'option évidente (exacte par construction, et
   sans inférence à refaire). Mesuré avant de choisir : les vecteurs stockés par
   chromadb ont une norme de 1,000000 et coïncident avec ce que
   ``VectorStore._embed`` recalcule à **1,5e-8 près** — du bruit de float32, très
   en dessous de tout écart capable d'inverser deux résultats. Les deux options
   étant équivalentes en sortie, celle-ci est préférée parce qu'elle n'utilise
   que l'API publique validée (``Collection.upsert``) au lieu d'écrire des BLOB
   directement dans les tables, et surtout parce qu'elle rend le store
   **homogène** : ce qu'il contient après migration est exactement ce qu'il
   écrirait lui-même en réindexant les mêmes fichiers. Des vecteurs importés
   d'un autre moteur auraient changé en silence à la première ré-indexation.

2. **Toutes les collections trouvées sont migrées, pas les trois attendues.**
   ``fiches``/``doc_analysis``/``history`` sont les seules aujourd'hui (vérifié),
   mais une liste en dur transformerait une quatrième collection oubliée en
   perte de données silencieuse. Ce qui est migré est listé dans le rapport.

Ré-exécutable sans dégât : ``upsert`` écrase par id. Une entrée supprimée de la
source après une première migration resterait en revanche dans la destination —
d'où le rapport de comptage, qui rend l'écart visible plutôt que supposé.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# La console Windows par défaut est en cp1252 : un simple « → » dans le rapport
# fait tomber le script sur un UnicodeEncodeError — APRÈS avoir écrit une partie
# des données, donc au pire moment. Mesuré ici, pas supposé. Sur une plateforme
# primaire Windows, un script de migration ne doit pas pouvoir échouer sur son
# propre affichage.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):    # flux redirigé/remplacé : tant pis pour l'accent
        pass

# Lot d'écriture. Le coût dominant est l'inférence du modèle d'embedding, pas
# SQLite : découper évite de tenir 170 documents et leurs vecteurs en mémoire en
# même temps sans rien coûter en vitesse (`encode` travaille déjà par batch).
TAILLE_LOT = 64


def _collections_source(client) -> list[str]:
    """Noms des collections de l'ancien index, triés — l'ordre du rapport ne doit
    pas dépendre de celui, non spécifié, que renvoie chromadb.
    """
    return sorted(c.name for c in client.list_collections())


def migrer(source: Path, dest: Path, modele: str) -> dict[str, dict]:
    """Copie chaque collection de ``source`` (chromadb) vers ``dest``
    (``VectorStore``). Renvoie un rapport ``{collection: {lus, ecrits, avant}}``.
    """
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    from core.vector_store import VectorStore

    if not source.exists():
        raise SystemExit(f"Source introuvable : {source}")

    client = chromadb.PersistentClient(path=str(source))
    # Le même embedding_function que celui qui a créé ces collections : chromadb
    # refuse d'ouvrir une collection avec une fonction d'embedding différente de
    # celle enregistrée dans ses métadonnées. Il n'est jamais appelé ici (on ne
    # fait que `get`, jamais `query`), mais il doit correspondre.
    ef = SentenceTransformerEmbeddingFunction(model_name=modele)

    store = VectorStore(dest, embedding_model=modele)

    rapport: dict[str, dict] = {}
    for nom in _collections_source(client):
        ancienne = client.get_or_create_collection(nom, embedding_function=ef)
        lues = ancienne.get(include=["documents", "metadatas"])
        ids = lues.get("ids", []) or []
        documents = lues.get("documents", []) or []
        metadatas = lues.get("metadatas", []) or []

        nouvelle = store.collection(nom)
        avant = nouvelle.count()

        for debut in range(0, len(ids), TAILLE_LOT):
            fin = debut + TAILLE_LOT
            nouvelle.upsert(
                ids=ids[debut:fin],
                documents=[d or "" for d in documents[debut:fin]],
                metadatas=[dict(m or {}) for m in metadatas[debut:fin]],
            )

        rapport[nom] = {
            "lus": len(ids),
            "avant": avant,
            "apres": nouvelle.count(),
        }
        print(
            f"  {nom:<14} {len(ids):>4} lus  →  {rapport[nom]['apres']:>4} en base"
            + (f"  (déjà {avant} avant migration)" if avant else "")
        )

    return rapport


def main() -> int:
    from core.paths import BACKEND_DIR, resolve_vector_dir

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", type=Path, default=BACKEND_DIR / "chroma_db",
        help="ancien index chromadb (défaut : backend/chroma_db)",
    )
    parser.add_argument(
        "--dest", type=Path, default=None,
        help="nouveau store (défaut : resolve_vector_dir(), backend/vector_db)",
    )
    parser.add_argument(
        "--modele", default="sentence-transformers/all-MiniLM-L6-v2",
        help="modèle d'embedding (doit être celui qui a créé l'ancien index)",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    dest = (args.dest.resolve() if args.dest else resolve_vector_dir())
    if dest == source:
        raise SystemExit("La destination ne peut pas être la source : l'ancien index doit rester intact.")

    print(f"Source (chromadb)   : {source}")
    print(f"Destination (SQLite): {dest}\n")

    rapport = migrer(source, dest, args.modele)

    total_lus = sum(r["lus"] for r in rapport.values())
    total_ecrits = sum(r["apres"] for r in rapport.values())
    print(f"\n{len(rapport)} collection(s), {total_lus} chunk(s) lus, {total_ecrits} en base.")
    if total_ecrits != total_lus:
        print(
            "⚠ Écart entre lus et écrits : la destination contenait déjà des entrées "
            "absentes de la source (migration relancée après des suppressions ?). "
            "Rien n'a été perdu côté source — vérifier avant de continuer."
        )
    print("L'ancien index n'a pas été modifié. Étape suivante : parite_vectorielle.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
