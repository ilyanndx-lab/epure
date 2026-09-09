r"""Banque d'expressions LaTeX pour la dictée inversée — module `encre`, phase 3.

**Point de départ, pas une référence figée.** Ce premier jeu (~40 expressions) vise
la diversité de notation d'une prépa générique — fractions, sommes/intégrales avec
bornes, matrices, indices/exposants imbriqués, lettres grecques, vecteurs,
limites — sans présumer d'une filière particulière (CLAUDE.md §1 : le cœur
d'Épure ne présume aucune matière). Ilyann pourra la réviser librement : ajouter,
retirer, reformuler une entrée n'a besoin de rien d'autre qu'éditer la liste
ci-dessous.

**Fichier Python et non JSON, délibérément.** Chaque backslash LaTeX (`\frac`,
`\sum`, `\alpha`…) devrait être doublé dans une chaîne JSON — un aller-retour
pénible sur un fichier explicitement destiné à être retouché à la main. Des
chaînes brutes Python (`r"..."`) le lisent tel qu'on l'écrirait sur une copie.

**L'identifiant d'une expression est dérivé de son CONTENU** (`identifiant_expression`,
un hash tronqué), jamais de sa position dans la liste : la progression persistée
(`ExemplesEncreEngine`, phase 3) tient une liste de ces identifiants, et un index
de liste se déciderait à la première réorganisation de ce fichier — exactement le
piège que CLAUDE.md §3.5 décrit pour les chemins figés, transposé à une donnée.
Conséquence acceptée : reformuler une expression existante (plutôt que d'en
ajouter une nouvelle) lui donne un nouvel identifiant, donc la remet dans le tirage
comme si elle n'avait jamais été faite. C'est le bon arbitrage : le contenu réel
proposé à la copie a changé, la progression ne doit pas prétendre le contraire.
"""

import hashlib

#: Les expressions elles-mêmes. Pas de catégorie ni de métadonnée : la sélection
#: (:func:`choisir_expression`) ne discrimine pas par thème, seulement par
#: « déjà faite ou non » — inutile de porter une structure que rien ne lit.
BANQUE_DICTEE_ENCRE: tuple[str, ...] = (
    # Fractions
    r"\frac{1}{2}",
    r"\frac{a+b}{c-d}",
    r"\frac{x^2}{a^2} + \frac{y^2}{b^2} = 1",
    r"\frac{\partial f}{\partial x}",
    r"\frac{d^2y}{dx^2} + \omega^2 y = 0",
    # Sommes et produits, bornes comprises
    r"\sum_{k=0}^{n} k^2",
    r"\sum_{i=1}^{n} \frac{1}{i}",
    r"\sum_{k=0}^{n} \binom{n}{k} x^k",
    r"\prod_{k=1}^{n} k = n!",
    r"\bigcup_{n=1}^{+\infty} A_n",
    r"\bigcap_{i \in I} B_i",
    # Intégrales
    r"\int_0^1 x^2\,dx",
    r"\int_{-\infty}^{+\infty} e^{-x^2}\,dx",
    r"\iint_D f(x,y)\,dx\,dy",
    # Limites
    r"\lim_{x \to 0} \frac{\sin x}{x}",
    r"\lim_{n \to +\infty} \left(1+\frac{1}{n}\right)^n",
    r"\forall \varepsilon > 0,\ \exists \delta > 0",
    # Matrices
    r"\begin{pmatrix} 1 & 0 \\ 0 & 1 \end{pmatrix}",
    r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}",
    r"A = \begin{pmatrix} 1 & 2 & 3 \\ 4 & 5 & 6 \\ 7 & 8 & 9 \end{pmatrix}",
    r"\det(A) = ad - bc",
    r"A^{-1} = \frac{1}{\det(A)} \, \mathrm{com}(A)^T",
    r"\lambda_1, \lambda_2 \text{ valeurs propres de } A",
    # Indices et exposants imbriqués
    r"x_n = x_{n-1} + \frac{1}{n^2}",
    r"u_{n+1} = 2u_n - 3",
    r"a_{i,j}",
    r"e^{i\pi} + 1 = 0",
    r"\binom{n}{k} = \frac{n!}{k!(n-k)!}",
    r"P(X = k) = \binom{n}{k}\, p^k (1-p)^{n-k}",
    # Lettres grecques
    r"\alpha + \beta = \gamma",
    r"\theta \equiv \pi \pmod{2\pi}",
    r"\cos^2\theta + \sin^2\theta = 1",
    r"\tan\left(\frac{\pi}{4}\right) = 1",
    # Vecteurs
    r"\vec{u} + \vec{v}",
    r"\vec{AB} = \vec{OB} - \vec{OA}",
    r"\|\vec{u}\|",
    r"\nabla f = \left(\frac{\partial f}{\partial x}, \frac{\partial f}{\partial y}\right)",
    # Divers (ensembles, complexes, équations)
    r"\sqrt{a^2 + b^2}",
    r"\ln(xy) = \ln x + \ln y",
    r"z = a + ib, \quad |z| = \sqrt{a^2+b^2}",
    r"\overline{z} = a - ib",
    r"\mathbb{R}^n",
    r"f: \mathbb{R} \to \mathbb{R}",
    r"y'' - 3y' + 2y = 0",
    r"\begin{cases} x + y = 1 \\ x - y = 3 \end{cases}",
)


def identifiant_expression(latex: str) -> str:
    """Identifiant stable d'une expression, dérivé de son CONTENU.

    Sha1 tronqué à 12 caractères hexadécimaux : largement suffisant pour ~40
    entrées (collision improbable), et court dans un fichier de progression
    destiné à être relu.
    """
    return hashlib.sha1(latex.encode("utf-8")).hexdigest()[:12]


def expressions() -> list[dict]:
    """La banque, sous la forme ``[{"id", "latex"}, ...]``.

    Fonction et non constante calculée au chargement : elle dérive de
    :data:`BANQUE_DICTEE_ENCRE`, qui est ce qu'Ilyann révise — recalculer à
    chaque appel évite un second endroit à tenir synchronisé si la banque change
    en cours de process (rechargement à chaud d'un module, notamment).
    """
    return [{"id": identifiant_expression(latex), "latex": latex}
            for latex in BANQUE_DICTEE_ENCRE]


def choisir_expression(deja_faites) -> dict:
    """Une expression au hasard, en excluant ``deja_faites`` — sauf épuisement.

    ``deja_faites`` : un ensemble d'identifiants (``frozenset``, ``set`` ou toute
    collection supportant ``in``). Sans répétition tant qu'il reste une
    expression non encore faite ; une fois la banque épuisée, retombe sur
    l'ensemble complet plutôt que de lever — la fonction est pure et ne décide
    PAS de réinitialiser la progression persistée, c'est à l'appelant
    (``ExemplesEncreEngine``) de le faire une fois qu'il a constaté l'épuisement,
    au moment où il enregistre la validation qui vient de tout compléter.

    Fonction PURE : aucun accès disque, aucun état — c'est ce qui la rend
    testable en drainant la banque sans mocker ``random``, et c'est aussi ce qui
    permet à ``GET /encre/entrainement/dictee/expression`` de rester une lecture
    sans effet de bord (le reset vit dans ``valider_dictee``, pas ici).
    """
    import random  # noqa: PLC0415 — le seul appelant qui a besoin d'aléatoire

    toutes = expressions()
    candidates = [e for e in toutes if e["id"] not in deja_faites]
    return random.choice(candidates or toutes)
