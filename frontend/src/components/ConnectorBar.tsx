import { useRef, useState, useCallback, useEffect } from 'react'
import type { EffortLevel, StepConfig } from '../App'

const API = 'http://localhost:8000'

interface ConnectorBarProps {
  activeInputRef: React.MutableRefObject<((text: string) => void) | null>
  ttsEnabled: boolean
  onTtsToggle: () => void
  speakingText: string | null
  effort: EffortLevel
  onEffortChange: (e: EffortLevel) => void
  pipelineSteps: StepConfig[]
  onPipelineStepsChange: (steps: StepConfig[]) => void
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
    { role: 'analyzer', label: 'Analyse', recommended: 'groq:deepseek-r1-distill-llama-70b' },
    { role: 'solver', label: 'Résolution', recommended: null },
    { role: 'pedagogue', label: 'Reformulation pédagogique', recommended: 'gemini:gemini-2.5-flash' },
  ],
  high: [
    { role: 'analyzer', label: 'Analyse approfondie', recommended: 'groq:deepseek-r1-distill-llama-70b' },
    { role: 'solver', label: 'Résolution rigoureuse', recommended: null },
    { role: 'verifier', label: 'Vérification', recommended: 'groq:deepseek-r1-distill-llama-70b' },
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

const PROVIDER_DOT: Record<string, string> = {
  ollama: 'bg-[#3a5a3a]', gemini: 'bg-[#3a4a7a]', groq: 'bg-[#5a3a7a]',
  cerebras: 'bg-[#3a6a6a]', deepseek: 'bg-[#3a5a7a]', nvidia: 'bg-[#3a6a5a]',
  flm: 'bg-[#6a3a9a]',
}
const PROVIDER_TEXT: Record<string, string> = {
  ollama: 'text-[#3a6a3a]', gemini: 'text-[#4a6a9a]', groq: 'text-[#7a5a9a]',
  cerebras: 'text-[#5a9a9a]', deepseek: 'text-[#5a7a9a]', nvidia: 'text-[#5a9a7a]',
  flm: 'text-[#8a5aaa]',
}

interface FileSummary {
  résumé: string
  pages_totales: number
  chunks_indexés: number
}

export default function ConnectorBar({
  activeInputRef,
  ttsEnabled,
  onTtsToggle,
  speakingText,
  effort,
  onEffortChange,
  pipelineSteps,
  onPipelineStepsChange,
}: ConnectorBarProps) {
  const [activePanel, setActivePanel] = useState<Panel>(null)

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
  const [recommandations, setRecommandations] = useState<Record<string, string>>({})
  const [selectedModel, setSelectedModel] = useState('qwen2.5:7b')

  // Preset state
  const [presets, setPresets] = useState<Preset[]>([])
  const [saveModalOpen, setSaveModalOpen] = useState(false)
  const [newPresetName, setNewPresetName] = useState('')

  // STT state
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<BlobPart[]>([])

  const togglePanel = (panel: Panel) =>
    setActivePanel(prev => (prev === panel ? null : panel))

  // ── Helpers ────────────────────────────────────────────────────────────────

  const allModels = useCallback((): ModelInfo[] => {
    return [
      ...localModels,
      ...localNpuModels,
      ...cloudCategories.rapide,
      ...cloudCategories.puissant,
      ...cloudCategories.long_contexte,
    ]
  }, [localModels, localNpuModels, cloudCategories])

  const defaultModelForRole = useCallback((recommended: string | null): string => {
    if (!recommended) return selectedModel
    const models = allModels()
    const found = models.find(m => m.id === recommended && m.disponible)
    return found ? recommended : selectedModel
  }, [selectedModel, allModels])

  // ── Build default steps when effort changes ───────────────────────────────

  const buildDefaultSteps = useCallback((e: EffortLevel): StepConfig[] => {
    if (e === 'direct' || e === 'adaptive') return []
    const defs = EFFORT_DEFINITIONS[e]
    return defs.map(d => ({
      role: d.role,
      model: defaultModelForRole(d.recommended),
    }))
  }, [defaultModelForRole])

  const handleEffortChange = useCallback((e: EffortLevel) => {
    onEffortChange(e)
    onPipelineStepsChange(buildDefaultSteps(e))
  }, [onEffortChange, onPipelineStepsChange, buildDefaultSteps])

  // Rebuild steps when selectedModel changes (for "active" placeholders)
  useEffect(() => {
    if (effort !== 'direct' && effort !== 'adaptive' && pipelineSteps.length === 0) {
      onPipelineStepsChange(buildDefaultSteps(effort))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedModel, effort])

  // ── Load initial context and files ────────────────────────────────────────

  useEffect(() => {
    fetch(`${API}/rag/files`)
      .then(r => r.json())
      .then((d: { files: string[] }) => setAvailableFiles(d.files))
      .catch(err => console.error('GET /rag/files:', err))

    fetch(`${API}/context`)
      .then(r => r.json())
      .then((d: Record<string, unknown>) => {
        const files = (d['fichiers_actifs'] as string[]) ?? []
        setActiveFiles(files)
        setSelectedFiles(files)
        setStrictMode((d['strict_mode'] as boolean) ?? false)
        const instr = (d['session_instruction'] as string) ?? ''
        setSessionInstruction(instr)
        setInstructionDraft(instr)
        setSelectedModel((d['modèle_actif'] as string) ?? 'qwen2.5:7b')
      })
      .catch(err => console.error('GET /context:', err))

    fetch(`${API}/models`)
      .then(r => r.json())
      .then((d: { local: ModelInfo[]; local_npu?: ModelInfo[]; cloud: CloudCategories; recommandations: Record<string, string> }) => {
        setLocalModels(d.local ?? [])
        setLocalNpuModels(d.local_npu ?? [])
        setCloudCategories(d.cloud ?? { rapide: [], puissant: [], long_contexte: [] })
        setRecommandations(d.recommandations ?? {})
      })
      .catch(err => console.error('GET /models:', err))

    fetch(`${API}/orchestrator/presets`)
      .then(r => r.json())
      .then((d: { presets: Preset[] }) => setPresets(d.presets ?? []))
      .catch(err => console.error('GET /orchestrator/presets:', err))
  }, [])

  // ── Settings sync ─────────────────────────────────────────────────────────

  const pushSettings = useCallback((patch: Record<string, unknown>) => {
    fetch(`${API}/context/settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).catch(err => console.error('PATCH /context/settings:', err))
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

  const handleModelSelect = useCallback(
    (model: string) => {
      setSelectedModel(model)
      pushSettings({ 'modèle_actif': model })
      setActivePanel(null)
    },
    [pushSettings]
  )

  // ── Pipeline step model change ─────────────────────────────────────────────

  const handleStepModelChange = useCallback((idx: number, model: string) => {
    const next = pipelineSteps.map((s, i) => i === idx ? { ...s, model } : s)
    onPipelineStepsChange(next)
  }, [pipelineSteps, onPipelineStepsChange])

  // ── Preset actions ────────────────────────────────────────────────────────

  const loadPreset = useCallback((preset: Preset) => {
    onEffortChange(preset.effort)
    onPipelineStepsChange(preset.steps)
  }, [onEffortChange, onPipelineStepsChange])

  const savePreset = useCallback(async () => {
    if (!newPresetName.trim()) return
    try {
      const res = await fetch(`${API}/orchestrator/presets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nom: newPresetName.trim(), effort, steps: pipelineSteps }),
      })
      const preset = await res.json() as Preset
      setPresets(prev => [...prev, preset])
      setNewPresetName('')
      setSaveModalOpen(false)
    } catch (err) {
      console.error('POST /orchestrator/presets:', err)
    }
  }, [newPresetName, effort, pipelineSteps])

  const deletePreset = useCallback(async (id: string) => {
    try {
      await fetch(`${API}/orchestrator/presets/${id}`, { method: 'DELETE' })
      setPresets(prev => prev.filter(p => p.id !== id))
    } catch (err) {
      console.error('DELETE /orchestrator/presets:', err)
    }
  }, [])

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
          if (ev.type === 'token') {
            summaryAccRef.current += ev.content
            setSummaryText(summaryAccRef.current)
          } else if (ev.type === 'done') {
            finalPages = ev.pages
            finalChunks = ev.chunks
          }
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
      const res = await fetch(`${API}/files/load`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: selectedFiles }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consumeLoadStream(res, selectedFiles)
    } catch (err) {
      console.error('POST /files/load:', err)
    } finally {
      setLoadingFiles(false)
    }
  }, [selectedFiles, consumeLoadStream])

  const clearActiveFiles = useCallback(async () => {
    await fetch(`${API}/files/active`, { method: 'DELETE' })
    setActiveFiles([])
    setSelectedFiles([])
    setSummary(null)
    setSummaryText('')
  }, [])

  const uploadFiles = useCallback(async (files: File[]) => {
    const pdfs = files.filter(f => f.name.endsWith('.pdf'))
    if (pdfs.length === 0) return
    setLoadingFiles(true)
    try {
      const form = new FormData()
      pdfs.forEach(f => form.append('files', f, f.name))
      const res = await fetch(`${API}/files/upload`, { method: 'POST', body: form })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consumeLoadStream(res)
      const [filesData, ctxData] = await Promise.all([
        fetch(`${API}/rag/files`).then(r => r.json()) as Promise<{ files: string[] }>,
        fetch(`${API}/context`).then(r => r.json()) as Promise<Record<string, unknown>>,
      ])
      setAvailableFiles(filesData.files)
      const active = (ctxData['fichiers_actifs'] as string[]) ?? []
      setActiveFiles(active)
      setSelectedFiles(active)
    } catch (err) {
      console.error('POST /files/upload:', err)
    } finally {
      setLoadingFiles(false)
    }
  }, [consumeLoadStream])

  // ── STT ───────────────────────────────────────────────────────────────────

  const stopRecording = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop()
    }
  }, [])

  const handleMicDown = useCallback(async (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      chunksRef.current = []
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : undefined
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
          const res = await fetch(`${API}/voice/transcribe`, { method: 'POST', body: form })
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          const data: { text: string } = await res.json()
          if (data.text && activeInputRef.current) activeInputRef.current(data.text)
        } catch (err) {
          console.error('Erreur transcription:', err)
        } finally {
          setTranscribing(false)
        }
      }
      recorder.start()
      recorderRef.current = recorder
      setRecording(true)
    } catch (err) {
      console.error('Accès microphone refusé:', err)
    }
  }, [activeInputRef])

  const handleMicUp = useCallback(() => stopRecording(), [stopRecording])

  const basename = (p: string) => p.split(/[/\\]/).pop() ?? p

  // ── Current step defs for pipeline panel ──────────────────────────────────

  const stepDefs = (effort !== 'direct' && effort !== 'adaptive')
    ? EFFORT_DEFINITIONS[effort] ?? []
    : []

  const relevantPresets = presets.filter(p => p.effort === effort)

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="relative border-t border-[#1e1e1e] bg-[#0a0a0a] shrink-0">

      {/* ── Pipeline config panel ── */}
      {effort !== 'direct' && effort !== 'adaptive' && stepDefs.length > 0 && (
        <div className="border-t border-[#1e1e1e] bg-[#0d0d0d] px-4 py-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-mono text-[#444] uppercase tracking-widest">
              Pipeline {EFFORT_LABELS[effort]} · {stepDefs.length} étapes
            </span>
            <div className="flex items-center gap-2">
              {/* Presets dropdown */}
              {relevantPresets.length > 0 && (
                <select
                  className="bg-[#141414] border border-[#242424] rounded px-2 py-0.5 text-[10px] font-mono text-[#666] focus:outline-none"
                  defaultValue=""
                  onChange={e => {
                    const preset = presets.find(p => p.id === e.target.value)
                    if (preset) loadPreset(preset)
                    e.target.value = ''
                  }}
                >
                  <option value="" disabled>Presets ▾</option>
                  {relevantPresets.map(p => (
                    <option key={p.id} value={p.id}>{p.nom}</option>
                  ))}
                </select>
              )}
              <button
                onClick={() => setSaveModalOpen(v => !v)}
                className="px-2 py-0.5 bg-[#141414] border border-[#242424] rounded text-[10px] font-mono text-[#555] hover:text-[#999] transition-colors"
              >
                sauvegarder preset
              </button>
            </div>
          </div>

          {/* Steps */}
          <div className="flex flex-wrap gap-2">
            {stepDefs.map((def, idx) => {
              const chosenModel = pipelineSteps[idx]?.model ?? selectedModel
              const isRecommended = def.recommended && chosenModel === def.recommended
              return (
                <div key={def.role} className="flex items-center gap-1.5 bg-[#111] border border-[#1e1e1e] rounded px-2 py-1">
                  <span className="text-[10px] font-mono text-[#444]">
                    {String(idx + 1).padStart(2, '0')}
                  </span>
                  <span className="text-[10px] font-mono text-[#666] shrink-0">{def.label}</span>
                  <select
                    value={chosenModel}
                    onChange={e => handleStepModelChange(idx, e.target.value)}
                    className="bg-[#0d0d0d] border border-[#1e1e1e] rounded px-1.5 py-0.5 text-[10px] font-mono text-[#888] focus:outline-none max-w-36"
                  >
                    {[...localModels, ...localNpuModels, ...cloudCategories.rapide, ...cloudCategories.puissant, ...cloudCategories.long_contexte]
                      .filter(m => m.disponible)
                      .map(m => (
                        <option key={m.id} value={m.id}>{m.nom}</option>
                      ))}
                  </select>
                  {isRecommended && (
                    <span className="text-[8px] font-mono text-[#4a6a4a] shrink-0">recommandé</span>
                  )}
                </div>
              )
            })}
          </div>

          {/* Save preset inline form */}
          {saveModalOpen && (
            <div className="flex items-center gap-2 pt-1">
              <input
                value={newPresetName}
                onChange={e => setNewPresetName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') savePreset() }}
                placeholder="Nom du preset..."
                className="flex-1 bg-[#141414] border border-[#242424] rounded px-2 py-1 text-[10px] font-mono text-[#e0e0e0] placeholder-[#333] focus:outline-none"
              />
              <button
                onClick={savePreset}
                disabled={!newPresetName.trim()}
                className="px-2 py-1 bg-[#141414] border border-[#2a4a2a] rounded text-[10px] font-mono text-[#5a9a5a] hover:text-[#8aca8a] disabled:opacity-30 transition-colors"
              >
                Sauvegarder
              </button>
              <button
                onClick={() => setSaveModalOpen(false)}
                className="px-2 py-1 text-[10px] font-mono text-[#444] hover:text-[#888]"
              >
                ✕
              </button>
            </div>
          )}
        </div>
      )}

      {/* ── Files panel ── */}
      {activePanel === 'files' && (
        <div className="border-t border-[#1e1e1e] bg-[#0d0d0d] px-4 py-4 max-h-72 overflow-y-auto space-y-4">
          <div
            onDragOver={e => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={e => { e.preventDefault(); setDragOver(false); uploadFiles(Array.from(e.dataTransfer.files)) }}
            onClick={() => fileInputRef.current?.click()}
            className={`border border-dashed rounded px-4 py-3 cursor-pointer transition-colors text-center ${
              dragOver ? 'border-[#4a4a4a] bg-[#141414]' : 'border-[#2a2a2a] hover:border-[#3a3a3a]'
            }`}
          >
            <span className="text-xs font-mono text-[#444]">
              {loadingFiles ? 'Chargement...' : 'Glisser un PDF ici · Cliquer pour parcourir'}
            </span>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf"
              className="hidden"
              onChange={e => { if (e.target.files) uploadFiles(Array.from(e.target.files)) }}
            />
          </div>

          {availableFiles.length > 0 && (
            <div className="space-y-1">
              <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest mb-2">Fichiers indexés</p>
              {availableFiles.map(f => (
                <label key={f} className="flex items-center gap-2 cursor-pointer group">
                  <input
                    type="checkbox"
                    checked={selectedFiles.includes(f)}
                    onChange={e =>
                      setSelectedFiles(prev =>
                        e.target.checked ? [...prev, f] : prev.filter(x => x !== f)
                      )
                    }
                    className="accent-[#555] shrink-0"
                  />
                  <span className="text-xs font-mono text-[#666] group-hover:text-[#aaa] transition-colors truncate">
                    {basename(f)}
                  </span>
                </label>
              ))}
            </div>
          )}

          <div className="flex gap-2 flex-wrap">
            <button
              onClick={loadSelectedFiles}
              disabled={selectedFiles.length === 0 || loadingFiles}
              className="px-3 py-1.5 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              {loadingFiles ? 'Chargement...' : `Charger ${selectedFiles.length > 0 ? `(${selectedFiles.length})` : ''}`}
            </button>
            {activeFiles.length > 0 && (
              <button
                onClick={clearActiveFiles}
                className="px-3 py-1.5 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#444] hover:text-[#888] transition-colors"
              >
                Vider le contexte
              </button>
            )}
          </div>

          {(summaryText || summary) && (
            <div className="border border-[#1e1e1e] rounded px-3 py-3 space-y-1">
              <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">
                {summary
                  ? `Résumé · ${summary.pages_totales} pages · ${summary.chunks_indexés} chunks`
                  : 'Génération du résumé...'}
              </p>
              <p className="text-xs font-mono text-[#888] leading-relaxed whitespace-pre-wrap">
                {summaryText || summary?.résumé}
                {!summary && summaryText && (
                  <span className="animate-pulse text-[#3a3a3a]">▍</span>
                )}
              </p>
            </div>
          )}
        </div>
      )}

      {/* ── Skills panel ── */}
      {activePanel === 'skills' && (
        <div className="border-t border-[#1e1e1e] bg-[#0d0d0d] px-4 py-4 space-y-4 max-h-96 overflow-y-auto">
          <div className="flex items-center gap-3 flex-wrap">
            <button
              onClick={onTtsToggle}
              className={`px-3 py-1.5 rounded text-xs font-mono border transition-colors ${
                ttsEnabled
                  ? 'bg-[#1a2a1a] border-[#2a4a2a] text-[#5a9a5a]'
                  : 'bg-[#141414] border-[#242424] text-[#555] hover:border-[#383838] hover:text-[#aaa]'
              }`}
            >
              {speakingText ? 'lecture...' : ttsEnabled ? '◆ lecture auto' : '◇ lecture auto'}
            </button>
            <button
              onClick={handleStrictToggle}
              className={`px-3 py-1.5 rounded text-xs font-mono border transition-colors ${
                strictMode
                  ? 'bg-[#1a1a2a] border-[#2a2a4a] text-[#7a7acc]'
                  : 'bg-[#141414] border-[#242424] text-[#555] hover:border-[#383838] hover:text-[#aaa]'
              }`}
            >
              {strictMode ? '◆ mode strict' : '◇ mode strict'}
            </button>
          </div>

          {/* Preset management */}
          {presets.length > 0 && (
            <div className="space-y-1">
              <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">Presets sauvegardés</p>
              {presets.map(p => (
                <div key={p.id} className="flex items-center gap-2">
                  <button
                    onClick={() => loadPreset(p)}
                    className="text-left flex-1 text-xs font-mono text-[#666] hover:text-[#aaa] truncate"
                  >
                    {p.nom} <span className="text-[#333]">· {EFFORT_LABELS[p.effort]} · {p.steps.length} étapes</span>
                  </button>
                  {!p.défaut && (
                    <button
                      onClick={() => deletePreset(p.id)}
                      className="text-[10px] font-mono text-[#333] hover:text-[#884444] transition-colors shrink-0"
                    >
                      ✕
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="space-y-1.5">
            <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">Préfixes @</p>
            {[
              { trigger: '@cours',      desc: 'RAG sur tous les fichiers indexés' },
              { trigger: '@strict',     desc: 'Réponse concise, sans intro' },
              { trigger: '@mémoire',    desc: 'Affiche le contexte mémoire actuel' },
              { trigger: '@historique', desc: 'Recherche dans les échanges passés [sujet]' },
            ].map(c => (
              <div key={c.trigger} className="flex gap-2 items-baseline">
                <span className="text-xs font-mono text-[#4a8a4a] shrink-0 w-24">{c.trigger}</span>
                <span className="text-[10px] font-mono text-[#444]">{c.desc}</span>
              </div>
            ))}
          </div>

          <div className="space-y-1.5">
            <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">Commandes /</p>
            {[
              { trigger: '/kholle',     desc: 'Ouvre le module Kholle' },
              { trigger: '/flashcards', desc: 'Ouvre les Flashcards' },
              { trigger: '/résumé',     desc: 'Résumé des fichiers actifs' },
              { trigger: '/modèle',     desc: 'Change le modèle actif [nom]' },
              { trigger: '/lacunes',    desc: 'Lacunes + erreurs des 7 derniers jours' },
              { trigger: '/direct',     desc: 'Envoie sans orchestrateur [message]' },
            ].map(c => (
              <div key={c.trigger} className="flex gap-2 items-baseline">
                <span className="text-xs font-mono text-[#4a6a8a] shrink-0 w-24">{c.trigger}</span>
                <span className="text-[10px] font-mono text-[#444]">{c.desc}</span>
              </div>
            ))}
          </div>

          <div className="space-y-2">
            <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">Instruction de session</p>
            <textarea
              value={instructionDraft}
              onChange={e => setInstructionDraft(e.target.value)}
              placeholder="Ex : répondre en LaTeX, ne pas utiliser de métaphores..."
              rows={2}
              className="w-full bg-[#141414] border border-[#242424] rounded px-3 py-2 text-xs text-[#e0e0e0] placeholder-[#333] resize-none focus:outline-none focus:border-[#383838] font-mono"
            />
            <button
              onClick={handleInstructionSave}
              disabled={instructionDraft === sessionInstruction}
              className="px-3 py-1.5 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              Sauvegarder
            </button>
          </div>
        </div>
      )}

      {/* ── Model panel ── */}
      {activePanel === 'model' && (() => {
        const allCloud = [...cloudCategories.rapide, ...cloudCategories.puissant, ...cloudCategories.long_contexte]
        const hasModels = localModels.length > 0 || localNpuModels.length > 0 || allCloud.length > 0
        return (
          <div className="border-t border-[#1e1e1e] bg-[#0d0d0d] px-4 py-3 max-h-[65vh] overflow-y-auto">
            {!hasModels ? (
              <p className="text-xs font-mono text-[#333]">Chargement des modèles...</p>
            ) : (
              <div className="space-y-0.5">
                {Object.keys(recommandations).length > 0 && (
                  <>
                    <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest px-3 py-1">Recommandations</p>
                    <div className="px-3 pb-2 flex flex-wrap gap-1.5">
                      {Object.entries(recommandations).map(([label, mid]) => (
                        <button
                          key={label}
                          onClick={() => handleModelSelect(mid)}
                          className={`px-2 py-0.5 rounded text-[10px] font-mono border transition-colors ${
                            selectedModel === mid
                              ? 'bg-[#1a2a1a] border-[#2a4a2a] text-[#6a9a6a]'
                              : 'bg-[#0d0d0d] border-[#1e1e1e] text-[#444] hover:border-[#2a2a2a] hover:text-[#777]'
                          }`}
                        >{label}</button>
                      ))}
                    </div>
                    <div className="border-t border-[#1a1a1a] my-1" />
                  </>
                )}

                {localModels.length > 0 && (
                  <>
                    <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest px-3 py-1">Local</p>
                    {localModels.map(m => (
                      <button
                        key={m.id}
                        onClick={() => handleModelSelect(m.id)}
                        className={`w-full text-left px-3 py-1.5 rounded text-xs font-mono transition-colors flex items-center gap-2 ${
                          m.id === selectedModel ? 'bg-[#1a1a1a] text-[#e0e0e0]' : 'text-[#555] hover:text-[#aaa] hover:bg-[#141414]'
                        }`}
                      >
                        <span className="w-1.5 h-1.5 rounded-full bg-[#3a5a3a] shrink-0" />
                        <span className="flex-1 truncate">{m.id === selectedModel ? '◆ ' : '◇ '}{m.nom}</span>
                      </button>
                    ))}
                  </>
                )}

                {localNpuModels.length > 0 && (
                  <>
                    <div className="border-t border-[#1a1a1a] my-1" />
                    <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest px-3 py-1">Local NPU</p>
                    {localNpuModels.map(m => (
                      <button
                        key={m.id}
                        onClick={() => m.disponible ? handleModelSelect(m.id) : undefined}
                        className={`w-full text-left px-3 py-1.5 rounded text-xs font-mono transition-colors flex items-center gap-2 ${
                          m.id === selectedModel ? 'bg-[#1a1a1a] text-[#e0e0e0]'
                          : m.disponible ? 'text-[#555] hover:text-[#aaa] hover:bg-[#141414]'
                          : 'text-[#333] cursor-default'
                        }`}
                      >
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${m.disponible ? 'bg-[#6a3a9a]' : 'bg-[#2a2a2a]'}`} />
                        <span className="flex-1 truncate">{m.id === selectedModel ? '◆ ' : '◇ '}{m.nom}</span>
                        <span className="text-[9px] text-[#6a3a9a] shrink-0">NPU</span>
                        {!m.disponible && <span className="text-[9px] text-[#7a4a2a] shrink-0">⚠ FLM non démarré</span>}
                      </button>
                    ))}
                  </>
                )}

                {(['rapide', 'puissant', 'long_contexte'] as const).map(cat => {
                  const catLabel = { rapide: 'RAPIDE', puissant: 'PUISSANT', long_contexte: 'LONG CONTEXTE' }[cat]
                  const models = cloudCategories[cat]
                  if (!models || models.length === 0) return null
                  return (
                    <div key={cat}>
                      <div className="border-t border-[#1a1a1a] my-1" />
                      <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest px-3 py-1">{catLabel}</p>
                      {models.map(m => (
                        <button
                          key={m.id}
                          onClick={() => m.disponible ? handleModelSelect(m.id) : undefined}
                          className={`w-full text-left px-3 py-1.5 rounded text-xs font-mono transition-colors flex items-center gap-2 ${
                            m.id === selectedModel ? 'bg-[#1a1a1a] text-[#e0e0e0]'
                            : m.disponible ? 'text-[#555] hover:text-[#aaa] hover:bg-[#141414]'
                            : 'text-[#333] cursor-default'
                          }`}
                        >
                          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${m.disponible ? (PROVIDER_DOT[m.provider] ?? 'bg-[#3a4a7a]') : 'bg-[#2a2a2a]'}`} />
                          <span className="flex-1 truncate">{m.id === selectedModel ? '◆ ' : '◇ '}{m.nom}</span>
                          <span className={`text-[9px] shrink-0 ${PROVIDER_TEXT[m.provider] ?? 'text-[#333]'}`}>{m.provider}</span>
                          {!m.disponible && <span className="text-[9px] text-[#7a4a2a] shrink-0">⚠</span>}
                        </button>
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
        {/* Files button */}
        <button
          onClick={() => togglePanel('files')}
          title="Fichiers"
          className={`relative px-3 py-2 rounded text-xs font-mono border transition-colors ${
            activePanel === 'files'
              ? 'bg-[#1a1a1a] border-[#383838] text-[#aaa]'
              : 'bg-[#0d0d0d] border-[#1e1e1e] text-[#444] hover:border-[#2a2a2a] hover:text-[#888]'
          }`}
        >
          📎
          {activeFiles.length > 0 && (
            <span className="absolute -top-1 -right-1 w-3.5 h-3.5 bg-[#3a4a3a] border border-[#2a4a2a] rounded-full text-[8px] font-mono text-[#5a9a5a] flex items-center justify-center leading-none">
              {activeFiles.length}
            </span>
          )}
        </button>

        {/* Mic button */}
        <button
          onPointerDown={handleMicDown}
          onPointerUp={handleMicUp}
          onPointerLeave={handleMicUp}
          disabled={transcribing}
          title="Micro (maintenir)"
          className={`px-3 py-2 rounded text-xs font-mono border transition-colors select-none touch-none ${
            recording
              ? 'bg-[#3a1a1a] border-[#6a2a2a] text-[#cc4444]'
              : transcribing
              ? 'bg-[#0d0d0d] border-[#1e1e1e] text-[#333] cursor-wait'
              : 'bg-[#0d0d0d] border-[#1e1e1e] text-[#444] hover:border-[#2a2a2a] hover:text-[#888]'
          }`}
        >
          {transcribing ? '…' : recording ? '●' : '🎤'}
        </button>

        {/* Skills button */}
        <button
          onClick={() => togglePanel('skills')}
          title="Paramètres de session"
          className={`px-3 py-2 rounded text-xs font-mono border transition-colors ${
            activePanel === 'skills'
              ? 'bg-[#1a1a1a] border-[#383838] text-[#aaa]'
              : strictMode || ttsEnabled || sessionInstruction
              ? 'bg-[#0d0d0d] border-[#1e1e1e] text-[#5a7a5a] hover:border-[#2a2a2a]'
              : 'bg-[#0d0d0d] border-[#1e1e1e] text-[#444] hover:border-[#2a2a2a] hover:text-[#888]'
          }`}
        >
          ⚡
        </button>

        {/* Model button */}
        <button
          onClick={() => togglePanel('model')}
          title="Modèle"
          className={`px-3 py-2 rounded text-xs font-mono border transition-colors ${
            activePanel === 'model'
              ? 'bg-[#1a1a1a] border-[#383838] text-[#aaa]'
              : 'bg-[#0d0d0d] border-[#1e1e1e] text-[#444] hover:border-[#2a2a2a] hover:text-[#888]'
          }`}
        >
          🤖
        </button>

        {/* Divider */}
        <div className="w-px h-4 bg-[#1e1e1e] mx-1" />

        {/* Effort selector */}
        {(['direct', 'low', 'medium', 'high', 'adaptive'] as EffortLevel[]).map(e => (
          <button
            key={e}
            onClick={() => handleEffortChange(e)}
            className={`px-2 py-1 rounded text-[10px] font-mono border transition-colors ${
              effort === e
                ? 'bg-[#1a1a2a] border-[#2a2a4a] text-[#8a8acc]'
                : 'bg-[#0d0d0d] border-[#1e1e1e] text-[#333] hover:border-[#2a2a2a] hover:text-[#666]'
            }`}
          >
            {EFFORT_LABELS[e]}
          </button>
        ))}

        {/* Model name indicator */}
        <span className="ml-auto text-[10px] font-mono text-[#2a2a2a] truncate max-w-24">
          {(() => {
            const all = [...localModels, ...localNpuModels, ...cloudCategories.rapide, ...cloudCategories.puissant, ...cloudCategories.long_contexte]
            const info = all.find(m => m.id === selectedModel)
            return info?.nom?.split(' ')[0] ?? selectedModel.split(':').pop() ?? selectedModel
          })()}
        </span>
      </div>
    </div>
  )
}
