import { useState, useEffect } from 'react'
import { MousePointer } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { Button } from '../../../components/ui'
import { API, apiFetch } from '../../../api'

/**
 * Composant du module de clicker.
 */
export default function Component(_props: SharedModuleProps) {
  const [counter, setCounter] = useState(0)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const fetchCounter = async () => {
      try {
        const res = await apiFetch(`${API}/clicker/counter`)
        const data = await res.json()
        setCounter(data.counter)
      } catch (error) {
        console.error(error)
      }
    }
    fetchCounter()
  }, [])

  const handleClick = async () => {
    setLoading(true)
    try {
      const res = await apiFetch(`${API}/clicker/increment`, { method: 'POST' })
      const data = await res.json()
      setCounter(counter + 1)
    } catch (error) {
      console.error(error)
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <MousePointer size={18} className="text-accent" /> Clicker
      </h1>
      <p className="text-sm text-secondary max-w-lg leading-relaxed">
        Cliquez sur le bouton pour incrémenter le compteur.
      </p>
      <div className="flex items-center gap-3">
        <Button onClick={handleClick} disabled={loading}>
          {loading ? '...' : 'Cliquez'}
        </Button>
        <span className="text-sm">Compteur: {counter}</span>
      </div>
    </main>
  )
}
