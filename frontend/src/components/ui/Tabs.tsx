interface Tab {
  id: string
  label: string
}

interface TabsProps {
  tabs: Tab[]
  active: string
  onChange: (id: string) => void
}

export default function Tabs({ tabs, active, onChange }: TabsProps) {
  return (
    <div className="flex items-center gap-1 border-b border-line">
      {tabs.map(t => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`px-3 py-2 text-sm transition-colors duration-150 border-b-2 -mb-px ${
            active === t.id
              ? 'border-accent text-primary font-medium'
              : 'border-transparent text-muted hover:text-secondary'
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}
