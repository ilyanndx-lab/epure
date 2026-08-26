"""Le moteur d'embedding : ONNX Runtime + `core/wordpiece.py` + numpy.

Remplace `sentence-transformers` (donc torch, transformers, scikit-learn, scipy —
198 Mo de wheels, 843 Mo sur disque) depuis le 2026-08-26, sur **le même modèle**
`sentence-transformers/all-MiniLM-L6-v2`, dont le dépôt HuggingFace publie déjà
l'export ONNX fp32. Les raisons du remplacement sont dans
`core/embedding_install.py` ; ce fichier ne fait que calculer.

**Ce qu'on utilisait de `sentence-transformers` : trois appels.**
`SentenceTransformer(nom)`, `.get_embedding_dimension()`, et
`.encode(textes, convert_to_numpy=True, normalize_embeddings=True)`. Tout le reste
de la bibliothèque — l'entraînement, les pertes, l'évaluation, le
`community_detection` qui tirait scikit-learn — n'a jamais été appelé une fois.

**Parité mesurée, pas espérée.** Contre `sentence-transformers` sur les 40 premiers
chunks réels de la collection `fiches` (dont **37 tronqués** à 256 jetons, ce qui
éprouve la troncature autant que le calcul) : **cosinus 1.0 pour les 40**, écart
absolu maximal **1,9e-07** — du bruit de flottant. Sur cinq textes synthétiques,
l'écart était exactement **0.0**. Les 180 chunks déjà indexés restent donc
valides : **pas de réindexation**.

Les trois pièces du calcul, et pourquoi chacune est écrite ici plutôt qu'importée :

1. **Tokenisation** — `core/wordpiece.py`, Python pur, pour n'avoir aucun binaire
   non signé sur le chemin (c'est le sujet du chantier : cf. son docstring).
2. **Inférence** — `onnxruntime`, dont les trois binaires sont signés Microsoft.
   Import **local**, dans `__init__`, jamais en tête de module : la règle de
   `core/vector_store.py` ne change pas de nature parce que le coût est passé de
   17,4 s (`sentence_transformers`) à 0,37 s. `core/rag.py` importe ce module et
   `core/runtime.py` importe `core/rag.py` — un import en tête de fichier se
   paierait donc au démarrage d'uvicorn, et la paresse de `_LazyEngine` ne couvre
   que la CONSTRUCTION des moteurs, jamais l'import de leurs dépendances
   (CLAUDE.md §3.2 et §3.4).
3. **Mise en commun (pooling)** — moyenne pondérée par le masque d'attention,
   puis normalisation L2, en numpy. Ce n'est pas un choix : c'est ce que décrit
   `1_Pooling/config.json` du modèle (`pooling_mode_mean_tokens: true`, les quatre
   autres modes à `false`). Prendre le jeton `[CLS]` à la place — l'erreur
   classique, et ce que fait un BERT « nu » — donnerait des vecteurs cohérents
   entre eux mais **incompatibles avec l'index existant**.

Gains mesurés au passage, sur ce poste : import 15,1 s → **0,37 s**, chargement du
modèle 4,7 s → **0,38 s**, 40 chunks réels 2,28 s → **1,87 s**.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from core.embedding_install import chemin_fichier_modele, exiger_pile
from core.wordpiece import LONGUEUR_MAX, TokeniseurWordPiece

logger = logging.getLogger(__name__)

#: Textes envoyés à ONNX Runtime en une fois. Le lot est rembourré à la longueur
#: du plus long de SES textes : de gros lots feraient payer 256 jetons à des
#: phrases de dix mots, en mémoire comme en calcul. 32 × 256 × 384 flottants
#: (~12 Mo) est confortable, et une réindexation complète passe par ici.
TAILLE_LOT = 32


class MoteurEmbedding:
    """Charge le modèle une fois, encode des textes en vecteurs normalisés.

    Un seul exemplaire dans l'application, porté par `VectorStore` et partagé par
    les trois collections — c'est `core/runtime.py` qui construit ce store unique
    et l'injecte (CLAUDE.md §3.4). Ne pas en instancier un second : ce serait
    90 Mo de poids chargés deux fois pour le même résultat.
    """

    def __init__(self, dossier: str | Path | None = None):
        # AVANT tout le reste, et c'est le contrat de ce module : un modèle absent
        # n'est pas une erreur terminale mais un téléchargement qui n'a pas encore
        # eu lieu. `exiger_pile()` lance ce téléchargement en tâche de fond et
        # lève `EmbeddingIndisponible`, que les endpoints traduisent en 503
        # lisible porteur de l'avancement — au lieu du
        # `500 {"detail": …, "type": "ImportError"}` qui tuait le panneau fichiers
        # du module Docs à chaque ouverture (CLAUDE.md §8).
        exiger_pile()

        # Import LOCAL : cf. le point 2 du docstring de module. Ce n'est pas un
        # détail de style, c'est ce qui garde le démarrage d'uvicorn hors du coût
        # de la pile d'embedding.
        import onnxruntime as ort

        base = Path(dossier) if dossier is not None else None
        chemin_modele = (base / "model.onnx") if base else chemin_fichier_modele("model.onnx")
        chemin_vocab = (base / "vocab.txt") if base else chemin_fichier_modele("vocab.txt")

        self.tokeniseur = TokeniseurWordPiece.depuis_vocabulaire(chemin_vocab)
        # `CPUExecutionProvider` nommé explicitement plutôt que le défaut : la
        # machine cible annonce aussi `AzureExecutionProvider`, et un fournisseur
        # choisi par l'ordre de la liste par défaut est un comportement qu'on
        # subit. Le calcul doit être local et reproductible — il l'est bit pour
        # bit entre x64 et ARM64, mesuré.
        self._session = ort.InferenceSession(
            str(chemin_modele), providers=["CPUExecutionProvider"]
        )
        self._entrees = {entree.name for entree in self._session.get_inputs()}
        # La dimension est LUE sur la sortie du graphe, jamais écrite en dur :
        # 384 pour ce modèle, mais `VectorStore` s'en sert pour dimensionner ses
        # tableaux vides, et un 384 codé en dur deviendrait un mensonge silencieux
        # le jour d'un changement de modèle.
        self.dimension = int(self._session.get_outputs()[0].shape[-1])
        logger.info("Moteur d'embedding ONNX prêt (dimension %d, %s)",
                    self.dimension, chemin_modele.name)

    def _inferer(self, lots: list[list[int]]) -> np.ndarray:
        """Un lot déjà tokenisé → vecteurs normalisés."""
        longueur = max(len(x) for x in lots)
        identifiants = np.zeros((len(lots), longueur), dtype=np.int64)
        masque = np.zeros((len(lots), longueur), dtype=np.int64)
        for ligne, sequence in enumerate(lots):
            identifiants[ligne, : len(sequence)] = sequence
            masque[ligne, : len(sequence)] = 1

        entrees = {
            "input_ids": identifiants,
            "attention_mask": masque,
            # Séquence unique → tous les jetons au segment 0. Le graphe exporté
            # déclare cette entrée ; d'autres exports ne la déclarent pas, d'où le
            # filtrage par `self._entrees` plutôt qu'un dict fixe.
            "token_type_ids": np.zeros_like(identifiants),
        }
        sortie = self._session.run(
            None, {nom: valeur for nom, valeur in entrees.items() if nom in self._entrees}
        )[0]

        # Moyenne PONDÉRÉE par le masque : sans lui, le rembourrage tire chaque
        # vecteur vers celui du jeton [PAD], et l'erreur grandit avec l'écart de
        # longueur dans le lot — donc elle se voit d'autant moins qu'on la teste
        # sur des textes de taille comparable.
        poids = masque[..., None].astype(np.float32)
        somme = (sortie * poids).sum(axis=1)
        moyenne = somme / np.clip(poids.sum(axis=1), 1e-9, None)
        normes = np.linalg.norm(moyenne, axis=1, keepdims=True)
        return (moyenne / np.clip(normes, 1e-12, None)).astype(np.float32)

    def encoder(self, textes: list[str],
                longueur_max: int = LONGUEUR_MAX) -> np.ndarray:
        """Vecteurs **normalisés** (norme 1), un par texte, en `float32`.

        Normalisés ici et non chez l'appelant : c'est ce que faisait
        `SentenceTransformer.encode(normalize_embeddings=True)`, et c'est ce qui
        permet à `core/vector_store.py` de réduire sa similarité cosinus à un
        simple produit scalaire.
        """
        if not textes:
            return np.zeros((0, self.dimension), dtype=np.float32)
        morceaux = []
        for debut in range(0, len(textes), TAILLE_LOT):
            tranche = textes[debut:debut + TAILLE_LOT]
            lots, _ = self.tokeniseur.encoder_lot(tranche, longueur_max)
            morceaux.append(self._inferer(lots))
        return np.vstack(morceaux)
