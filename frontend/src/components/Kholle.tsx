import { useState, useEffect, useRef, useCallback } from 'react'

const API = 'http://localhost:8000'
const WS_KHOLLE = 'ws://localhost:8000/ws/kholle'

type Phase = 'config' | 'session' | 'summary'
type Mode = 'generate' | 'list'

interface CurrentQuestion {
  content: string
  index: number
  total: number
}

const basename = (path: string) => path.split(/[/\\]/).pop() ?? path

export default function Kholle() {
  // Config state
  const [mode, setMode] = useState<Mode>('generate')
  const [indexedFiles, setIndexedFiles] = useState<string[]>([])
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [questionText, setQuestionText] = useState('')
  const [configError, setConfigError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  // Session state
  const [phase, setPhase] = useState<Phase>('config')
  const [currentQ, setCurrentQ] = useState<CurrentQuestion | null>(null)
  const [answer, setAnswer] = useState('')
  const [correction, setCorrection] = useState('')
  const [correcting, setCorrecting] = useState(false)
  const [correctionDone, setCorrectionDone] = useState(false)
  const [sessionError, setSessionError] = useState<string | null>(null)

  // Summary state
  const [sessionErrors, setSessionErrors] = useState<string[]>([])

  const wsRef = useRef<WebSocket | null>(null)
  const correctionBottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetch(`${API}/rag/files`)
      .then(r => r.json())
      .then((data: { files: string[] }) => setIndexedFiles(data.files))
      .catch(err => console.error('Erreur GET /rag/files:', err))
  }, [])

  useEffect(() => {
    correctionBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [correction])

  const openWS = useCallback((questions: string[]) => {
    console.log('openWS appelé avec', questions.length, 'questions')
    const ws = new WebSocket(WS_KHOLLE)

    ws.onopen = () => {
      console.log('WS Kholle ouvert, envoi start')
      ws.send(JSON.stringify({ type: 'start', questions }))
      setPhase('session')
    }

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      console.log('WS message:', data)

      if (data.type === 'question') {
        setCurrentQ({ content: data.content, index: data.index, total: data.total })
        setCorrection('')
        setCorrecting(false)
        setCorrectionDone(false)
        setAnswer('')
        setSessionError(null)
      } else if (data.type === 'token') {
        setCorrection(prev => prev + data.content)
        setCorrecting(true)
      } else if (data.type === 'done') {
        setCorrecting(false)
        setCorrectionDone(true)
      } else if (data.type === 'session_end') {
        setSessionErrors(data.errors)
        setPhase('summary')
      } else if (data.type === 'error') {
        setSessionError(data.content)
        setCorrecting(false)
      }
    }

    ws.onerror = (e) => {
      console.error('WS Kholle erreur:', e)
      setSessionError('Erreur WebSocket kholle')
    }
    wsRef.current = ws
  }, [])

  const startSession = useCallback(async () => {
    setConfigError(null)
    setStarting(true)
    console.log('startSession appelé, mode:', mode, 'files:', selectedFiles)
    try {
      const body =
        mode === 'generate'
          ? { mode: 'generate', source_files: selectedFiles }
          : {
              mode: 'list',
              questions: questionText.split('\n').filter(q => q.trim()),
            }

      console.log('POST /kholle/start body:', body)
      const res = await fetch(`${API}/kholle/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      console.log('Réponse POST:', res.status)
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail ?? 'Erreur serveur')
      }
      const data: { questions: string[] } = await res.json()
      console.log('Questions reçues:', data.questions)
      openWS(data.questions)
    } catch (err) {
      console.error('Erreur startSession:', err)
      setConfigError(err instanceof Error ? err.message : String(err))
    } finally {
      setStarting(false)
    }
  }, [mode, selectedFiles, questionText, openWS])

  const sendAnswer = useCallback(() => {
    const text = answer.trim()
    if (!text || correcting) return
    setCorrecting(true)
    setCorrectionDone(false)
    setCorrection('')
    wsRef.current?.send(JSON.stringify({ type: 'answer', content: text }))
  }, [answer, correcting])

  const nextQuestion = useCallback(() => {
    setCorrectionDone(false)
    wsRef.current?.send(JSON.stringify({ type: 'next' }))
  }, [])

  const reset = useCallback(() => {
    wsRef.current?.close()
    wsRef.current = null
    setPhase('config')
    setCurrentQ(null)
    setCorrection('')
    setCorrecting(false)
    setCorrectionDone(false)
    setAnswer('')
    setSessionErrors([])
    setSessionError(null)
    setConfigError(null)
  }, [])

  // ── Config ──────────────────────────────────────────────────────────────

  if (phase === 'config') {
    const canStart =
      !starting &&
      (mode === 'generate' ? selectedFiles.length > 0 : questionText.trim().length > 0)

    return (
      <main className="flex flex-col flex-1 overflow-hidden">
        <div className="flex-1 overflow-y-auto px-8 py-8">
          <div className="max-w-lg">
            <p className="text-xs font-mono text-[#444] uppercase tracking-widest mb-6">
              Configuration kholle
            </p>

            <div className="space-y-2 mb-7">
              {(['generate', 'list'] as Mode[]).map(m => (
                <label key={m} className="flex items-center gap-3 cursor-pointer group">
                  <input
                    type="radio"
                    checked={mode === m}
                    onChange={() => setMode(m)}
                    className="accent-[#555]"
                  />
                  <span className="text-xs font-mono text-[#888] group-hover:text-[#ccc] transition-colors">
                    {m === 'generate' ? 'Générer depuis mes fiches' : 'Fournir une liste'}
                  </span>
                </label>
              ))}
            </div>

            {mode === 'generate' ? (
              <div>
                <p className="text-xs font-mono text-[#444] mb-3">Sources disponibles :</p>
                {indexedFiles.length === 0 ? (
                  <p className="text-xs font-mono text-[#333]">Aucun fichier indexé.</p>
                ) : (
                  <div className="space-y-2">
                    {indexedFiles.map(f => (
                      <label key={f} className="flex items-start gap-3 cursor-pointer group">
                        <input
                          type="checkbox"
                          checked={selectedFiles.includes(f)}
                          onChange={e =>
                            setSelectedFiles(prev =>
                              e.target.checked ? [...prev, f] : prev.filter(x => x !== f)
                            )
                          }
                          className="mt-0.5 accent-[#555] shrink-0"
                        />
                        <span className="text-xs font-mono leading-relaxed">
                          <span className="text-[#aaa] group-hover:text-[#e0e0e0] transition-colors">
                            {basename(f)}
                          </span>
                          <br />
                          <span className="text-[#2e2e2e] text-[10px] break-all">{f}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div>
                <p className="text-xs font-mono text-[#444] mb-2">Questions (une par ligne) :</p>
                <textarea
                  value={questionText}
                  onChange={e => setQuestionText(e.target.value)}
                  placeholder="Définir un espace vectoriel..."
                  rows={8}
                  className="w-full bg-[#141414] border border-[#242424] rounded px-3 py-2 text-xs text-[#e0e0e0] placeholder-[#333] resize-none focus:outline-none focus:border-[#383838] font-mono"
                />
              </div>
            )}

            {configError && (
              <p className="mt-4 text-xs font-mono text-[#7a3333]">{configError}</p>
            )}
          </div>
        </div>

        <div className="border-t border-[#1e1e1e] px-8 py-4">
          <button
            onClick={startSession}
            disabled={!canStart}
            className="px-5 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] disabled:opacity-20 disabled:cursor-not-allowed transition-colors"
          >
            {starting ? 'Génération des questions...' : 'Démarrer la kholle'}
          </button>
        </div>
      </main>
    )
  }

  // ── Session ─────────────────────────────────────────────────────────────

  if (phase === 'session') {
    const isLast = currentQ ? currentQ.index + 1 >= currentQ.total : false

    return (
      <main className="flex flex-col flex-1 overflow-hidden">
        {/* Question header */}
        <div className="border-b border-[#1e1e1e] px-8 py-5 shrink-0">
          {currentQ && (
            <>
              <div className="flex items-center justify-between mb-3">
                <span className="text-xs font-mono text-[#3a3a3a]">
                  {currentQ.index + 1} / {currentQ.total}
                </span>
                <button
                  onClick={reset}
                  className="text-xs font-mono text-[#2a2a2a] hover:text-[#666] transition-colors"
                >
                  abandonner
                </button>
              </div>
              <p className="text-base font-mono text-[#e0e0e0] leading-relaxed">
                {currentQ.content}
              </p>
            </>
          )}
        </div>

        {/* Correction */}
        <div className="flex-1 overflow-y-auto px-8 py-5">
          {correction ? (
            <pre className="text-sm font-mono text-[#b8b8b8] leading-relaxed whitespace-pre-wrap break-words">
              {correction}
              {correcting && <span className="animate-pulse text-[#3a3a3a]">▍</span>}
            </pre>
          ) : (
            !correcting && (
              <div className="flex items-center justify-center h-full">
                <span className="text-xs font-mono text-[#2a2a2a] select-none">
                  — en attente de ta réponse —
                </span>
              </div>
            )
          )}
          {sessionError && (
            <p className="mt-3 text-xs font-mono text-[#7a3333]">{sessionError}</p>
          )}
          <div ref={correctionBottomRef} />
        </div>

        {/* Input */}
        <div className="border-t border-[#1e1e1e] px-8 py-4 shrink-0">
          {correctionDone ? (
            <button
              onClick={nextQuestion}
              className="px-5 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] transition-colors"
            >
              {isLast ? 'Voir le bilan →' : 'Question suivante →'}
            </button>
          ) : (
            <div className="flex gap-3 items-end">
              <textarea
                value={answer}
                onChange={e => setAnswer(e.target.value)}
                disabled={correcting}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    sendAnswer()
                  }
                }}
                placeholder="Ta réponse..."
                rows={1}
                className="flex-1 bg-[#141414] border border-[#242424] rounded px-3 py-2 text-sm text-[#e0e0e0] placeholder-[#383838] resize-none focus:outline-none focus:border-[#383838] font-mono disabled:opacity-40"
                style={{ minHeight: '40px', maxHeight: '160px' }}
                onInput={e => {
                  const el = e.currentTarget
                  el.style.height = 'auto'
                  el.style.height = `${Math.min(el.scrollHeight, 160)}px`
                }}
              />
              <button
                onClick={sendAnswer}
                disabled={correcting || !answer.trim()}
                className="px-4 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#666] hover:border-[#383838] hover:text-[#aaa] disabled:opacity-20 disabled:cursor-not-allowed transition-colors shrink-0"
              >
                {correcting ? '...' : 'répondre'}
              </button>
            </div>
          )}
        </div>
      </main>
    )
  }

  // ── Summary ──────────────────────────────────────────────────────────────

  return (
    <main className="flex flex-col flex-1 overflow-hidden">
      <div className="flex-1 overflow-y-auto px-8 py-8">
        <div className="max-w-2xl">
          <p className="text-xs font-mono text-[#444] uppercase tracking-widest mb-6">
            Bilan de kholle
          </p>
          {sessionErrors.length === 0 ? (
            <p className="text-xs font-mono text-[#555]">
              Aucune erreur détectée. Excellent travail.
            </p>
          ) : (
            <div className="space-y-2">
              {sessionErrors.map((err, i) => (
                <div key={i} className="border border-[#1e1e1e] rounded px-4 py-3">
                  <p className="text-xs font-mono text-[#888] leading-relaxed">{err}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="border-t border-[#1e1e1e] px-8 py-4">
        <button
          onClick={reset}
          className="px-5 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] transition-colors"
        >
          Nouvelle kholle
        </button>
      </div>
    </main>
  )
}
