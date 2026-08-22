/**
 * Remplaçant de `Workshop.tsx` dans un paquet distribué — jamais rendu.
 *
 * Pourquoi ce fichier existe alors que `registry.ts` ne référence déjà plus
 * l'Atelier quand `ATELIER_PRESENT` est faux : rolldown **émet un chunk pour
 * tout `import()` qu'il rencontre**, même dans une branche morte. Mesuré : le
 * paquet contenait un `Workshop-*.js` de 26,1 ko, non référencé par l'index mais
 * bien écrit sur le disque, avec le code source de l'Atelier dedans. Orphelin
 * n'est pas absent — c'était lisible par le destinataire.
 *
 * `vite.config.ts` détourne donc le specifier vers ce fichier quand
 * `VITE_ATELIER=0`. Résultat mesuré : plus aucun chunk `Workshop-*.js` du tout —
 * ce module est assez trivial pour que rolldown l'insère directement.
 *
 * Le contrôle automatique porte sur la FORME du drapeau et sur la présence de
 * l'alias (`backend/test_paquet.py`), pas sur le bundle : la suite Python ne
 * lance pas npm. Le contrôle sur le bundle lui-même appartient au job `frontend`
 * de la CI, qui construit déjà.
 */
export default function AtelierAbsent() {
  return null
}
