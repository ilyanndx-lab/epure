import { useState, useEffect } from 'react'

type Module = 'chat' | 'kholle' | 'flashcards' | 'admin' | 'settings'

interface SidebarProps {
  activeModule: Module
  onModuleChange: (m: Module) => void
}

const MODULES: { id: Module; label: string }[] = [
  { id: 'chat', label: 'chat' },
  { id: 'kholle', label: 'kholle' },
  { id: 'flashcards', label: 'flashcards' },
  { id: 'admin', label: 'admin' },
]

const API = 'http://localhost:8000'

export default function Sidebar({ activeModule, onModuleChange }: SidebarProps) {
  const [ollamaOk, setOllamaOk] = useState<boolean | null>(null)
  const [modelName, setModelName] = useState('')

  useEffect(() => {
    const check = () => {
      fetch(`${API}/health`)
        .then(r => r.json())
        .then((d: { ollama: boolean; model: string }) => {
          setOllamaOk(d.ollama)
          setModelName(d.model)
        })
        .catch(() => setOllamaOk(false))
    }
    check()
    const id = setInterval(check, 10_000)
    return () => clearInterval(id)
  }, [])

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
      <div className="px-2 pb-2 border-t border-[#1e1e1e] pt-3">
        <button
          onClick={() => onModuleChange('settings')}
          className={`w-full text-left px-3 py-2 rounded text-xs font-mono transition-colors ${
            activeModule === 'settings'
              ? 'bg-[#1a1a1a] text-[#e0e0e0]'
              : 'text-[#555] hover:text-[#aaa] hover:bg-[#141414]'
          }`}
        >
          ⚙ profil
        </button>
      </div>
      <div className="px-4 py-4 border-t border-[#1e1e1e] space-y-2">
        {/* Ollama status indicator */}
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              ollamaOk === null
                ? 'bg-[#3a3a3a]'
                : ollamaOk
                ? 'bg-[#3a6a3a]'
                : 'bg-[#6a3a3a]'
            }`}
          />
          <span className="text-[10px] font-mono text-[#2a2a2a] truncate">
            {ollamaOk === null
              ? '...'
              : ollamaOk
              ? (modelName.split(':')[0] || 'ollama')
              : 'ollama inactif'}
          </span>
        </div>
        <span className="text-xs font-mono text-[#1e1e1e]">épure</span>
        <span className="text-[10px] font-mono text-[#2a2a2a]">
          build {import.meta.env.VITE_BUILD_TIME}
        </span>
      </div>
    </aside>
  )
}
