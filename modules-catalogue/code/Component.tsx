import { useState, useEffect, useRef, useCallback } from 'react'
import Editor from '@monaco-editor/react'
import {
  Brain, Check, ChevronDown, ChevronRight, Code2, CornerDownLeft, FilePlus,
  FlaskConical, FolderPlus, Loader2, Maximize2, Monitor, Package, Pencil, Play, RefreshCw,
  Save, Terminal as TerminalIcon, Wrench, X, Zap,
} from 'lucide-react'
import { Badge, Button, Input, Select, Toggle } from '../../../components/ui'
import RichMessage from '../../../components/RichMessage'
import ModuleBar from '../../../components/ModuleBar'
import { usePersistentState } from '../../../usePersistentState'
import { API, apiFetch, wsUrl } from '../../../api'

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
  // Le modèle a répondu par un bloc de code sans balise d'outil. Rien n'a été
  // écrit : le backend PROPOSE l'écriture (cf. le repli de `run_turn`, qui
  // écrasait le fichier actif en silence avant le 2026-09-05).
  | { type: 'write_request'; path: string; content: string; existant: boolean }
  | { type: 'execute_result'; stdout: string; stderr: string; returncode: number; duration_ms: number }
  | { type: 'execute_external'; path: string }
  | { type: 'html_preview'; content: string }
  | { type: 'conclusion'; content: string }
  | { type: 'error'; content: string }

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

/**
 * Message d'erreur lisible pour une réponse HTTP en échec.
 *
 * `apiFetch` ne lève JAMAIS sur un statut HTTP (cf. `src/api.ts`) : elle rend
 * la `Response` telle quelle, donc un `.catch()` ne voit que les pannes réseau.
 * Toute vérification d'échec doit passer par `res.ok` — et c'est le corps
 * d'erreur qui dit pourquoi.
 *
 * `detail` est vérifié TYPE par TYPE, pas seulement en présence (§8 de
 * CLAUDE.md, même famille que le `.json()` sur une réponse d'erreur) : le
 * gestionnaire d'exceptions rend `{detail: "<texte>"}`, mais une erreur de
 * validation FastAPI y met une LISTE d'objets — l'afficher brut donnerait
 * « [object Object] » à l'utilisateur, ce qui est pire que le statut nu.
 */
async function messageErreur(res: Response, defaut: string): Promise<string> {
  try {
    const data = await res.json()
    const detail = (data as { detail?: unknown } | null)?.detail
    if (typeof detail === 'string' && detail.trim()) return detail.trim()
  } catch { /* corps vide ou non-JSON : on retombe sur le message par défaut */ }
  return `${defaut} (HTTP ${res.status})`
}

/** `v` si c'est bien un tableau, `[]` sinon. `?? []` laisserait passer une
 *  chaîne ou un objet venus d'un corps d'erreur (§8 de CLAUDE.md). */
function liste<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

/**
 * Ancienneté en clair : « à l'instant », « il y a 12 s », « il y a 4 min ».
 *
 * Sert au bandeau d'échec d'enregistrement, dont c'est la seule façon de dire
 * si la panne dure ENCORE ou date de dix minutes : le message du backend, lui,
 * est identique dans les deux cas.
 */
function depuis(horodatage: number, maintenant: number): string {
  const s = Math.max(0, Math.round((maintenant - horodatage) / 1000))
  if (s < 3) return "à l'instant"
  if (s < 60) return `il y a ${s} s`
  return `il y a ${Math.floor(s / 60)} min`
}

/** Heure murale d'un horodatage — un « il y a 47 min » relatif ne dit pas
 *  QUAND, et c'est ce qu'on veut recouper avec les logs du backend. */
function heure(horodatage: number): string {
  return new Date(horodatage).toLocaleTimeString()
}

// ── File Tree ──────────────────────────────────────────────────────────────

function TreeNodeItem({
  node, depth, activeFile, onSelect, onDelete, onRename,
}: {
  node: TreeNode
  depth: number
  activeFile: string | null
  onSelect: (path: string) => void
  onDelete: (path: string, e: React.MouseEvent) => void
  onRename: (node: TreeNode, e: React.MouseEvent) => void
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
          <span className="truncate flex-1">{node.name}/</span>
          <button
            onClick={e => onRename(node, e)}
            title="Renommer"
            className="opacity-0 group-hover:opacity-100 text-muted hover:text-accent shrink-0 ml-1 transition-colors duration-150"
          >
            <Pencil size={10} />
          </button>
          <button
            onClick={e => onDelete(node.path, e)}
            title="Supprimer"
            className="opacity-0 group-hover:opacity-100 text-muted hover:text-error shrink-0 ml-0.5 transition-colors duration-150"
          >
            <X size={11} />
          </button>
        </div>
        {open && node.children?.map(child => (
          <TreeNodeItem
            key={child.path}
            node={child}
            depth={depth + 1}
            activeFile={activeFile}
            onSelect={onSelect}
            onDelete={onDelete}
            onRename={onRename}
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
      <span className="truncate flex-1">{node.name}</span>
      <button
        onClick={e => onRename(node, e)}
        title="Renommer"
        className="opacity-0 group-hover:opacity-100 text-muted hover:text-accent shrink-0 ml-1 transition-colors duration-150"
      >
        <Pencil size={10} />
      </button>
      <button
        onClick={e => onDelete(node.path, e)}
        title="Supprimer"
        className="opacity-0 group-hover:opacity-100 text-muted hover:text-error shrink-0 ml-0.5 transition-colors duration-150"
      >
        <X size={11} />
      </button>
    </div>
  )
}

// ── Vue plein écran (web ou terminal) ───────────────────────────────────────

type Expanded =
  | { kind: 'html'; content: string }
  | { kind: 'terminal'; stdout: string; stderr: string; returncode: number; duration_ms: number }

function FullscreenView({ data, onClose }: { data: Expanded; onClose: () => void }) {
  // Échap pour fermer
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const isHtml = data.kind === 'html'

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/70 backdrop-blur-sm p-4 sm:p-8">
      <div className="flex flex-col flex-1 min-h-0 rounded-lg overflow-hidden border border-line shadow-2xl bg-surface">
        {/* Barre de titre façon fenêtre / onglet de navigateur */}
        <div className="flex items-center gap-2 px-3 py-2 bg-[#16140f] border-b border-[#2a2620] shrink-0">
          <span className="flex gap-1.5">
            <span className="w-3 h-3 rounded-full bg-[#ff5f56]" />
            <span className="w-3 h-3 rounded-full bg-[#ffbd2e]" />
            <span className="w-3 h-3 rounded-full bg-[#27c93f]" />
          </span>
          <span className="text-xs font-mono text-[#8a8478] flex items-center gap-1.5 ml-2">
            {isHtml
              ? <><Code2 size={12} /> aperçu web</>
              : <><TerminalIcon size={12} /> terminal · {data.duration_ms >= 1000 ? `${(data.duration_ms / 1000).toFixed(1)}s` : `${data.duration_ms}ms`}</>}
          </span>
          {!isHtml && (
            <span className={`text-xs font-mono ${data.returncode === 0 ? 'text-success' : 'text-error'}`}>
              exit {data.returncode}
            </span>
          )}
          <button
            onClick={onClose}
            title="Fermer (Échap)"
            className="ml-auto text-[#8a8478] hover:text-[#d4cfc4] transition-colors duration-150 p-1 rounded-sm hover:bg-white/5"
          >
            <X size={15} />
          </button>
        </div>

        {/* Contenu */}
        {isHtml ? (
          <iframe
            sandbox="allow-scripts allow-forms allow-modals allow-popups"
            srcDoc={data.content}
            className="flex-1 w-full bg-white"
            title="Aperçu web plein écran"
          />
        ) : (
          <div className="flex-1 overflow-auto bg-[#100e0a] px-5 py-4 font-mono text-sm leading-relaxed">
            {data.stdout && <pre className="text-[#d4cfc4] whitespace-pre-wrap break-words">{data.stdout}</pre>}
            {data.stderr && <pre className="text-error whitespace-pre-wrap break-words">{data.stderr}</pre>}
            {!data.stdout && !data.stderr && (
              <span className="text-[#8a8478]">(pas de sortie)</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function ExpandButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      title="Ouvrir en grand"
      className="text-[#8a8478] hover:text-[#d4cfc4] transition-colors duration-150 p-0.5 rounded-sm hover:bg-white/5"
    >
      <Maximize2 size={12} />
    </button>
  )
}

// ── Terminal output ────────────────────────────────────────────────────────

function Terminal({
  event,
  onInstall,
  onExpand,
}: {
  event: Extract<ChatEventType, { type: 'execute_result' }>
  onInstall: (pkg: string) => void
  onExpand: (data: Expanded) => void
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
        <span className="flex items-center gap-2">
          <span className={`text-xs font-mono ${event.returncode === 0 ? 'text-success' : 'text-error'}`}>
            exit {event.returncode}
          </span>
          <ExpandButton onClick={() => onExpand({
            kind: 'terminal',
            stdout: event.stdout,
            stderr: event.stderr,
            returncode: event.returncode,
            duration_ms: event.duration_ms,
          })} />
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
  onWriteConfirm,
  onWriteCancel,
  onInstall,
  onGenerateTests,
  onExpand,
}: {
  event: ChatEventType
  onExecuteConfirm: (path: string, args: string) => void
  onExecuteCancel: () => void
  onWriteConfirm: (path: string, content: string) => void
  onWriteCancel: (path: string) => void
  onInstall: (pkg: string) => void
  onGenerateTests: (path: string) => void
  onExpand: (data: Expanded) => void
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
  if (event.type === 'write_request') {
    const lignes = event.content.split('\n').length
    return (
      <div className="mt-2 border border-warning/30 rounded-md bg-warning/5 p-3">
        <div className="text-xs font-mono mb-2 text-secondary">
          {event.existant ? 'Remplacer tout le contenu de ' : 'Créer '}
          <span className="text-primary">{event.path}</span>
          <span className="text-muted"> par ce bloc ({lignes} lignes)</span>
        </div>
        <div className="text-xs text-muted mb-2">
          Le modèle a montré du code sans utiliser d'outil.{' '}
          {event.existant && 'La version actuelle sera sauvegardée.'}
        </div>
        <Collapsible label="voir le bloc" icon={<Code2 size={11} />}>
          <pre className="text-[11px] font-mono text-secondary whitespace-pre-wrap max-h-64 overflow-y-auto">
            {event.content}
          </pre>
        </Collapsible>
        <div className="flex gap-2 mt-2">
          <Button variant="primary" size="sm" icon={<Check size={12} />}
                  onClick={() => onWriteConfirm(event.path, event.content)}>
            {event.existant ? 'écraser' : 'créer'}
          </Button>
          <Button variant="ghost" size="sm" icon={<X size={12} />}
                  onClick={() => onWriteCancel(event.path)}>
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
    return <Terminal event={event} onInstall={onInstall} onExpand={onExpand} />
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
  if (event.type === 'error') {
    return (
      <div className="mt-2 flex items-start gap-2 rounded-md border border-error/40 bg-error/5 px-3 py-2">
        <X size={13} className="text-error shrink-0 mt-0.5" />
        <span className="text-xs text-error leading-relaxed whitespace-pre-wrap break-words">{event.content}</span>
      </div>
    )
  }
  if (event.type === 'conclusion') {
    return (
      <div className="mt-2 flex items-start gap-2 rounded-md border border-success/30 bg-success/5 px-3 py-2">
        <Check size={13} className="text-success shrink-0 mt-0.5" />
        <span className="text-xs text-secondary leading-relaxed">{event.content}</span>
      </div>
    )
  }
  if (event.type === 'html_preview') {
    return (
      <div className="mt-2 border border-line rounded-md overflow-hidden">
        <div className="px-3 py-1 bg-elevated border-b border-line text-xs font-mono text-muted flex items-center justify-between">
          <span>aperçu web</span>
          <button
            onClick={() => onExpand({ kind: 'html', content: event.content })}
            title="Ouvrir en grand"
            className="text-muted hover:text-secondary transition-colors duration-150 flex items-center gap-1"
          >
            <Maximize2 size={11} /> plein écran
          </button>
        </div>
        {/* fond blanc fixe : le HTML arbitraire suppose une page claire */}
        <iframe
          sandbox="allow-scripts"
          srcDoc={event.content}
          className="w-full h-48 bg-on-accent cursor-pointer"
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
        onClick={() => apiFetch(`${API}/code/usage/reset`, { method: 'POST' })}
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
  // Persistés : la progression (fichiers ouverts, fichier actif, conversation)
  // survit à un rechargement (notamment le reload Vite après approbation atelier).
  // 3e élément : l'état de la dernière écriture dans `localStorage`. Le garde
  // `beforeunload` plus bas repose sur l'idée que ces onglets seront rejoués
  // d'ici ; quand ce n'est pas vrai, il faut le DIRE, sinon l'utilisateur
  // arbitre la fermeture avec une information fausse.
  const [openFiles, setOpenFiles, persistanceOnglets] =
    usePersistentState<OpenFile[]>('epure.code.openFiles', [])
  const [activeFile, setActiveFile] = usePersistentState<string | null>('epure.code.activeFile', null)
  const [chatTurns, setChatTurns] = usePersistentState<ChatTurn[]>('epure.code.chatTurns', [])
  const [chatInput, setChatInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [stepConfig, setStepConfig] = useState<PipelineConfig>(EMPTY_PIPELINE)
  const [models, setModels] = useState<{ id: string; nom: string }[]>([])
  const [newItemName, setNewItemName] = useState('')
  const [newItemType, setNewItemType] = useState<'file' | 'dir' | null>(null)
  const [autoSave, setAutoSave] = useState(false)
  // Dernier échec d'enregistrement/lecture — affiché dans la barre de l'éditeur.
  // La pastille `dirty` ne suffit pas : elle dit « non enregistré », pas
  // « le backend a REFUSÉ, et voici pourquoi ».
  //
  // `tentatives` et `dernier` sont là parce que l'auto-save RÉESSAIE à chaque
  // pause de frappe (et doit le faire : un auto-save qui abandonne laisserait
  // le contenu dans le seul navigateur). Sans eux, un bandeau figé ne distingue
  // pas une panne qui dure d'un échec vieux de dix minutes déjà résolu.
  const [saveError, setSaveError] = useState<
    { path: string; message: string; tentatives: number; dernier: number } | null
  >(null)
  // Horloge du bandeau : re-rendu chaque seconde tant qu'un échec est affiché,
  // pour que « il y a 12 s » avance TOUT SEUL. Sans ça, lire « ça échoue
  // encore » demanderait une action de l'utilisateur — exactement ce qu'on veut
  // éviter. L'intervalle ne tourne pas quand il n'y a pas d'erreur.
  const [maintenant, setMaintenant] = useState(() => Date.now())
  const [showPkgInstall, setShowPkgInstall] = useState(false)
  const [pkgName, setPkgName] = useState('')
  const [pkgInstalling, setPkgInstalling] = useState(false)
  const [pkgLines, setPkgLines] = useState<{ text: string; ok: boolean }[]>([])

  const [usage, setUsage] = useState<UsageStats | null>(null)
  const [expanded, setExpanded] = useState<Expanded | null>(null)

  const [editorHeight, setEditorHeight] = useState<number | null>(null)
  const editorWrapperRef = useRef<HTMLDivElement>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)
  const autoSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevModelsKey = useRef('')

  const activeContent = openFiles.find(f => f.path === activeFile)?.content ?? ''

  /**
   * Enregistre un échec d'écriture/lecture pour affichage.
   *
   * Compte les tentatives CONSÉCUTIVES sur le même chemin : c'est ce nombre,
   * plus l'horodatage, qui fait la différence entre « ça a raté une fois » et
   * « ça rate en boucle depuis dix minutes ». Un chemin différent repart à 1 —
   * c'est une autre panne. Le compteur est remis à zéro par un succès, au même
   * endroit que l'effacement du bandeau.
   */
  const signalerEchec = useCallback((path: string, message: string) => {
    setSaveError(prev => ({
      path,
      message,
      tentatives: prev && prev.path === path ? prev.tentatives + 1 : 1,
      dernier: Date.now(),
    }))
    setMaintenant(Date.now())
  }, [])

  /**
   * Succès sur `path` : efface le bandeau — et le compteur avec, puisque les
   * deux vivent dans le même état.
   *
   * **Portée au chemin, jamais global.** Un `setSaveError(null)` inconditionnel
   * effacerait l'échec d'un AUTRE fichier : ouvrir `b.py` ferait disparaître
   * « `a.py` — enregistrement refusé » alors que `a.py` est toujours en échec
   * et toujours sale. Un seul helper pour les deux sites d'appel, sinon les
   * deux remises à zéro divergent.
   */
  const oublierEchec = useCallback((path: string) => {
    setSaveError(prev => prev?.path === path ? null : prev)
  }, [])

  useEffect(() => {
    if (!saveError) return
    const id = setInterval(() => setMaintenant(Date.now()), 1000)
    return () => clearInterval(id)
  }, [saveError])

  /**
   * Garde-fou de fermeture : demande confirmation tant qu'un onglet est sale.
   *
   * C'est LE point où du travail se perd pour de bon quand l'enregistrement
   * échoue durablement — l'auto-save réessaie sans fin, mais rien n'empêche de
   * fermer la fenêtre entre deux tentatives. Il couvre aussi le cas, plus
   * banal, de l'auto-save DÉSACTIVÉ (son défaut).
   *
   * Second filet et non premier : `openFiles` passe par `usePersistentState`,
   * donc son contenu est normalement rejoué depuis `localStorage` au
   * rechargement. « Normalement » : l'écriture peut échouer sur un quota
   * dépassé, ce qui arrive d'autant plus vite qu'un fichier peut peser 50 000
   * caractères et que `epure.code.chatTurns` partage le même stockage — et elle
   * ne suit ni un autre navigateur, ni un profil nettoyé. **Cet échec n'est plus
   * silencieux** (`persistanceOnglets`, cf. le bandeau) : c'est ce qui rend le
   * dialogue de fermeture honnête, puisque c'est là que l'utilisateur tranche.
   */
  useEffect(() => {
    if (!openFiles.some(f => f.dirty)) return
    const garde = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''  // exigé par les navigateurs qui ignorent preventDefault
    }
    window.addEventListener('beforeunload', garde)
    return () => window.removeEventListener('beforeunload', garde)
  }, [openFiles])

  // ── Fetch helpers ────────────────────────────────────────────────────────

  const fetchTree = useCallback(async () => {
    try {
      const res = await apiFetch(`${API}/code/files`)
      if (!res.ok) return  // on garde l'arbre précédent plutôt que d'annoncer « workspace vide »
      const data = await res.json()
      setTree(liste<TreeNode>(data.tree))
    } catch { /* ignore */ }
  }, [])

  const fetchAvailableModels = useCallback(async () => {
    try {
      const res = await apiFetch(`${API}/models`)
      if (!res.ok) return
      const data = await res.json()
      const cloud = data.cloud
      const all = [
        ...liste<{ id: string; nom: string }>(data.local),
        ...liste<{ id: string; nom: string }>(data.local_npu),
        ...(cloud && typeof cloud === 'object'
          ? Object.values(cloud).flatMap(v => liste<{ id: string; nom: string }>(v))
          : []),
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

  // Poll usage stats every 3s.
  // `StatsBar` déréférence `usage.session.total_tokens` sans garde : poser un
  // corps d'erreur (`{detail, type}`) dans cet état plantait le RENDU du module
  // — le « Cannot read properties of undefined » du §8, à l'identique. D'où le
  // `res.ok` ET la vérification de forme : on garde la valeur précédente.
  useEffect(() => {
    const poll = async () => {
      try {
        const res = await apiFetch(`${API}/code/usage`)
        if (!res.ok) return
        const data = await res.json()
        if (data?.session && data?.session_tokens_by_step) setUsage(data as UsageStats)
      } catch { /* ignore */ }
    }
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [])

  // ── WebSocket ────────────────────────────────────────────────────────────

  useEffect(() => {
    const ws = new WebSocket(wsUrl('/ws/code'))
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

      } else if (msg.type === 'write_request') {
        patchLast(turn => ({
          ...turn,
          events: [
            ...closeStreaming(turn.events),
            { type: 'write_request', path: msg.path, content: msg.content ?? '',
              existant: !!msg.existant },
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

      } else if (msg.type === 'conclusion') {
        patchLast(turn => ({
          ...turn,
          events: [...closeStreaming(turn.events), { type: 'conclusion', content: msg.content }],
        }))

      } else if (msg.type === 'done') {
        patchLast(turn => ({ ...turn, events: closeStreaming(turn.events) }))
        setStreaming(false)

      } else if (msg.type === 'error') {
        patchLast(turn => ({
          ...turn,
          events: [...closeStreaming(turn.events),
                   { type: 'error', content: msg.content || 'Erreur inconnue' }],
        }))
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

  /**
   * Ouvre un onglet. **Rien n'est ouvert si la lecture échoue**, et ce n'est
   * pas de la prudence gratuite : sans le contrôle, un GET en erreur donnait un
   * onglet à `content: undefined`, que `activeContent` coerce en `''` — la
   * sauvegarde suivante (auto-save comprise) écrivait alors une chaîne VIDE sur
   * un fichier bien réel. Perte de données silencieuse, déclenchée par un
   * simple clic dans l'arborescence.
   */
  const openFile = useCallback(async (path: string) => {
    const already = openFiles.find(f => f.path === path)
    if (already) { setActiveFile(path); return }
    try {
      const res = await apiFetch(`${API}/code/file?path=${encodeURIComponent(path)}`)
      if (!res.ok) {
        signalerEchec(path, await messageErreur(res, 'Lecture impossible'))
        return
      }
      const data = await res.json()
      if (typeof data?.content !== 'string') {
        signalerEchec(path, 'Lecture impossible : réponse inattendue du backend.')
        return
      }
      oublierEchec(path)
      setOpenFiles(prev => [...prev, { path, content: data.content, dirty: false }])
      setActiveFile(path)
    } catch {
      signalerEchec(path, 'Lecture impossible : backend injoignable.')
    }
  }, [openFiles, signalerEchec, oublierEchec])

  /**
   * Enregistre, et ne marque l'onglet propre QUE si le backend a confirmé.
   *
   * `apiFetch` ne lève pas sur un statut HTTP : le `catch` ne voyait que les
   * pannes réseau, donc un 4xx/5xx faisait passer l'onglet en `dirty: false`
   * — un faux signal de succès. Ce mode d'échec est né avec l'écriture
   * fail-closed du backend (`SauvegardeError` → 409) : avant, ce chemin
   * n'échouait pas. L'onglet reste sale ET le message du backend s'affiche :
   * la pastille seule est trop discrète pour dire « refusé ».
   */
  const saveFile = useCallback(async (path: string, content: string) => {
    try {
      const res = await apiFetch(`${API}/code/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, content }),
      })
      if (!res.ok) {
        signalerEchec(path, await messageErreur(res, 'Enregistrement refusé'))
        return  // l'onglet reste `dirty` : rien n'est sur le disque
      }
      oublierEchec(path)
      setOpenFiles(prev => prev.map(f => f.path === path ? { ...f, dirty: false } : f))
    } catch {
      signalerEchec(path, 'Enregistrement impossible : backend injoignable.')
    }
  }, [signalerEchec, oublierEchec])

  const reloadOpenFile = useCallback(async (path: string) => {
    const isOpen = openFiles.some(f => f.path === path)
    if (!isOpen) return
    try {
      const res = await apiFetch(`${API}/code/file?path=${encodeURIComponent(path)}`)
      if (!res.ok) return  // on garde le contenu affiché plutôt que de le vider
      const data = await res.json()
      if (typeof data?.content !== 'string') return
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
    await apiFetch(`${API}/code/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' })
    setOpenFiles(prev => prev.filter(f => f.path !== path))
    if (activeFile === path) setActiveFile(null)
    fetchTree()
  }, [activeFile, fetchTree])

  const handleRename = useCallback(async (node: TreeNode, e: React.MouseEvent) => {
    e.stopPropagation()
    const parent = node.path.includes('/') ? node.path.slice(0, node.path.lastIndexOf('/') + 1) : ''
    const proposed = window.prompt(`Renommer « ${node.name} » en :`, node.name)
    if (!proposed) return
    const newName = proposed.trim()
    if (!newName || newName === node.name) return
    // Si l'utilisateur ne met pas de slash, on garde le même dossier parent.
    const newPath = newName.includes('/') ? newName : parent + newName
    try {
      const res = await apiFetch(`${API}/code/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old: node.path, new: newPath }),
      })
      const data = await res.json()
      if (!data.ok) { window.alert(data.result ?? 'Renommage impossible'); return }
      // Met à jour les onglets/fichier actif ouverts sous l'ancien chemin.
      setOpenFiles(prev => prev.map(f =>
        f.path === node.path ? { ...f, path: newPath }
        : f.path.startsWith(node.path + '/') ? { ...f, path: newPath + f.path.slice(node.path.length) }
        : f
      ))
      setActiveFile(cur =>
        cur === node.path ? newPath
        : cur && cur.startsWith(node.path + '/') ? newPath + cur.slice(node.path.length)
        : cur
      )
      fetchTree()
    } catch { window.alert('Renommage échoué (réseau).') }
  }, [fetchTree])

  const handleCreateItem = useCallback(async () => {
    if (!newItemName.trim()) return
    if (newItemType === 'file') {
      await apiFetch(`${API}/code/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: newItemName.trim(), content: '' }),
      })
    } else {
      await apiFetch(`${API}/code/folder`, {
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
      const res = await apiFetch(`${API}/code/install`, {
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

    // Historique des tours précédents → l'IA garde le contexte (utile pour
    // corriger un code généré juste avant). On garde les 6 derniers tours.
    const history: { role: string; content: string }[] = []
    for (const turn of chatTurns.slice(-6)) {
      if (turn.role === 'user') {
        const txt = (turn.events[0] as { content?: string })?.content ?? ''
        if (txt) history.push({ role: 'user', content: txt })
      } else {
        const txt = turn.events
          .filter(e => e.type === 'text' || e.type === 'conclusion')
          .map(e => (e as { content: string }).content)
          .join('\n').trim()
        if (txt) history.push({ role: 'assistant', content: txt })
      }
    }

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
      history,
    }))
  }, [chatInput, streaming, activeFile, openFiles, stepConfig, chatTurns])

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

  // ── Écriture proposée par le repli « aucun tool appelé » ─────────────────
  // Même mécanique que confirmExecute/cancelExecute juste au-dessus : le
  // backend n'a RIEN écrit, il attend ce message. La carte disparaît dans les
  // deux cas ; le `tool_result` de retour rafraîchit l'arbre et l'onglet
  // ouvert (cf. le handler `tool_result`).

  const confirmWrite = useCallback((path: string, content: string) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'write_confirm', path, content }))
    setChatTurns(prev => {
      const turns = [...prev]
      const last = turns[turns.length - 1]
      if (!last) return prev
      const events = last.events.filter(e => !(e.type === 'write_request' && e.path === path))
      turns[turns.length - 1] = { ...last, events }
      return turns
    })
  }, [setChatTurns])

  const cancelWrite = useCallback((path: string) => {
    setChatTurns(prev => {
      const turns = [...prev]
      const last = turns[turns.length - 1]
      if (!last) return prev
      const events = last.events.filter(e => !(e.type === 'write_request' && e.path === path))
      turns[turns.length - 1] = { ...last, events }
      return turns
    })
  }, [setChatTurns])

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
                onRename={handleRename}
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

        {/* Échec d'enregistrement ou de lecture : le message du backend, pas un
            statut nu. Il dit POURQUOI le fichier n'est pas sur le disque —
            typiquement « impossible de sauvegarder la version actuelle ».
            HORS du bloc `activeFile &&` volontairement : une ouverture qui
            échoue n'ouvre AUCUN onglet, donc un bandeau placé dans la barre
            d'outils ne s'afficherait jamais dans ce cas précis. */}
        {(saveError || !persistanceOnglets.ok) && (
          <div className="m-2 flex items-start gap-2 rounded-md border border-error/40 bg-error/10 px-2.5 py-2 shrink-0">
            <div className="flex-1 min-w-0 space-y-1">
              {saveError && (
                <div>
                  <div className="text-xs font-mono text-error break-all">
                    <span className="font-semibold">{saveError.path}</span> — {saveError.message}
                  </div>
                  {/* La ligne qui dit si ça dure : elle avance toute seule (cf.
                      l'intervalle sur `maintenant`). Le compteur n'apparaît qu'à
                      partir de la 2e tentative — à 1, il n'apprendrait rien. */}
                  <div className="text-[11px] font-mono text-error/70 mt-0.5">
                    dernière tentative {depuis(saveError.dernier, maintenant)} ({heure(saveError.dernier)})
                    {saveError.tentatives > 1 && ` — ${saveError.tentatives} tentatives consécutives`}
                  </div>
                </div>
              )}
              {/* Persistance locale en échec. DANS le bandeau existant et non
                  dans une seconde zone : c'est la même information pour
                  l'utilisateur — « ce que tu as tapé n'est nulle part ». Sans
                  cette ligne, le dialogue de fermeture (`beforeunload`) laisse
                  croire que fermer est rattrapable. */}
              {!persistanceOnglets.ok && (
                <div className="text-xs font-mono text-error">
                  Onglets non conservés par le navigateur
                  {persistanceOnglets.erreur && ` (${persistanceOnglets.erreur})`} —
                  le contenu non enregistré ne survivra pas à la fermeture, même
                  après confirmation.
                </div>
              )}
            </div>
            {/* Rien à masquer quand le seul motif est la persistance : elle
                reviendrait au rendu suivant. */}
            {saveError && (
              <button
                onClick={() => setSaveError(null)}
                title="Masquer"
                className="text-error/70 hover:text-error shrink-0 transition-colors duration-150"
              ><X size={12} /></button>
            )}
          </div>
        )}

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
                  <div className="bg-[#100e0a] rounded-sm border border-line px-2 py-1.5 max-h-60 overflow-y-auto">
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
                      onWriteConfirm={confirmWrite}
                      onWriteCancel={cancelWrite}
                      onInstall={installPackage}
                      onGenerateTests={generateTests}
                      onExpand={setExpanded}
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

      {expanded && <FullscreenView data={expanded} onClose={() => setExpanded(null)} />}

      <ModuleBar module="code" showModel />
    </div>
  )
}
