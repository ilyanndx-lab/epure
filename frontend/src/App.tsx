import { useState, useRef, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import Chat from './components/Chat'
import Kholle from './components/Kholle'
import Flashcards from './components/Flashcards'
import Admin from './components/Admin'
import Settings from './components/Settings'
import ConnectorBar from './components/ConnectorBar'

type Module = 'chat' | 'kholle' | 'flashcards' | 'admin' | 'settings'

const API = 'http://localhost:8000'

export default function App() {
  const [activeModule, setActiveModule] = useState<Module>('chat')
  const [ttsEnabled, setTtsEnabled] = useState(false)
  const [speakingText, setSpeakingText] = useState<string | null>(null)

  const activeInputRef = useRef<((text: string) => void) | null>(null)
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
    <div className="flex h-screen w-full bg-[#0d0d0d] text-[#e0e0e0] overflow-hidden">
      <Sidebar activeModule={activeModule} onModuleChange={setActiveModule} />
      <div className="flex flex-col flex-1 overflow-hidden">
        {activeModule === 'chat' && (
          <Chat
            inputRef={activeInputRef}
            onAssistantDone={onAssistantDone}
            playSpeech={playSpeech}
            stopSpeech={stopSpeech}
            speakingText={speakingText}
            onNavigate={setActiveModule}
          />
        )}
        {activeModule === 'kholle' && (
          <Kholle
            inputRef={activeInputRef}
            onAssistantDone={onAssistantDone}
            playSpeech={playSpeech}
            stopSpeech={stopSpeech}
            speakingText={speakingText}
          />
        )}
        {activeModule === 'flashcards' && <Flashcards />}
        {activeModule === 'admin' && <Admin />}
        {activeModule === 'settings' && <Settings />}
        <ConnectorBar
          activeInputRef={activeInputRef}
          ttsEnabled={ttsEnabled}
          onTtsToggle={() => setTtsEnabled(v => !v)}
          speakingText={speakingText}
        />
      </div>
    </div>
  )
}
