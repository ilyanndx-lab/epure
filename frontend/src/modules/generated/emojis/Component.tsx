import { useState, useEffect } from 'react'
import { Smile } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { Card } from '../../../components/ui'

const API = 'http://localhost:8000'

/**
 * Composant du module d'affichage d'emojis.
 * Découvert automatiquement par registry.ts via import.meta.glob — aucun import
 * à écrire dans App.tsx / Sidebar.tsx.
 */
export default function EmojisModule(_props: SharedModuleProps) {
  const [emojis, setEmojis] = useState<any[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const fetchEmojis = async () => {
      setLoading(true)
      try {
        const res = await fetch(`${API}/emojis/`)
        setEmojis(await res.json())
      } catch {
        console.error('Erreur réseau')
      } finally {
        setLoading(false)
      }
    }
    fetchEmojis()
  }, [])

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <Smile size={18} className="text-accent" /> Module Emojis
      </h1>
      <p className="text-sm text-secondary max-w-lg leading-relaxed">
        Afficheur d'emojis chargé dynamiquement.
      </p>
      {loading ? (
        <p>Chargement...</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {emojis.map((emoji) => (
            <Card key={emoji.emoji}>
              <div className="p-4">
                <p className="text-sm font-medium">{emoji.name}</p>
                <div className="mt-2 text-3xl">{emoji.emoji}</div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </main>
  )
}
