import { useState, useEffect, useRef, useCallback } from 'react'
import { FileSearch, Loader2, Plus, Send, Trash2 } from 'lucide-react'
import { Badge, Button, Card, Input, ProgressBar, Tabs, Toggle } from '../../components/ui'
import RichMessage from '../../components/RichMessage'
import ModuleBar from '../../components/ModuleBar'

const API = 'http://localhost:8000'

interface DocInfo {
  id: string
  titre: string
  path: string
  n_pages: number
  n_chunks: number
}

interface SearchResult {
  chunk: string
  page_approx: number
  score: number
  chunk_index: number
}

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
}

type Tab = 'search' | 'summary' | 'chat'

async function* readSSE(res: Response) {
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
      if (line.startsWith('data: ')) {
        yield JSON.parse(line.slice(6))
      }
    }
  }
}

export default function Docs() {
  const [docs, setDocs] = useState<DocInfo[]>([])
  const [selected, setSelected] = useState<DocInfo | null>(null)
  const [tab, setTab] = useState<Tab>('search')

  // Upload
  const [uploading, setUploading] = useState(false)
  const [uploadProg, setUploadProg] = useState<{ current: number; total: number } | null>(null)
  const [uploadErr, setUploadErr] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  // Search
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [results, setResults] = useState<SearchResult[]>([])
  const [synthesis, setSynthesis] = useState('')
  const [synthStreaming, setSynthStreaming] = useState(false)
  const [deepContent, setDeepContent] = useState<Record<number, string>>({})
  const [deepStreaming, setDeepStreaming] = useState<Record<number, boolean>>({})

  // Summary
  const [summaryContent, setSummaryContent] = useState('')
  const [summaryStreaming, setSummaryStreaming] = useState(false)
  const [useCloud, setUseCloud] = useState(false)

  // Chat
  const [chatMsgs, setChatMsgs] = useState<ChatMsg[]>([])
  const [chatInput, setChatInput] = useState('')
  const [chatStreaming, setChatStreaming] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)

  // ── Fetch docs list ───────────────────────────────────────────────────────

  const fetchDocs = useCallback(async () => {
    try {
      const res = await fetch(`${API}/docanalysis/docs`)
      const data = await res.json()
      setDocs(data.docs ?? [])
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => { fetchDocs() }, [fetchDocs])

  // ── Reset per-doc state on doc change ────────────────────────────────────

  useEffect(() => {
    setQuery('')
    setResults([])
    setSynthesis('')
    setSummaryContent('')
    setChatMsgs([])
    setDeepContent({})
    setDeepStreaming({})
  }, [selected?.id])

  // ── WebSocket for doc chat ────────────────────────────────────────────────

  useEffect(() => {
    if (tab !== 'chat' || !selected) return
    const ws = new WebSocket('ws://localhost:8000/ws/docchat')
    wsRef.current = ws
    ws.onmessage = evt => {
      const msg = JSON.parse(evt.data)
      if (msg.type === 'token') {
        setChatMsgs(prev => {
          const last = prev[prev.length - 1]
          if (last?.role === 'assistant' && last.streaming) {
            return [...prev.slice(0, -1), { ...last, content: last.content + msg.content }]
          }
          return prev
        })
      } else if (msg.type === 'done') {
        setChatMsgs(prev => {
          const last = prev[prev.length - 1]
          if (last?.role === 'assistant') {
            return [...prev.slice(0, -1), { ...last, streaming: false }]
          }
          return prev
        })
        setChatStreaming(false)
      } else if (msg.type === 'error') {
        setChatStreaming(false)
      }
    }
    ws.onerror = () => setChatStreaming(false)
    ws.onclose = () => setChatStreaming(false)
    return () => { ws.close(); wsRef.current = null }
  }, [tab, selected?.id])

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMsgs])

  // ── Upload ────────────────────────────────────────────────────────────────

  const handleUpload = useCallback(async (file: File) => {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setUploadErr('Seuls les fichiers PDF sont supportés')
      return
    }
    setUploading(true)
    setUploadErr(null)
    setUploadProg(null)
    const form = new FormData()
    form.append('file', file)
    try {
      const res = await fetch(`${API}/docanalysis/upload`, { method: 'POST', body: form })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      for await (const ev of readSSE(res)) {
        if (ev.type === 'progress') {
          setUploadProg({ current: ev.chunk, total: ev.total })
        } else if (ev.type === 'done') {
          setDocs(prev => prev.find(d => d.id === ev.doc.id) ? prev : [...prev, ev.doc])
          setSelected(ev.doc)
          setUploadProg(null)
        } else if (ev.type === 'error') {
          setUploadErr(ev.message)
        }
      }
    } catch {
      setUploadErr('Erreur lors du chargement')
    } finally {
      setUploading(false)
    }
  }, [])

  const handleUnload = useCallback(async (docId: string, e: React.MouseEvent) => {
    e.stopPropagation()
    await fetch(`${API}/docanalysis/docs/${docId}`, { method: 'DELETE' })
    setDocs(prev => prev.filter(d => d.id !== docId))
    if (selected?.id === docId) setSelected(null)
  }, [selected])

  // ── Search ────────────────────────────────────────────────────────────────

  const handleSearch = useCallback(async () => {
    if (!selected || !query.trim() || searching) return
    setSearching(true)
    setResults([])
    setSynthesis('')
    setSynthStreaming(false)
    try {
      const res = await fetch(`${API}/docanalysis/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: selected.id, query }),
      })
      for await (const ev of readSSE(res)) {
        if (ev.type === 'chunks') {
          setResults(ev.results)
          setSearching(false)
          setSynthStreaming(true)
        } else if (ev.type === 'token') {
          setSynthesis(prev => prev + ev.content)
        } else if (ev.type === 'done') {
          setSynthStreaming(false)
        }
      }
    } catch {
      // ignore
    } finally {
      setSearching(false)
      setSynthStreaming(false)
    }
  }, [selected, query, searching])

  const handleDeepen = useCallback(async (chunkIndex: number, chunk: string) => {
    if (!selected) return
    setDeepStreaming(prev => ({ ...prev, [chunkIndex]: true }))
    setDeepContent(prev => ({ ...prev, [chunkIndex]: '' }))
    try {
      const res = await fetch(`${API}/docanalysis/deepen`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chunks: [chunk] }),
      })
      for await (const ev of readSSE(res)) {
        if (ev.type === 'token') {
          setDeepContent(prev => ({ ...prev, [chunkIndex]: (prev[chunkIndex] ?? '') + ev.content }))
        } else if (ev.type === 'done') {
          setDeepStreaming(prev => ({ ...prev, [chunkIndex]: false }))
        }
      }
    } catch {
      setDeepStreaming(prev => ({ ...prev, [chunkIndex]: false }))
    }
  }, [selected])

  // ── Summary ───────────────────────────────────────────────────────────────

  const handleSummarize = useCallback(async (level: 'short' | 'medium' | 'full') => {
    if (!selected || summaryStreaming) return
    setSummaryContent('')
    setSummaryStreaming(true)
    try {
      const res = await fetch(`${API}/docanalysis/summarize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: selected.id, level, use_cloud: useCloud }),
      })
      for await (const ev of readSSE(res)) {
        if (ev.type === 'token') {
          setSummaryContent(prev => prev + ev.content)
        } else if (ev.type === 'done') {
          setSummaryStreaming(false)
        }
      }
    } catch {
      setSummaryStreaming(false)
    }
  }, [selected, summaryStreaming, useCloud])

  // ── Chat ──────────────────────────────────────────────────────────────────

  const handleChatSend = useCallback(() => {
    if (!chatInput.trim() || chatStreaming || !selected) return
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    const userMsg = chatInput.trim()
    setChatInput('')
    setChatMsgs(prev => [
      ...prev,
      { role: 'user', content: userMsg },
      { role: 'assistant', content: '', streaming: true },
    ])
    setChatStreaming(true)
    ws.send(JSON.stringify({ doc_id: selected.id, content: userMsg }))
  }, [chatInput, chatStreaming, selected])

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
    <div className="flex flex-1 overflow-hidden min-h-0">

      {/* LEFT — Bibliothèque */}
      <div className="w-60 shrink-0 border-r border-line flex flex-col bg-surface">
        <div className="px-4 py-3 border-b border-line flex items-center justify-between">
          <span className="text-xs text-muted uppercase tracking-wide">Documents</span>
          <Button
            variant="ghost"
            size="sm"
            icon={<Plus size={13} />}
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
          >
            charger
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.docx,.txt,.md,.csv,.json,.png,.jpg,.jpeg,.webp"
            className="hidden"
            onChange={e => {
              const f = e.target.files?.[0]
              if (f) handleUpload(f)
              e.target.value = ''
            }}
          />
        </div>

        {/* Drag-drop zone */}
        <div
          className={`mx-3 my-2 rounded-md border border-dashed text-center text-xs py-3 cursor-pointer transition-colors duration-150 ${
            dragOver ? 'border-accent/50 bg-accent/5 text-secondary' : 'border-line text-muted'
          } ${uploading ? 'opacity-50 cursor-not-allowed' : 'hover:border-accent/30 hover:text-secondary'}`}
          onDragOver={e => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={e => {
            e.preventDefault()
            setDragOver(false)
            const f = e.dataTransfer.files[0]
            if (f && !uploading) handleUpload(f)
          }}
          onClick={() => !uploading && fileRef.current?.click()}
        >
          {uploading ? (
            uploadProg ? (
              <div className="space-y-1.5 px-2">
                <div className="font-mono">indexation... {uploadProg.current}/{uploadProg.total}</div>
                <ProgressBar value={(uploadProg.current / uploadProg.total) * 100} />
              </div>
            ) : (
              <span>chargement...</span>
            )
          ) : (
            'glisser-déposer un fichier'
          )}
        </div>

        {uploadErr && (
          <div className="mx-3 mb-2 text-xs text-error px-2 py-1.5 bg-error/10 rounded-sm border border-error/30">
            {uploadErr}
          </div>
        )}

        {/* Doc list */}
        <div className="flex-1 overflow-y-auto px-2 py-1 space-y-0.5">
          {docs.length === 0 && !uploading && (
            <div className="flex flex-col items-center gap-2 py-8 text-muted select-none">
              <FileSearch size={15} />
              <span className="text-xs">aucun document</span>
            </div>
          )}
          {docs.map(doc => (
            <div
              key={doc.id}
              onClick={() => setSelected(doc)}
              className={`relative px-3 py-2.5 rounded-sm cursor-pointer group flex items-start justify-between gap-1 transition-colors duration-150 ${
                selected?.id === doc.id
                  ? 'bg-accent/10 text-primary'
                  : 'text-secondary hover:text-primary hover:bg-elevated'
              }`}
            >
              {selected?.id === doc.id && (
                <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-accent" />
              )}
              <div className="flex-1 min-w-0">
                <div className="text-xs truncate leading-tight">{doc.titre}</div>
                <div className="text-xs font-mono text-muted mt-0.5">
                  {doc.n_pages}p · {doc.n_chunks} chunks
                </div>
              </div>
              <button
                onClick={e => handleUnload(doc.id, e)}
                title="Décharger"
                className="opacity-0 group-hover:opacity-100 text-muted hover:text-error shrink-0 mt-0.5 transition-all duration-150"
              >
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* RIGHT — Zone de travail */}
      <div className={`flex flex-col flex-1 overflow-hidden`}>
        {!selected ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="flex flex-col items-center gap-2 text-muted select-none">
              <FileSearch size={20} />
              <div className="text-sm">Chargez un document pour commencer</div>
            </div>
          </div>
        ) : (
          <>
            {/* Header */}
            <div className="px-6 py-3 border-b border-line shrink-0 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-primary truncate">{selected.titre}</div>
                <div className="text-xs font-mono text-muted mt-0.5">
                  {selected.n_pages} pages · {selected.n_chunks} chunks indexés
                </div>
              </div>
            </div>

            {/* Tabs */}
            <div className="px-4 shrink-0">
              <Tabs
                tabs={[
                  { id: 'search', label: 'Recherche' },
                  { id: 'summary', label: 'Résumé' },
                  { id: 'chat', label: 'Chat' },
                ]}
                active={tab}
                onChange={id => setTab(id as Tab)}
              />
            </div>

            {/* Tab content */}
            {tab === 'chat' ? (
              <div className="flex flex-col flex-1 overflow-hidden">
                <div className="flex-1 overflow-y-auto p-4 space-y-4">
                  {chatMsgs.length === 0 && (
                    <div className="flex flex-col items-center gap-2 text-muted text-sm py-8 select-none">
                      <Send size={15} />
                      Posez une question sur le document
                    </div>
                  )}
                  {chatMsgs.map((m, i) => (
                    <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                      <div className={`max-w-[80%] ${
                        m.role === 'user'
                          ? 'bg-elevated border border-line rounded-lg px-3 py-2 text-sm text-primary'
                          : 'text-secondary'
                      }`}>
                        {m.role === 'user'
                          ? m.content
                          : <RichMessage content={m.content} streaming={m.streaming} />
                        }
                      </div>
                    </div>
                  ))}
                  <div ref={chatEndRef} />
                </div>
                <div className="px-4 pb-4 pt-2 border-t border-line shrink-0">
                  <div className="flex gap-2">
                    <Input
                      value={chatInput}
                      onChange={e => setChatInput(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && !e.shiftKey && handleChatSend()}
                      placeholder="Question sur le document..."
                      disabled={chatStreaming}
                      className="flex-1 text-xs disabled:opacity-50"
                    />
                    <button
                      onClick={handleChatSend}
                      disabled={chatStreaming || !chatInput.trim()}
                      title="Envoyer"
                      className="p-2 rounded-md bg-gradient-primary text-on-accent shadow-sm hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 shrink-0"
                    >
                      {chatStreaming ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <div className="flex-1 overflow-y-auto">

                {/* Search tab */}
                {tab === 'search' && (
                  <div className="p-6 space-y-4">
                    <div className="flex gap-2">
                      <Input
                        value={query}
                        onChange={e => setQuery(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && handleSearch()}
                        placeholder="Que cherchez-vous ?"
                        className="flex-1 text-xs"
                      />
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={handleSearch}
                        disabled={searching || !query.trim()}
                      >
                        {searching ? '...' : 'chercher'}
                      </Button>
                    </div>

                    {results.length > 0 && (
                      <div className="space-y-3">
                        {results.map((r, i) => (
                          <Card key={i} accent="secondary" padded={false}>
                            <div className="px-4 py-2 flex items-center justify-between border-b border-line">
                              <div className="flex items-center gap-2">
                                <Badge variant="neutral" mono>p.{r.page_approx}</Badge>
                                <Badge variant="secondary" mono>{(r.score * 100).toFixed(0)}%</Badge>
                              </div>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => handleDeepen(r.chunk_index, r.chunk)}
                                disabled={deepStreaming[r.chunk_index]}
                              >
                                {deepStreaming[r.chunk_index] ? '...' : 'approfondir'}
                              </Button>
                            </div>
                            <pre className="px-4 py-3 text-xs font-mono text-secondary leading-relaxed whitespace-pre-wrap break-words">
                              {r.chunk}
                            </pre>
                            {(deepContent[r.chunk_index] || deepStreaming[r.chunk_index]) && (
                              <div className="border-t border-line px-4 py-3 bg-elevated/50">
                                <RichMessage
                                  content={deepContent[r.chunk_index] ?? ''}
                                  streaming={deepStreaming[r.chunk_index]}
                                />
                              </div>
                            )}
                          </Card>
                        ))}
                      </div>
                    )}

                    {(synthesis || synthStreaming) && (
                      <Card accent="secondary">
                        <div className="text-xs text-muted mb-2 uppercase tracking-wide">
                          synthèse
                        </div>
                        <RichMessage content={synthesis} streaming={synthStreaming} />
                      </Card>
                    )}
                  </div>
                )}

                {/* Summary tab */}
                {tab === 'summary' && (
                  <div className="p-6 space-y-4">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Button variant="secondary" size="sm" onClick={() => handleSummarize('short')} disabled={summaryStreaming}>
                        court (200 mots)
                      </Button>
                      <Button variant="secondary" size="sm" onClick={() => handleSummarize('medium')} disabled={summaryStreaming}>
                        structuré
                      </Button>
                      <Button variant="secondary" size="sm" onClick={() => handleSummarize('full')} disabled={summaryStreaming}>
                        complet
                      </Button>
                      <label className="flex items-center gap-2 ml-auto cursor-pointer select-none">
                        <span className="text-xs text-muted">cloud</span>
                        <Toggle checked={useCloud} onChange={setUseCloud} label="Résumé via cloud" />
                      </label>
                    </div>

                    <div className="text-xs text-muted bg-elevated border border-line rounded-sm px-3 py-2">
                      résumé complet : peut prendre plusieurs minutes sur un document long
                    </div>

                    {(summaryContent || summaryStreaming) && (
                      <Card>
                        <RichMessage content={summaryContent} streaming={summaryStreaming} />
                      </Card>
                    )}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
    <ModuleBar module="docs" showFile showModel />
    </div>
  )
}
