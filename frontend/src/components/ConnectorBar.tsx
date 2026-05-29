import { useRef, useState, useCallback, useEffect } from 'react'

const API = 'http://localhost:8000'

interface ConnectorBarProps {
  activeInputRef: React.MutableRefObject<((text: string) => void) | null>
  ttsEnabled: boolean
  onTtsToggle: () => void
  speakingText: string | null
}

type Panel = 'files' | 'skills' | 'model' | null

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
  const [availableModels, setAvailableModels] = useState<string[]>([])
  const [selectedModel, setSelectedModel] = useState('qwen2.5:7b')

  // STT state
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<BlobPart[]>([])

  const togglePanel = (panel: Panel) =>
    setActivePanel(prev => (prev === panel ? null : panel))

  // ── Load initial context and files ──────────────────────────────────────

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
      .then((d: { models: string[] }) => setAvailableModels(d.models))
      .catch(err => console.error('GET /models:', err))
  }, [])

  // ── Settings sync to backend ─────────────────────────────────────────────

  const pushSettings = useCallback(
    (patch: Record<string, unknown>) => {
      fetch(`${API}/context/settings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }).catch(err => console.error('PATCH /context/settings:', err))
    },
    []
  )

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

  // ── Files ────────────────────────────────────────────────────────────────

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
        } catch {
          // skip malformed line
        }
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
      // Refresh file list and active context
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

  // ── STT ──────────────────────────────────────────────────────────────────

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

  // ── Helpers ───────────────────────────────────────────────────────────────

  const basename = (p: string) => p.split(/[/\\]/).pop() ?? p

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="relative border-t border-[#1e1e1e] bg-[#0a0a0a] shrink-0">

      {/* ── Files panel ── */}
      {activePanel === 'files' && (
        <div className="border-t border-[#1e1e1e] bg-[#0d0d0d] px-4 py-4 max-h-72 overflow-y-auto space-y-4">
          {/* Drag & drop upload */}
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

          {/* Available files list */}
          {availableFiles.length > 0 && (
            <div className="space-y-1">
              <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest mb-2">
                Fichiers indexés
              </p>
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

          {/* Actions */}
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

          {/* Summary card — streaming or final */}
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

          {/* Toggles */}
          <div className="flex items-center gap-3">
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

          {/* @ commands */}
          <div className="space-y-1.5">
            <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">Préfixes @</p>
            {[
              { trigger: '@cours',   desc: 'RAG sur tous les fichiers indexés' },
              { trigger: '@strict',  desc: 'Réponse concise, sans intro' },
              { trigger: '@mémoire', desc: 'Affiche le contexte mémoire actuel' },
            ].map(c => (
              <div key={c.trigger} className="flex gap-2 items-baseline">
                <span className="text-xs font-mono text-[#4a8a4a] shrink-0 w-24">{c.trigger}</span>
                <span className="text-[10px] font-mono text-[#444]">{c.desc}</span>
              </div>
            ))}
          </div>

          {/* / commands */}
          <div className="space-y-1.5">
            <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">Commandes /</p>
            {[
              { trigger: '/kholle',     desc: 'Ouvre le module Kholle [matière?]' },
              { trigger: '/flashcards', desc: 'Ouvre les Flashcards [source?]' },
              { trigger: '/résumé',     desc: 'Résumé des fichiers actifs' },
              { trigger: '/modèle',     desc: 'Change le modèle actif [nom]' },
              { trigger: '/lacunes',    desc: 'Lacunes + erreurs des 7 derniers jours' },
            ].map(c => (
              <div key={c.trigger} className="flex gap-2 items-baseline">
                <span className="text-xs font-mono text-[#4a6a8a] shrink-0 w-24">{c.trigger}</span>
                <span className="text-[10px] font-mono text-[#444]">{c.desc}</span>
              </div>
            ))}
          </div>

          {/* Session instruction */}
          <div className="space-y-2">
            <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest">
              Instruction de session
            </p>
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
      {activePanel === 'model' && (
        <div className="border-t border-[#1e1e1e] bg-[#0d0d0d] px-4 py-3 space-y-1">
          {availableModels.length === 0 ? (
            <p className="text-xs font-mono text-[#333]">Chargement des modèles...</p>
          ) : (
            availableModels.map(m => (
              <button
                key={m}
                onClick={() => handleModelSelect(m)}
                className={`w-full text-left px-3 py-1.5 rounded text-xs font-mono transition-colors ${
                  m === selectedModel
                    ? 'bg-[#1a1a1a] text-[#e0e0e0]'
                    : 'text-[#555] hover:text-[#aaa] hover:bg-[#141414]'
                }`}
              >
                {m === selectedModel ? '◆ ' : '◇ '}{m}
              </button>
            ))
          )}
        </div>
      )}

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

        {/* Mic button (push-to-talk) */}
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

        {/* Model name indicator */}
        <span className="ml-1 text-[10px] font-mono text-[#2a2a2a] truncate max-w-24">
          {selectedModel.split(':')[0]}
        </span>
      </div>
    </div>
  )
}
