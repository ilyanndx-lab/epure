/**
 * Commandes du chat (« @ » et « / ») — extraites de Component.tsx.
 *
 * Pourquoi un fichier séparé : `react-refresh/only-export-components` interdit
 * qu'un fichier exportant un composant exporte aussi autre chose. Un module
 * mixte fait perdre le Fast Refresh — à la moindre édition, Vite recharge le
 * composant au lieu de préserver son état, ce qui est particulièrement pénible
 * sur le chat (conversation en cours perdue à chaque sauvegarde).
 *
 * `SKILL_COMMANDS` n'est consommé nulle part dans le dépôt à ce jour ; il est
 * conservé tel quel (surface publique inchangée) plutôt que supprimé au passage.
 */

export const AT_COMMANDS = [
  { trigger: '@cours',      desc: 'RAG sur tous les fichiers indexés' },
  { trigger: '@strict',     desc: 'Réponse concise, sans intro' },
  { trigger: '@mémoire',    desc: 'Affiche le contexte mémoire actuel' },
  { trigger: '@historique', desc: 'Recherche dans les échanges passés [sujet]' },
  { trigger: '@web',        desc: 'Recherche web complémentaire avant la réponse [sujet]' },
] as const

/**
 * Commandes du CŒUR : elles existent quels que soient les modules installés.
 *
 * `/kholle` et `/flashcards` étaient ici, en dur. Le chat les annonçait donc à
 * tout le monde, y compris sur une installation où ces modules n'existent pas —
 * l'utilisateur tapait une commande qui ne pouvait rien ouvrir. Elles sont
 * désormais dérivées des modules réellement installés, cf. `moduleCommands`.
 */
export const SLASH_COMMANDS = [
  { trigger: '/résumé',  desc: 'Résumé des fichiers actifs (streaming)' },
  { trigger: '/modèle',  desc: 'Change le modèle actif [nom]' },
  { trigger: '/lacunes', desc: 'Lacunes + erreurs des 7 derniers jours' },
  { trigger: '/direct',  desc: 'Bypass orchestrateur — 1 modèle direct [message]' },
] as const

export const SKILL_COMMANDS = { at: AT_COMMANDS, slash: SLASH_COMMANDS }

/** Ce qu'il faut d'un module pour en faire une commande. */
export interface ModuleOuvrable {
  id: string
  nom: string
}

/** Modules qu'une commande `/` ne doit pas proposer d'ouvrir. */
const NON_OUVRABLES = new Set(['chat'])

/**
 * Commandes d'ouverture, DÉRIVÉES des modules installés.
 *
 * C'est le principe d'Épure appliqué à la barre de commandes : le cœur ne
 * connaît aucun module par son nom, il expose ce qui est là. Installer un
 * module lui donne sa commande ; le désinstaller la retire. Aucune liste à
 * tenir à jour, donc aucune liste qui puisse mentir.
 */
export function moduleCommands(modules: readonly ModuleOuvrable[]) {
  return modules
    .filter(m => !NON_OUVRABLES.has(m.id))
    .map(m => ({ trigger: `/${m.id}`, desc: `Ouvre ${m.nom}` }))
}

/** Liste complète affichée à l'utilisateur : modules d'abord, cœur ensuite. */
export function allSlashCommands(modules: readonly ModuleOuvrable[]) {
  return [...moduleCommands(modules), ...SLASH_COMMANDS]
}
