import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import RichMessage from './RichMessage'
import type { EffortLevel, StepConfig } from '../App'

const API = 'http://localhost:8000'
const WS_URL = 'ws://localhost:8000/ws/chat'

interface MsgStats {
  tps: number
  outputTokens: number
  promptTokens: number
  durationMs: number
}

interface PipelineStepData {
  role: string
  label: string
  model: string
  output: string
  stats?: { tps: number; tokens: number; duration_ms: number }
  status: 'pending' | 'running' | 'done' | 'error'
  errorMsg?: string
}

interface PipelineTotalStats {
  duration_ms: number
  steps: number
  total_tokens: number
}

interface ThinkingBlock {
  steps: PipelineStepData[]
  totalStats?: PipelineTotalStats
  done: boolean
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  stats?: MsgStats
  isError?: boolean
  thinking?: ThinkingBlock
}

interface ChatProps {
  inputRef?: React.MutableRefObject<((text: string) => void) | null>
  onAssistantDone?: (text: string) => void
  playSpeech?: (text: string) => void
  stopSpeech?: () => void
  speakingText?: string | null
  onNavigate?: (module: 'chat' | 'kholle' | 'flashcards' | 'settings') => void
  effort: EffortLevel
  pipelineSteps: StepConfig[]
}

const AT_COMMANDS = [
  { trigger: '@cours',      desc: 'RAG sur tous les fichiers indexés' },
  { trigger: '@strict',     desc: 'Réponse concise, sans intro' },
  { trigger: '@mémoire',    desc: 'Affiche le contexte mémoire actuel' },
  { trigger: '@historique', desc: 'Recherche dans les échanges passés [sujet]' },
] as const

const SLASH_COMMANDS = [
  { trigger: '/kholle',     desc: 'Ouvre le module Kholle [matière?]' },
  { trigger: '/flashcards', desc: 'Ouvre les Flashcards [source?]' },
  { trigger: '/résumé',     desc: 'Résumé des fichiers actifs (streaming)' },
  { trigger: '/modèle',     desc: 'Change le modèle actif [nom]' },
  { trigger: '/lacunes',    desc: 'Lacunes + erreurs des 7 derniers jours' },
  { trigger: '/direct',     desc: 'Bypass orchestrateur — 1 modèle direct [message]' },
] as const

export const SKILL_COMMANDS = { at: AT_COMMANDS, slash: SLASH_COMMANDS }

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s % 60)
  return `${m}m${rem}s`
}

function ThinkingBlockView({ thinking, collapsed, onToggle }: {
  thinking: ThinkingBlock
  collapsed: boolean
  onToggle: () => void
}) {
  const total = thinking.totalStats
  const label = total
    ? `${thinking.steps.length} étapes · ${fmtDuration(total.duration_ms)} · ${total.total_tokens} tokens`
    : thinking.steps.length > 0
    ? `${thinking.steps.filter(s => s.status === 'running').length > 0
        ? `étape ${thinking.steps.findIndex(s => s.status === 'running') + 1}/${thinking.steps.length}...`
        : `${thinking.steps.length} étapes`}`
    : 'Réflexion...'

  return (
    <div className="mt-2 mb-1 border border-[#1e1e1e] rounded bg-[#0d0d0d] overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-[#111] transition-colors"
      >
        <span className="text-[10px] font-mono text-[#4a4a7a] flex items-center gap-2">
          <span className={thinking.done ? '' : 'animate-pulse'}>⬡</span>
          <span>Réflexion · {label}</span>
        </span>
        <span className="text-[10px] font-mono text-[#333]">{collapsed ? '▸' : '▾'}</span>
      </button>

      {!collapsed && (
        <div className="border-t border-[#1a1a1a] divide-y divide-[#141414]">
          {thinking.steps.map((step, i) => (
            <div key={i} className="px-3 py-2">
              <div className="flex items-center gap-2 mb-1">
                <span className={`text-[10px] font-mono shrink-0 ${
                  step.status === 'done' ? 'text-[#5a9a5a]'
                  : step.status === 'running' ? 'text-[#9a9a5a] animate-pulse'
                  : step.status === 'error' ? 'text-[#9a4a4a]'
                  : 'text-[#333]'
                }`}>
                  {step.status === 'done' ? '●' : step.status === 'running' ? '◉' : step.status === 'error' ? '✕' : '○'}
                  {' '}{String(i + 1).padStart(2, '0')} {step.label}
                </span>
                <span className="text-[9px] font-mono text-[#333] shrink-0">
                  {step.model.split(':').pop()}
                </span>
                {step.stats && (
                  <span className="text-[9px] font-mono text-[#2a2a5a] shrink-0">
                    {step.stats.tps.toFixed(1)} tok/s · {step.stats.tokens} tokens · {fmtDuration(step.stats.duration_ms)}
                  </span>
                )}
              </div>
              {step.errorMsg ? (
                <p className="text-[10px] font-mono text-[#7a3a3a]">{step.errorMsg}</p>
              ) : step.output ? (
                <div className="text-xs text-[#666] max-h-40 overflow-y-auto">
                  <RichMessage content={step.output} streaming={step.status === 'running'} />
                </div>
              ) : step.status === 'running' ? (
                <span className="text-[10px] font-mono text-[#333] animate-pulse">▍</span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Chat({
  inputRef,
  onAssistantDone,
  playSpeech,
  stopSpeech,
  speakingText,
  onNavigate,
  effort,
  pipelineSteps,
}: ChatProps) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [connected, setConnected] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)
  const [streamStats, setStreamStats] = useState<{ tps: number; count: number } | null>(null)
  const [collapsedThinking, setCollapsedThinking] = useState<Record<number, boolean>>({})

  const wsRef = useRef<WebSocket | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const lastAssistantRef = useRef('')
  const tokenCountRef = useRef(0)
  const streamStartRef = useRef<number | null>(null)
  const pendingOllamaStatsRef = useRef<{ promptTokens: number; outputTokens: number; evalMs: number } | null>(null)
  const inPipelineRef = useRef(false)
  const pipelineUserMsgIdxRef = useRef(-1)

  useEffect(() => {
    if (inputRef) inputRef.current = (text: string) => setInput(text)
    return () => { if (inputRef) inputRef.current = null }
  }, [inputRef])

  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(WS_URL)
      ws.onopen = () => setConnected(true)
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000) }
      ws.onmessage = (event) => {
        const data = JSON.parse(event.data)

        if (data.type === 'pipeline_info') {
          inPipelineRef.current = true
          const steps: PipelineStepData[] = (data.steps ?? []).map((s: { role: string; label: string; model: string }) => ({
            role: s.role,
            label: s.label || s.role,
            model: s.model,
            output: '',
            status: 'pending' as const,
          }))
          const thinking: ThinkingBlock = { steps, done: false }
          setMessages(prev => {
            // Attach thinking to last user message
            const idx = [...prev].reverse().findIndex(m => m.role === 'user')
            if (idx === -1) return prev
            const realIdx = prev.length - 1 - idx
            pipelineUserMsgIdxRef.current = realIdx
            const updated = [...prev]
            updated[realIdx] = { ...updated[realIdx], thinking }
            return updated
          })
          setCollapsedThinking(prev => {
            const idx = pipelineUserMsgIdxRef.current
            return idx >= 0 ? { ...prev, [idx]: false } : prev
          })

        } else if (data.type === 'step_start') {
          const stepIdx: number = data.step
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            thinking.steps = thinking.steps.map((s, i) =>
              i === stepIdx ? { ...s, status: 'running' as const } : s
            )
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'token' && inPipelineRef.current) {
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            const runningIdx = thinking.steps.findIndex(s => s.status === 'running')
            if (runningIdx >= 0) {
              thinking.steps = thinking.steps.map((s, i) =>
                i === runningIdx ? { ...s, output: s.output + data.content } : s
              )
            }
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'step_end') {
          const stepIdx: number = data.step
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            thinking.steps = thinking.steps.map((s, i) =>
              i === stepIdx ? {
                ...s,
                output: data.output ?? s.output,
                stats: data.stats,
                status: 'done' as const,
              } : s
            )
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'step_error') {
          const stepIdx: number = data.step
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            thinking.steps = thinking.steps.map((s, i) =>
              i === stepIdx ? { ...s, status: 'error' as const, errorMsg: data.message } : s
            )
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'pipeline_done') {
          inPipelineRef.current = false
          const finalOutput: string = data.final_output ?? ''
          const totalStats: PipelineTotalStats = data.total_stats
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            const updated = [...prev]
            if (msgIdx >= 0 && updated[msgIdx]?.thinking) {
              const thinking = { ...updated[msgIdx].thinking!, done: true, totalStats }
              updated[msgIdx] = { ...updated[msgIdx], thinking }
            }
            if (finalOutput) {
              updated.push({ role: 'assistant', content: finalOutput })
              lastAssistantRef.current = finalOutput
            }
            return updated
          })
          setCollapsedThinking(prev => {
            const idx = pipelineUserMsgIdxRef.current
            return idx >= 0 ? { ...prev, [idx]: true } : prev
          })

        } else if (data.type === 'token' && !inPipelineRef.current) {
          setMessages(prev => {
            const last = prev[prev.length - 1]
            if (last?.role === 'assistant' && !last.thinking) {
              const next = last.content + data.content
              lastAssistantRef.current = next
              return [...prev.slice(0, -1), { ...last, content: next }]
            }
            lastAssistantRef.current = data.content
            return [...prev, { role: 'assistant', content: data.content }]
          })
          tokenCountRef.current += 1
          if (streamStartRef.current === null) streamStartRef.current = Date.now()
          const elapsed = (Date.now() - (streamStartRef.current ?? Date.now())) / 1000
          if (elapsed > 0) setStreamStats({ tps: tokenCountRef.current / elapsed, count: tokenCountRef.current })

        } else if (data.type === 'stats') {
          pendingOllamaStatsRef.current = {
            promptTokens: data.prompt_tokens as number,
            outputTokens: data.output_tokens as number,
            evalMs: data.eval_duration_ms as number,
          }

        } else if (data.type === 'done') {
          const pending = pendingOllamaStatsRef.current
          let finalStats: MsgStats | null = null
          if (pending && pending.outputTokens > 0 && pending.evalMs > 0) {
            finalStats = {
              tps: pending.outputTokens / (pending.evalMs / 1000),
              outputTokens: pending.outputTokens,
              promptTokens: pending.promptTokens,
              durationMs: pending.evalMs,
            }
          } else {
            const count = tokenCountRef.current
            const dur = streamStartRef.current !== null ? (Date.now() - streamStartRef.current) / 1000 : 0
            if (count > 0 && dur > 0) {
              finalStats = { tps: count / dur, outputTokens: count, promptTokens: 0, durationMs: Math.round(dur * 1000) }
            }
          }
          if (finalStats) {
            const s = finalStats
            setMessages(prev => {
              const last = prev[prev.length - 1]
              if (last?.role === 'assistant' && !last.thinking) return [...prev.slice(0, -1), { ...last, stats: s }]
              return prev
            })
          }
          pendingOllamaStatsRef.current = null
          setStreaming(false)
          setStreamStats(null)
          tokenCountRef.current = 0
          streamStartRef.current = null
          onAssistantDone?.(lastAssistantRef.current)
          lastAssistantRef.current = ''
          inPipelineRef.current = false

        } else if (data.type === 'error') {
          inPipelineRef.current = false
          setMessages(prev => [...prev, { role: 'assistant', content: data.content, isError: true }])
          setStreaming(false)
          setStreamStats(null)
          tokenCountRef.current = 0
          streamStartRef.current = null
          pendingOllamaStatsRef.current = null
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

  // ── Autocomplete ──────────────────────────────────────────────────────────

  const suggestions = useMemo(() => {
    if (input.includes(' ')) return []
    if (input.startsWith('@')) return AT_COMMANDS.filter(c => c.trigger.startsWith(input))
    if (input.startsWith('/')) return SLASH_COMMANDS.filter(c => c.trigger.startsWith(input))
    return []
  }, [input])

  useEffect(() => { setSelectedSuggestion(0) }, [suggestions])

  const applySuggestion = useCallback((trigger: string) => {
    setInput(trigger + ' ')
  }, [])

  // ── Skill handlers ────────────────────────────────────────────────────────

  const pushMsg = (role: Message['role'], content: string) =>
    setMessages(prev => [...prev, { role, content }])

  const streamSSE = useCallback(async (userText: string) => {
    pushMsg('user', userText)
    setStreaming(true)
    try {
      const res = await fetch(`${API}/skills/résumé`, { method: 'POST' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        pushMsg('assistant', `[erreur: ${(err as { detail?: string }).detail ?? res.status}]`)
        return
      }
      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue
          try {
            const ev = JSON.parse(part.slice(6))
            if (ev.type === 'token') {
              setMessages(prev => {
                const last = prev[prev.length - 1]
                if (last?.role === 'assistant') {
                  return [...prev.slice(0, -1), { ...last, content: last.content + ev.content }]
                }
                return [...prev, { role: 'assistant', content: ev.content }]
              })
            } else if (ev.type === 'error') {
              pushMsg('assistant', `[erreur: ${ev.content}]`)
            }
          } catch { /* skip */ }
        }
      }
    } catch {
      pushMsg('assistant', '[erreur réseau]')
    } finally {
      setStreaming(false)
    }
  }, [])

  const handleMémoire = useCallback(async (userText: string) => {
    pushMsg('user', userText)
    try {
      const res = await fetch(`${API}/memory/context`)
      const data = await res.json() as { context: string }
      pushMsg('assistant', data.context)
    } catch {
      pushMsg('assistant', '[erreur lecture mémoire]')
    }
  }, [])

  const handleModèle = useCallback(async (userText: string, nom: string) => {
    pushMsg('user', userText)
    try {
      await fetch(`${API}/context/settings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 'modèle_actif': nom }),
      })
      pushMsg('assistant', `Modèle → ${nom}`)
    } catch {
      pushMsg('assistant', '[erreur changement modèle]')
    }
  }, [])

  const handleLacunes = useCallback(async (userText: string) => {
    pushMsg('user', userText)
    try {
      const res = await fetch(`${API}/memory/lacunes`)
      const data = await res.json() as {
        lacunes: string[]
        erreurs_recentes: { date: string; erreur: string }[]
      }
      const lines: string[] = []
      if (data.lacunes.length > 0) {
        lines.push('LACUNES CONFIRMÉES')
        data.lacunes.forEach(l => lines.push(`  · ${l}`))
      } else {
        lines.push('Aucune lacune confirmée.')
      }
      if (data.erreurs_recentes.length > 0) {
        lines.push('')
        lines.push('ERREURS RÉCENTES (7j)')
        data.erreurs_recentes.forEach(e => lines.push(`  · [${e.date}] ${e.erreur}`))
      }
      pushMsg('assistant', lines.join('\n'))
    } catch {
      pushMsg('assistant', '[erreur lecture lacunes]')
    }
  }, [])

  const handleNavigate = useCallback(
    (userText: string, module: 'kholle' | 'flashcards', param?: string) => {
      pushMsg('user', userText)
      onNavigate?.(module)
      const label = module === 'kholle' ? 'Kholle' : 'Flashcards'
      pushMsg('assistant', `→ ${label}${param ? ` — ${param}` : ''}`)
    },
    [onNavigate]
  )

  // ── Send ──────────────────────────────────────────────────────────────────

  const send = useCallback(async () => {
    const rawText = input.trim()
    if (!rawText || streaming) return
    setInput('')

    if (rawText.startsWith('/')) {
      const [cmd, ...argParts] = rawText.slice(1).trim().split(/\s+/)
      const arg = argParts.join(' ')
      switch (cmd?.toLowerCase()) {
        case 'kholle':
          handleNavigate(rawText, 'kholle', arg || undefined)
          return
        case 'flashcards':
          handleNavigate(rawText, 'flashcards', arg || undefined)
          return
        case 'résumé':
          await streamSSE(rawText)
          return
        case 'modèle':
          if (arg) await handleModèle(rawText, arg)
          else { pushMsg('user', rawText); pushMsg('assistant', 'Usage : /modèle <nom>') }
          return
        case 'lacunes':
          await handleLacunes(rawText)
          return
        case 'direct': {
          if (!arg) {
            pushMsg('user', rawText)
            pushMsg('assistant', 'Usage : /direct [message] — envoie sans orchestrateur')
            return
          }
          if (!connected) return
          pushMsg('user', rawText)
          setStreaming(true)
          tokenCountRef.current = 0
          streamStartRef.current = null
          pendingOllamaStatsRef.current = null
          setStreamStats(null)
          inPipelineRef.current = false
          wsRef.current?.send(JSON.stringify({ role: 'user', content: arg, effort: 'direct' }))
          return
        }
      }
    }

    if (rawText === '@mémoire' || rawText.startsWith('@mémoire ')) {
      await handleMémoire(rawText)
      return
    }

    if (!connected) return

    let cleanText = rawText
    let ragOverride: string | undefined
    let strictOverride = false

    let again = true
    while (again) {
      again = false
      if (cleanText === '@cours' || cleanText.startsWith('@cours ')) {
        ragOverride = 'all'
        cleanText = cleanText.replace(/^@cours\s*/, '').trim()
        again = true
      } else if (cleanText === '@strict' || cleanText.startsWith('@strict ')) {
        strictOverride = true
        cleanText = cleanText.replace(/^@strict\s*/, '').trim()
        again = true
      }
    }

    pushMsg('user', rawText)
    setStreaming(true)
    tokenCountRef.current = 0
    streamStartRef.current = null
    pendingOllamaStatsRef.current = null
    setStreamStats(null)
    inPipelineRef.current = false
    pipelineUserMsgIdxRef.current = -1

    const wsMsg: Record<string, unknown> = {
      role: 'user',
      content: cleanText || rawText,
      effort,
    }
    if (effort !== 'direct' && pipelineSteps.length > 0) {
      wsMsg.steps = pipelineSteps
    }
    if (ragOverride) wsMsg.rag_override = ragOverride
    if (strictOverride) wsMsg.strict_override = true
    wsRef.current?.send(JSON.stringify(wsMsg))
  }, [
    input, connected, streaming, effort, pipelineSteps,
    streamSSE, handleMémoire, handleModèle, handleLacunes, handleNavigate,
  ])

  // ── Keyboard ──────────────────────────────────────────────────────────────

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (suggestions.length > 0) {
      if (e.key === 'Escape') { setInput(''); e.preventDefault(); return }
      if (e.key === 'ArrowUp') { setSelectedSuggestion(i => Math.max(0, i - 1)); e.preventDefault(); return }
      if (e.key === 'ArrowDown') { setSelectedSuggestion(i => Math.min(suggestions.length - 1, i + 1)); e.preventDefault(); return }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault()
        applySuggestion(suggestions[selectedSuggestion].trigger)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <main className="flex flex-col flex-1 overflow-hidden">
      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-5">
        {messages.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <span className="text-xs font-mono text-[#2a2a2a] select-none">— en attente —</span>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex group ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[78%] ${
                msg.role === 'user'
                  ? 'px-4 py-3 rounded bg-[#1a1a1a] border border-[#282828] text-sm leading-relaxed text-[#d8d8d8]'
                  : 'text-sm leading-relaxed text-[#b8b8b8] font-mono'
              }`}
            >
              {msg.role === 'user' ? (
                <>
                  <p className="whitespace-pre-wrap break-words m-0">{msg.content}</p>
                  {msg.thinking && (
                    <ThinkingBlockView
                      thinking={msg.thinking}
                      collapsed={collapsedThinking[i] ?? false}
                      onToggle={() => setCollapsedThinking(prev => ({ ...prev, [i]: !prev[i] }))}
                    />
                  )}
                </>
              ) : msg.isError ? (
                <p className="text-xs text-[#7a3a3a] whitespace-pre-wrap">{msg.content}</p>
              ) : (
                <RichMessage content={msg.content} streaming={streaming && i === messages.length - 1} />
              )}
              {msg.role === 'assistant' && i === messages.length - 1 && streaming && streamStats && (
                <div className="mt-1 text-[10px] font-mono text-[#2a2a2a]">
                  {streamStats.tps.toFixed(1)} tok/s · {streamStats.count} tokens
                </div>
              )}
              {msg.role === 'assistant' && msg.stats && (
                <div className="mt-1 text-[10px] font-mono text-[#2a2a2a]">
                  {msg.stats.tps.toFixed(1)} tok/s · {msg.stats.durationMs}ms · {msg.stats.promptTokens}in / {msg.stats.outputTokens}out tokens
                </div>
              )}
              {msg.role === 'assistant' && playSpeech && (
                <div className="mt-2 flex">
                  <button
                    onClick={() => speakingText === msg.content ? stopSpeech?.() : playSpeech(msg.content)}
                    className={`text-xs font-mono transition-colors
                      [@media(pointer:fine)]:opacity-0 [@media(pointer:fine)]:group-hover:opacity-100
                      [@media(pointer:coarse)]:opacity-100
                      ${speakingText === msg.content
                        ? 'text-[#5a9a5a] hover:text-[#7aba7a]'
                        : 'text-[#333] hover:text-[#888]'}`}
                    title={speakingText === msg.content ? 'Arrêter' : 'Lire'}
                  >
                    {speakingText === msg.content ? '■' : '▶'}
                  </button>
                </div>
              )}
            </div>
          </div>
        ))}
        {streaming && messages[messages.length - 1]?.role !== 'assistant' && !inPipelineRef.current && (
          <div className="flex justify-start">
            <span className="text-xs font-mono text-[#333] animate-pulse">▍</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-[#1e1e1e] px-4 py-4 relative">
        {suggestions.length > 0 && (
          <div className="absolute bottom-full left-4 mb-2 bg-[#141414] border border-[#282828] rounded shadow-lg overflow-hidden z-10 min-w-60">
            {suggestions.map((s, i) => (
              <button
                key={s.trigger}
                onMouseDown={e => { e.preventDefault(); applySuggestion(s.trigger) }}
                className={`w-full text-left px-3 py-2 flex gap-3 items-baseline transition-colors ${
                  i === selectedSuggestion ? 'bg-[#1e1e1e]' : 'hover:bg-[#181818]'
                }`}
              >
                <span className="text-xs font-mono text-[#6a9a6a] shrink-0">{s.trigger}</span>
                <span className="text-[10px] font-mono text-[#444] truncate">{s.desc}</span>
              </button>
            ))}
          </div>
        )}

        <div className="flex gap-3 items-end">
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={streaming}
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
            onClick={() => { send() }}
            disabled={streaming || !input.trim()}
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
