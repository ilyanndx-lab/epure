import { useState, useEffect } from 'react'
import { Settings as SettingsIcon, Hammer, PanelLeft, Search } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { ThemeToggle, Input } from './ui'
import { API, apiFetch } from '../api'
import { ATELIER_PRESENT } from '../atelier'
import { useInstanceConfig } from '../instance'
import { useModules, resolveIcon, orderedModules } from '../modules'

interface SidebarProps {
  activeModule: string
  onModuleChange: (m: string) => void
  /** Rail repliée à 68px (icônes seules) ou dépliée à 250px. Porté par
   * App.tsx — même pattern que `activeModule`/`zoomByModule`, seul autre
   * état de layout persisté du dépôt. */
  collapsed: boolean
  onToggleCollapsed: () => void
}

function SectionLabel({ children }: { children: string }) {
  return (
    <div className="px-3 pt-3 pb-1.5 font-display text-[11px] font-semibold uppercase tracking-wide text-muted/70">
      {children}
    </div>
  )
}

function NavItem({
  active, label, icon: Icon, onClick, collapsed,
}: {
  active: boolean
  label: string
  icon: LucideIcon
  onClick: () => void
  collapsed: boolean
}) {
  return (
    <button
      onClick={onClick}
      title={collapsed ? label : undefined}
      className={`relative w-full flex items-center gap-2.5 rounded-md text-sm transition-colors duration-150 ${
        collapsed ? 'justify-center px-0 py-2.5' : 'px-3 py-2'
      } ${
        active
          ? 'bg-accent/[0.16] text-primary font-medium'
          : 'text-muted hover:text-secondary hover:bg-elevated'
      }`}
    >
      {active && (
        <span className="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-full bg-gradient-primary" />
      )}
      <Icon size={15} className={active ? 'text-accent-soft shrink-0' : 'shrink-0'} />
      {!collapsed && (
        <span className={`truncate ${active ? 'font-display' : ''}`}>{label}</span>
      )}
    </button>
  )
}

export default function Sidebar({ activeModule, onModuleChange, collapsed, onToggleCollapsed }: SidebarProps) {
  const config = useInstanceConfig()
  const modules = useModules()
  const [ollamaOk, setOllamaOk] = useState<boolean | null>(null)
  const [modelName, setModelName] = useState('')
  const [flmOk, setFlmOk] = useState<boolean | null>(null)
  const [search, setSearch] = useState('')

  useEffect(() => {
    const check = () => {
      apiFetch(`${API}/health`)
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
  const navModules = orderedModules(modules, config.modules_activés)
    .filter(m => m.id !== 'settings')

  // Le filtre de recherche n'a de sens qu'en rail dépliée : repliée, le champ
  // est masqué et il n'y a aucun moyen de le renseigner.
  const filteredNavModules = collapsed || !search.trim()
    ? navModules
    : navModules.filter(m => m.nom.toLowerCase().includes(search.trim().toLowerCase()))

  const settingsModule = modules.find(m => m.id === 'settings')

  const ollamaDot = ollamaOk === null ? 'bg-line' : ollamaOk ? 'bg-success' : 'bg-error'
  const ollamaLabel = ollamaOk === null ? '...' : ollamaOk ? (modelName.split(':')[0] || 'ollama') : 'ollama inactif'

  return (
    <aside
      className={`shrink-0 bg-surface border-r border-line flex flex-col transition-[width] duration-200 ${
        collapsed ? 'w-[68px]' : 'w-[250px]'
      }`}
    >
      {/* Logo + bascule de la rail. Le ThemeToggle n'apparaît que dépliée :
          il reste fonctionnel (jamais retiré), simplement pas dans les 68px
          d'une rail repliée pensée icônes-seules. */}
      <div className={`flex items-center border-b border-line ${collapsed ? 'flex-col gap-2 px-2 py-4' : 'justify-between px-4 py-5'}`}>
        {!collapsed && (
          <span className="font-display text-lg font-semibold text-gradient select-none lowercase">
            {config.nom_affiché || 'épure'}
          </span>
        )}
        <div className={`flex items-center ${collapsed ? '' : 'gap-1'}`}>
          {!collapsed && <ThemeToggle />}
          <button
            onClick={onToggleCollapsed}
            title={collapsed ? 'Déplier la barre latérale' : 'Réduire la barre latérale'}
            className="inline-flex items-center p-1.5 rounded-sm text-muted hover:text-accent2 hover:bg-elevated transition-colors duration-150"
          >
            <PanelLeft size={15} />
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="px-3 pt-3">
          <div className="relative">
            <Search size={13} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
            <Input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Rechercher un module..."
              className="w-full pl-8 text-xs"
            />
          </div>
        </div>
      )}

      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-0.5">
        {!collapsed && <SectionLabel>Espace de travail</SectionLabel>}
        {filteredNavModules.map(m => (
          <NavItem
            key={m.id}
            active={activeModule === m.id}
            label={m.nom}
            icon={resolveIcon(m.icon)}
            onClick={() => onModuleChange(m.id)}
            collapsed={collapsed}
          />
        ))}
      </nav>

      <div className="px-2 pb-2 border-t border-line pt-2 space-y-0.5">
        {!collapsed && <SectionLabel>Système</SectionLabel>}
        {ATELIER_PRESENT && (
          <NavItem
            active={activeModule === 'workshop'}
            label="Atelier"
            icon={Hammer}
            onClick={() => onModuleChange('workshop')}
            collapsed={collapsed}
          />
        )}
        <NavItem
          active={activeModule === 'settings'}
          label={settingsModule?.nom ?? 'Profil'}
          icon={settingsModule ? resolveIcon(settingsModule.icon) : SettingsIcon}
          onClick={() => onModuleChange('settings')}
          collapsed={collapsed}
        />
      </div>

      {/* Santé serveurs — repliée : le point seul, centré, sans nom de modèle. */}
      <div className={`border-t border-line ${collapsed ? 'flex flex-col items-center gap-2 py-3' : 'px-4 py-3 space-y-1.5'}`}>
        <div className={`flex items-center ${collapsed ? '' : 'gap-2'}`}>
          <span
            className={`w-1.5 h-1.5 rounded-full shrink-0 ${ollamaDot}`}
            title={collapsed ? ollamaLabel : undefined}
          />
          {!collapsed && (
            <span className="text-xs font-mono text-muted truncate">{ollamaLabel}</span>
          )}
        </div>
        {flmOk !== null && (
          <div className={`flex items-center ${collapsed ? '' : 'gap-2'}`}>
            <span
              className={`w-1.5 h-1.5 rounded-full shrink-0 ${flmOk ? 'bg-accent' : 'bg-line'}`}
              title={collapsed ? (flmOk ? 'flm (npu)' : 'flm inactif') : undefined}
            />
            {!collapsed && (
              <span className="text-xs font-mono text-muted truncate">
                {flmOk ? 'flm (npu)' : 'flm inactif'}
              </span>
            )}
          </div>
        )}
        {!collapsed && (
          <span className="block text-xs font-mono text-muted/60 pt-1">
            build {import.meta.env.VITE_BUILD_TIME}
          </span>
        )}
      </div>
    </aside>
  )
}
