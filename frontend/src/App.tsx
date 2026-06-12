import { useState, useRef, useCallback, useEffect } from 'react'
import Sidebar from './components/Sidebar'
import Chat from './components/Chat'
import Kholle from './components/Kholle'
import Flashcards from './components/Flashcards'
import Admin from './components/Admin'
import Code from './components/Code'
import Docs from './components/Docs'
import History from './components/History'
import Settings from './components/Settings'
import { useInstanceConfig } from './instance'
import { useModules } from './modules'

export type EffortLevel = 'direct' | 'low' | 'medium' | 'high' | 'adaptive'
export interface StepConfig { role: string; model: string }

const API = 'http://localhost:8000'

export default function App() {
  const config = useInstanceConfig()
  const modules = useModules()
  const [activeModule, setActiveModule] = useState<string>('chat')
  const [ttsEnabled, setTtsEnabled] = useState(false)
  const [speakingText, setSpeakingText] = useState<string | null>(null)

  // Ensemble des modules réellement accessibles (settings toujours inclus).
  const visibleIds = new Set<string>([
    'settings',
    ...modules
      .filter(m => m.status === 'active' && config.modules_activés.includes(m.id))
      .map(m => m.id),
  ])

  // Si le module courant devient inaccessible (désactivé), bascule vers le
  // premier module visible (ou Réglages en dernier recours).
  useEffect(() => {
    if (!visibleIds.has(activeModule)) {
      const first = config.modules_activés.find(id => visibleIds.has(id))
      setActiveModule(first ?? 'settings')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeModule, config.modules_activés, modules])

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

  return (
    <div className="flex h-screen w-full bg-base text-primary overflow-hidden">
      <Sidebar activeModule={activeModule} onModuleChange={setActiveModule} />
      <div className="flex flex-col flex-1 overflow-hidden">
        {activeModule === 'chat' && (
          <Chat
            onAssistantDone={onAssistantDone}
            playSpeech={playSpeech}
            stopSpeech={stopSpeech}
            speakingText={speakingText}
            onNavigate={setActiveModule}
            ttsEnabled={ttsEnabled}
            onTtsToggle={() => setTtsEnabled(v => !v)}
          />
        )}
        {activeModule === 'kholle' && (
          <Kholle
            onAssistantDone={onAssistantDone}
            playSpeech={playSpeech}
            stopSpeech={stopSpeech}
            speakingText={speakingText}
            ttsEnabled={ttsEnabled}
            onTtsToggle={() => setTtsEnabled(v => !v)}
          />
        )}
        {activeModule === 'flashcards' && <Flashcards />}
        {activeModule === 'code' && <Code />}
        {activeModule === 'docs' && <Docs />}
        {activeModule === 'admin' && <Admin />}
        {activeModule === 'history' && <History />}
        {activeModule === 'settings' && <Settings />}
      </div>
    </div>
  )
}
