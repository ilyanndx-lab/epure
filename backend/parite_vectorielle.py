"""Parité ancien/nouveau store, sur les vraies données d'Ilyann.

Étape C de ``docs/remplacement-vectoriel.md``, deuxième temps : après
``migrer_vectoriel.py``, faire tourner **côte à côte** ``chromadb`` (sur
``backend/chroma_db/``) et ``VectorStore`` (sur ``backend/vector_db/``), leur
poser exactement les mêmes questions, et comparer. Rien ne doit être branché ni
supprimé avant que ce script sorte en 0.

    cd backend
    python parite_vectorielle.py

Exige `pip install chromadb`, qui n'est plus une dépendance du projet (retiré à
l'étape D) : comparer deux stockages demande d'avoir les deux. Même remarque que
`migrer_vectoriel.py`.

Ce que ce script est, et n'est pas :

- **Il n'est pas** ``integration_vector_store.py``. Celui-là compare les deux
  moteurs sur des données **fabriquées** pour l'occasion, et vérifie l'API. Ici
  les données sont les 170 chunks réels — fiches de prépa, documents analysés,
  conversations — avec leurs accents, leurs chemins Windows, leurs doublons de
  chunks qui se chevauchent. Un moteur peut passer l'un et rater l'autre : le
  premier ne contient aucun texte capable de produire deux distances à 1e-8
  l'une de l'autre, le vrai corpus si (le chunking à recouvrement fabrique des
  quasi-doublons par construction).
- **Il ne teste pas le code applicatif** : ``core/rag.py`` et consorts ne sont
  pas encore branchés à ce stade (c'est le temps suivant de l'étape C). Ce sont
  leurs **profils d'appel**, relevés dans le code, qui sont rejoués ici sur les
  deux stockages.

Les requêtes ne sont pas inventées : elles sont **extraites du corpus lui-même**
(des morceaux de vrais chunks, choisis par un pas déterministe sur les ids
triés), plus quelques questions en français. Une requête tirée d'un document
indexé est le cas le plus exigeant qui soit — elle a une réponse exacte, à
distance ~0, et toute divergence d'ordre saute aux yeux au lieu de se diluer
dans des scores tous médiocres.

Sortie : 0 si tout concorde, 1 sinon, avec le détail de chaque écart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

#: Tolérance sur les distances. Les vecteurs sont ré-embeddés à la migration et
#: coïncident avec ceux de chromadb à ~1,5e-8 (mesuré, cf. migrer_vectoriel.py) ;
#: 1e-5 laisse la marge du float32 sans masquer un vrai écart de calcul.
TOLERANCE = 1e-5

#: Questions en français, en plus des extraits du corpus. Volontairement
#: génériques : elles n'ont pas de bonne réponse évidente, donc elles trient des
#: distances proches les unes des autres — exactement le régime où deux moteurs
#: qui « marchent » peuvent diverger.
QUESTIONS = [
    "Comment résoudre cet exercice ?",
    "Quelle est la méthode à appliquer ici ?",
    "Explique le principe physique en jeu.",
    "Quel est le résultat de la question précédente ?",
]


class Rapport:
    """Compteur d'écarts — un script de parité qui n'échoue jamais ne prouve rien."""

    def __init__(self) -> None:
        self.verifs = 0
        self.ecarts: list[str] = []

    def egal(self, quoi: str, ancien, nouveau) -> None:
        self.verifs += 1
        if ancien != nouveau:
            self.ecarts.append(f"{quoi}\n      chromadb    : {ancien!r}\n      VectorStore : {nouveau!r}")

    def proches(self, quoi: str, anciens: list[float], nouveaux: list[float]) -> None:
        self.verifs += 1
        if len(anciens) != len(nouveaux):
            self.ecarts.append(f"{quoi} : {len(anciens)} distances vs {len(nouveaux)}")
            return
        pires = [abs(a - b) for a, b in zip(anciens, nouveaux)]
        if pires and max(pires) > TOLERANCE:
            self.ecarts.append(
                f"{quoi} : écart max {max(pires):.3e} > {TOLERANCE:.0e}\n"
                f"      chromadb    : {anciens}\n      VectorStore : {nouveaux}"
            )

    def leve_des_deux(self, quoi: str, appel_ancien, appel_nouveau) -> None:
        """Les deux doivent lever, ou aucun. Sert au `where` multi-clés, qui est
        un bug préexistant que le remplacement doit reproduire (§0.3 du plan) :
        si le nouveau store le « corrigeait », `core/docanalysis.py` changerait
        de comportement à l'insu de tout le monde.
        """
        self.verifs += 1
        a = b = None
        try:
            appel_ancien()
        except Exception as exc:
            a = type(exc).__name__
        try:
            appel_nouveau()
        except Exception as exc:
            b = type(exc).__name__
        if (a is None) != (b is None):
            self.ecarts.append(
                f"{quoi} : chromadb {'lève ' + a if a else 'passe'}, "
                f"VectorStore {'lève ' + b if b else 'passe'}"
            )


def _canonique(metadatas: list[dict]) -> list[dict]:
    """Liste de métadonnées comparable sans dépendre de deux ordres arbitraires.

    Deux pièges empilés, tous deux rencontrés pour de bon en écrivant ce script :
    ``get()`` ne garantit aucun ordre de LIGNES (il faut donc trier), et l'ordre
    des CLÉS d'un dict diffère entre les deux stockages — chromadb le reconstruit
    depuis ses colonnes, ``VectorStore`` le relit d'un JSON. Trier sur ``repr``
    mélange les deux : ``{'a': 1, 'b': 2}`` et ``{'b': 2, 'a': 1}`` sont égaux
    pour Python mais donnent des chaînes différentes, donc un tri différent, donc
    un faux écart sur des données identiques. D'où le tri par clé canonique
    (``sort_keys=True``) et la comparaison des dicts eux-mêmes, dont l'égalité
    ignore l'ordre des clés.
    """
    return sorted((dict(m or {}) for m in metadatas),
                  key=lambda m: json.dumps(m, sort_keys=True, ensure_ascii=False))


def _extraits(documents: list[str], combien: int) -> list[str]:
    """Morceaux de vrais chunks, pris à pas régulier — déterministe, et couvrant
    le corpus au lieu de ses seuls premiers éléments.
    """
    if not documents:
        return []
    pas = max(1, len(documents) // combien)
    choisis = documents[::pas][:combien]
    return [d[:200] for d in choisis if d and d.strip()]


def _cmp_query(rap: Rapport, etiquette: str, ancienne, nouvelle,
               textes: list[str], n: int, where: dict | None = None) -> None:
    """Même requête des deux côtés : mêmes ids dans le même ordre, mêmes
    documents, mêmes distances.
    """
    if n <= 0:                      # filtre sans résultat : chromadb refuse n_results=0
        return
    for texte in textes:
        kw = {"query_texts": [texte], "n_results": n}
        if where is not None:
            kw["where"] = where
        a = ancienne.query(include=["documents", "metadatas", "distances"], **kw)
        b = nouvelle.query(include=["documents", "metadatas", "distances"], **kw)
        apercu = texte[:40].replace("\n", " ")
        quoi = f"{etiquette} query({apercu!r}, n={n}{', where' if where else ''})"
        rap.egal(f"{quoi} → ids", a["ids"][0], b["ids"][0])
        rap.egal(f"{quoi} → documents", a["documents"][0], b["documents"][0])
        rap.egal(f"{quoi} → metadatas", a["metadatas"][0], b["metadatas"][0])
        rap.proches(f"{quoi} → distances", a["distances"][0], b["distances"][0])


def comparer(rap: Rapport, source: Path, dest: Path, modele: str) -> None:
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    from core.vector_store import VectorStore

    client = chromadb.PersistentClient(path=str(source))
    ef = SentenceTransformerEmbeddingFunction(model_name=modele)
    store = VectorStore(dest)

    noms = sorted(c.name for c in client.list_collections())
    print(f"Collections comparées : {', '.join(noms)}\n")

    for nom in noms:
        ancienne = client.get_or_create_collection(nom, embedding_function=ef)
        nouvelle = store.collection(nom)
        print(f"── {nom}")

        # ── Commun aux trois appelants : count() et la relecture totale ────────
        rap.egal(f"{nom} count()", ancienne.count(), nouvelle.count())
        n_total = ancienne.count()
        print(f"   count = {n_total}")

        a_tout = ancienne.get(include=["documents", "metadatas"])
        b_tout = nouvelle.get(include=["documents", "metadatas"])
        # get() ne garantit pas d'ordre : on compare des ensembles d'ids, puis le
        # contenu id par id — un ordre différent n'est pas un écart, un document
        # différent en est un.
        rap.egal(f"{nom} get() → ids (ensemble)", set(a_tout["ids"]), set(b_tout["ids"]))
        par_id_a = dict(zip(a_tout["ids"], zip(a_tout["documents"], a_tout["metadatas"])))
        par_id_b = dict(zip(b_tout["ids"], zip(b_tout["documents"], b_tout["metadatas"])))
        for rid in sorted(set(par_id_a) & set(par_id_b)):
            rap.egal(f"{nom} get() → contenu de {rid!r}", par_id_a[rid], par_id_b[rid])

        if n_total == 0:
            print("   (vide — rien à interroger)")
            continue

        documents = [par_id_a[i][0] for i in sorted(par_id_a)]
        metadatas = [par_id_a[i][1] for i in sorted(par_id_a)]
        textes = _extraits(documents, 4) + QUESTIONS
        n = min(3, n_total)

        # ── Requête sans filtre — profil de core/rag.py::_do_query et
        #    core/history.py::search ────────────────────────────────────────────
        _cmp_query(rap, nom, ancienne, nouvelle, textes, n)
        print(f"   query() sans filtre : {len(textes)} requêtes")

        # ── Filtres, selon le champ réellement utilisé par le propriétaire ─────
        if nom == "fiches":
            # core/rag.py::_do_query_filtered — `$in` sur les chemins de fichiers.
            sources = sorted({m["source"] for m in metadatas if m and "source" in m})
            lot = sources[: max(1, len(sources) // 2)]
            a = ancienne.get(where={"source": {"$in": lot}}, include=[])
            b = nouvelle.get(where={"source": {"$in": lot}}, include=[])
            rap.egal(f"{nom} get(where $in, include=[]) → ids", set(a["ids"]), set(b["ids"]))
            rap.egal(f"{nom} get(where $in) → nombre", len(a["ids"]), len(b["ids"]))
            _cmp_query(rap, nom, ancienne, nouvelle, textes, min(3, len(a["ids"])),
                       where={"source": {"$in": lot}})
            # Égalité simple, la forme du delete de index_file().
            if sources:
                a = ancienne.get(where={"source": sources[0]}, include=[])
                b = nouvelle.get(where={"source": sources[0]}, include=[])
                rap.egal(f"{nom} get(where égalité) → ids", set(a["ids"]), set(b["ids"]))
            print(f"   filtres $in / égalité sur {len(lot)}/{len(sources)} source(s)")

        elif nom == "doc_analysis":
            # core/docanalysis.py — tout est filtré par doc_id.
            doc_ids = sorted({m["doc_id"] for m in metadatas if m and "doc_id" in m})
            for doc_id in doc_ids:
                a = ancienne.get(where={"doc_id": doc_id}, include=[])
                b = nouvelle.get(where={"doc_id": doc_id}, include=[])
                rap.egal(f"{nom} get(doc_id={doc_id!r}, include=[]) → ids", set(a["ids"]), set(b["ids"]))
                a = ancienne.get(where={"doc_id": doc_id}, include=["metadatas"])
                b = nouvelle.get(where={"doc_id": doc_id}, include=["metadatas"])
                rap.egal(
                    f"{nom} get(doc_id={doc_id!r}, metadatas)",
                    _canonique(a["metadatas"]), _canonique(b["metadatas"]),
                )
                _cmp_query(rap, nom, ancienne, nouvelle, textes[:3],
                           min(3, len(a["ids"])), where={"doc_id": doc_id})
            print(f"   filtres doc_id sur {len(doc_ids)} document(s)")

            # Le `where` à deux clés : bug préexistant à REPRODUIRE, pas à corriger.
            if doc_ids:
                deux_cles = {"doc_id": doc_ids[0], "chunk_index": 0}
                rap.leve_des_deux(
                    f"{nom} get(where à 2 clés) — bug préexistant de load_document_streaming",
                    lambda: ancienne.get(where=deux_cles, include=["documents"]),
                    lambda: nouvelle.get(where=deux_cles, include=["documents"]),
                )
                print("   where à 2 clés : rejet des deux côtés (bug préservé)")

        elif nom == "history":
            # core/history.py::delete_conversation supprime par ids — vérifié en
            # lecture seule ici : quels ids une suppression VISERAIT-elle ?
            # (l'exécuter détruirait des conversations réelles, cf. plus bas)
            ids = sorted(par_id_a)
            a = ancienne.get(ids=ids[:3], include=["documents", "metadatas"])
            b = nouvelle.get(ids=ids[:3], include=["documents", "metadatas"])
            rap.egal(f"{nom} get(ids=…) → ids", set(a["ids"]), set(b["ids"]))
            print(f"   get(ids=…) sur {min(3, len(ids))} conversation(s)")

        # ── get() sans filtre renvoie tout ────────────────────────────────────
        rap.egal(f"{nom} get() sans filtre → tout", len(a_tout["ids"]), n_total)

        # Ce qui N'EST PAS vérifié ici, délibérément : `delete()` sans filtre.
        # C'est bien un point de parité (§1, point 3 du plan : chromadb lève,
        # VectorStore doit lever), mais le vérifier consiste à APPELER une
        # suppression non filtrée sur les 170 chunks réels d'Ilyann. Tout
        # l'intérêt du test est que l'appel lève — s'il ne levait pas, c'est-à-dire
        # précisément dans le cas que le test cherche à détecter, il viderait la
        # collection. Un test dont le mode d'échec est « détruire les données
        # qu'on est en train de protéger » n'a pas sa place sur des données
        # réelles : il est fait sur du jeté par `integration_vector_store.py`,
        # où il ne coûte rien. Même raison pour `delete(ids=…)` de
        # `core/history.py`, remplacé plus haut par le `get(ids=…)` équivalent.


def main() -> int:
    from core.paths import BACKEND_DIR, resolve_vector_dir

    source = (BACKEND_DIR / "chroma_db").resolve()
    dest = resolve_vector_dir()
    modele = "sentence-transformers/all-MiniLM-L6-v2"

    print(f"Ancien (chromadb)   : {source}")
    print(f"Nouveau (SQLite)    : {dest}\n")
    if not source.exists() or not dest.exists():
        raise SystemExit("Les deux stockages doivent exister — lancer migrer_vectoriel.py d'abord.")

    rap = Rapport()
    comparer(rap, source, dest, modele)

    print(f"\n{rap.verifs} comparaisons.")
    if rap.ecarts:
        print(f"\n{len(rap.ecarts)} ÉCART(S) :\n")
        for e in rap.ecarts:
            print(f"  - {e}")
        print("\nParité NON confirmée — ne rien brancher, ne rien supprimer.")
        return 1
    print("Parité confirmée : mêmes ids, même ordre, mêmes distances, mêmes rejets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
