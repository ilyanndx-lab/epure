import { useState } from 'react'
import { Sparkles } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'

const API = 'http://localhost:8000'

/**
 * Composant du module de démonstration « hello ».
 * Découvert automatiquement par registry.ts via import.meta.glob — aucun import
 * à écrire dans App.tsx / Sidebar.tsx.
 */
export default function HelloModule(_props: SharedModuleProps) {
  const [result, setResult] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const ping = async () => {
    setLoading(true)
    try {
      const res = await fetch(`${API}/hello/ping`)
      setResult(JSON.stringify(await res.json()))
    } catch {
      setResult('erreur réseau')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <Sparkles size={18} className="text-accent" /> Module Hello
      </h1>
      <p className="text-sm text-secondary max-w-lg leading-relaxed">
        Module de démonstration chargé dynamiquement : composant généré (découvert
        par <code className="font-mono text-xs">import.meta.glob</code>) et routeur
        backend monté automatiquement. Aucune édition d'App.tsx, Sidebar.tsx ou
        main.py n'a été nécessaire pour l'ajouter.
      </p>
      <div className="flex items-center gap-3">
        <button
          onClick={ping}
          disabled={loading}
          className="px-3 py-2 rounded-md bg-gradient-primary text-on-accent text-sm shadow-sm hover:opacity-90 disabled:opacity-40 transition-all duration-150"
        >
          {loading ? '...' : 'GET /hello/ping'}
        </button>
        {result && (
          <code className="text-xs font-mono text-secondary break-all">{result}</code>
        )}
      </div>
    </main>
  )
}
