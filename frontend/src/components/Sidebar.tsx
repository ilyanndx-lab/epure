import { useState, useEffect } from 'react'
import {
  MessageSquare, GraduationCap, Layers, Code2, FileSearch,
  FolderCog, Clock, Settings as SettingsIcon,
} from 'lucide-react'
import { ThemeToggle } from './ui'

type Module = 'chat' | 'kholle' | 'flashcards' | 'code' | 'docs' | 'admin' | 'history' | 'settings'

interface SidebarProps {
  activeModule: Module
  onModuleChange: (m: Module) => void
}

const MODULES: { id: Module; label: string; icon: typeof MessageSquare }[] = [
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'kholle', label: 'Kholle', icon: GraduationCap },
  { id: 'flashcards', label: 'Flashcards', icon: Layers },
  { id: 'code', label: 'Code', icon: Code2 },
  { id: 'docs', label: 'Docs', icon: FileSearch },
  { id: 'admin', label: 'Admin', icon: FolderCog },
  { id: 'history', label: 'Historique', icon: Clock },
]

const API = 'http://localhost:8000'

function NavItem({
  active, label, icon: Icon, onClick,
}: {
  active: boolean
  label: string
  icon: typeof MessageSquare
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

  return (
    <aside className="w-52 shrink-0 bg-surface border-r border-line flex flex-col">
      {/* Logo */}
      <div className="px-4 py-5 border-b border-line flex items-center justify-between">
        <span className="text-lg font-semibold text-gradient select-none">épure</span>
        <ThemeToggle />
      </div>

      <nav className="flex-1 px-2 py-3 space-y-0.5">
        {MODULES.map(({ id, label, icon }) => (
          <NavItem
            key={id}
            active={activeModule === id}
            label={label}
            icon={icon}
            onClick={() => onModuleChange(id)}
          />
        ))}
      </nav>

      <div className="px-2 pb-2 border-t border-line pt-2">
        <NavItem
          active={activeModule === 'settings'}
          label="Profil"
          icon={SettingsIcon}
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
