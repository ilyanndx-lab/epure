r"""Tokenizer LaTeX et règles de normalisation.

Ce module décide de tout : la mesure 1 (ExpRate) compare des chaînes
normalisées, la mesure 2 (taxonomie) compare des multi-ensembles de ces
mêmes tokens. Une tokenisation grossière rend la taxonomie — et donc la
porte de décision n°1 — inexploitable. Les choix ci-dessous sont figés
volontairement, pas laissés à l'implicite d'un `.split()` :

- `\commande` est un seul token (`\frac`, `\int`, ...).
- `{` et `}` sont chacun un token à part entière (ils portent la portée).
- un nombre entier consécutif (`123`) est UN token, pas trois chiffres.
- `^` et `_` sont chacun un token.

Limite connue, non corrigée (jugée non bloquante sur un corpus de maths de
prépa avec peu de grands nombres) : un nombre à N chiffres totalement
différent d'un autre (31415 vs 92653) compte comme une différence de UN
token dans le multi-ensemble, donc classé « glyphe » alors que visuellement
l'écart est énorme. Si le corpus contient un jour des nombres longs et
fréquemment mal reconnus, il faudra soit tokeniser chiffre par chiffre, soit
donner un poids à la distance d'édition interne au token nombre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_MOTIF_TOKEN = re.compile(
    r"""
    \\[a-zA-Z]+      # commande LaTeX : \frac, \int, \cdot, \left, ...
  | \\.              # commande à un caractère : \,  \;  \!  \{  \}  \$
  | [{}^_]           # tokens structurels à un caractère
  | [0-9]+           # un nombre entier consécutif = un seul token
  | [a-zA-Z]         # une lettre seule = un token (variable)
  | \S               # tout autre caractère non-espace isolé
    """,
    re.VERBOSE,
)


def tokeniser(latex: str) -> list[str]:
    """Découpe une chaîne LaTeX en tokens selon les règles documentées ci-dessus."""
    return _MOTIF_TOKEN.findall(latex)


@dataclass
class ResultatRegle:
    nom: str
    nb_applications: int = 0


@dataclass
class JournalNormalisation:
    """Compte, par règle, le nombre de fois où elle a changé quelque chose.

    Sert à juger si la normalisation est trop généreuse (cf. contrainte du
    banc) : si une règle change 40 des 50 expressions, elle mérite un
    examen avant d'être créditée dans l'ExpRate normalisé.
    """

    regles: dict[str, ResultatRegle] = field(default_factory=dict)

    def enregistrer(self, nom: str, a_change: bool) -> None:
        regle = self.regles.setdefault(nom, ResultatRegle(nom))
        if a_change:
            regle.nb_applications += 1

    def vers_dict(self) -> dict[str, int]:
        return {nom: r.nb_applications for nom, r in self.regles.items()}


def _retirer_espacements_fins(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in (r"\,", r"\;", r"\!", r"\ ", r"\quad", r"\qquad")]


def _retirer_left_right(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in (r"\left", r"\right")]


def _dfrac_vers_frac(tokens: list[str]) -> list[str]:
    return [r"\frac" if t == r"\dfrac" else t for t in tokens]


def _cdot_vers_etoile(tokens: list[str]) -> list[str]:
    return ["*" if t == r"\cdot" else t for t in tokens]


def _reduire_groupes_token_unique(tokens: list[str]) -> list[str]:
    """`{x}` -> `x` quand le groupe ne contient qu'un seul token."""
    resultat: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        if (
            tokens[i] == "{"
            and i + 2 < n
            and tokens[i + 2] == "}"
        ):
            resultat.append(tokens[i + 1])
            i += 3
            continue
        resultat.append(tokens[i])
        i += 1
    return resultat


# Ordre d'application : chaque règle reçoit la sortie de la précédente.
_REGLES = [
    ("espaces_fins", _retirer_espacements_fins),
    ("left_right", _retirer_left_right),
    ("dfrac_vers_frac", _dfrac_vers_frac),
    ("cdot_vers_etoile", _cdot_vers_etoile),
    ("groupes_token_unique", _reduire_groupes_token_unique),
]


def normaliser(
    latex: str, journal: Optional[JournalNormalisation] = None
) -> list[str]:
    """Applique les règles de normalisation, journalise ce qui a changé."""
    tokens = tokeniser(latex)
    for nom, fonction in _REGLES:
        avant = tokens
        tokens = fonction(tokens)
        if journal is not None:
            journal.enregistrer(nom, avant != tokens)
    return tokens


def tokens_vers_chaine(tokens: list[str]) -> str:
    """Reconstruction canonique pour comparaison de chaînes (pas pour affichage)."""
    return " ".join(tokens)
