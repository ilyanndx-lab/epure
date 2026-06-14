import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { usePersistentState } from '../../usePersistentState'
import { ChevronDown, Brain, Check, X, Circle, Loader2, Sparkles, Send, Play, Square, Globe, RotateCcw } from 'lucide-react'
import { Card, Textarea, Toggle } from '../../components/ui'
import RichMessage from '../../components/RichMessage'
import ModuleBar from '../../components/ModuleBar'
import type { EffortLevel, StepConfig } from '../../App'

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
  onAssistantDone?: (text: string) => void
  playSpeech?: (text: string) => void
  stopSpeech?: () => void
  speakingText?: string | null
  onNavigate?: (module: 'chat' | 'kholle' | 'flashcards' | 'settings') => void
  ttsEnabled?: boolean
  onTtsToggle?: () => void
}

const AT_COMMANDS = [
  { trigger: '@cours',      desc: 'RAG sur tous les fichiers indexés' },
  { trigger: '@strict',     desc: 'Réponse concise, sans intro' },
  { trigger: '@mémoire',    desc: 'Affiche le contexte mémoire actuel' },
  { trigger: '@historique', desc: 'Recherche dans les échanges passés [sujet]' },
  { trigger: '@web',        desc: 'Recherche web complémentaire avant la réponse [sujet]' },
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
    <Card accent="secondary" padded={false} className="mt-2 mb-1 overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-elevated transition-colors duration-150"
      >
        <span className="text-xs text-secondary flex items-center gap-2">
          <Brain size={14} className={`text-accent2 shrink-0 ${thinking.done ? '' : 'animate-pulse'}`} />
          <span>Réflexion · {label}</span>
        </span>
        <ChevronDown
          size={14}
          className={`text-muted shrink-0 transition-transform duration-150 ${collapsed ? '' : 'rotate-180'}`}
        />
      </button>

      {!collapsed && (
        <div className="border-t border-line divide-y divide-line">
          {thinking.steps.map((step, i) => (
            <div key={i} className="px-3 py-2">
              <div className="flex items-center gap-2 mb-1">
                <span className={`text-xs font-mono shrink-0 inline-flex items-center gap-1.5 ${
                  step.status === 'done' ? 'text-success'
                  : step.status === 'running' ? 'text-warning animate-pulse'
                  : step.status === 'error' ? 'text-error'
                  : 'text-muted'
                }`}>
                  {step.status === 'done' ? <Check size={12} /> : step.status === 'running' ? <Loader2 size={12} className="animate-spin" /> : step.status === 'error' ? <X size={12} /> : <Circle size={12} />}
                  {' '}{String(i + 1).padStart(2, '0')} {step.label}
                </span>
                <span className="text-xs font-mono text-muted shrink-0">
                  {step.model.split(':').pop()}
                </span>
                {step.stats && (
                  <span className="text-xs font-mono text-muted shrink-0">
                    {step.stats.tps.toFixed(1)} tok/s · {step.stats.tokens} tokens · {fmtDuration(step.stats.duration_ms)}
                  </span>
                )}
              </div>
              {step.errorMsg ? (
                <p className="text-xs font-mono text-error">{step.errorMsg}</p>
              ) : step.output ? (
                <div className="text-sm text-secondary max-h-40 overflow-y-auto">
                  <RichMessage content={step.output} streaming={step.status === 'running'} />
                </div>
              ) : step.status === 'running' ? (
                <span className="text-xs font-mono text-muted animate-pulse">▍</span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export default function Chat({
  onAssistantDone,
  playSpeech,
  stopSpeech,
  speakingText,
  onNavigate,
  ttsEnabled,
  onTtsToggle,
}: ChatProps) {
  const [effort, setEffort] = usePersistentState<EffortLevel>('epure.chat.effort', 'direct')
  const [pipelineSteps, setPipelineSteps] = useState<StepConfig[]>([])
  const [messages, setMessages] = usePersistentState<Message[]>('epure.chat.messages', [])
  const [input, setInput] = usePersistentState<string>('epure.chat.input', '')
  const [connected, setConnected] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)
  const [streamStats, setStreamStats] = useState<{ tps: number; count: number } | null>(null)
  const [collapsedThinking, setCollapsedThinking] = useState<Record<number, boolean>>({})

  // Recherche web : active = force une recherche avant la réponse.
  // Mode 'once' = réinitialisé après chaque message (défaut, non handicapant) ;
  // 'always' = reste actif jusqu'à désactivation explicite.
  const [webSearch, setWebSearch] = usePersistentState<boolean>('epure.chat.webSearch', false)
  const [webSearchMode, setWebSearchMode] = usePersistentState<'once' | 'always'>('epure.chat.webSearchMode', 'once')
  const [webMenuOpen, setWebMenuOpen] = useState(false)

  const wsRef = useRef<WebSocket | null>(null)
  const webMenuRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const lastAssistantRef = useRef('')
  const tokenCountRef = useRef(0)
  const streamStartRef = useRef<number | null>(null)
  const pendingOllamaStatsRef = useRef<{ promptTokens: number; outputTokens: number; evalMs: number } | null>(null)
  const inPipelineRef = useRef(false)
  const pipelineUserMsgIdxRef = useRef(-1)
  // Arrêt : ignore les events de streaming entrants après un stop manuel.
  const cancelledRef = useRef(false)
  // Dernier message envoyé (pour « relancer »).
  const lastSentRef = useRef<Record<string, unknown> | null>(null)

  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(WS_URL)
      ws.onopen = () => setConnected(true)
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000) }
      ws.onmessage = (event) => {
        const data = JSON.parse(event.data)

        // Après un arrêt manuel : on ignore les tokens encore en vol, mais on
        // laisse passer done/error pour réinitialiser proprement l'état.
        if (cancelledRef.current && data.type !== 'done' && data.type !== 'error') return

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
          cancelledRef.current = false

        } else if (data.type === 'error') {
          inPipelineRef.current = false
          cancelledRef.current = false
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

  // Ferme le menu de recherche web au clic extérieur.
  useEffect(() => {
    if (!webMenuOpen) return
    const onDown = (e: MouseEvent) => {
      if (webMenuRef.current && !webMenuRef.current.contains(e.target as Node)) {
        setWebMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [webMenuOpen])

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

  // Envoi d'un message « normal » (hors commandes /…) — factorisé pour être
  // réutilisé par « relancer ».
  const sendUserText = useCallback((rawText: string) => {
    if (!connected) return
    cancelledRef.current = false

    let cleanText = rawText
    let ragOverride: string | undefined
    let strictOverride = false
    let webSearchOverride = webSearch

    let again = true
    while (again) {
      again = false
      if (cleanText === '@cours' || cleanText.startsWith('@cours ')) {
        ragOverride = 'all'; cleanText = cleanText.replace(/^@cours\s*/, '').trim(); again = true
      } else if (cleanText === '@strict' || cleanText.startsWith('@strict ')) {
        strictOverride = true; cleanText = cleanText.replace(/^@strict\s*/, '').trim(); again = true
      } else if (cleanText === '@web' || cleanText.startsWith('@web ')) {
        webSearchOverride = true; cleanText = cleanText.replace(/^@web\s*/, '').trim(); again = true
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

    const wsMsg: Record<string, unknown> = { role: 'user', content: cleanText || rawText, effort }
    if (effort !== 'direct' && pipelineSteps.length > 0) wsMsg.steps = pipelineSteps
    if (ragOverride) wsMsg.rag_override = ragOverride
    if (strictOverride) wsMsg.strict_override = true
    if (webSearchOverride) wsMsg.web_search_override = true
    lastSentRef.current = wsMsg
    wsRef.current?.send(JSON.stringify(wsMsg))

    if (webSearch && webSearchMode === 'once') setWebSearch(false)
  }, [connected, effort, pipelineSteps, webSearch, webSearchMode, pushMsg, setWebSearch])

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
          cancelledRef.current = false
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

    // Message normal : délégué à sendUserText (réutilisé par « relancer »).
    sendUserText(rawText)
  }, [
    input, connected, streaming, sendUserText,
    streamSSE, handleMémoire, handleModèle, handleLacunes, handleNavigate,
  ])

  // ── Stop & relancer ─────────────────────────────────────────────────────────

  const stop = useCallback(() => {
    if (!streaming) return
    // Arrêt côté client : on cesse d'afficher les tokens et on débloque l'UI.
    // (Le backend termine sa génération en silence ; ses tokens sont ignorés
    // grâce à cancelledRef, et le 'done' final réinitialise l'état.)
    cancelledRef.current = true
    setStreaming(false)
    setStreamStats(null)
    inPipelineRef.current = false
  }, [streaming])

  const relancer = useCallback(() => {
    if (streaming || !connected) return
    const lastUser = [...messages].reverse().find(m => m.role === 'user')
    if (lastUser) sendUserText(lastUser.content)
  }, [streaming, connected, messages, sendUserText])

  const canResume = !streaming && messages.some(m => m.role === 'user')

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
          <div className="flex flex-col items-center justify-center gap-2 h-full text-muted select-none">
            <Sparkles size={16} />
            <span className="text-sm">En attente d'un message</span>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex group ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[78%] ${
                msg.role === 'user'
                  ? 'px-4 py-3 rounded-lg bg-elevated border border-line text-sm leading-relaxed text-primary'
                  : 'text-sm leading-relaxed text-secondary'
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
                <p className="text-xs text-error whitespace-pre-wrap">{msg.content}</p>
              ) : (
                <RichMessage content={msg.content} streaming={streaming && i === messages.length - 1} />
              )}
              {msg.role === 'assistant' && i === messages.length - 1 && streaming && streamStats && (
                <div className="mt-1 text-xs font-mono text-muted/70">
                  {streamStats.tps.toFixed(1)} tok/s · {streamStats.count} tokens
                </div>
              )}
              {msg.role === 'assistant' && msg.stats && (
                <div className="mt-1 text-xs font-mono text-muted/70">
                  {msg.stats.tps.toFixed(1)} tok/s · {msg.stats.durationMs}ms · {msg.stats.promptTokens}in / {msg.stats.outputTokens}out tokens
                </div>
              )}
              {msg.role === 'assistant' && playSpeech && (
                <div className="mt-2 flex">
                  <button
                    onClick={() => speakingText === msg.content ? stopSpeech?.() : playSpeech(msg.content)}
                    className={`transition-colors duration-150
                      [@media(pointer:fine)]:opacity-0 [@media(pointer:fine)]:group-hover:opacity-100
                      [@media(pointer:coarse)]:opacity-100
                      ${speakingText === msg.content
                        ? 'text-accent2 hover:text-accent2-hover'
                        : 'text-muted hover:text-secondary'}`}
                    title={speakingText === msg.content ? 'Arrêter' : 'Lire'}
                  >
                    {speakingText === msg.content ? <Square size={13} fill="currentColor" /> : <Play size={13} />}
                  </button>
                </div>
              )}
            </div>
          </div>
        ))}
        {streaming && messages[messages.length - 1]?.role !== 'assistant' && !inPipelineRef.current && (
          <div className="flex justify-start">
            <span className="text-xs font-mono text-accent2 animate-pulse">▍</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <ModuleBar
        module="chat"
        showFile
        showMic
        showSkills
        showModel
        showEffort
        onTranscribed={(t) => setInput(prev => prev + t)}
        ttsEnabled={ttsEnabled}
        onTtsToggle={onTtsToggle}
        speakingText={speakingText}
        effort={effort}
        onEffortChange={setEffort}
        pipelineSteps={pipelineSteps}
        onPipelineStepsChange={setPipelineSteps}
      />

      <div className="border-t border-line px-4 py-4 relative">
        {suggestions.length > 0 && (
          <div className="absolute bottom-full left-4 mb-2 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-10 min-w-60">
            {suggestions.map((s, i) => (
              <button
                key={s.trigger}
                onMouseDown={e => { e.preventDefault(); applySuggestion(s.trigger) }}
                className={`w-full text-left px-3 py-2 flex gap-3 items-baseline transition-colors duration-150 ${
                  i === selectedSuggestion ? 'bg-accent/10' : 'hover:bg-surface'
                }`}
              >
                <span className="text-xs font-mono text-accent2 shrink-0">{s.trigger}</span>
                <span className="text-xs text-muted truncate">{s.desc}</span>
              </button>
            ))}
          </div>
        )}

        <div className="flex gap-3 items-end">
          {/* ── Recherche web : icône cliquable + menu déroulable ── */}
          <div className="relative shrink-0" ref={webMenuRef}>
            <div
              className={`flex items-stretch rounded-md border transition-colors duration-150 ${
                webSearch ? 'border-accent/40 bg-accent/10' : 'border-line bg-elevated'
              }`}
            >
              <button
                type="button"
                onClick={() => setWebSearch(v => !v)}
                aria-pressed={webSearch}
                title={webSearch
                  ? 'Recherche web activée — forcée avant la réponse'
                  : 'Forcer une recherche web avant la réponse'}
                className={`relative p-2.5 rounded-l-md transition-colors duration-150 ${
                  webSearch ? 'text-accent' : 'text-muted hover:text-secondary'
                }`}
              >
                <Globe size={16} className={webSearch && streaming ? 'animate-pulse' : ''} />
                {webSearch && (
                  <span className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-accent text-on-accent text-[10px] font-mono leading-none flex items-center justify-center">
                    {webSearchMode === 'once' ? '1×' : '∞'}
                  </span>
                )}
              </button>
              <button
                type="button"
                onClick={() => setWebMenuOpen(v => !v)}
                aria-haspopup="menu"
                aria-expanded={webMenuOpen}
                title="Options de recherche web"
                className={`px-1 rounded-r-md border-l transition-colors duration-150 ${
                  webSearch
                    ? 'border-accent/30 text-accent hover:bg-accent/10'
                    : 'border-line text-muted hover:text-secondary hover:bg-elevated'
                }`}
              >
                <ChevronDown
                  size={13}
                  className={`transition-transform duration-150 ${webMenuOpen ? 'rotate-180' : ''}`}
                />
              </button>
            </div>

            {webMenuOpen && (
              <div className="absolute bottom-full left-0 mb-2 w-64 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20">
                <div className="flex items-center justify-between px-3 py-2.5 border-b border-line">
                  <span className="text-xs font-medium text-primary flex items-center gap-2">
                    <Globe size={13} className={webSearch ? 'text-accent' : 'text-muted'} />
                    Recherche web
                  </span>
                  <Toggle checked={webSearch} onChange={setWebSearch} label="Activer la recherche web" />
                </div>

                <div className="p-1.5 space-y-0.5">
                  <p className="px-2 py-1 text-xs text-muted uppercase tracking-wide">Mode</p>
                  {([
                    { id: 'once', label: 'Activer une fois', desc: 'Réinitialisé après chaque message' },
                    { id: 'always', label: 'Toujours activé', desc: "Reste actif jusqu'à désactivation" },
                  ] as const).map(opt => {
                    const selected = webSearchMode === opt.id
                    return (
                      <button
                        key={opt.id}
                        onClick={() => { setWebSearchMode(opt.id); setWebSearch(true) }}
                        className={`w-full text-left px-2.5 py-1.5 rounded-sm transition-colors duration-150 flex items-start gap-2 ${
                          selected ? 'bg-accent/10' : 'hover:bg-surface'
                        }`}
                      >
                        <span className="shrink-0 w-4 inline-flex justify-center pt-0.5">
                          {selected
                            ? <Check size={13} className="text-accent" />
                            : <span className="w-1.5 h-1.5 rounded-full bg-line inline-block mt-1" />}
                        </span>
                        <span className="flex-1 min-w-0">
                          <span className={`block text-xs ${selected ? 'text-accent font-medium' : 'text-secondary'}`}>
                            {opt.label}
                          </span>
                          <span className="block text-[11px] text-muted">{opt.desc}</span>
                        </span>
                      </button>
                    )
                  })}
                </div>

                <div className="px-3 py-2.5 border-t border-line space-y-1.5">
                  <p className="text-xs text-muted uppercase tracking-wide">Sources utilisées</p>
                  <div className="flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-accent2 shrink-0" />
                    <span className="text-xs text-secondary">DuckDuckGo</span>
                    <span className="text-[11px] font-mono text-muted ml-auto">Instant + HTML</span>
                  </div>
                </div>
              </div>
            )}
          </div>

          <Textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={streaming}
            placeholder={connected ? 'Message...' : 'Connexion au serveur...'}
            rows={1}
            className="flex-1"
            style={{ minHeight: '40px', maxHeight: '160px' }}
            onInput={e => {
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = `${Math.min(el.scrollHeight, 160)}px`
            }}
          />
          {streaming ? (
            <button
              onClick={stop}
              title="Arrêter la génération"
              className="p-2.5 rounded-md bg-error/90 text-on-accent shadow-sm hover:opacity-90 transition-all duration-150 shrink-0"
            >
              <Square size={16} fill="currentColor" />
            </button>
          ) : (
            <>
              {canResume && !input.trim() && (
                <button
                  onClick={relancer}
                  title="Relancer le dernier message"
                  className="p-2.5 rounded-md border border-line text-muted hover:text-secondary hover:bg-elevated transition-all duration-150 shrink-0"
                >
                  <RotateCcw size={16} />
                </button>
              )}
              <button
                onClick={() => { send() }}
                disabled={!input.trim()}
                title="Envoyer"
                className="p-2.5 rounded-md bg-gradient-primary text-on-accent shadow-sm hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 shrink-0"
              >
                <Send size={16} />
              </button>
            </>
          )}
        </div>
        {!connected && (
          <div className="mt-2 text-xs font-mono text-error">ws déconnecté — reconnexion...</div>
        )}
      </div>
    </main>
  )
}
