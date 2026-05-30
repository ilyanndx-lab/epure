import { useState, useEffect } from 'react'
import RichMessage from './RichMessage'

const API = 'http://localhost:8000'

interface ConvSummary {
  id: string
  date: string
  titre: string
  apercu: string
  modèle: string
  n_messages: number
  modules: string[]
}

interface ConvFull {
  id: string
  date: string
  titre: string
  modèle: string
  n_messages: number
  messages: { role: string; content: string }[]
}

interface SearchResult {
  id: string
  date: string
  titre: string
  modèle: string
  extrait: string
}

export default function History() {
  const [conversations, setConversations] = useState<ConvSummary[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [selected, setSelected] = useState<ConvFull | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetch(`${API}/history`)
      .then(r => r.json())
      .then(setConversations)
      .catch(err => console.error('GET /history:', err))
  }, [])

  const search = async () => {
    const q = searchQuery.trim()
    if (!q) { setSearchResults(null); return }
    setSearching(true)
    try {
      const res = await fetch(`${API}/history/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q }),
      })
      const data = await res.json()
      setSearchResults(data.results ?? [])
    } catch (err) {
      console.error('POST /history/search:', err)
    } finally {
      setSearching(false)
    }
  }

  const openConversation = async (id: string) => {
    setLoading(true)
    try {
      const res = await fetch(`${API}/history/${id}`)
      setSelected(await res.json())
    } catch (err) {
      console.error('GET /history/{id}:', err)
    } finally {
      setLoading(false)
    }
  }

  const deleteConversation = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await fetch(`${API}/history/${id}`, { method: 'DELETE' })
      setConversations(prev => prev.filter(c => c.id !== id))
      if (searchResults) setSearchResults(prev => prev?.filter(c => c.id !== id) ?? null)
      if (selected?.id === id) setSelected(null)
    } catch (err) {
      console.error('DELETE /history/{id}:', err)
    }
  }

  // ── Reader view ──────────────────────────────────────────────────────────

  if (selected) {
    return (
      <div className="flex-1 overflow-y-auto px-6 py-6 font-mono space-y-4">
        <div className="flex items-center gap-4">
          <button
            onClick={() => setSelected(null)}
            className="text-xs text-[#444] hover:text-[#aaa] transition-colors"
          >
            ← retour
          </button>
          <div className="flex-1 min-w-0">
            <p className="text-xs text-[#ccc] truncate">{selected.titre}</p>
            <p className="text-[10px] text-[#333]">
              {selected.date} · {selected.modèle} · {selected.n_messages} messages
            </p>
          </div>
        </div>

        <div className="space-y-4">
          {selected.messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[78%] px-4 py-3 rounded text-sm ${
                m.role === 'user'
                  ? 'bg-[#1a1a1a] border border-[#282828] text-[#d8d8d8] font-mono'
                  : 'text-[#b8b8b8] font-mono'
              }`}>
                {m.role === 'user'
                  ? <p className="whitespace-pre-wrap break-words text-xs m-0">{m.content}</p>
                  : <RichMessage content={m.content} />
                }
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  // ── List view ─────────────────────────────────────────────────────────────

  const displayList: (ConvSummary | SearchResult)[] = searchResults !== null ? searchResults : conversations

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6 font-mono space-y-4">
      {/* Search bar */}
      <div className="flex gap-2">
        <input
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && search()}
          placeholder="Rechercher dans les échanges passés..."
          className="flex-1 bg-[#141414] border border-[#242424] rounded px-3 py-2 text-xs text-[#e0e0e0] placeholder-[#333] focus:outline-none focus:border-[#383838]"
        />
        <button
          onClick={search}
          disabled={searching}
          className="px-3 py-2 bg-[#1a1a1a] border border-[#2a2a2a] rounded text-xs text-[#888] hover:border-[#444] hover:text-[#ccc] disabled:opacity-30 transition-colors"
        >
          {searching ? '...' : 'Chercher'}
        </button>
        {searchResults !== null && (
          <button
            onClick={() => { setSearchResults(null); setSearchQuery('') }}
            className="px-3 py-2 text-xs text-[#444] hover:text-[#888] transition-colors"
          >
            ✕
          </button>
        )}
      </div>

      {/* Loading overlay */}
      {loading && (
        <p className="text-xs text-[#444]">Chargement...</p>
      )}

      {/* List */}
      {displayList.length === 0 ? (
        <p className="text-xs text-[#2a2a2a]">
          {searchResults !== null ? 'Aucun résultat.' : 'Aucune conversation enregistrée.'}
        </p>
      ) : (
        <div className="space-y-2">
          {displayList.map(c => {
            const isSummary = 'apercu' in c
            const isSearch = 'extrait' in c
            return (
              <div
                key={c.id}
                className="border border-[#1e1e1e] rounded px-4 py-3 hover:border-[#2a2a2a] transition-colors cursor-pointer group"
                onClick={() => openConversation(c.id)}
              >
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-[#ccc] truncate">{c.titre}</p>
                    <p className="text-[10px] text-[#444] mt-0.5">
                      {c.date} · {c.modèle}
                      {isSummary && ` · ${(c as ConvSummary).n_messages} msg`}
                    </p>
                    {isSummary && (c as ConvSummary).apercu && (
                      <p className="text-[10px] text-[#333] mt-1 truncate">
                        {(c as ConvSummary).apercu}
                      </p>
                    )}
                    {isSearch && (
                      <p className="text-[10px] text-[#333] mt-1 line-clamp-2">
                        {(c as SearchResult).extrait}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={e => deleteConversation(c.id, e)}
                    className="text-[10px] text-[#2a2a2a] hover:text-[#888] shrink-0 opacity-0 group-hover:opacity-100 transition-all"
                  >
                    ✕
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
