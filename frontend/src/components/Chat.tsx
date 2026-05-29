import { useState, useEffect, useRef, useCallback } from 'react'

interface Message {
  role: 'user' | 'assistant'
  content: string
}

interface ChatProps {
  inputRef?: React.MutableRefObject<((text: string) => void) | null>
  onAssistantDone?: (text: string) => void
  playSpeech?: (text: string) => void
  stopSpeech?: () => void
  speakingText?: string | null
}

const WS_URL = 'ws://localhost:8000/ws/chat'

export default function Chat({ inputRef, onAssistantDone, playSpeech, stopSpeech, speakingText }: ChatProps) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [connected, setConnected] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const lastAssistantRef = useRef('')

  useEffect(() => {
    if (inputRef) inputRef.current = (text: string) => setInput(text)
    return () => {
      if (inputRef) inputRef.current = null
    }
  }, [inputRef])

  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(WS_URL)
      ws.onopen = () => setConnected(true)
      ws.onclose = () => {
        setConnected(false)
        setTimeout(connect, 2000)
      }
      ws.onmessage = (event) => {
        const data = JSON.parse(event.data)
        if (data.type === 'token') {
          setMessages(prev => {
            const last = prev[prev.length - 1]
            if (last?.role === 'assistant') {
              const next = last.content + data.content
              lastAssistantRef.current = next
              return [...prev.slice(0, -1), { ...last, content: next }]
            }
            lastAssistantRef.current = data.content
            return [...prev, { role: 'assistant', content: data.content }]
          })
        } else if (data.type === 'done') {
          setStreaming(false)
          onAssistantDone?.(lastAssistantRef.current)
          lastAssistantRef.current = ''
        } else if (data.type === 'error') {
          setMessages(prev => [...prev, { role: 'assistant', content: `[erreur: ${data.content}]` }])
          setStreaming(false)
          lastAssistantRef.current = ''
        }
      }
      wsRef.current = ws
    }
    connect()
    return () => wsRef.current?.close()
  }, [onAssistantDone])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const send = useCallback(() => {
    const text = input.trim()
    if (!text || !connected || streaming) return
    const msg: Message = { role: 'user', content: text }
    setMessages(prev => [...prev, msg])
    setInput('')
    setStreaming(true)
    wsRef.current?.send(JSON.stringify(msg))
  }, [input, connected, streaming])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <main className="flex flex-col flex-1 overflow-hidden">
      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-5">
        {messages.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <span className="text-xs font-mono text-[#2a2a2a] select-none">— en attente —</span>
          </div>
        )}
        {messages.map((msg, i) => (
          <div
            key={i}
            className={`flex group ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[78%] px-4 py-3 rounded text-sm leading-relaxed ${
                msg.role === 'user'
                  ? 'bg-[#1a1a1a] border border-[#282828] text-[#d8d8d8]'
                  : 'text-[#b8b8b8] font-mono'
              }`}
            >
              <pre className="whitespace-pre-wrap break-words font-[inherit] m-0">{msg.content}</pre>
              {msg.role === 'assistant' && playSpeech && (
                <div className="mt-2 flex">
                  <button
                    onClick={() =>
                      speakingText === msg.content ? stopSpeech?.() : playSpeech(msg.content)
                    }
                    className={`text-xs font-mono transition-colors
                      [@media(pointer:fine)]:opacity-0 [@media(pointer:fine)]:group-hover:opacity-100
                      [@media(pointer:coarse)]:opacity-100
                      ${speakingText === msg.content
                        ? 'text-[#5a9a5a] hover:text-[#7aba7a]'
                        : 'text-[#333] hover:text-[#888]'
                      }`}
                    title={speakingText === msg.content ? 'Arrêter' : 'Lire'}
                  >
                    {speakingText === msg.content ? '■' : '▶'}
                  </button>
                </div>
              )}
            </div>
          </div>
        ))}
        {streaming && messages[messages.length - 1]?.role !== 'assistant' && (
          <div className="flex justify-start">
            <span className="text-xs font-mono text-[#333] animate-pulse">▍</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-[#1e1e1e] px-4 py-4">
        <div className="flex gap-3 items-end">
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={!connected || streaming}
            placeholder={connected ? 'Message...' : 'Connexion au serveur...'}
            rows={1}
            className="flex-1 bg-[#141414] border border-[#242424] rounded px-3 py-2 text-sm text-[#e0e0e0] placeholder-[#383838] resize-none focus:outline-none focus:border-[#383838] font-mono"
            style={{ minHeight: '40px', maxHeight: '160px' }}
            onInput={e => {
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = `${Math.min(el.scrollHeight, 160)}px`
            }}
          />
          <button
            onClick={send}
            disabled={!connected || streaming || !input.trim()}
            className="px-4 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#666] hover:border-[#383838] hover:text-[#aaa] disabled:opacity-20 disabled:cursor-not-allowed transition-colors shrink-0"
          >
            {streaming ? '...' : 'envoyer'}
          </button>
        </div>
        {!connected && (
          <div className="mt-2 text-xs font-mono text-[#7a3333]">ws déconnecté — reconnexion...</div>
        )}
      </div>
    </main>
  )
}
