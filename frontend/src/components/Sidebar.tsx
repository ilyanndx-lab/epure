type Module = 'chat' | 'kholle'

interface SidebarProps {
  activeModule: Module
  onModuleChange: (m: Module) => void
}

const MODULES: { id: Module; label: string }[] = [
  { id: 'chat', label: 'chat' },
  { id: 'kholle', label: 'kholle' },
]

export default function Sidebar({ activeModule, onModuleChange }: SidebarProps) {
  return (
    <aside className="w-52 shrink-0 bg-[#0f0f0f] border-r border-[#1e1e1e] flex flex-col">
      <div className="px-4 py-5 border-b border-[#1e1e1e]">
        <span className="text-xs font-mono text-[#444] uppercase tracking-[0.2em]">
          Modules
        </span>
      </div>
      <nav className="flex-1 px-2 py-3 space-y-0.5">
        {MODULES.map(({ id, label }) => (
          <button
            key={id}
            onClick={() => onModuleChange(id)}
            className={`w-full text-left px-3 py-2 rounded text-xs font-mono transition-colors ${
              activeModule === id
                ? 'bg-[#1a1a1a] text-[#e0e0e0]'
                : 'text-[#555] hover:text-[#aaa] hover:bg-[#141414]'
            }`}
          >
            {label}
          </button>
        ))}
      </nav>
      <div className="px-4 py-4 border-t border-[#1e1e1e]">
        <span className="text-xs font-mono text-[#2a2a2a]">épure</span>
      </div>
    </aside>
  )
}
