import { useRef, useState, useCallback, useEffect } from 'react'
import {
  Paperclip, Mic, Zap, Bot, X, ChevronLeft, ChevronRight, Check,
  AlertTriangle, HelpCircle, Loader2, FileText, FileImage, FileJson,
  FileSpreadsheet, File as FileIcon, Save,
} from 'lucide-react'
import { Button, Input, Textarea, Select, Toggle, Tooltip } from './ui'
import type { EffortLevel, StepConfig } from '../App'
import { API, apiFetch } from '../api'
import { useModules } from '../modules'
import { useVoix } from '../voix'
import { AT_COMMANDS, allSlashCommands } from '../modules/chat/commands'

// Curated model recommendations per module (IDs morts retirés —
// la disponibilité live /models grise automatiquement le reste)
const MODULE_RECOMMENDATIONS: Record<string, { id: string; label: string }[]> = {
  chat: [
    { id: 'flm:qwen3:4b',                             label: 'Instant · NPU' },
    { id: 'groq:llama-3.1-8b-instant',                label: 'Rapide · Cloud' },
    { id: 'gemini:gemini-2.5-flash',                  label: 'Général · Cloud' },
    { id: 'nvidia:nvidia/nemotron-3-super-120b-a12b',  label: 'Puissant · Cloud' },
  ],
  // Les libellés décrivent le MODÈLE, jamais une matière : « Maths · Cloud » et
  // « Physique · Cloud » supposaient la filière de l'auteur dans un composant du
  // cœur. Une entrée dont le module n'est pas installé est simplement ignorée
  // (`MODULE_RECOMMENDATIONS[module] ?? []`).
  kholle: [
    { id: 'groq:openai/gpt-oss-120b',                 label: 'Puissant · Cloud' },
    { id: 'nvidia:nvidia/nemotron-3-super-120b-a12b',  label: 'Détaillé · Cloud' },
    { id: 'flm:qwen3:8b',                             label: 'Raisonnement · NPU' },
    { id: 'gemini:gemini-2.5-flash',                  label: 'Général · Cloud' },
  ],
  code: [
    { id: 'mistral:codestral-latest',                 label: 'Code · Mistral' },
    { id: 'groq:openai/gpt-oss-120b',                 label: 'Agent · Groq' },
    { id: 'nvidia:deepseek-ai/deepseek-v4-flash',     label: 'Raisonnement · Cloud' },
    { id: 'qwen2.5-coder:7b',                         label: 'Local · CPU' },
  ],
  docs: [
    { id: 'gemini:gemini-2.5-flash',                  label: 'Long contexte · Cloud' },
    { id: 'nvidia:deepseek-ai/deepseek-v4-flash',     label: 'Analyse · Cloud' },
    { id: 'flm:qwen3:8b',                             label: 'Local · NPU' },
  ],
}

type Panel = 'files' | 'skills' | 'model' | null

interface ModelInfo {
  id: string
  nom: string
  provider: string
  disponible: boolean
  gratuit?: boolean
  description?: string
}

interface CloudCategories {
  rapide: ModelInfo[]
  puissant: ModelInfo[]
  long_contexte: ModelInfo[]
}

interface Preset {
  id: string
  nom: string
  effort: EffortLevel
  steps: StepConfig[]
  défaut?: boolean
}

interface StepDef {
  role: string
  label: string
  recommended: string | null
}

const EFFORT_DEFINITIONS: Record<Exclude<EffortLevel, 'direct' | 'adaptive'>, StepDef[]> = {
  low: [
    { role: 'contextualizer', label: 'Contextualisation', recommended: null },
    { role: 'responder', label: 'Réponse', recommended: null },
  ],
  medium: [
    { role: 'analyzer', label: 'Analyse', recommended: 'groq:openai/gpt-oss-120b' },
    { role: 'solver', label: 'Résolution', recommended: null },
    { role: 'pedagogue', label: 'Reformulation pédagogique', recommended: 'gemini:gemini-2.5-flash' },
  ],
  high: [
    { role: 'analyzer', label: 'Analyse approfondie', recommended: 'groq:openai/gpt-oss-120b' },
    { role: 'solver', label: 'Résolution rigoureuse', recommended: null },
    { role: 'verifier', label: 'Vérification', recommended: 'groq:openai/gpt-oss-120b' },
    { role: 'pedagogue', label: 'Synthèse finale', recommended: 'gemini:gemini-2.5-flash' },
  ],
}

const EFFORT_LABELS: Record<EffortLevel, string> = {
  direct: 'Direct',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  adaptive: 'Adaptatif',
}

// Pastilles provider : local→success, NPU→violet, cloud→turquoise
const PROVIDER_DOT: Record<string, string> = {
  ollama: 'bg-success', flm: 'bg-accent',
  gemini: 'bg-accent2', groq: 'bg-accent2', cerebras: 'bg-accent2',
  nvidia: 'bg-accent2', mistral: 'bg-accent2',
}
const PROVIDER_TEXT: Record<string, string> = {
  ollama: 'text-success', flm: 'text-accent',
  gemini: 'text-accent2', groq: 'text-accent2', cerebras: 'text-accent2',
  nvidia: 'text-accent2', mistral: 'text-accent2',
}

function FileTypeIcon({ name }: { name: string }) {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  const cls = 'shrink-0 text-muted'
  if (ext === 'pdf' || ext === 'docx' || ext === 'txt' || ext === 'md')
    return <FileText size={13} className={cls} />
  if (ext === 'png' || ext === 'jpg' || ext === 'jpeg' || ext === 'webp')
    return <FileImage size={13} className={cls} />
  if (ext === 'json') return <FileJson size={13} className={cls} />
  if (ext === 'csv') return <FileSpreadsheet size={13} className={cls} />
  return <FileIcon size={13} className={cls} />
}

interface FileSummary {
  résumé: string
  pages_totales: number
  chunks_indexés: number
}

export interface ModuleBarProps {
  module: string
  showFile?: boolean
  showMic?: boolean
  showSkills?: boolean
  showModel?: boolean
  showEffort?: boolean
  onTranscribed?: (text: string) => void
  ttsEnabled?: boolean
  onTtsToggle?: () => void
  synthesizingText?: string | null
  speakingText?: string | null
  effort?: EffortLevel
  onEffortChange?: (e: EffortLevel) => void
  pipelineSteps?: StepConfig[]
  onPipelineStepsChange?: (steps: StepConfig[]) => void
}

export default function ModuleBar({
  module,
  showFile,
  showMic,
  showSkills,
  showModel,
  showEffort,
  onTranscribed,
  ttsEnabled,
  onTtsToggle,
  synthesizingText,
  speakingText,
  effort = 'direct',
  onEffortChange,
  pipelineSteps = [],
  onPipelineStepsChange,
}: ModuleBarProps) {
  // Modules installés : source des commandes `/` du panneau Compétences.
  const modules = useModules()
  // Le micro se décide ICI et pas chez les appelants. `showMic` dit « ce module
  // veut un micro » ; la capacité dit « cette machine en a un ». Le filtre est
  // dans le composant partagé pour qu'un module ajouté plus tard — un module
  // généré par l'Atelier, qui ne sait rien de tout ça — hérite du bon
  // comportement sans une ligne à écrire. Sur ARM64, où faster-whisper n'est pas
  // installé (cf. voix.ts), un micro affiché ne rend qu'un 503 par appui.
  const voix = useVoix()
  const micDisponible = !!showMic && voix.transcription
  const [activePanel, setActivePanel] = useState<Panel>(null)
  const [showFullModelList, setShowFullModelList] = useState(false)

  // Files state
  const [availableFiles, setAvailableFiles] = useState<string[]>([])
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [activeFiles, setActiveFiles] = useState<string[]>([])
  const [summary, setSummary] = useState<FileSummary | null>(null)
  const [summaryText, setSummaryText] = useState('')
  const summaryAccRef = useRef('')
  const [loadingFiles, setLoadingFiles] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Skills state
  const [strictMode, setStrictMode] = useState(false)
  const [sessionInstruction, setSessionInstruction] = useState('')
  const [instructionDraft, setInstructionDraft] = useState('')

  // Model state
  const [localModels, setLocalModels] = useState<ModelInfo[]>([])
  const [localNpuModels, setLocalNpuModels] = useState<ModelInfo[]>([])
  const [cloudCategories, setCloudCategories] = useState<CloudCategories>({ rapide: [], puissant: [], long_contexte: [] })
  const [selectedModel, setSelectedModel] = useState('qwen2.5:7b')

  // Preset state (for effort panel)
  const [presets, setPresets] = useState<Preset[]>([])
  const [saveModalOpen, setSaveModalOpen] = useState(false)
  const [newPresetName, setNewPresetName] = useState('')

  // STT state
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<BlobPart[]>([])

  const togglePanel = (panel: Panel) => {
    setActivePanel(prev => (prev === panel ? null : panel))
    if (panel === 'model') setShowFullModelList(false)
  }

  const allModels = useCallback((): ModelInfo[] => [
    ...localModels,
    ...localNpuModels,
    ...cloudCategories.rapide,
    ...cloudCategories.puissant,
    ...cloudCategories.long_contexte,
  ], [localModels, localNpuModels, cloudCategories])

  const defaultModelForRole = useCallback((recommended: string | null): string => {
    if (!recommended) return selectedModel
    const found = allModels().find(m => m.id === recommended && m.disponible)
    return found ? recommended : selectedModel
  }, [selectedModel, allModels])

  const buildDefaultSteps = useCallback((e: EffortLevel): StepConfig[] => {
    if (e === 'direct' || e === 'adaptive') return []
    const defs = EFFORT_DEFINITIONS[e]
    return defs.map(d => ({ role: d.role, model: defaultModelForRole(d.recommended) }))
  }, [defaultModelForRole])

  const handleEffortChange = useCallback((e: EffortLevel) => {
    onEffortChange?.(e)
    onPipelineStepsChange?.(buildDefaultSteps(e))
  }, [onEffortChange, onPipelineStepsChange, buildDefaultSteps])

  // Rebuild steps when selectedModel changes
  useEffect(() => {
    if (effort !== 'direct' && effort !== 'adaptive' && pipelineSteps.length === 0) {
      onPipelineStepsChange?.(buildDefaultSteps(effort))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedModel, effort])

  // ── Initial load ──────────────────────────────────────────────────────────

  useEffect(() => {
    if (showFile) {
      apiFetch(`${API}/rag/files`)
        .then(r => r.json())
        .then((d: { files: string[] }) => setAvailableFiles(d.files))
        .catch(() => {})
    }

    apiFetch(`${API}/context`)
      .then(r => r.json())
      .then((d: Record<string, unknown>) => {
        if (showFile) {
          const files = (d['fichiers_actifs'] as string[]) ?? []
          setActiveFiles(files)
          setSelectedFiles(files)
        }
        setStrictMode((d['strict_mode'] as boolean) ?? false)
        const instr = (d['session_instruction'] as string) ?? ''
        setSessionInstruction(instr)
        setInstructionDraft(instr)
        setSelectedModel((d['modèle_actif'] as string) ?? 'qwen2.5:7b')
      })
      .catch(() => {})

    if (showModel) {
      apiFetch(`${API}/models`)
        .then(r => r.json())
        .then((d: { local: ModelInfo[]; local_npu?: ModelInfo[]; cloud: CloudCategories }) => {
          setLocalModels(d.local ?? [])
          setLocalNpuModels(d.local_npu ?? [])
          setCloudCategories(d.cloud ?? { rapide: [], puissant: [], long_contexte: [] })
        })
        .catch(() => {})
    }

    if (showEffort) {
      apiFetch(`${API}/orchestrator/presets`)
        .then(r => r.json())
        .then((d: { presets: Preset[] }) => setPresets(d.presets ?? []))
        .catch(() => {})
    }
  }, [showFile, showModel, showEffort])

  // ── Settings sync ─────────────────────────────────────────────────────────

  const pushSettings = useCallback((patch: Record<string, unknown>) => {
    apiFetch(`${API}/context/settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).catch(() => {})
  }, [])

  const handleStrictToggle = useCallback(() => {
    const next = !strictMode
    setStrictMode(next)
    pushSettings({ strict_mode: next })
  }, [strictMode, pushSettings])

  const handleInstructionSave = useCallback(() => {
    setSessionInstruction(instructionDraft)
    pushSettings({ session_instruction: instructionDraft })
  }, [instructionDraft, pushSettings])

  const handleModelSelect = useCallback((model: string) => {
    setSelectedModel(model)
    pushSettings({ 'modèle_actif': model })
    setActivePanel(null)
  }, [pushSettings])

  // ── Pipeline ──────────────────────────────────────────────────────────────

  const handleStepModelChange = useCallback((idx: number, model: string) => {
    const next = pipelineSteps.map((s, i) => i === idx ? { ...s, model } : s)
    onPipelineStepsChange?.(next)
  }, [pipelineSteps, onPipelineStepsChange])

  const loadPreset = useCallback((preset: Preset) => {
    onEffortChange?.(preset.effort)
    onPipelineStepsChange?.(preset.steps)
  }, [onEffortChange, onPipelineStepsChange])

  const savePreset = useCallback(async () => {
    if (!newPresetName.trim()) return
    try {
      const res = await apiFetch(`${API}/orchestrator/presets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nom: newPresetName.trim(), effort, steps: pipelineSteps }),
      })
      const preset = await res.json() as Preset
      setPresets(prev => [...prev, preset])
      setNewPresetName('')
      setSaveModalOpen(false)
    } catch { /* ignore */ }
  }, [newPresetName, effort, pipelineSteps])

  // ── Files ─────────────────────────────────────────────────────────────────

  const consumeLoadStream = useCallback(async (res: Response, finalPaths?: string[]) => {
    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let finalPages = 0
    let finalChunks = 0
    summaryAccRef.current = ''
    setSummaryText('')
    setSummary(null)
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
          if (ev.type === 'token') { summaryAccRef.current += ev.content; setSummaryText(summaryAccRef.current) }
          else if (ev.type === 'done') { finalPages = ev.pages; finalChunks = ev.chunks }
        } catch { /* skip */ }
      }
    }
    setSummary({ résumé: summaryAccRef.current, pages_totales: finalPages, chunks_indexés: finalChunks })
    if (finalPaths) setActiveFiles(finalPaths)
  }, [])

  const loadSelectedFiles = useCallback(async () => {
    if (selectedFiles.length === 0) return
    setLoadingFiles(true)
    try {
      const res = await apiFetch(`${API}/files/load`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: selectedFiles }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consumeLoadStream(res, selectedFiles)
    } catch { /* ignore */ }
    finally { setLoadingFiles(false) }
  }, [selectedFiles, consumeLoadStream])

  const clearActiveFiles = useCallback(async () => {
    await apiFetch(`${API}/files/active`, { method: 'DELETE' })
    setActiveFiles([]); setSelectedFiles([]); setSummary(null); setSummaryText('')
  }, [])

  const uploadFiles = useCallback(async (files: File[]) => {
    const supported = files.filter(f => {
      const ext = f.name.split('.').pop()?.toLowerCase() ?? ''
      return ['pdf','docx','txt','md','csv','json','png','jpg','jpeg','webp'].includes(ext)
    })
    if (supported.length === 0) return
    setLoadingFiles(true)
    try {
      const form = new FormData()
      supported.forEach(f => form.append('files', f, f.name))
      const res = await apiFetch(`${API}/files/upload`, { method: 'POST', body: form })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consumeLoadStream(res)
      const [filesData, ctxData] = await Promise.all([
        apiFetch(`${API}/rag/files`).then(r => r.json()) as Promise<{ files: string[] }>,
        apiFetch(`${API}/context`).then(r => r.json()) as Promise<Record<string, unknown>>,
      ])
      setAvailableFiles(filesData.files)
      const active = (ctxData['fichiers_actifs'] as string[]) ?? []
      setActiveFiles(active); setSelectedFiles(active)
    } catch { /* ignore */ }
    finally { setLoadingFiles(false) }
  }, [consumeLoadStream])

  // ── STT ───────────────────────────────────────────────────────────────────

  const stopRecording = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state !== 'inactive') recorderRef.current.stop()
  }, [])

  const handleMicDown = useCallback(async (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      chunksRef.current = []
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : undefined
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      recorder.ondataavailable = ev => { if (ev.data.size > 0) chunksRef.current.push(ev.data) }
      recorder.onstop = async () => {
        stream.getTracks().forEach(t => t.stop())
        setRecording(false)
        if (chunksRef.current.length === 0) return
        setTranscribing(true)
        try {
          const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
          const form = new FormData()
          form.append('audio', blob, 'recording.webm')
          const res = await apiFetch(`${API}/voice/transcribe`, { method: 'POST', body: form })
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          const data: { text: string } = await res.json()
          if (data.text) onTranscribed?.(data.text)
        } catch { /* ignore */ }
        finally { setTranscribing(false) }
      }
      recorder.start()
      recorderRef.current = recorder
      setRecording(true)
    } catch { /* mic denied */ }
  }, [onTranscribed])

  const handleMicUp = useCallback(() => stopRecording(), [stopRecording])

  const basename = (p: string) => p.split(/[/\\]/).pop() ?? p

  // ── Effort panel vars ─────────────────────────────────────────────────────

  const stepDefs = showEffort && effort !== 'direct' && effort !== 'adaptive'
    ? EFFORT_DEFINITIONS[effort as Exclude<EffortLevel, 'direct' | 'adaptive'>] ?? []
    : []
  const relevantPresets = presets.filter(p => p.effort === effort)

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="relative border-t border-line bg-surface shrink-0">

      {/* ── Pipeline config panel (effort) ── */}
      {showEffort && effort !== 'direct' && effort !== 'adaptive' && stepDefs.length > 0 && (
        <div className="border-t border-line bg-surface px-4 py-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted uppercase tracking-wide">
              Pipeline {EFFORT_LABELS[effort]} · {stepDefs.length} étapes
            </span>
            <div className="flex items-center gap-2">
              {relevantPresets.length > 0 && (
                <Select
                  mono
                  defaultValue=""
                  onChange={e => {
                    const preset = presets.find(p => p.id === e.target.value)
                    if (preset) loadPreset(preset)
                    e.target.value = ''
                  }}
                >
                  <option value="" disabled>Presets</option>
                  {relevantPresets.map(p => (
                    <option key={p.id} value={p.id}>{p.nom}</option>
                  ))}
                </Select>
              )}
              <Button variant="ghost" size="sm" icon={<Save size={13} />} onClick={() => setSaveModalOpen(v => !v)}>
                preset
              </Button>
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            {stepDefs.map((def, idx) => {
              const chosenModel = pipelineSteps[idx]?.model ?? selectedModel
              const isRecommended = def.recommended && chosenModel === def.recommended
              return (
                <div key={def.role} className="flex items-center gap-1.5 bg-elevated border border-line rounded-sm px-2 py-1">
                  <span className="text-xs font-mono text-muted">{String(idx + 1).padStart(2, '0')}</span>
                  <span className="text-xs text-secondary shrink-0">{def.label}</span>
                  <Select
                    mono
                    value={chosenModel}
                    onChange={e => handleStepModelChange(idx, e.target.value)}
                    className="max-w-36"
                  >
                    {allModels().filter(m => m.disponible).map(m => (
                      <option key={m.id} value={m.id}>{m.nom}</option>
                    ))}
                  </Select>
                  {isRecommended && <span className="text-xs text-accent2 shrink-0">recommandé</span>}
                </div>
              )
            })}
          </div>

          {saveModalOpen && (
            <div className="flex items-center gap-2 pt-1">
              <Input
                value={newPresetName}
                onChange={e => setNewPresetName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') savePreset() }}
                placeholder="Nom du preset..."
                className="flex-1 text-xs"
              />
              <Button variant="secondary" size="sm" onClick={savePreset} disabled={!newPresetName.trim()}>
                Sauvegarder
              </Button>
              <Button variant="ghost" size="sm" icon={<X size={13} />} onClick={() => setSaveModalOpen(false)} aria-label="Fermer" />
            </div>
          )}
        </div>
      )}

      {/* ── Files panel ── */}
      {showFile && activePanel === 'files' && (
        <div className="border-t border-line bg-surface px-4 py-4 max-h-72 overflow-y-auto space-y-4">
          <div
            onDragOver={e => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={e => { e.preventDefault(); setDragOver(false); uploadFiles(Array.from(e.dataTransfer.files)) }}
            onClick={() => fileInputRef.current?.click()}
            className={`border border-dashed rounded-md px-4 py-3 cursor-pointer transition-colors duration-150 text-center ${
              dragOver ? 'border-accent/50 bg-accent/5' : 'border-line hover:border-accent/30'
            }`}
          >
            <span className="text-xs text-muted">
              {loadingFiles ? 'Chargement...' : 'Glisser un fichier ici · Cliquer pour parcourir'}
            </span>
            <input ref={fileInputRef} type="file" multiple
              accept=".pdf,.docx,.txt,.md,.csv,.json,.png,.jpg,.jpeg,.webp"
              className="hidden"
              onChange={e => { if (e.target.files) uploadFiles(Array.from(e.target.files)) }}
            />
          </div>

          {availableFiles.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs text-muted uppercase tracking-wide mb-2">Fichiers indexés</p>
              {availableFiles.map(f => (
                <label key={f} className="flex items-center gap-2 cursor-pointer group">
                  <input type="checkbox" checked={selectedFiles.includes(f)}
                    onChange={e => setSelectedFiles(prev => e.target.checked ? [...prev, f] : prev.filter(x => x !== f))}
                    className="accent-[--accent-primary] shrink-0"
                  />
                  <FileTypeIcon name={basename(f)} />
                  <span className="text-xs font-mono text-secondary group-hover:text-primary transition-colors duration-150 truncate">
                    {basename(f)}
                  </span>
                </label>
              ))}
            </div>
          )}

          <div className="flex gap-2 flex-wrap">
            <Button variant="secondary" size="sm" onClick={loadSelectedFiles} disabled={selectedFiles.length === 0 || loadingFiles}>
              {loadingFiles ? 'Chargement...' : `Charger${selectedFiles.length > 0 ? ` (${selectedFiles.length})` : ''}`}
            </Button>
            {activeFiles.length > 0 && (
              <Button variant="ghost" size="sm" onClick={clearActiveFiles}>
                Vider le contexte
              </Button>
            )}
          </div>

          {(summaryText || summary) && (
            <div className="bg-elevated border border-line rounded-md px-3 py-3 space-y-1">
              <p className="text-xs text-muted uppercase tracking-wide">
                {summary ? `Résumé · ${summary.pages_totales} pages · ${summary.chunks_indexés} chunks` : 'Génération du résumé...'}
              </p>
              <p className="text-xs text-secondary leading-relaxed whitespace-pre-wrap">
                {summaryText || summary?.résumé}
                {!summary && summaryText && <span className="animate-pulse text-accent2">▍</span>}
              </p>
            </div>
          )}
        </div>
      )}

      {/* ── Skills panel ── */}
      {showSkills && activePanel === 'skills' && (
        <div className="border-t border-line bg-surface px-4 py-4 space-y-4 max-h-96 overflow-y-auto">
          <div className="flex items-center gap-5 flex-wrap">
            {onTtsToggle && (
              <div className="flex items-center gap-2">
                <Toggle checked={!!ttsEnabled} onChange={onTtsToggle} label="Lecture auto" />
                {/* Trois états distincts, et l'ordre compte : la synthèse précède
                    toujours la lecture. Annoncer « lecture... » pendant une synthèse
                    de 49 s (mesuré sur un message long) donnait une interface qui
                    prétend jouer un son qu'on n'entend pas. */}
                <span className="text-xs text-secondary">
                  {synthesizingText ? 'synthèse...' : speakingText ? 'lecture...' : 'lecture auto'}
                </span>
              </div>
            )}
            <div className="flex items-center gap-2">
              <Toggle checked={strictMode} onChange={handleStrictToggle} label="Mode strict" />
              <span className="text-xs text-secondary">mode strict</span>
            </div>
          </div>

          <div className="space-y-1.5">
            <p className="text-xs text-muted uppercase tracking-wide">Préfixes @</p>
            {/* AT_COMMANDS et non une copie : cette liste était dupliquée du
                fichier de commandes du chat, et avait déjà divergé — il y
                manquait `@web`. */}
            {AT_COMMANDS.map(c => (
              <div key={c.trigger} className="flex gap-2 items-baseline">
                <span className="text-xs font-mono text-accent2 shrink-0 w-24">{c.trigger}</span>
                <span className="text-xs text-muted">{c.desc}</span>
              </div>
            ))}
          </div>

          <div className="space-y-1.5">
            <p className="text-xs text-muted uppercase tracking-wide">Commandes /</p>
            {/* Dérivées des modules installés, comme dans le chat : ce panneau
                annonçait `/kholle` et `/flashcards` à tout le monde, y compris
                là où ces modules n'existent pas. */}
            {allSlashCommands(modules).map(c => (
              <div key={c.trigger} className="flex gap-2 items-baseline">
                <span className="text-xs font-mono text-accent shrink-0 w-24">{c.trigger}</span>
                <span className="text-xs text-muted">{c.desc}</span>
              </div>
            ))}
          </div>

          <div className="space-y-2">
            <p className="text-xs text-muted uppercase tracking-wide">Instruction de session</p>
            <Textarea
              value={instructionDraft}
              onChange={e => setInstructionDraft(e.target.value)}
              placeholder="Ex : répondre en LaTeX, ne pas utiliser de métaphores..."
              rows={2}
              className="w-full text-xs"
            />
            <Button variant="secondary" size="sm" onClick={handleInstructionSave} disabled={instructionDraft === sessionInstruction}>
              Sauvegarder
            </Button>
          </div>
        </div>
      )}

      {/* ── Model panel ── */}
      {showModel && activePanel === 'model' && (() => {
        const allCloud = [...cloudCategories.rapide, ...cloudCategories.puissant, ...cloudCategories.long_contexte]
        const hasModels = localModels.length > 0 || localNpuModels.length > 0 || allCloud.length > 0
        const curatedRecs = MODULE_RECOMMENDATIONS[module] ?? []

        // Item de la liste complète — disponibilité pilotée par /models
        const modelRow = (m: ModelInfo, dot: string, tag?: string, tagCls?: string) => {
          const isSelected = m.id === selectedModel
          const row = (
            <button key={m.id} onClick={() => m.disponible ? handleModelSelect(m.id) : undefined}
              disabled={!m.disponible}
              className={`w-full text-left px-3 py-1.5 rounded-sm text-xs font-mono transition-colors duration-150 flex items-center gap-2 ${
                isSelected ? 'bg-accent/10 text-primary'
                : m.disponible ? 'text-secondary hover:text-primary hover:bg-elevated'
                : 'text-muted/60 cursor-not-allowed'
              }`}>
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${m.disponible ? dot : 'bg-line'}`} />
              <span className="flex-1 truncate">{m.nom}</span>
              {tag && <span className={`text-xs shrink-0 ${tagCls}`}>{tag}</span>}
              {isSelected && <Check size={13} className="text-accent shrink-0" />}
              {!m.disponible && <AlertTriangle size={11} className="text-warning shrink-0" />}
            </button>
          )
          return m.disponible ? row : (
            <Tooltip key={m.id} content="indisponible : clé ou catalogue" side="top">
              {row}
            </Tooltip>
          )
        }

        if (!showFullModelList) {
          return (
            <div className="border-t border-line bg-surface px-4 py-3">
              {curatedRecs.length > 0 && (
                <>
                  <p className="text-xs text-muted uppercase tracking-wide mb-2">
                    Recommandés pour ce module
                  </p>
                  <div className="space-y-0.5 mb-3">
                    {curatedRecs.map(rec => {
                      const info = allModels().find(m => m.id === rec.id)
                      const isSelected = selectedModel === rec.id
                      // 3 états : dispo (cliquable) / indispo (grisé) / inconnu du catalogue (marqué ?)
                      const isAvail = info?.disponible ?? false
                      const isUnknown = !info && hasModels
                      const clickable = isAvail || isUnknown
                      const row = (
                        <button
                          key={rec.id}
                          onClick={() => clickable ? handleModelSelect(rec.id) : undefined}
                          disabled={!clickable}
                          className={`w-full text-left px-3 py-1.5 rounded-sm text-xs transition-colors duration-150 flex items-center gap-3 ${
                            isSelected ? 'bg-accent/10 text-primary font-medium'
                            : isAvail ? 'text-secondary hover:text-primary hover:bg-elevated'
                            : isUnknown ? 'text-secondary'
                            : 'text-muted/60 cursor-not-allowed'
                          }`}
                        >
                          <span className="shrink-0 w-4 inline-flex justify-center">
                            {isSelected ? <Check size={13} className="text-accent" /> : <span className="w-1.5 h-1.5 rounded-full bg-line inline-block" />}
                          </span>
                          <span className="flex-1 truncate">{rec.label}</span>
                          <span className="text-xs font-mono text-muted shrink-0 truncate max-w-28">
                            {info?.nom ?? rec.id.split(':').pop()}
                          </span>
                          {isUnknown && <HelpCircle size={11} className="text-muted shrink-0" />}
                          {info && !info.disponible && <AlertTriangle size={11} className="text-warning shrink-0" />}
                        </button>
                      )
                      return info && !info.disponible ? (
                        <Tooltip key={rec.id} content="indisponible : clé ou catalogue" side="top">
                          {row}
                        </Tooltip>
                      ) : row
                    })}
                  </div>
                </>
              )}
              <button
                onClick={() => setShowFullModelList(true)}
                className="w-full flex items-center justify-between px-3 py-1.5 rounded-sm text-xs text-muted hover:text-primary hover:bg-elevated transition-colors duration-150 border border-line"
              >
                Voir tous les modèles
                <ChevronRight size={13} />
              </button>
            </div>
          )
        }

        return (
          <div className="border-t border-line bg-surface px-4 py-3 max-h-[60vh] overflow-y-auto">
            <button
              onClick={() => setShowFullModelList(false)}
              className="flex items-center gap-1 text-xs text-muted hover:text-primary transition-colors duration-150 mb-2"
            >
              <ChevronLeft size={13} />
              Recommandés
            </button>
            {!hasModels ? (
              <p className="text-xs text-muted flex items-center gap-2">
                <Loader2 size={13} className="animate-spin" />
                Chargement des modèles...
              </p>
            ) : (
              <div className="space-y-0.5">
                {localModels.length > 0 && (
                  <>
                    <p className="text-xs text-muted uppercase tracking-wide px-3 py-1">Local</p>
                    {localModels.map(m => modelRow(m, 'bg-success'))}
                  </>
                )}

                {localNpuModels.length > 0 && (
                  <>
                    <div className="border-t border-line my-1" />
                    <p className="text-xs text-muted uppercase tracking-wide px-3 py-1">Local NPU</p>
                    {localNpuModels.map(m => modelRow(m, 'bg-accent', 'NPU', 'text-accent'))}
                  </>
                )}

                {(['rapide', 'puissant', 'long_contexte'] as const).map(cat => {
                  const catLabel = { rapide: 'Rapide', puissant: 'Puissant', long_contexte: 'Long contexte' }[cat]
                  const models = cloudCategories[cat]
                  if (!models || models.length === 0) return null
                  return (
                    <div key={cat}>
                      <div className="border-t border-line my-1" />
                      <p className="text-xs text-muted uppercase tracking-wide px-3 py-1">{catLabel}</p>
                      {models.map(m => modelRow(
                        m,
                        PROVIDER_DOT[m.provider] ?? 'bg-accent2',
                        m.provider,
                        PROVIDER_TEXT[m.provider] ?? 'text-muted',
                      ))}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )
      })()}

      {/* ── Main bar ── */}
      <div className="flex items-center gap-1 px-4 py-2">
        {showFile && (
          <button onClick={() => togglePanel('files')} title="Fichiers"
            className={`relative p-2 rounded-sm transition-colors duration-150 ${
              activePanel === 'files'
                ? 'bg-accent/10 text-accent'
                : 'text-muted hover:text-secondary hover:bg-elevated'
            }`}>
            <Paperclip size={15} />
            {activeFiles.length > 0 && (
              <span className="absolute -top-0.5 -right-0.5 min-w-4 h-4 px-0.5 bg-accent2 rounded-full text-xs font-mono text-on-accent flex items-center justify-center leading-none">
                {activeFiles.length}
              </span>
            )}
          </button>
        )}

        {micDisponible && (
          <button
            onPointerDown={handleMicDown}
            onPointerUp={handleMicUp}
            onPointerLeave={handleMicUp}
            disabled={transcribing}
            title="Micro (maintenir)"
            className={`p-2 rounded-sm transition-colors duration-150 select-none touch-none ${
              recording
                ? 'bg-error/15 text-error'
                : transcribing
                ? 'text-muted cursor-wait'
                : 'text-muted hover:text-secondary hover:bg-elevated'
            }`}>
            {transcribing
              ? <Loader2 size={15} className="animate-spin" />
              : recording
              ? <Mic size={15} className="animate-pulse" />
              : <Mic size={15} />}
          </button>
        )}

        {showSkills && (
          <button onClick={() => togglePanel('skills')} title="Paramètres de session"
            className={`p-2 rounded-sm transition-colors duration-150 ${
              activePanel === 'skills'
                ? 'bg-accent/10 text-accent'
                : strictMode || ttsEnabled || sessionInstruction
                ? 'text-accent2 hover:bg-elevated'
                : 'text-muted hover:text-secondary hover:bg-elevated'
            }`}>
            <Zap size={15} />
          </button>
        )}

        {showModel && (
          <button onClick={() => togglePanel('model')} title="Modèle"
            className={`p-2 rounded-sm transition-colors duration-150 ${
              activePanel === 'model'
                ? 'bg-accent/10 text-accent'
                : 'text-muted hover:text-secondary hover:bg-elevated'
            }`}>
            <Bot size={15} />
          </button>
        )}

        {showEffort && (
          <>
            <div className="w-px h-4 bg-line mx-1" />
            {(['direct', 'low', 'medium', 'high', 'adaptive'] as EffortLevel[]).map(e => (
              <button key={e} onClick={() => handleEffortChange(e)}
                className={`px-2.5 py-1 rounded-full text-xs border transition-colors duration-150 ${
                  effort === e
                    ? 'bg-accent/15 border-accent/30 text-accent font-medium'
                    : 'border-line text-muted hover:text-secondary hover:border-accent/30'
                }`}>
                {EFFORT_LABELS[e]}
              </button>
            ))}
          </>
        )}

        {showModel && (
          <span className="ml-auto text-xs font-mono text-muted truncate max-w-28">
            {(() => {
              const info = allModels().find(m => m.id === selectedModel)
              return info?.nom?.split(' ')[0] ?? selectedModel.split(':').pop() ?? selectedModel
            })()}
          </span>
        )}
      </div>
    </div>
  )
}
