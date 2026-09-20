/**
 * Extensions acceptées à l'upload. **Une seule liste**, et c'est le point.
 *
 * Elle était écrite deux fois — l'attribut `accept` du champ de fichier et le
 * filtre de `uploadFiles` — donc un ajout demandait de penser aux deux. Or les
 * deux copies ne disent pas la même chose à l'utilisateur quand elles divergent :
 * `accept` décide de ce que le sélecteur de fichiers PROPOSE, le filtre décide de
 * ce qui part vraiment. Désaccordées, elles produisent un fichier qu'on peut
 * choisir et qui disparaît sans message.
 *
 * Miroir de `SUPPORTED_EXTENSIONS` dans `backend/core/rag.py`, qui reste
 * l'autorité : le backend refuse en 400 ce qu'il ne sait pas lire, et le message
 * de ce 400 est dérivé de sa liste. Celle-ci n'est donc pas une garantie mais une
 * commodité — elle évite de laisser choisir un fichier voué au refus.
 *
 * Extraite de `ModuleBar.tsx` (qui la définissait localement) dans son propre
 * fichier pour que `modules/chat/Component.tsx` puisse la réutiliser (filtrer
 * un collage Ctrl+V avant `uploadFiles`) sans faire de `ModuleBar.tsx` un
 * module mixte (composant + constante) : `react-refresh/only-export-components`
 * l'interdit en ERREUR, pas en avertissement — même raison que `effort.ts`.
 */
export const EXTENSIONS_ACCEPTEES = [
  'pdf', 'docx', 'pptx', 'xlsx', 'txt', 'md', 'csv', 'json',
  'png', 'jpg', 'jpeg', 'webp',
] as const

export const ACCEPT_FICHIERS = EXTENSIONS_ACCEPTEES.map(e => '.' + e).join(',')
