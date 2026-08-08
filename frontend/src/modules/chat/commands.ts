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

export const SLASH_COMMANDS = [
  { trigger: '/kholle',     desc: 'Ouvre le module Kholle [matière?]' },
  { trigger: '/flashcards', desc: 'Ouvre les Flashcards [source?]' },
  { trigger: '/résumé',     desc: 'Résumé des fichiers actifs (streaming)' },
  { trigger: '/modèle',     desc: 'Change le modèle actif [nom]' },
  { trigger: '/lacunes',    desc: 'Lacunes + erreurs des 7 derniers jours' },
  { trigger: '/direct',     desc: 'Bypass orchestrateur — 1 modèle direct [message]' },
] as const

export const SKILL_COMMANDS = { at: AT_COMMANDS, slash: SLASH_COMMANDS }
