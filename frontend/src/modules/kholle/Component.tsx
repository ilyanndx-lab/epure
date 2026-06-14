import { useState, useEffect, useRef, useCallback } from 'react'
import { usePersistentState } from '../../usePersistentState'
import { GraduationCap, Play, Square, Send, ArrowRight, Loader2, CheckCircle2, XCircle } from 'lucide-react'
import { Button, Card, ProgressBar, Textarea } from '../../components/ui'
import RichMessage from '../../components/RichMessage'
import ModuleBar from '../../components/ModuleBar'

const API = 'http://localhost:8000'
const WS_KHOLLE = 'ws://localhost:8000/ws/kholle'

type Phase = 'config' | 'session' | 'summary'
type Mode = 'generate' | 'list'

interface CurrentQuestion {
  content: string
  index: number
  total: number
}

interface KholleProps {
  onAssistantDone?: (text: string) => void
  playSpeech?: (text: string) => void
  stopSpeech?: () => void
  speakingText?: string | null
  ttsEnabled?: boolean
  onTtsToggle?: () => void
}

const basename = (path: string) => path.split(/[/\\]/).pop() ?? path

export default function Kholle({ onAssistantDone, playSpeech, stopSpeech, speakingText, ttsEnabled, onTtsToggle }: KholleProps) {
  // Config state
  const [mode, setMode] = usePersistentState<Mode>('epure.kholle.mode', 'generate')
  const [indexedFiles, setIndexedFiles] = useState<string[]>([])
  const [selectedFiles, setSelectedFiles] = usePersistentState<string[]>('epure.kholle.selectedFiles', [])
  const [questionText, setQuestionText] = usePersistentState<string>('epure.kholle.questionText', '')
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
  const [streamStats, setStreamStats] = useState<{ tps: number; count: number } | null>(null)
  const [correctionStats, setCorrectionStats] = useState<{ tps: number; outputTokens: number; promptTokens: number; durationMs: number } | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const correctionBottomRef = useRef<HTMLDivElement>(null)
  const correctionRef = useRef('')
  const tokenCountRef = useRef(0)
  const streamStartRef = useRef<number | null>(null)
  const pendingOllamaStatsRef = useRef<{ promptTokens: number; outputTokens: number; evalMs: number } | null>(null)

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
        correctionRef.current = ''
        setCorrecting(false)
        setCorrectionDone(false)
        setAnswer('')
        setSessionError(null)
      } else if (data.type === 'token') {
        setCorrection(prev => {
          const next = prev + data.content
          correctionRef.current = next
          return next
        })
        setCorrecting(true)
        tokenCountRef.current += 1
        if (streamStartRef.current === null) streamStartRef.current = Date.now()
        const elapsed = (Date.now() - streamStartRef.current) / 1000
        if (elapsed > 0) setStreamStats({ tps: tokenCountRef.current / elapsed, count: tokenCountRef.current })
      } else if (data.type === 'stats') {
        pendingOllamaStatsRef.current = {
          promptTokens: data.prompt_tokens as number,
          outputTokens: data.output_tokens as number,
          evalMs: data.eval_duration_ms as number,
        }
      } else if (data.type === 'done') {
        const pending = pendingOllamaStatsRef.current
        if (pending && pending.outputTokens > 0 && pending.evalMs > 0) {
          setCorrectionStats({
            tps: pending.outputTokens / (pending.evalMs / 1000),
            outputTokens: pending.outputTokens,
            promptTokens: pending.promptTokens,
            durationMs: pending.evalMs,
          })
        } else {
          const count = tokenCountRef.current
          const dur = streamStartRef.current !== null ? (Date.now() - streamStartRef.current) / 1000 : 0
          if (count > 0 && dur > 0) setCorrectionStats({ tps: count / dur, outputTokens: count, promptTokens: 0, durationMs: Math.round(dur * 1000) })
        }
        pendingOllamaStatsRef.current = null
        setCorrecting(false)
        setCorrectionDone(true)
        setStreamStats(null)
        tokenCountRef.current = 0
        streamStartRef.current = null
        onAssistantDone?.(correctionRef.current)
        correctionRef.current = ''
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
    tokenCountRef.current = 0
    streamStartRef.current = null
    pendingOllamaStatsRef.current = null
    setStreamStats(null)
    setCorrectionStats(null)
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
    setCorrectionStats(null)
    setStreamStats(null)
    pendingOllamaStatsRef.current = null
  }, [])

  // ── Config ──────────────────────────────────────────────────────────────

  if (phase === 'config') {
    const canStart =
      !starting &&
      (mode === 'generate' ? selectedFiles.length > 0 : questionText.trim().length > 0)

    return (
      <main className="flex flex-col flex-1 overflow-hidden">
        <div className="flex-1 overflow-y-auto px-8 py-8">
          <div className="max-w-lg space-y-5">
            <h1 className="text-lg font-semibold text-primary flex items-center gap-2">
              <GraduationCap size={18} className="text-accent" />
              Configuration kholle
            </h1>

            <Card className="space-y-2">
              {(['generate', 'list'] as Mode[]).map(m => (
                <label key={m} className="flex items-center gap-3 cursor-pointer group">
                  <input
                    type="radio"
                    checked={mode === m}
                    onChange={() => setMode(m)}
                    className="accent-[--accent-primary]"
                  />
                  <span className="text-sm text-secondary group-hover:text-primary transition-colors duration-150">
                    {m === 'generate' ? 'Générer depuis mes fiches' : 'Fournir une liste'}
                  </span>
                </label>
              ))}
            </Card>

            {mode === 'generate' ? (
              <Card>
                <p className="text-xs text-muted uppercase tracking-wide mb-3">Sources disponibles</p>
                {indexedFiles.length === 0 ? (
                  <p className="text-sm text-muted">Aucun fichier indexé.</p>
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
                          className="mt-0.5 accent-[--accent-primary] shrink-0"
                        />
                        <span className="text-xs leading-relaxed">
                          <span className="text-secondary group-hover:text-primary transition-colors duration-150">
                            {basename(f)}
                          </span>
                          <br />
                          <span className="text-muted/60 font-mono text-xs break-all">{f}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                )}
              </Card>
            ) : (
              <Card>
                <p className="text-xs text-muted uppercase tracking-wide mb-2">Questions (une par ligne)</p>
                <Textarea
                  value={questionText}
                  onChange={e => setQuestionText(e.target.value)}
                  placeholder="Définir un espace vectoriel..."
                  rows={8}
                  className="w-full text-xs"
                />
              </Card>
            )}

            {configError && (
              <p className="text-xs text-error">{configError}</p>
            )}
          </div>
        </div>

        <ModuleBar
          module="kholle"
          showMic
          showModel
          onTranscribed={(t) => setQuestionText(prev => prev + t)}
          ttsEnabled={ttsEnabled}
          onTtsToggle={onTtsToggle}
          speakingText={speakingText}
        />
        <div className="border-t border-line px-8 py-4">
          <Button
            variant="primary"
            onClick={startSession}
            disabled={!canStart}
            icon={starting ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
          >
            {starting ? 'Génération des questions...' : 'Démarrer la kholle'}
          </Button>
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
        <div className="border-b border-line px-8 py-5 shrink-0 bg-surface">
          {currentQ && (
            <>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-mono text-muted">
                  Question {currentQ.index + 1} / {currentQ.total}
                </span>
                <Button variant="ghost" size="sm" onClick={reset}>
                  abandonner
                </Button>
              </div>
              <ProgressBar
                value={((currentQ.index + 1) / currentQ.total) * 100}
                className="mb-3"
              />
              <p className="text-base text-primary leading-relaxed">
                {currentQ.content}
              </p>
            </>
          )}
        </div>

        {/* Correction */}
        <div className="flex-1 overflow-y-auto px-8 py-5">
          {correction ? (
            <>
              <RichMessage content={correction} streaming={correcting} />
              {correcting && streamStats && (
                <div className="mt-1 text-xs font-mono text-muted/70">
                  {streamStats.tps.toFixed(1)} tok/s · {streamStats.count} tokens
                </div>
              )}
              {!correcting && correctionStats && (
                <div className="mt-1 text-xs font-mono text-muted/70">
                  {correctionStats.tps.toFixed(1)} tok/s · {correctionStats.durationMs}ms · {correctionStats.promptTokens}in / {correctionStats.outputTokens}out tokens
                </div>
              )}
              {correctionDone && playSpeech && (
                <button
                  onClick={() =>
                    speakingText === correction ? stopSpeech?.() : playSpeech(correction)
                  }
                  className={`mt-3 inline-flex items-center gap-1.5 text-xs transition-colors duration-150 ${
                    speakingText === correction
                      ? 'text-accent2 hover:text-accent2-hover'
                      : 'text-muted hover:text-secondary'
                  }`}
                  title={speakingText === correction ? 'Arrêter' : 'Lire la correction'}
                >
                  {speakingText === correction
                    ? <><Square size={12} fill="currentColor" /> arrêter</>
                    : <><Play size={12} /> lire</>}
                </button>
              )}
            </>
          ) : (
            !correcting && (
              <div className="flex flex-col items-center justify-center gap-2 h-full text-muted select-none">
                <GraduationCap size={16} />
                <span className="text-sm">En attente de ta réponse</span>
              </div>
            )
          )}
          {sessionError && (
            <p className="mt-3 text-xs text-error">{sessionError}</p>
          )}
          <div ref={correctionBottomRef} />
        </div>

        <ModuleBar
          module="kholle"
          showMic
          showModel
          onTranscribed={(t) => setAnswer(prev => prev + t)}
          ttsEnabled={ttsEnabled}
          onTtsToggle={onTtsToggle}
          speakingText={speakingText}
        />
        {/* Input */}
        <div className="border-t border-line px-8 py-4 shrink-0">
          {correctionDone ? (
            <Button variant="primary" onClick={nextQuestion} icon={<ArrowRight size={15} />}>
              {isLast ? 'Voir le bilan' : 'Question suivante'}
            </Button>
          ) : (
            <div className="flex gap-3 items-end">
              <Textarea
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
                className="flex-1 disabled:opacity-40"
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
                title="Répondre"
                className="p-2.5 rounded-md bg-gradient-primary text-on-accent shadow-sm hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 shrink-0"
              >
                {correcting ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
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
        <div className="max-w-2xl space-y-5">
          <h1 className="text-lg font-semibold text-primary flex items-center gap-2">
            <GraduationCap size={18} className="text-accent" />
            Bilan de kholle
          </h1>
          {sessionErrors.length === 0 ? (
            <Card className="flex items-center gap-3">
              <CheckCircle2 size={18} className="text-success shrink-0" />
              <p className="text-sm text-secondary">
                Aucune erreur détectée. Excellent travail.
              </p>
            </Card>
          ) : (
            <div className="space-y-2">
              {sessionErrors.map((err, i) => (
                <Card key={i} className="flex items-start gap-3">
                  <XCircle size={15} className="text-error shrink-0 mt-0.5" />
                  <p className="text-sm text-secondary leading-relaxed">{err}</p>
                </Card>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="border-t border-line px-8 py-4">
        <Button variant="primary" onClick={reset}>
          Nouvelle kholle
        </Button>
      </div>
    </main>
  )
}
