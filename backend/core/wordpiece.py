"""WordPiece BERT en Python pur — le tokeniseur du modèle d'embedding.

**Pourquoi il est écrit ici plutôt qu'importé.** `all-MiniLM-L6-v2` se tokenise
avec `tokenizers` (la bibliothèque Rust de HuggingFace) en trois lignes. Ce n'est
pas ce qu'on fait, et la raison n'est pas l'économie de dépendance :
`tokenizers.pyd` est un binaire **non signé**, et c'est exactement la catégorie
qui a rendu Épure inutilisable sur la machine ARM64 d'un destinataire — Smart App
Control y bloque durablement `sklearn/utils/_isfinite` (importé sans condition par
`sentence-transformers`), et le blocage se décide **par fichier**, sur la
réputation ISG : les `.pyd` de numpy, non signés eux aussi, passent sur cette même
machine ; celui de scikit-learn non. On ne peut donc pas *raisonner* qu'un binaire
non signé passera. Le seul choix qui ne dépende pas de la réputation est de n'en
avoir aucun : la nouvelle pile d'embedding est `onnxruntime` (dont les trois
binaires sont signés `CN=Microsoft Corporation`, vérifié sur la machine cible) et
ce fichier.

**Coût mesuré, avant d'accepter de réécrire un tokeniseur** : 805 ms contre 349 ms
pour 200 textes, soit ~4 ms par texte contre ~1,7 ms — sur un chemin où
l'inférence ONNX en coûte ~47 ms. La lenteur est réelle et invisible.

**Parité prouvée, pas supposée.** Les identifiants produits ici ont été comparés
un par un à ceux de `tokenizers` sur 200 échantillons : les 180 chunks réels des
trois collections vectorielles de l'instance, plus 20 cas limites choisis pour
casser une implémentation naïve — accent combinant (`e` + U+0301) contre accent
précomposé, CJK, pleine largeur (`ＦＵＬＬ`), demi-largeur katakana, `ß`, emoji
hors BMP, mot de 150 caractères (au-delà de `max_input_chars_per_word`), `U+0000`,
`�`, espace de largeur nulle, chemin Windows, apostrophe française.
**Zéro divergence.** `backend/test_wordpiece.py` rejoue cette table, figée, pour
que la CI la tienne sans installer `tokenizers`.

**Ce qui est implémenté, et d'où viennent les règles.** Elles ne sont pas
devinées : elles sont lues dans le `tokenizer.json` du modèle, qui décrit sa
chaîne comme
``BertNormalizer(clean_text, handle_chinese_chars, lowercase=True,
strip_accents=null)`` → ``BertPreTokenizer`` → ``WordPiece(##,
max_input_chars_per_word=100)`` → ``TemplateProcessing([CLS] A [SEP])``.

Le point qui se déduit mal : ``strip_accents=null`` **ne veut pas dire « ne pas
retirer les accents »**. Dans `BertNormalizer`, `None` signifie « suivre
`lowercase` » — donc ici les accents SONT retirés. Le lire comme un `False`
donnerait un tokeniseur qui marche sur l'anglais et diverge sur chaque mot
accentué, c'est-à-dire sur tout le corpus réel de cette instance.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

#: Longueur maximale d'un mot avant abandon, `[UNK]` renvoyé tel quel.
#: `max_input_chars_per_word` du modèle. Ce n'est pas une borne de confort :
#: WordPiece est en O(n²) sur la longueur du mot, et un « mot » de 50 000
#: caractères existe dès qu'un PDF mal extrait recolle une page entière.
MAX_CARACTERES_PAR_MOT = 100

#: Longueur maximale de la séquence, **jetons spéciaux inclus**.
#: `sentence_bert_config.json` du modèle : `max_seq_length = 256`. C'est la
#: troncature que `sentence-transformers` appliquait, donc celle qu'il faut
#: reproduire pour que les vecteurs restent comparables aux 180 chunks déjà
#: indexés — 37 des 40 chunks de la mesure de parité étaient tronqués, et le
#: cosinus est resté à 1.0.
LONGUEUR_MAX = 256

_PAD, _UNK, _CLS, _SEP = "[PAD]", "[UNK]", "[CLS]", "[SEP]"


def lire_vocabulaire(chemin: str | Path) -> dict[str, int]:
    """`vocab.txt` → {jeton: identifiant}, l'identifiant étant le numéro de ligne.

    `vocab.txt` et non `tokenizer.json` : les deux portent le même vocabulaire
    (vérifié, dictionnaire par dictionnaire, sur les 30 522 entrées), et c'est
    231 ko contre 466 ko à télécharger.

    Lu en `utf-8` strict et sans `.strip()` : seul le saut de ligne final est
    retiré. Un `.strip()` détruirait les jetons qui SONT des espaces ou de la
    ponctuation, et le résultat ne se verrait qu'en divergence de tokenisation.
    """
    vocabulaire: dict[str, int] = {}
    with open(chemin, encoding="utf-8") as f:
        for identifiant, ligne in enumerate(f):
            vocabulaire[ligne.rstrip("\n")] = identifiant
    for special in (_PAD, _UNK, _CLS, _SEP):
        if special not in vocabulaire:
            raise ValueError(f"vocabulaire incomplet : {special} absent de {chemin}")
    return vocabulaire


def _est_controle(caractere: str) -> bool:
    """Caractère de contrôle à supprimer (`clean_text` de BertNormalizer).

    Tabulation, saut de ligne et retour chariot en sont exclus : ils deviennent
    des espaces, ce qui sépare deux mots au lieu de les coller.
    """
    if caractere in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(caractere).startswith("C")


def _est_chinois(point_de_code: int) -> bool:
    """Idéogramme CJK, à isoler entre deux espaces.

    Les plages sont celles du BERT d'origine, recopiées telles quelles — elles
    ne couvrent volontairement ni les kana ni le hangul, et « corriger » ça
    ferait diverger la tokenisation d'un modèle entraîné avec cette table.
    """
    return (
        0x4E00 <= point_de_code <= 0x9FFF
        or 0x3400 <= point_de_code <= 0x4DBF
        or 0x20000 <= point_de_code <= 0x2A6DF
        or 0x2A700 <= point_de_code <= 0x2B73F
        or 0x2B740 <= point_de_code <= 0x2B81F
        or 0x2B820 <= point_de_code <= 0x2CEAF
        or 0xF900 <= point_de_code <= 0xFAFF
        or 0x2F800 <= point_de_code <= 0x2FA1F
    )


def _est_ponctuation(caractere: str) -> bool:
    """Ponctuation au sens de BERT : sa table ASCII, PLUS la catégorie Unicode P.

    Les quatre plages ASCII explicites ne sont pas redondantes avec la catégorie :
    `$`, `+`, `<`, `=`, `^`, `` ` ``, `|`, `~` sont des SYMBOLES pour Unicode (Sc,
    Sm, Sk), pas de la ponctuation, et BERT les traite pourtant comme telle.
    S'en tenir à `category().startswith("P")` collerait `f(x)=y` en un seul mot.
    """
    point_de_code = ord(caractere)
    if (
        33 <= point_de_code <= 47
        or 58 <= point_de_code <= 64
        or 91 <= point_de_code <= 96
        or 123 <= point_de_code <= 126
    ):
        return True
    return unicodedata.category(caractere).startswith("P")


class TokeniseurWordPiece:
    """Le tokeniseur du modèle, construit sur son seul `vocab.txt`.

    Sans état entre deux appels : réutilisable depuis plusieurs threads, ce qui
    compte puisque `VectorStore` sérialise ses accès mais que le proxy paresseux
    de `core/runtime.py` peut le construire depuis n'importe quel fil.
    """

    def __init__(self, vocabulaire: dict[str, int], *, minuscules: bool = True,
                 retirer_accents: bool = True):
        self.vocabulaire = vocabulaire
        self.minuscules = minuscules
        # Défaut `True` et non `False` : cf. le docstring du module, c'est ce que
        # `strip_accents: null` veut dire quand `lowercase` est vrai.
        self.retirer_accents = retirer_accents
        self.inconnu = vocabulaire[_UNK]
        self.debut = vocabulaire[_CLS]
        self.fin = vocabulaire[_SEP]
        self.remplissage = vocabulaire[_PAD]

    @classmethod
    def depuis_vocabulaire(cls, chemin: str | Path) -> "TokeniseurWordPiece":
        return cls(lire_vocabulaire(chemin))

    # ── BertNormalizer ────────────────────────────────────────────────────────

    def _normaliser(self, texte: str) -> str:
        propre: list[str] = []
        for caractere in texte:
            point_de_code = ord(caractere)
            if point_de_code == 0 or point_de_code == 0xFFFD or _est_controle(caractere):
                continue
            propre.append(" " if caractere in ("\t", "\n", "\r") else caractere)
        texte = "".join(propre)
        texte = "".join(
            f" {c} " if _est_chinois(ord(c)) else c for c in texte
        )
        if self.minuscules:
            texte = texte.lower()
        if self.retirer_accents:
            # NFD puis suppression des marques non espacantes : c'est ce que fait
            # `strip_accents`. « é » précomposé et « e » + U+0301 doivent donner
            # le MÊME identifiant, et c'est un des cas limites de la table de
            # parité.
            texte = "".join(
                c for c in unicodedata.normalize("NFD", texte)
                if unicodedata.category(c) != "Mn"
            )
        return texte

    # ── BertPreTokenizer : blancs, puis ponctuation isolée ────────────────────

    def _pre_decouper(self, texte: str) -> list[str]:
        mots: list[str] = []
        for brut in texte.split():
            courant = ""
            for caractere in brut:
                if _est_ponctuation(caractere):
                    if courant:
                        mots.append(courant)
                        courant = ""
                    mots.append(caractere)
                else:
                    courant += caractere
            if courant:
                mots.append(courant)
        return mots

    # ── WordPiece : plus long préfixe d'abord ─────────────────────────────────

    def _decouper_mot(self, mot: str) -> list[int]:
        """Greedy longest-match-first. Un seul morceau introuvable → `[UNK]` pour
        le mot ENTIER, pas pour le morceau : c'est le comportement de référence,
        et le confondre change la tokenisation de tout mot rare.
        """
        if len(mot) > MAX_CARACTERES_PAR_MOT:
            return [self.inconnu]
        identifiants: list[int] = []
        debut = 0
        while debut < len(mot):
            fin = len(mot)
            trouve: int | None = None
            while debut < fin:
                morceau = mot[debut:fin] if debut == 0 else "##" + mot[debut:fin]
                identifiant = self.vocabulaire.get(morceau)
                if identifiant is not None:
                    trouve = identifiant
                    break
                fin -= 1
            if trouve is None:
                return [self.inconnu]
            identifiants.append(trouve)
            debut = fin
        return identifiants

    # ── API ───────────────────────────────────────────────────────────────────

    def encoder(self, texte: str, longueur_max: int = LONGUEUR_MAX) -> list[int]:
        """Identifiants d'un texte, `[CLS]` et `[SEP]` compris, tronqués.

        La troncature retire du CONTENU, jamais les jetons spéciaux : le modèle a
        été entraîné avec eux, et une séquence qui perdrait son `[SEP]` final
        produirait un vecteur subtilement différent — le genre d'écart qui ne se
        voit pas sur un test à trois mots et décale toute une réindexation.
        """
        identifiants: list[int] = []
        for mot in self._pre_decouper(self._normaliser(texte)):
            identifiants.extend(self._decouper_mot(mot))
        del identifiants[max(0, longueur_max - 2):]
        return [self.debut, *identifiants, self.fin]

    def encoder_lot(self, textes: list[str],
                    longueur_max: int = LONGUEUR_MAX) -> tuple[list[list[int]], int]:
        """Un lot, et la longueur du plus long — le remplissage est fait par
        l'appelant, qui a besoin du masque d'attention de toute façon.
        """
        lots = [self.encoder(t, longueur_max) for t in textes]
        return lots, (max((len(x) for x in lots), default=0))
