import type { ReactNode } from 'react'

interface EntityCardProps {
  /** 2 caractères, ex. "Co", "Kh", "@w" — dérivé du nom/trigger par l'appelant. */
  badgeLabel: string
  /** Classe Tailwind du badge (token de couleur du dépôt, jamais une couleur en dur). */
  badgeColorClass?: string
  title: string
  description: string
  /** Bouton, toggle, ou groupe d'actions rendu en pied de carte. */
  footer: ReactNode
  /** Skill/préfixe désactivé — grisé (même traitement que l'interrupteur
   *  général de l'onglet Tool calling : opacité réduite, jamais caché). */
  muted?: boolean
}

/**
 * Carte réutilisable — Catalogue, Préfixes & commandes, et à terme Tool
 * calling (cf. CLAUDE.md, règle de non-duplication) partagent ce seul
 * composant plutôt que trois variantes quasi identiques.
 */
export default function EntityCard({
  badgeLabel,
  badgeColorClass = 'bg-accent/15 text-accent',
  title,
  description,
  footer,
  muted = false,
}: EntityCardProps) {
  return (
    <div
      className={`flex flex-col gap-3 p-4 bg-surface border border-line rounded-md shadow-sm transition-opacity duration-150 ${muted ? 'opacity-50' : ''}`}
    >
      <div className="flex items-start gap-2.5">
        <span
          className={`shrink-0 inline-flex items-center justify-center w-8 h-8 rounded-md text-xs font-semibold ${badgeColorClass}`}
        >
          {badgeLabel}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-primary truncate">{title}</p>
          <p className="text-xs text-muted/80 line-clamp-2 mt-0.5">{description}</p>
        </div>
      </div>
      <div className="mt-auto">{footer}</div>
    </div>
  )
}
