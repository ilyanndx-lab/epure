import { useState, useEffect, useRef, useCallback } from 'react'
import Editor from '@monaco-editor/react'
import {
  Brain, Check, ChevronDown, ChevronRight, Code2, CornerDownLeft, FilePlus,
  FlaskConical, FolderPlus, Loader2, Monitor, Package, Play, RefreshCw,
  Save, Terminal as TerminalIcon, Wrench, X, Zap,
} from 'lucide-react'
import { Badge, Button, Input, Select, Toggle } from './ui'
import RichMessage from './RichMessage'
import ModuleBar from './ModuleBar'

const API = 'http://localhost:8000'

// ── Types ──────────────────────────────────────────────────────────────────

interface TreeNode {
  name: string
  path: string
  type: 'file' | 'dir'
  children?: TreeNode[]
}

interface OpenFile {
  path: string
  content: string
  dirty: boolean
}

type ChatEventType =
  | { type: 'text'; content: string; streaming: boolean }
  | { type: 'reflection'; content: string; streaming: boolean }
  | { type: 'tool_call'; tool: string; path: string; status: 'pending' | 'success' | 'error' }
  | { type: 'tool_result'; tool: string; path: string; result: string; status: 'success' | 'error' }
  | { type: 'verification'; path: string; result: string; pending: boolean }
  | { type: 'tests_prompt'; path: string }
  | { type: 'tests_result'; path: string; content: string; streaming: boolean }
  | { type: 'execute_request'; path: string; args: string }
  | { type: 'execute_result'; stdout: string; stderr: string; returncode: number; duration_ms: number }
  | { type: 'execute_external'; path: string }
  | { type: 'html_preview'; content: string }

interface TurnStats {
  reflection: number
  generation: number
  verification: number
  tests: number
}

interface ChatTurn {
  role: 'user' | 'agent'
  events: ChatEventType[]
  stats: TurnStats
}

interface UsageStats {
  session: { total_tokens: number; providers: Record<string, number> }
  session_tokens_by_step: Record<string, number>
}

// ── Helpers ────────────────────────────────────────────────────────────────

function extLang(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  const map: Record<string, string> = {
    py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript',
    jsx: 'javascript', json: 'json', md: 'markdown', html: 'html',
    css: 'css', yaml: 'yaml', yml: 'yaml', sh: 'shell', txt: 'plaintext',
    rs: 'rust', go: 'go', cpp: 'cpp', c: 'c', java: 'java',
  }
  return map[ext] ?? 'plaintext'
}

// ── File Tree ──────────────────────────────────────────────────────────────

function TreeNodeItem({
  node, depth, activeFile, onSelect, onDelete,
}: {
  node: TreeNode
  depth: number
  activeFile: string | null
  onSelect: (path: string) => void
  onDelete: (path: string, e: React.MouseEvent) => void
}) {
  const [open, setOpen] = useState(depth === 0)
  const isActive = node.type === 'file' && activeFile === node.path

  if (node.type === 'dir') {
    return (
      <div>
        <div
          className="flex items-center gap-1 px-2 py-1 cursor-pointer hover:bg-elevated rounded-sm text-xs font-mono text-muted group transition-colors duration-150"
          style={{ paddingLeft: `${8 + depth * 12}px` }}
          onClick={() => setOpen(o => !o)}
        >
          {open ? <ChevronDown size={11} className="shrink-0" /> : <ChevronRight size={11} className="shrink-0" />}
          <span>{node.name}/</span>
        </div>
        {open && node.children?.map(child => (
          <TreeNodeItem
            key={child.path}
            node={child}
            depth={depth + 1}
            activeFile={activeFile}
            onSelect={onSelect}
            onDelete={onDelete}
          />
        ))}
      </div>
    )
  }

  return (
    <div
      className={`relative flex items-center justify-between px-2 py-1 rounded-sm cursor-pointer group text-xs font-mono transition-colors duration-150 ${
        isActive ? 'bg-accent/10 text-primary' : 'text-secondary hover:text-primary hover:bg-elevated'
      }`}
      style={{ paddingLeft: `${8 + depth * 12}px` }}
      onClick={() => onSelect(node.path)}
    >
      {isActive && <span className="absolute left-0 top-1 bottom-1 w-0.5 rounded-full bg-accent" />}
      <span className="truncate">{node.name}</span>
      <button
        onClick={e => onDelete(node.path, e)}
        title="Supprimer"
        className="opacity-0 group-hover:opacity-100 text-muted hover:text-error shrink-0 ml-1 transition-colors duration-150"
      >
        <X size={11} />
      </button>
    </div>
  )
}

// ── Terminal output ────────────────────────────────────────────────────────

function Terminal({
  event,
  onInstall,
}: {
  event: Extract<ChatEventType, { type: 'execute_result' }>
  onInstall: (pkg: string) => void
}) {
  console.log('[execute_result]', event)
  const ms = event.duration_ms
  const dur = ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`

  // Detect missing module in stderr
  const missingModule = (() => {
    const m = event.stderr.match(/ModuleNotFoundError: No module named '([^'.]+)/)
    return m ? m[1] : null
  })()

  // Terminal volontairement SOMBRE même en thème light (couleurs fixes)
  return (
    <div className="mt-2 rounded-md border border-line overflow-hidden">
      <div className="px-3 py-1.5 bg-[#16140f] border-b border-[#2a2620] flex items-center justify-between">
        <span className="text-xs font-mono text-[#8a8478] flex items-center gap-1.5">
          <TerminalIcon size={11} />
          output · {dur}
        </span>
        <span className={`text-xs font-mono ${event.returncode === 0 ? 'text-success' : 'text-error'}`}>
          exit {event.returncode}
        </span>
      </div>
      <div className="bg-[#100e0a] px-3 py-2 font-mono text-xs leading-relaxed">
        {event.stdout && (
          <pre className="text-[#d4cfc4] whitespace-pre-wrap break-words">{event.stdout}</pre>
        )}
        {event.stderr && (
          <pre className="text-error whitespace-pre-wrap break-words">{event.stderr}</pre>
        )}
        {!event.stdout && !event.stderr && (
          <span className={event.returncode === 0 ? 'text-success' : 'text-[#8a8478]'}>
            {event.returncode === 0 ? '✓ Exécution terminée (pas de sortie)' : '(pas de sortie)'}
          </span>
        )}
      </div>
      {missingModule && (
        <div className="px-3 py-2 bg-[#16140f] border-t border-[#2a2620] flex items-center gap-2">
          <span className="text-xs font-mono text-warning">
            Module '{missingModule}' non trouvé.
          </span>
          <button
            onClick={() => onInstall(missingModule)}
            className="text-xs font-mono text-accent2 hover:text-accent2-hover border border-accent2/30 rounded-sm px-2 py-0.5 transition-colors duration-150"
          >
            Installer {missingModule} ?
          </button>
        </div>
      )}
    </div>
  )
}

// ── Chat event renderer ───────────────────────────────────────────────────

function Collapsible({ label, icon, children, defaultOpen = false }: {
  label: string
  icon?: React.ReactNode
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border border-line border-l-2 border-l-accent2/50 rounded-sm overflow-hidden my-1 bg-surface">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-muted hover:text-secondary hover:bg-elevated transition-colors duration-150 text-left"
      >
        <ChevronRight size={11} className={`shrink-0 transition-transform duration-150 ${open ? 'rotate-90' : ''}`} />
        {icon}
        <span>{label}</span>
      </button>
      {open && <div className="px-3 pb-2 pt-1">{children}</div>}
    </div>
  )
}

function ChatEventView({
  event,
  onExecuteConfirm,
  onExecuteCancel,
  onInstall,
  onGenerateTests,
}: {
  event: ChatEventType
  onExecuteConfirm: (path: string, args: string) => void
  onExecuteCancel: () => void
  onInstall: (pkg: string) => void
  onGenerateTests: (path: string) => void
}) {
  if (event.type === 'text') {
    return <RichMessage content={event.content} streaming={event.streaming} />
  }
  if (event.type === 'reflection') {
    return (
      <Collapsible
        label={`Réflexion${event.streaming ? ' ...' : ''}`}
        icon={<Brain size={11} className="text-accent2 shrink-0" />}
        defaultOpen={false}
      >
        <div className="text-xs font-mono text-secondary leading-relaxed whitespace-pre-wrap">
          {event.content}
          {event.streaming && <span className="animate-pulse text-accent2">▍</span>}
        </div>
      </Collapsible>
    )
  }
  if (event.type === 'tool_call') {
    return (
      <div className={`flex items-center gap-2 text-xs font-mono py-1 ${
        event.status === 'pending' ? 'text-warning' : 'text-muted'
      }`}>
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
          event.status === 'pending' ? 'bg-warning animate-pulse' : 'bg-line'
        }`} />
        <Wrench size={11} className="shrink-0" />
        <span>{event.tool}</span>
        {event.path && <span className="text-muted/60">{event.path}</span>}
      </div>
    )
  }
  if (event.type === 'tool_result') {
    return (
      <div className={`flex items-start gap-2 text-xs font-mono py-1 ${
        event.status === 'success' ? 'text-success' : 'text-error'
      }`}>
        {event.status === 'success'
          ? <Check size={12} className="shrink-0 mt-0.5" />
          : <X size={12} className="shrink-0 mt-0.5" />}
        <span className="break-words">{event.result}</span>
      </div>
    )
  }
  if (event.type === 'execute_request') {
    return (
      <div className="mt-2 border border-warning/30 rounded-md bg-warning/5 p-3">
        <div className="text-xs font-mono mb-2 text-secondary">
          Exécuter <span className="text-primary">{event.path}</span>
          {event.args && <span className="text-muted"> {event.args}</span>}
        </div>
        <div className="flex gap-2">
          <Button variant="primary" size="sm" icon={<Play size={12} />} onClick={() => onExecuteConfirm(event.path, event.args)}>
            exécuter
          </Button>
          <Button variant="ghost" size="sm" icon={<X size={12} />} onClick={onExecuteCancel}>
            annuler
          </Button>
        </div>
      </div>
    )
  }
  if (event.type === 'verification') {
    return (
      <div className={`flex items-start gap-2 text-xs font-mono py-1 ${
        event.pending ? 'text-warning' : event.result.startsWith('✓') ? 'text-success' : 'text-warning'
      }`}>
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 mt-1 ${
          event.pending ? 'bg-warning animate-pulse' : event.result.startsWith('✓') ? 'bg-success' : 'bg-warning'
        }`} />
        <div>
          <span>{event.pending ? `vérification ${event.path}...` : event.result}</span>
        </div>
      </div>
    )
  }
  if (event.type === 'tests_prompt') {
    return (
      <div className="flex items-center gap-2 py-1">
        <span className="text-xs font-mono text-muted flex items-center gap-1">
          <FlaskConical size={11} />
          tests disponibles
        </span>
        <Button variant="secondary" size="sm" onClick={() => onGenerateTests(event.path)}>
          Générer les tests
        </Button>
      </div>
    )
  }
  if (event.type === 'tests_result') {
    return (
      <Collapsible
        label={`Tests${event.streaming ? ' (génération...)' : event.path ? ` → ${event.path}` : ''}`}
        icon={<FlaskConical size={11} className="text-accent2 shrink-0" />}
        defaultOpen={!event.streaming}
      >
        <pre className="text-xs font-mono text-secondary whitespace-pre-wrap leading-relaxed">
          {event.content}
          {event.streaming && <span className="animate-pulse text-accent2">▍</span>}
        </pre>
      </Collapsible>
    )
  }
  if (event.type === 'execute_result') {
    return <Terminal event={event} onInstall={onInstall} />
  }
  if (event.type === 'execute_external') {
    return (
      <div className="mt-2 border border-accent2/30 rounded-md bg-accent2/5 px-3 py-2">
        <span className="text-xs font-mono text-accent2 flex items-center gap-1.5">
          <Monitor size={12} />
          Application GUI — lancée dans une fenêtre externe
        </span>
      </div>
    )
  }
  if (event.type === 'html_preview') {
    return (
      <div className="mt-2 border border-line rounded-md overflow-hidden">
        <div className="px-3 py-1 bg-elevated border-b border-line text-xs font-mono text-muted">
          html preview
        </div>
        {/* fond blanc fixe : le HTML arbitraire suppose une page claire */}
        <iframe
          sandbox="allow-scripts"
          srcDoc={event.content}
          className="w-full h-48 bg-on-accent"
          title="HTML preview"
        />
      </div>
    )
  }
  return null
}

// ── Stats bar ─────────────────────────────────────────────────────────────

function fmt(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}

function StatsBar({ usage }: { usage: UsageStats | null }) {
  if (!usage) return null
  const s = usage.session_tokens_by_step
  const total = usage.session.total_tokens
  const providers = Object.entries(usage.session.providers)
  if (total === 0) return null
  return (
    <div className="px-4 py-1.5 bg-surface border-b border-line flex items-center gap-3 flex-wrap shrink-0">
      <span className="text-xs font-mono text-muted flex items-center gap-1">
        <Zap size={11} className="text-accent" />
        session : <span className="text-secondary">{fmt(total)}</span>
      </span>
      {s.reflection > 0 && (
        <span className="text-xs font-mono text-muted flex items-center gap-1"><Brain size={10} /> {fmt(s.reflection)}</span>
      )}
      {s.generation > 0 && (
        <span className="text-xs font-mono text-muted flex items-center gap-1"><Code2 size={10} /> {fmt(s.generation)}</span>
      )}
      {s.verification > 0 && (
        <span className="text-xs font-mono text-muted flex items-center gap-1"><Check size={10} /> {fmt(s.verification)}</span>
      )}
      {s.tests > 0 && (
        <span className="text-xs font-mono text-muted flex items-center gap-1"><FlaskConical size={10} /> {fmt(s.tests)}</span>
      )}
      {providers.length > 0 && (
        <span className="ml-auto flex items-center gap-1.5">
          {providers.map(([p, t]) => (
            <Badge key={p} variant="secondary" mono>{p} {fmt(t)}</Badge>
          ))}
        </span>
      )}
      <button
        onClick={() => fetch(`${API}/code/usage/reset`, { method: 'POST' })}
        className="text-xs font-mono text-muted hover:text-secondary transition-colors duration-150"
      >reset</button>
    </div>
  )
}

// ── Turn stats footer ─────────────────────────────────────────────────────

function TurnStatsFooter({ stats: rawStats }: { stats: TurnStats | undefined }) {
  const stats = rawStats ?? { reflection: 0, generation: 0, verification: 0, tests: 0 }
  const parts: string[] = []
  if (stats.reflection) parts.push(`réfl ${fmt(stats.reflection)}`)
  if (stats.generation) parts.push(`code ${fmt(stats.generation)}`)
  if (stats.verification) parts.push(`vérif ${fmt(stats.verification)}`)
  if (stats.tests) parts.push(`tests ${fmt(stats.tests)}`)
  if (parts.length === 0) return null
  const total = stats.reflection + stats.generation + stats.verification + stats.tests
  return (
    <div className="text-xs font-mono text-muted/70 mt-1 flex gap-2 flex-wrap">
      {parts.join(' + ')}
      {parts.length > 1 && <span>= {fmt(total)}</span>}
    </div>
  )
}

// ── Step config ───────────────────────────────────────────────────────────

interface StepConfig {
  enabled: boolean
  model: string
}

interface PipelineConfig {
  reflection: StepConfig
  code: StepConfig
  verification: StepConfig
  tests: StepConfig
}

const STEP_DEFS: { key: keyof PipelineConfig; label: string; skippable: boolean }[] = [
  { key: 'reflection',   label: 'Réflexion', skippable: true  },
  { key: 'code',         label: 'Code',      skippable: false },
  { key: 'verification', label: 'Vérif',     skippable: false },
  { key: 'tests',        label: 'Tests',     skippable: true  },
]

const EMPTY_PIPELINE: PipelineConfig = {
  reflection:   { enabled: true,  model: '' },
  code:         { enabled: true,  model: '' },
  verification: { enabled: true,  model: '' },
  tests:        { enabled: true,  model: '' },
}

// ── Main component ────────────────────────────────────────────────────────

export default function Code() {
  const [tree, setTree] = useState<TreeNode[]>([])
  const [openFiles, setOpenFiles] = useState<OpenFile[]>([])
  const [activeFile, setActiveFile] = useState<string | null>(null)
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([])
  const [chatInput, setChatInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [stepConfig, setStepConfig] = useState<PipelineConfig>(EMPTY_PIPELINE)
  const [models, setModels] = useState<{ id: string; nom: string }[]>([])
  const [newItemName, setNewItemName] = useState('')
  const [newItemType, setNewItemType] = useState<'file' | 'dir' | null>(null)
  const [autoSave, setAutoSave] = useState(false)
  const [showPkgInstall, setShowPkgInstall] = useState(false)
  const [pkgName, setPkgName] = useState('')
  const [pkgInstalling, setPkgInstalling] = useState(false)
  const [pkgLines, setPkgLines] = useState<{ text: string; ok: boolean }[]>([])

  const [usage, setUsage] = useState<UsageStats | null>(null)

  const [editorHeight, setEditorHeight] = useState<number | null>(null)
  const editorWrapperRef = useRef<HTMLDivElement>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)
  const autoSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevModelsKey = useRef('')

  const activeContent = openFiles.find(f => f.path === activeFile)?.content ?? ''

  // ── Fetch helpers ────────────────────────────────────────────────────────

  const fetchTree = useCallback(async () => {
    try {
      const res = await fetch(`${API}/code/files`)
      const data = await res.json()
      setTree(data.tree ?? [])
    } catch { /* ignore */ }
  }, [])

  const fetchAvailableModels = useCallback(async () => {
    try {
      const res = await fetch(`${API}/models`)
      const data = await res.json()
      const all = [
        ...(data.local ?? []),
        ...(data.local_npu ?? []),
        ...Object.values(data.cloud ?? {}).flat() as { id: string; nom: string }[],
      ]
      const key = all.map(m => m.id).join(',')
      if (key === prevModelsKey.current) return  // zero flicker
      prevModelsKey.current = key
      setModels(all)
      // Initialise each step model to first available if not yet set
      const firstId = all[0]?.id || ''
      setStepConfig(prev => ({
        reflection:   { ...prev.reflection,   model: prev.reflection.model   || firstId },
        code:         { ...prev.code,         model: prev.code.model         || firstId },
        verification: { ...prev.verification, model: prev.verification.model || firstId },
        tests:        { ...prev.tests,        model: prev.tests.model        || firstId },
      }))
    } catch { /* ignore */ }
  }, [])

  // Fetch tree + models on mount; refresh models every 5s (zero-flicker)
  useEffect(() => {
    fetchTree()
    fetchAvailableModels()
    const id = setInterval(fetchAvailableModels, 5000)
    return () => clearInterval(id)
  }, [fetchTree, fetchAvailableModels])

  // ResizeObserver on Monaco wrapper
  useEffect(() => {
    const el = editorWrapperRef.current
    if (!el) return
    const ro = new ResizeObserver(entries => {
      const h = entries[0]?.contentRect.height
      if (h && h > 0) setEditorHeight(h)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Poll usage stats every 3s
  useEffect(() => {
    const poll = () =>
      fetch(`${API}/code/usage`).then(r => r.json()).then(setUsage).catch(() => {})
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [])

  // ── WebSocket ────────────────────────────────────────────────────────────

  useEffect(() => {
    const ws = new WebSocket('ws://localhost:8000/ws/code')
    wsRef.current = ws

    ws.onmessage = evt => {
      const msg = JSON.parse(evt.data)

      const patchLast = (fn: (turn: ChatTurn) => ChatTurn) =>
        setChatTurns(prev => {
          const turns = [...prev]
          const last = turns[turns.length - 1]
          if (!last || last.role !== 'agent') return prev
          turns[turns.length - 1] = fn(last)
          return turns
        })

      const closeStreaming = (events: ChatEventType[]) =>
        events.map(e =>
          (e.type === 'text' || e.type === 'reflection' || e.type === 'tests_result')
            ? { ...e, streaming: false }
            : e
        )

      if (msg.type === 'token') {
        patchLast(turn => {
          const events = [...turn.events]
          const last = events[events.length - 1]
          if (last?.type === 'text') {
            events[events.length - 1] = { ...last, content: last.content + msg.content }
          } else {
            events.push({ type: 'text', content: msg.content, streaming: true })
          }
          return { ...turn, events }
        })

      } else if (msg.type === 'reflection_start') {
        patchLast(turn => ({
          ...turn,
          events: [...turn.events, { type: 'reflection', content: '', streaming: true }],
        }))

      } else if (msg.type === 'reflection_token') {
        patchLast(turn => {
          const events = [...turn.events]
          const last = events[events.length - 1]
          if (last?.type === 'reflection') {
            events[events.length - 1] = { ...last, content: last.content + msg.content }
          }
          return { ...turn, events }
        })

      } else if (msg.type === 'reflection_done') {
        patchLast(turn => ({
          ...turn,
          events: turn.events.map(e =>
            e.type === 'reflection' ? { ...e, streaming: false } : e
          ),
        }))

      } else if (msg.type === 'tool_call') {
        patchLast(turn => ({
          ...turn,
          events: [
            ...closeStreaming(turn.events),
            { type: 'tool_call', tool: msg.tool, path: msg.path, status: 'pending' as const },
          ],
        }))

      } else if (msg.type === 'tool_result') {
        patchLast(turn => ({
          ...turn,
          events: [
            ...turn.events.map(e =>
              e.type === 'tool_call' && e.path === msg.path && e.status === 'pending'
                ? { ...e, status: msg.status as 'success' | 'error' }
                : e
            ),
            { type: 'tool_result', tool: msg.tool, path: msg.path, result: msg.result, status: msg.status },
          ],
        }))
        if (msg.status === 'success') {
          fetchTree()
          const FILE_TOOLS = ['create_file', 'edit_file', 'write_file', 'patch_file']
          if (FILE_TOOLS.includes(msg.tool) && msg.path) reloadOpenFile(msg.path)
        }

      } else if (msg.type === 'verification_start') {
        patchLast(turn => ({
          ...turn,
          events: [...turn.events, { type: 'verification', path: msg.path, result: '', pending: true }],
        }))

      } else if (msg.type === 'verification_done') {
        patchLast(turn => ({
          ...turn,
          events: turn.events.map(e =>
            e.type === 'verification' && e.path === msg.path
              ? { ...e, result: msg.result, pending: false }
              : e
          ),
        }))

      } else if (msg.type === 'tests_prompt') {
        patchLast(turn => ({
          ...turn,
          events: [...turn.events, { type: 'tests_prompt', path: msg.path }],
        }))

      } else if (msg.type === 'tests_token') {
        patchLast(turn => {
          const events = [...turn.events]
          const last = events[events.length - 1]
          if (last?.type === 'tests_result') {
            events[events.length - 1] = { ...last, content: last.content + msg.content }
          } else {
            events.push({ type: 'tests_result', path: '', content: msg.content, streaming: true })
          }
          return { ...turn, events }
        })

      } else if (msg.type === 'tests_done') {
        patchLast(turn => ({
          ...turn,
          events: [
            ...turn.events.map(e =>
              e.type === 'tests_result' ? { ...e, path: msg.path, streaming: false } : e
            ).filter(e => e.type !== 'tests_prompt'),
          ],
        }))
        fetchTree()

      } else if (msg.type === 'tokens') {
        const step = msg.step as keyof TurnStats
        patchLast(turn => ({
          ...turn,
          stats: { ...turn.stats, [step]: (turn.stats[step] ?? 0) + msg.count },
        }))

      } else if (msg.type === 'execute_request') {
        patchLast(turn => ({
          ...turn,
          events: [
            ...closeStreaming(turn.events),
            { type: 'execute_request', path: msg.path, args: msg.args ?? '' },
          ],
        }))

      } else if (msg.type === 'execute_result') {
        patchLast(turn => ({
          ...turn,
          events: [
            ...turn.events,
            { type: 'execute_result', stdout: msg.stdout, stderr: msg.stderr,
              returncode: msg.returncode, duration_ms: msg.duration_ms },
          ],
        }))

      } else if (msg.type === 'execute_external') {
        patchLast(turn => ({
          ...turn,
          events: [...turn.events, { type: 'execute_external', path: msg.path }],
        }))

      } else if (msg.type === 'html_preview') {
        patchLast(turn => ({
          ...turn,
          events: [...turn.events, { type: 'html_preview', content: msg.content }],
        }))

      } else if (msg.type === 'done') {
        patchLast(turn => ({ ...turn, events: closeStreaming(turn.events) }))
        setStreaming(false)

      } else if (msg.type === 'error') {
        setStreaming(false)
      }
    }

    ws.onerror = () => setStreaming(false)

    return () => { ws.close(); wsRef.current = null }
  }, [fetchTree])

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatTurns])

  // ── File operations ──────────────────────────────────────────────────────

  const openFile = useCallback(async (path: string) => {
    const already = openFiles.find(f => f.path === path)
    if (already) { setActiveFile(path); return }
    try {
      const res = await fetch(`${API}/code/file?path=${encodeURIComponent(path)}`)
      const data = await res.json()
      setOpenFiles(prev => [...prev, { path, content: data.content, dirty: false }])
      setActiveFile(path)
    } catch { /* ignore */ }
  }, [openFiles])

  const saveFile = useCallback(async (path: string, content: string) => {
    try {
      await fetch(`${API}/code/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, content }),
      })
      setOpenFiles(prev => prev.map(f => f.path === path ? { ...f, dirty: false } : f))
    } catch { /* ignore */ }
  }, [])

  const reloadOpenFile = useCallback(async (path: string) => {
    const isOpen = openFiles.some(f => f.path === path)
    if (!isOpen) return
    try {
      const res = await fetch(`${API}/code/file?path=${encodeURIComponent(path)}`)
      const data = await res.json()
      setOpenFiles(prev => prev.map(f =>
        f.path === path ? { ...f, content: data.content, dirty: false } : f
      ))
    } catch { /* ignore */ }
  }, [openFiles])

  const closeTab = useCallback((path: string, e: React.MouseEvent) => {
    e.stopPropagation()
    setOpenFiles(prev => {
      const next = prev.filter(f => f.path !== path)
      if (activeFile === path) setActiveFile(next[next.length - 1]?.path ?? null)
      return next
    })
  }, [activeFile])

  const handleEditorChange = useCallback((value: string | undefined) => {
    if (activeFile === null || value === undefined) return
    setOpenFiles(prev => prev.map(f => f.path === activeFile ? { ...f, content: value, dirty: true } : f))
    if (autoSave) {
      if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current)
      autoSaveTimer.current = setTimeout(() => saveFile(activeFile, value), 1500)
    }
  }, [activeFile, autoSave, saveFile])

  const handleDeleteFile = useCallback(async (path: string, e: React.MouseEvent) => {
    e.stopPropagation()
    await fetch(`${API}/code/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' })
    setOpenFiles(prev => prev.filter(f => f.path !== path))
    if (activeFile === path) setActiveFile(null)
    fetchTree()
  }, [activeFile, fetchTree])

  const handleCreateItem = useCallback(async () => {
    if (!newItemName.trim()) return
    if (newItemType === 'file') {
      await fetch(`${API}/code/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: newItemName.trim(), content: '' }),
      })
    } else {
      await fetch(`${API}/code/folder`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: newItemName.trim() }),
      })
    }
    setNewItemName('')
    setNewItemType(null)
    fetchTree()
  }, [newItemName, newItemType, fetchTree])

  // ── Package install ──────────────────────────────────────────────────────

  const installPackage = useCallback(async (pkg: string) => {
    const name = pkg.trim()
    if (!name || pkgInstalling) return
    setPkgInstalling(true)
    setPkgLines([])
    setShowPkgInstall(true)
    setPkgName(name)
    try {
      const res = await fetch(`${API}/code/install`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package: name }),
      })
      const reader = res.body!.getReader()
      const dec = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const ev = JSON.parse(line.slice(6))
          if (ev.type === 'line') {
            setPkgLines(prev => [...prev, { text: ev.line, ok: true }])
          } else if (ev.type === 'error') {
            setPkgLines(prev => [...prev, { text: ev.line, ok: false }])
          } else if (ev.type === 'done') {
            const success = ev.returncode === 0
            setPkgLines(prev => [
              ...prev,
              { text: success ? `✓ ${name} installé` : `✗ échec (code ${ev.returncode})`, ok: success },
            ])
          }
        }
      }
    } catch {
      setPkgLines(prev => [...prev, { text: 'Erreur réseau', ok: false }])
    } finally {
      setPkgInstalling(false)
    }
  }, [pkgInstalling])

  // ── Chat ─────────────────────────────────────────────────────────────────

  const sendMessage = useCallback(() => {
    if (!chatInput.trim() || streaming) return
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return

    const content = chatInput.trim()
    setChatInput('')
    setStreaming(true)

    const activeContent = openFiles.find(f => f.path === activeFile)?.content ?? ''
    const fileCtx = activeFile
      ? `${activeFile}\n\`\`\`\n${activeContent.slice(0, 3000)}\n\`\`\``
      : ''

    const emptyStats: TurnStats = { reflection: 0, generation: 0, verification: 0, tests: 0 }
    setChatTurns(prev => [
      ...prev,
      { role: 'user', events: [{ type: 'text', content, streaming: false }], stats: emptyStats },
      { role: 'agent', events: [], stats: { ...emptyStats } },
    ])

    ws.send(JSON.stringify({
      type: 'message',
      content,
      file_context: fileCtx,
      pipeline: stepConfig,
    }))
  }, [chatInput, streaming, activeFile, openFiles, stepConfig])

  const generateTests = useCallback((path: string) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    // Replace tests_prompt with tests_result placeholder
    setChatTurns(prev => {
      const turns = [...prev]
      const last = turns[turns.length - 1]
      if (!last) return prev
      const events = last.events.map(e =>
        e.type === 'tests_prompt' && e.path === path
          ? { type: 'tests_result' as const, path, content: '', streaming: true }
          : e
      )
      turns[turns.length - 1] = { ...last, events }
      return turns
    })
    ws.send(JSON.stringify({ type: 'generate_tests', path, model: stepConfig.tests.model || undefined }))
  }, [stepConfig.tests.model])

  const confirmExecute = useCallback((path: string, args: string) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'execute_confirm', path, args }))
    // Mark execute_request as consumed (replace with "running" state)
    setChatTurns(prev => {
      const turns = [...prev]
      const last = turns[turns.length - 1]
      if (!last) return prev
      const events = last.events.filter(e => !(e.type === 'execute_request' && e.path === path))
      turns[turns.length - 1] = { ...last, events }
      return turns
    })
  }, [])

  const cancelExecute = useCallback((path: string) => {
    setChatTurns(prev => {
      const turns = [...prev]
      const last = turns[turns.length - 1]
      if (!last) return prev
      const events = last.events.filter(e => !(e.type === 'execute_request' && e.path === path))
      turns[turns.length - 1] = { ...last, events }
      return turns
    })
  }, [])

  // ── Run active file ──────────────────────────────────────────────────────

  const runActiveFile = useCallback(async () => {
    if (!activeFile) return
    const af = openFiles.find(f => f.path === activeFile)
    if (!af) return
    // Save first
    await saveFile(activeFile, af.content)
    // Add execute_request event to a new agent turn
    setChatTurns(prev => [
      ...prev,
      {
        role: 'agent',
        events: [{ type: 'execute_request', path: activeFile, args: '' }],
        stats: { reflection: 0, generation: 0, verification: 0, tests: 0 },
      },
    ])
  }, [activeFile, openFiles, saveFile])

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      <StatsBar usage={usage} />

      <div className="flex flex-1 overflow-hidden">

      {/* ── Colonne 1: Arborescence ── */}
      <div className="w-52 shrink-0 border-r border-line flex flex-col bg-surface">
        <div className="px-3 py-2.5 border-b border-line flex items-center justify-between">
          <span className="text-xs text-muted uppercase tracking-wide">workspace</span>
          <div className="flex gap-0.5">
            <button
              onClick={() => { setNewItemType('file'); setNewItemName('') }}
              title="Nouveau fichier"
              className="p-1 rounded-sm text-muted hover:text-secondary hover:bg-elevated transition-colors duration-150"
            ><FilePlus size={12} /></button>
            <button
              onClick={() => { setNewItemType('dir'); setNewItemName('') }}
              title="Nouveau dossier"
              className="p-1 rounded-sm text-muted hover:text-secondary hover:bg-elevated transition-colors duration-150"
            ><FolderPlus size={12} /></button>
            <button
              onClick={fetchTree}
              title="Rafraîchir"
              className="p-1 rounded-sm text-muted hover:text-secondary hover:bg-elevated transition-colors duration-150"
            ><RefreshCw size={12} /></button>
          </div>
        </div>

        {newItemType && (
          <div className="px-2 py-1.5 border-b border-line flex gap-1">
            <Input
              autoFocus
              mono
              value={newItemName}
              onChange={e => setNewItemName(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') handleCreateItem()
                if (e.key === 'Escape') setNewItemType(null)
              }}
              placeholder={newItemType === 'file' ? 'fichier.py' : 'dossier/'}
              className="flex-1 text-xs py-1 px-2 min-w-0"
            />
            <button
              onClick={handleCreateItem}
              title="Créer"
              className="p-1 rounded-sm text-muted hover:text-secondary transition-colors duration-150"
            ><CornerDownLeft size={12} /></button>
          </div>
        )}

        <div className="flex-1 overflow-y-auto py-1 px-1">
          {tree.length === 0 ? (
            <div className="text-center py-6 text-xs text-muted">workspace vide</div>
          ) : (
            tree.map(node => (
              <TreeNodeItem
                key={node.path}
                node={node}
                depth={0}
                activeFile={activeFile}
                onSelect={openFile}
                onDelete={handleDeleteFile}
              />
            ))
          )}
        </div>
      </div>

      {/* ── Colonne 2: Éditeur Monaco ── */}
      <div className="flex flex-col flex-1 min-w-0 border-r border-line">
        {/* Tabs */}
        <div className="flex border-b border-line bg-surface shrink-0 overflow-x-auto">
          {openFiles.map(f => (
            <div
              key={f.path}
              onClick={() => setActiveFile(f.path)}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-mono cursor-pointer border-r border-line shrink-0 transition-colors duration-150 ${
                f.path === activeFile
                  ? 'bg-elevated text-primary border-t-2 border-t-accent'
                  : 'text-muted hover:text-secondary hover:bg-elevated/50'
              }`}
            >
              <span className="truncate max-w-[120px]">{f.path.split('/').pop()}</span>
              {f.dirty && <span className="text-warning shrink-0">●</span>}
              <button
                onClick={e => closeTab(f.path, e)}
                title="Fermer"
                className="text-muted hover:text-secondary shrink-0 ml-0.5 transition-colors duration-150"
              ><X size={11} /></button>
            </div>
          ))}
          {openFiles.length === 0 && (
            <span className="px-4 py-2 text-xs text-muted">aucun fichier ouvert</span>
          )}
          <div className="ml-auto flex items-center gap-2 px-3 shrink-0">
            <label className="flex items-center gap-1.5 cursor-pointer">
              <span className="text-xs text-muted">auto-save</span>
              <Toggle checked={autoSave} onChange={() => setAutoSave(v => !v)} label="Auto-save" />
            </label>
          </div>
        </div>

        {/* Editor toolbar */}
        {activeFile && (
          <div className="border-b border-line bg-surface shrink-0">
            <div className="flex items-center gap-1 px-3 py-1.5">
              <span className="text-xs font-mono text-muted truncate flex-1">{activeFile}</span>
              <Button
                variant="ghost"
                size="sm"
                icon={<Save size={12} />}
                onClick={() => {
                  const f = openFiles.find(x => x.path === activeFile)
                  if (f) saveFile(f.path, f.content)
                }}
              >
                sauv
              </Button>
              <Button variant="ghost" size="sm" icon={<Play size={12} className="text-success" />} onClick={runActiveFile}>
                exécuter
              </Button>
              <Button
                variant="ghost"
                size="sm"
                icon={<Package size={12} />}
                onClick={() => { setShowPkgInstall(v => !v); setPkgLines([]) }}
                className={showPkgInstall ? 'bg-accent/10 text-accent' : ''}
              >
                pkg
              </Button>
            </div>

            {/* Package install panel */}
            {showPkgInstall && (
              <div className="px-3 pb-2 space-y-1.5">
                <div className="flex gap-1.5">
                  <Input
                    mono
                    value={pkgName}
                    onChange={e => setPkgName(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && installPackage(pkgName)}
                    placeholder="ex: numpy, pygame==2.5.0"
                    disabled={pkgInstalling}
                    className="flex-1 text-xs py-1 disabled:opacity-50"
                  />
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => installPackage(pkgName)}
                    disabled={pkgInstalling || !pkgName.trim()}
                  >
                    {pkgInstalling ? '...' : 'pip install'}
                  </Button>
                </div>
                {pkgLines.length > 0 && (
                  // sortie pip : volontairement sombre comme le terminal
                  <div className="bg-[#100e0a] rounded-sm border border-line px-2 py-1.5 max-h-24 overflow-y-auto">
                    {pkgLines.map((l, i) => (
                      <div key={i} className={`text-xs font-mono leading-relaxed ${l.ok ? 'text-[#d4cfc4]' : 'text-error'}`}>
                        {l.text}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Monaco editor — garde son thème sombre vs-dark dans les deux thèmes */}
        <div ref={editorWrapperRef} className="flex-1 min-h-0 overflow-hidden relative">
          {activeFile ? (
            <Editor
              key={activeFile}
              height={editorHeight ? `${editorHeight}px` : '400px'}
              language={extLang(activeFile)}
              value={activeContent}
              onChange={handleEditorChange}
              theme="vs-dark"
              options={{
                fontSize: 12,
                fontFamily: 'JetBrains Mono, Consolas, monospace',
                minimap: { enabled: false },
                scrollBeyondLastLine: false,
                lineNumbers: 'on',
                renderWhitespace: 'none',
                wordWrap: 'on',
                tabSize: 2,
                padding: { top: 8 },
              }}
            />
          ) : (
            <div className="flex flex-col items-center justify-center gap-2 h-full text-muted text-sm select-none">
              <Code2 size={18} />
              Ouvrez un fichier depuis l'arborescence
            </div>
          )}
        </div>
      </div>

      {/* ── Colonne 3: Chat IA ── */}
      <div className="w-80 shrink-0 flex flex-col bg-surface">
        {/* Pipeline step config */}
        <div className="px-2 py-1.5 border-b border-line shrink-0 space-y-1">
          {STEP_DEFS.map(({ key, label, skippable }) => (
            <div key={key} className="flex items-center gap-1.5">
              <button
                onClick={() => {
                  if (!skippable) return
                  setStepConfig(prev => ({
                    ...prev,
                    [key]: { ...prev[key], enabled: !prev[key].enabled },
                  }))
                }}
                className={`w-3.5 h-3.5 rounded-sm border shrink-0 flex items-center justify-center transition-colors duration-150 ${
                  stepConfig[key].enabled
                    ? 'bg-accent/15 border-accent/40 text-accent'
                    : 'bg-elevated border-line text-transparent'
                } ${!skippable ? 'opacity-40 cursor-default' : 'cursor-pointer hover:opacity-80'}`}
              >
                <Check size={9} strokeWidth={3} />
              </button>
              <span className={`text-xs shrink-0 w-[5.5rem] ${
                stepConfig[key].enabled ? 'text-secondary' : 'text-muted/60'
              }`}>
                {label}
              </span>
              <Select
                mono
                value={stepConfig[key].model}
                onChange={e => setStepConfig(prev => ({
                  ...prev,
                  [key]: { ...prev[key], model: e.target.value },
                }))}
                className="flex-1 min-w-0 py-0.5 px-1"
              >
                {models.map(m => (
                  <option key={m.id} value={m.id}>{m.nom}</option>
                ))}
              </Select>
            </div>
          ))}
        </div>

        {/* Chat messages */}
        <div className="flex-1 overflow-y-auto p-3 space-y-3">
          {chatTurns.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-8 text-muted select-none">
              <Code2 size={15} />
              <span className="text-xs">Décrivez ce que vous voulez construire</span>
            </div>
          )}
          {chatTurns.map((turn, i) => (
            <div key={i} className={turn.role === 'user' ? 'flex justify-end' : ''}>
              {turn.role === 'user' ? (
                <div className="max-w-[90%] bg-elevated border border-line rounded-lg px-3 py-2 text-xs text-primary">
                  {(turn.events[0] as { content: string })?.content}
                </div>
              ) : (
                <div className="space-y-1">
                  {turn.events.map((ev, j) => (
                    <ChatEventView
                      key={j}
                      event={ev}
                      onExecuteConfirm={confirmExecute}
                      onExecuteCancel={() => {
                        const reqEv = ev as { type: string; path: string }
                        if (reqEv.type === 'execute_request') cancelExecute(reqEv.path)
                      }}
                      onInstall={installPackage}
                      onGenerateTests={generateTests}
                    />
                  ))}
                  {turn.stats && (turn.stats.reflection + turn.stats.generation + turn.stats.verification + turn.stats.tests) > 0 && (
                    <TurnStatsFooter stats={turn.stats} />
                  )}
                </div>
              )}
            </div>
          ))}
          <div ref={chatEndRef} />
        </div>

        {/* Chat input */}
        <div className="px-3 pb-3 pt-2 border-t border-line shrink-0">
          <div className="flex gap-1.5">
            <textarea
              value={chatInput}
              onChange={e => setChatInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
              }}
              placeholder="Décrivez votre besoin..."
              disabled={streaming}
              rows={2}
              className="flex-1 bg-elevated border border-line rounded-sm px-2 py-1.5 text-xs text-primary placeholder-muted focus:outline-none focus:border-accent disabled:opacity-50 resize-none transition-colors duration-150"
            />
            <button
              onClick={sendMessage}
              disabled={streaming || !chatInput.trim()}
              title="Envoyer"
              className="p-2 rounded-md bg-gradient-primary text-on-accent shadow-sm hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 self-end"
            >
              {streaming ? <Loader2 size={13} className="animate-spin" /> : <CornerDownLeft size={13} />}
            </button>
          </div>
        </div>
      </div>

      </div>  {/* end 3-column flex */}

      <ModuleBar module="code" showModel />
    </div>
  )
}
