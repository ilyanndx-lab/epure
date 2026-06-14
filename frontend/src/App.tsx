import { useState, useRef, useCallback, useEffect, Suspense } from 'react'
import { Loader2 } from 'lucide-react'
import Sidebar from './components/Sidebar'
import ModuleErrorBoundary from './components/ModuleErrorBoundary'
import { useInstanceConfig } from './instance'
import { useModules } from './modules'
import { getModuleDef, type SharedModuleProps } from './modules/registry'
import { usePersistentState } from './usePersistentState'

export type EffortLevel = 'direct' | 'low' | 'medium' | 'high' | 'adaptive'
export interface StepConfig { role: string; model: string }

const API = 'http://localhost:8000'

export default function App() {
  const config = useInstanceConfig()
  const modules = useModules()
  const [activeModule, setActiveModule] = usePersistentState<string>('epure.activeModule', 'chat')
  // Modules déjà visités : on les garde MONTÉS (cachés) pour que leurs tâches
  // (streaming chat, génération…) continuent en arrière-plan quand on change de
  // module. On n'ajoute jamais, on ne retire pas → pas d'interruption.
  const [mountedIds, setMountedIds] = useState<string[]>([activeModule])
  const [ttsEnabled, setTtsEnabled] = useState(false)
  const [speakingText, setSpeakingText] = useState<string | null>(null)

  const audioRef = useRef<HTMLAudioElement | null>(null)

  const stopSpeech = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setSpeakingText(null)
  }, [])

  const playSpeech = useCallback(async (text: string) => {
    if (!text.trim()) return
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setSpeakingText(text)
    try {
      const res = await fetch(`${API}/voice/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      audioRef.current = audio
      audio.onended = () => {
        URL.revokeObjectURL(url)
        setSpeakingText(null)
        audioRef.current = null
      }
      audio.onerror = () => {
        setSpeakingText(null)
        audioRef.current = null
      }
      audio.play()
    } catch (err) {
      console.error('Erreur TTS:', err)
      setSpeakingText(null)
    }
  }, [])

  const onAssistantDone = useCallback(
    (text: string) => {
      if (ttsEnabled) playSpeech(text)
    },
    [ttsEnabled, playSpeech]
  )

  // Modules réellement accessibles (settings toujours inclus).
  const visibleIds = new Set<string>([
    'settings',
    'workshop',
    ...modules
      .filter(m => m.status === 'active' && config.modules_activés.includes(m.id))
      .map(m => m.id),
  ])

  // Si le module courant devient inaccessible, bascule vers le premier visible.
  useEffect(() => {
    if (!visibleIds.has(activeModule)) {
      const first = config.modules_activés.find(id => visibleIds.has(id))
      setActiveModule(first ?? 'settings')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeModule, config.modules_activés, modules])

  // Monte le module actif s'il ne l'est pas déjà (et le garde monté ensuite).
  useEffect(() => {
    setMountedIds(prev => (prev.includes(activeModule) ? prev : [...prev, activeModule]))
  }, [activeModule])

  // Props partagées passées à tout module rendu.
  const sharedProps: SharedModuleProps = {
    onAssistantDone,
    playSpeech,
    stopSpeech,
    speakingText,
    onNavigate: setActiveModule,
    ttsEnabled,
    onTtsToggle: () => setTtsEnabled(v => !v),
  }

  const activeHasInterface = !!getModuleDef(activeModule)

  return (
    <div className="flex h-screen w-full bg-base text-primary overflow-hidden">
      <Sidebar activeModule={activeModule} onModuleChange={setActiveModule} />
      <div className="flex flex-col flex-1 overflow-hidden">
        {/* Tous les modules visités restent montés ; seul l'actif est affiché.
            Leurs tâches (WebSocket, streaming) ne sont donc pas interrompues. */}
        {mountedIds.map(id => {
          const def = getModuleDef(id)
          if (!def) return null
          const C = def.component
          const isActive = id === activeModule
          return (
            <div
              key={id}
              className="flex flex-col flex-1 min-h-0 overflow-hidden"
              style={{ display: isActive ? 'flex' : 'none' }}
            >
              <ModuleErrorBoundary moduleId={id} onNavigate={setActiveModule}>
                <Suspense
                  fallback={
                    <div className="flex flex-1 items-center justify-center text-muted">
                      <Loader2 size={18} className="animate-spin" />
                    </div>
                  }
                >
                  <C {...sharedProps} />
                </Suspense>
              </ModuleErrorBoundary>
            </div>
          )
        })}
        {!activeHasInterface && (
          <div className="flex flex-1 items-center justify-center text-sm text-muted">
            Module « {activeModule} » sans interface frontend
          </div>
        )}
      </div>
    </div>
  )
}
