/**
 * L'Atelier est-il présent dans cette construction du frontend ?
 *
 * Vrai partout sauf dans un paquet distribué (`docs/distribution-empaquetee.md`) :
 * là, Ilyann crée les modules et le destinataire les utilise — l'Atelier n'a
 * aucune raison d'être chez lui, et le laisser afficherait quatre moteurs de
 * génération « indisponibles » dans ses Réglages.
 *
 * Constante de compilation, et c'est le point : `VITE_ATELIER` est remplacée par
 * un littéral au build, donc `ATELIER_PRESENT` vaut `false` en dur et le
 * ramasse-miettes de rolldown élimine tout ce qui n'est plus atteignable. Le
 * composant `Workshop.tsx` est déjà un chunk paresseux : couper ses points
 * d'entrée (ce fichier est importé par `registry.ts`, `Sidebar.tsx`,
 * `settings/Component.tsx` et `ModuleErrorBoundary.tsx`) le fait disparaître du
 * paquet au lieu de simplement le cacher. La différence compte : un écran
 * seulement caché reste du code lisible chez le destinataire.
 *
 * Le pendant backend est `EPURE_ATELIER=0`, qui fait répondre 404 aux routes
 * `/workshop*`. Les deux doivent être posés ensemble — `tools/faire_paquet.py`
 * s'en charge —, mais aucun ne dépend de l'autre : le backend refuse même si un
 * front bavard demandait quand même.
 *
 * Comme `VITE_API_URL`, la valeur retenue est `0` et non la chaîne vide : sous
 * Windows `$env:VITE_ATELIER = ''` supprime la variable (cf. `api.ts`).
 *
 * ⚠️ **La comparaison doit rester pliable en constante, donc directe.** Écrite
 * `…VITE_ATELIER?.trim() !== '0'`, elle a l'air plus tolérante et elle ne l'est
 * pas : vite remplace bien `import.meta.env.VITE_ATELIER` par `"0"`, mais
 * rolldown ne plie pas un appel de méthode, donc `ATELIER_PRESENT` reste une
 * valeur calculée à l'exécution, la branche du ternaire de `registry.ts` reste
 * atteignable, et le chunk `Workshop-*.js` **est quand même émis** — mesuré :
 * 26,76 ko d'Atelier dans un paquet censé ne pas en avoir. L'écran était caché,
 * pas retiré. **Ne pas ajouter de `.trim()`, de `String(...)` ni de valeur par
 * défaut ici.** `backend/test_paquet.py` vérifie que cette ligne reste une
 * comparaison directe — il ne construit pas le bundle (pas de npm dans la suite
 * Python), donc c'est la forme de l'expression qui est l'invariant testé.
 */
export const ATELIER_PRESENT: boolean = import.meta.env.VITE_ATELIER !== '0'
