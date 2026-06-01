import { useState, useEffect, useRef, useCallback } from 'react'
import RichMessage from './RichMessage'

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
    <div className="flex flex-1 overflow-hidden">

      {/* LEFT — Bibliothèque */}
      <div className="w-60 shrink-0 border-r border-[#1e1e1e] flex flex-col bg-[#0d0d0d]">
        <div className="px-4 py-3 border-b border-[#1e1e1e] flex items-center justify-between">
          <span className="text-[10px] font-mono text-[#333] uppercase tracking-[0.2em]">Documents</span>
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="text-[10px] font-mono text-[#555] hover:text-[#aaa] px-2 py-0.5 rounded hover:bg-[#1a1a1a] disabled:opacity-40 transition-colors"
          >
            + charger
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf"
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
          className={`mx-3 my-2 rounded border-2 border-dashed text-center text-[10px] font-mono py-3 cursor-pointer transition-colors ${
            dragOver ? 'border-[#2a2a2a] text-[#666]' : 'border-[#191919] text-[#2a2a2a]'
          } ${uploading ? 'opacity-50 cursor-not-allowed' : 'hover:border-[#252525] hover:text-[#444]'}`}
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
                <div>indexation... {uploadProg.current}/{uploadProg.total}</div>
                <div className="h-0.5 bg-[#181818] rounded overflow-hidden">
                  <div
                    className="h-full bg-[#2a4a2a] transition-all duration-300"
                    style={{ width: `${(uploadProg.current / uploadProg.total) * 100}%` }}
                  />
                </div>
              </div>
            ) : (
              <span>chargement...</span>
            )
          ) : (
            'glisser-déposer PDF'
          )}
        </div>

        {uploadErr && (
          <div className="mx-3 mb-2 text-[10px] font-mono text-[#7a3a3a] px-2 py-1.5 bg-[#120808] rounded border border-[#2a1010]">
            {uploadErr}
          </div>
        )}

        {/* Doc list */}
        <div className="flex-1 overflow-y-auto px-2 py-1 space-y-0.5">
          {docs.length === 0 && !uploading && (
            <div className="text-center py-8 text-[10px] font-mono text-[#282828]">
              aucun document
            </div>
          )}
          {docs.map(doc => (
            <div
              key={doc.id}
              onClick={() => setSelected(doc)}
              className={`px-3 py-2.5 rounded cursor-pointer group flex items-start justify-between gap-1 transition-colors ${
                selected?.id === doc.id
                  ? 'bg-[#1a1a1a] text-[#e0e0e0]'
                  : 'text-[#555] hover:text-[#aaa] hover:bg-[#111]'
              }`}
            >
              <div className="flex-1 min-w-0">
                <div className="text-[11px] font-mono truncate leading-tight">{doc.titre}</div>
                <div className="text-[9px] font-mono text-[#2a2a2a] mt-0.5">
                  {doc.n_pages}p · {doc.n_chunks} chunks
                </div>
              </div>
              <button
                onClick={e => handleUnload(doc.id, e)}
                className="opacity-0 group-hover:opacity-100 text-[#333] hover:text-[#777] text-[10px] font-mono shrink-0 mt-0.5 transition-all"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* RIGHT — Zone de travail */}
      <div className={`flex flex-col flex-1 overflow-hidden`}>
        {!selected ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center font-mono text-[#222]">
              <div className="text-2xl mb-2">◻</div>
              <div className="text-xs">Chargez un document pour commencer</div>
            </div>
          </div>
        ) : (
          <>
            {/* Header */}
            <div className="px-6 py-3 border-b border-[#1e1e1e] shrink-0">
              <div className="text-sm font-mono text-[#bbb] truncate">{selected.titre}</div>
              <div className="text-[10px] font-mono text-[#2a2a2a] mt-0.5">
                {selected.n_pages} pages · {selected.n_chunks} chunks indexés
              </div>
            </div>

            {/* Tabs */}
            <div className="flex border-b border-[#1e1e1e] px-4 shrink-0">
              {(['search', 'summary', 'chat'] as const).map(t => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-4 py-2.5 text-[11px] font-mono border-b-2 transition-colors ${
                    tab === t
                      ? 'border-[#333] text-[#bbb]'
                      : 'border-transparent text-[#3a3a3a] hover:text-[#666]'
                  }`}
                >
                  {t === 'search' ? 'recherche' : t === 'summary' ? 'résumé' : 'chat'}
                </button>
              ))}
            </div>

            {/* Tab content */}
            {tab === 'chat' ? (
              <div className="flex flex-col flex-1 overflow-hidden">
                <div className="flex-1 overflow-y-auto p-4 space-y-4">
                  {chatMsgs.length === 0 && (
                    <div className="text-center text-[#252525] font-mono text-xs py-8">
                      Posez une question sur le document
                    </div>
                  )}
                  {chatMsgs.map((m, i) => (
                    <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                      <div className={`max-w-[80%] ${
                        m.role === 'user'
                          ? 'bg-[#111] border border-[#1e1e1e] rounded px-3 py-2 text-xs font-mono text-[#ccc]'
                          : 'text-[#b8b8b8]'
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
                <div className="px-4 pb-4 pt-2 border-t border-[#1e1e1e] shrink-0">
                  <div className="flex gap-2">
                    <input
                      value={chatInput}
                      onChange={e => setChatInput(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && !e.shiftKey && handleChatSend()}
                      placeholder="Question sur le document..."
                      disabled={chatStreaming}
                      className="flex-1 bg-[#0f0f0f] border border-[#1e1e1e] rounded px-3 py-2 text-xs font-mono text-[#ccc] placeholder-[#282828] focus:outline-none focus:border-[#252525] disabled:opacity-50"
                    />
                    <button
                      onClick={handleChatSend}
                      disabled={chatStreaming || !chatInput.trim()}
                      className="px-4 py-2 text-xs font-mono bg-[#141414] text-[#777] border border-[#222] rounded hover:text-[#ccc] disabled:opacity-40 transition-colors"
                    >
                      {chatStreaming ? '...' : 'envoyer'}
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
                      <input
                        value={query}
                        onChange={e => setQuery(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && handleSearch()}
                        placeholder="Que cherchez-vous ?"
                        className="flex-1 bg-[#0f0f0f] border border-[#1e1e1e] rounded px-3 py-2 text-xs font-mono text-[#ccc] placeholder-[#282828] focus:outline-none focus:border-[#252525]"
                      />
                      <button
                        onClick={handleSearch}
                        disabled={searching || !query.trim()}
                        className="px-4 py-2 text-xs font-mono bg-[#141414] text-[#777] border border-[#222] rounded hover:text-[#ccc] disabled:opacity-40 transition-colors"
                      >
                        {searching ? '...' : 'chercher'}
                      </button>
                    </div>

                    {results.length > 0 && (
                      <div className="space-y-3">
                        {results.map((r, i) => (
                          <div key={i} className="border border-[#1a1a1a] rounded bg-[#090909]">
                            <div className="px-4 py-2 flex items-center justify-between border-b border-[#141414]">
                              <div className="flex items-center gap-3">
                                <span className="text-[9px] font-mono text-[#2a2a2a]">p.{r.page_approx}</span>
                                <span className="text-[9px] font-mono text-[#222]">
                                  {(r.score * 100).toFixed(0)}%
                                </span>
                              </div>
                              <button
                                onClick={() => handleDeepen(r.chunk_index, r.chunk)}
                                disabled={deepStreaming[r.chunk_index]}
                                className="text-[10px] font-mono text-[#333] hover:text-[#777] disabled:opacity-40 transition-colors"
                              >
                                {deepStreaming[r.chunk_index] ? '...' : 'approfondir →'}
                              </button>
                            </div>
                            <pre className="px-4 py-3 text-[11px] font-mono text-[#555] leading-relaxed whitespace-pre-wrap break-words">
                              {r.chunk}
                            </pre>
                            {(deepContent[r.chunk_index] || deepStreaming[r.chunk_index]) && (
                              <div className="border-t border-[#141414] px-4 py-3 bg-[#0d0d0d]">
                                <RichMessage
                                  content={deepContent[r.chunk_index] ?? ''}
                                  streaming={deepStreaming[r.chunk_index]}
                                />
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}

                    {(synthesis || synthStreaming) && (
                      <div className="border border-[#1a1a1a] rounded bg-[#090909] px-4 py-3">
                        <div className="text-[9px] font-mono text-[#2a2a2a] mb-2 uppercase tracking-[0.15em]">
                          synthèse
                        </div>
                        <RichMessage content={synthesis} streaming={synthStreaming} />
                      </div>
                    )}
                  </div>
                )}

                {/* Summary tab */}
                {tab === 'summary' && (
                  <div className="p-6 space-y-4">
                    <div className="flex items-center gap-2 flex-wrap">
                      <button
                        onClick={() => handleSummarize('short')}
                        disabled={summaryStreaming}
                        className="px-3 py-1.5 text-xs font-mono bg-[#141414] text-[#666] border border-[#1e1e1e] rounded hover:text-[#bbb] disabled:opacity-40 transition-colors"
                      >
                        court (200 mots)
                      </button>
                      <button
                        onClick={() => handleSummarize('medium')}
                        disabled={summaryStreaming}
                        className="px-3 py-1.5 text-xs font-mono bg-[#141414] text-[#666] border border-[#1e1e1e] rounded hover:text-[#bbb] disabled:opacity-40 transition-colors"
                      >
                        structuré
                      </button>
                      <button
                        onClick={() => handleSummarize('full')}
                        disabled={summaryStreaming}
                        className="px-3 py-1.5 text-xs font-mono bg-[#141414] text-[#666] border border-[#1e1e1e] rounded hover:text-[#bbb] disabled:opacity-40 transition-colors"
                      >
                        complet
                      </button>
                      <label className="flex items-center gap-2 ml-auto cursor-pointer select-none">
                        <span className="text-[9px] font-mono text-[#2a2a2a]">cloud</span>
                        <div
                          onClick={() => setUseCloud(v => !v)}
                          className={`w-7 h-3.5 rounded-full relative cursor-pointer transition-colors ${
                            useCloud ? 'bg-[#1a3a2a]' : 'bg-[#181818]'
                          }`}
                        >
                          <div
                            className={`absolute top-0.5 w-2.5 h-2.5 rounded-full bg-[#444] transition-all ${
                              useCloud ? 'left-3.5' : 'left-0.5'
                            }`}
                          />
                        </div>
                      </label>
                    </div>

                    <div className="text-[10px] font-mono text-[#2a2a2a] bg-[#0d0d0d] border border-[#181818] rounded px-3 py-2">
                      résumé complet : peut prendre plusieurs minutes sur un document long
                    </div>

                    {(summaryContent || summaryStreaming) && (
                      <div className="border border-[#1a1a1a] rounded bg-[#090909] px-4 py-4">
                        <RichMessage content={summaryContent} streaming={summaryStreaming} />
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
