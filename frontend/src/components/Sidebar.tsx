import { useState, useEffect } from 'react'
import { Settings as SettingsIcon, Hammer } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { ThemeToggle } from './ui'
import { useInstanceConfig } from '../instance'
import { useModules, resolveIcon } from '../modules'

interface SidebarProps {
  activeModule: string
  onModuleChange: (m: string) => void
}

const API = 'http://localhost:8000'

function NavItem({
  active, label, icon: Icon, onClick,
}: {
  active: boolean
  label: string
  icon: LucideIcon
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={`relative w-full flex items-center gap-2.5 px-3 py-2 rounded-sm text-sm transition-colors duration-150 ${
        active
          ? 'bg-accent/10 text-primary font-medium'
          : 'text-muted hover:text-secondary hover:bg-elevated'
      }`}
    >
      {active && (
        <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-accent" />
      )}
      <Icon size={15} className={active ? 'text-accent' : ''} />
      {label}
    </button>
  )
}

export default function Sidebar({ activeModule, onModuleChange }: SidebarProps) {
  const config = useInstanceConfig()
  const modules = useModules()
  const [ollamaOk, setOllamaOk] = useState<boolean | null>(null)
  const [modelName, setModelName] = useState('')
  const [flmOk, setFlmOk] = useState<boolean | null>(null)

  useEffect(() => {
    const check = () => {
      fetch(`${API}/health`)
        .then(r => r.json())
        .then((d: { ollama: boolean; model: string; flm?: boolean }) => {
          setOllamaOk(d.ollama)
          setModelName(d.model)
          setFlmOk(d.flm ?? null)
        })
        .catch(() => setOllamaOk(false))
    }
    check()
    const id = setInterval(check, 10_000)
    return () => clearInterval(id)
  }, [])

  // Modules visibles, DANS L'ORDRE de modules_activés (réordonnable depuis
  // Réglages) : on mappe la liste ordonnée vers les manifestes, en ne gardant
  // que ceux actifs au catalogue. settings est exclu (bouton Profil dédié).
  const byId = new Map(modules.map(m => [m.id, m]))
  const navModules = config.modules_activés
    .map(id => byId.get(id))
    .filter((m): m is NonNullable<typeof m> => !!m && m.id !== 'settings' && m.status === 'active')

  const settingsModule = modules.find(m => m.id === 'settings')

  return (
    <aside className="w-52 shrink-0 bg-surface border-r border-line flex flex-col">
      {/* Logo */}
      <div className="px-4 py-5 border-b border-line flex items-center justify-between">
        <span className="text-lg font-semibold text-gradient select-none lowercase">
          {config.nom_affiché || 'épure'}
        </span>
        <ThemeToggle />
      </div>

      <nav className="flex-1 px-2 py-3 space-y-0.5">
        {navModules.map(m => (
          <NavItem
            key={m.id}
            active={activeModule === m.id}
            label={m.nom}
            icon={resolveIcon(m.icon)}
            onClick={() => onModuleChange(m.id)}
          />
        ))}
      </nav>

      <div className="px-2 pb-2 border-t border-line pt-2 space-y-0.5">
        <NavItem
          active={activeModule === 'workshop'}
          label="Atelier"
          icon={Hammer}
          onClick={() => onModuleChange('workshop')}
        />
        <NavItem
          active={activeModule === 'settings'}
          label={settingsModule?.nom ?? 'Profil'}
          icon={settingsModule ? resolveIcon(settingsModule.icon) : SettingsIcon}
          onClick={() => onModuleChange('settings')}
        />
      </div>

      {/* Santé serveurs */}
      <div className="px-4 py-3 border-t border-line space-y-1.5">
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              ollamaOk === null ? 'bg-line' : ollamaOk ? 'bg-success' : 'bg-error'
            }`}
          />
          <span className="text-xs font-mono text-muted truncate">
            {ollamaOk === null ? '...' : ollamaOk ? (modelName.split(':')[0] || 'ollama') : 'ollama inactif'}
          </span>
        </div>
        {flmOk !== null && (
          <div className="flex items-center gap-2">
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${flmOk ? 'bg-accent' : 'bg-line'}`} />
            <span className="text-xs font-mono text-muted truncate">
              {flmOk ? 'flm (npu)' : 'flm inactif'}
            </span>
          </div>
        )}
        <span className="block text-xs font-mono text-muted/60 pt-1">
          build {import.meta.env.VITE_BUILD_TIME}
        </span>
      </div>
    </aside>
  )
}
