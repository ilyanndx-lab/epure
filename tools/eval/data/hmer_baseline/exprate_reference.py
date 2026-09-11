"""ExpRate (taux d'expressions exactes), brut et normalisé, avec intervalle de Wilson.

À n≈50, l'intervalle de Wilson à 95% vaut environ ±14 points. Ce module
n'essaie pas de cacher ça : chaque taux est renvoyé avec son intervalle, et
`rapport.py` doit citer explicitement ce chiffre pour empêcher toute
comparaison à deux chiffres près entre modèles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from latex_tokenise import JournalNormalisation, normaliser, tokens_vers_chaine


@dataclass
class TauxAvecIntervalle:
    k: int  # nombre de succès
    n: int  # nombre total
    taux: float
    borne_basse: float
    borne_haute: float

    def largeur(self) -> float:
        return self.borne_haute - self.borne_basse


def intervalle_wilson(k: int, n: int, z: float = 1.96) -> TauxAvecIntervalle:
    """Intervalle de Wilson à 95% (z=1.96 par défaut) pour une proportion k/n."""
    if n == 0:
        return TauxAvecIntervalle(k=0, n=0, taux=0.0, borne_basse=0.0, borne_haute=0.0)
    p = k / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    marge = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    basse = (centre - marge) / denom
    haute = (centre + marge) / denom
    return TauxAvecIntervalle(
        k=k, n=n, taux=p, borne_basse=max(0.0, basse), borne_haute=min(1.0, haute)
    )


@dataclass
class ResultatExpRate:
    brut: TauxAvecIntervalle
    normalise: TauxAvecIntervalle
    journal_normalisation: dict[str, int]
    passes_a_juste_grace_normalisation: list[str]  # noms de fichiers


def calculer_exprate(
    predictions: list[dict],
) -> ResultatExpRate:
    """predictions: liste de {"fichier", "latex_predit", "latex_verite"}."""
    journal = JournalNormalisation()
    k_brut = 0
    k_normalise = 0
    passes_grace_normalisation = []
    n = len(predictions)

    for entree in predictions:
        pred = entree["latex_predit"] or ""
        verite = entree["latex_verite"]

        brut_egal = pred.strip() == verite.strip()
        if brut_egal:
            k_brut += 1

        tokens_pred = normaliser(pred, journal)
        tokens_verite = normaliser(verite, journal)
        normalise_egal = tokens_vers_chaine(tokens_pred) == tokens_vers_chaine(tokens_verite)
        if normalise_egal:
            k_normalise += 1
            if not brut_egal:
                passes_grace_normalisation.append(entree["fichier"])

    return ResultatExpRate(
        brut=intervalle_wilson(k_brut, n),
        normalise=intervalle_wilson(k_normalise, n),
        journal_normalisation=journal.vers_dict(),
        passes_a_juste_grace_normalisation=passes_grace_normalisation,
    )
