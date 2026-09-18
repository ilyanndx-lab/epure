interface Tab {
  id: string
  label: string
}

interface TabsProps {
  tabs: Tab[]
  active: string
  onChange: (id: string) => void
}

/**
 * Barre d'onglets en pilule — piste `bg-elevated` arrondie, onglet actif en
 * dégradé violet→turquoise. Même vocabulaire que les pilules d'effort du chat
 * (`Component.tsx` : `rounded-full` + `bg-gradient-primary`/`text-on-accent`
 * pour l'actif, `text-muted` pour le reste), pour rester visuellement une
 * seule famille de composant plutôt que deux styles de pilule dans le dépôt.
 */
export default function Tabs({ tabs, active, onChange }: TabsProps) {
  return (
    <div className="inline-flex items-center gap-1 p-1 bg-elevated border border-line rounded-full">
      {tabs.map(t => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`px-3.5 py-1.5 rounded-full text-sm font-medium whitespace-nowrap transition-colors duration-150 ${
            active === t.id
              ? 'bg-gradient-primary text-on-accent shadow-sm'
              : 'text-muted hover:text-secondary hover:bg-surface'
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}
