import { useState, useRef, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import Chat from './components/Chat'
import Kholle from './components/Kholle'
import Flashcards from './components/Flashcards'
import Admin from './components/Admin'
import Code from './components/Code'
import Docs from './components/Docs'
import History from './components/History'
import Settings from './components/Settings'

type Module = 'chat' | 'kholle' | 'flashcards' | 'code' | 'docs' | 'admin' | 'history' | 'settings'
export type EffortLevel = 'direct' | 'low' | 'medium' | 'high' | 'adaptive'
export interface StepConfig { role: string; model: string }

const API = 'http://localhost:8000'

export default function App() {
  const [activeModule, setActiveModule] = useState<Module>('chat')
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
