import { useState, useEffect } from 'react'
import { usePersistentState } from '../../usePersistentState'
import { ArrowLeft, Clock, Search, Trash2, X } from 'lucide-react'
import { Badge, Button, Card, Input } from '../../components/ui'
import RichMessage from '../../components/RichMessage'
import { API, apiFetch } from '../../api'

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
  const [searchQuery, setSearchQuery] = usePersistentState<string>('epure.history.searchQuery', '')
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [selected, setSelected] = useState<ConvFull | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    apiFetch(`${API}/history`)
      .then(r => r.json())
      .then(setConversations)
      .catch(err => console.error('GET /history:', err))
  }, [])

  const search = async () => {
    const q = searchQuery.trim()
    if (!q) { setSearchResults(null); return }
    setSearching(true)
    try {
      const res = await apiFetch(`${API}/history/search`, {
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
      const res = await apiFetch(`${API}/history/${id}`)
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
      await apiFetch(`${API}/history/${id}`, { method: 'DELETE' })
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
      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-4">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="sm" icon={<ArrowLeft size={14} />} onClick={() => setSelected(null)}>
            retour
          </Button>
          <div className="flex-1 min-w-0">
            <p className="text-sm text-primary truncate">{selected.titre}</p>
            <p className="text-xs font-mono text-muted">
              {selected.date} · {selected.modèle} · {selected.n_messages} messages
            </p>
          </div>
        </div>

        <div className="space-y-4">
          {selected.messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[78%] ${
                m.role === 'user'
                  ? 'px-4 py-3 rounded-lg bg-elevated border border-line text-primary'
                  : 'text-secondary'
              }`}>
                {m.role === 'user'
                  ? <p className="whitespace-pre-wrap break-words text-sm m-0">{m.content}</p>
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
    <div className="flex-1 overflow-y-auto px-6 py-6 space-y-4">
      {/* Search bar */}
      <div className="flex gap-2">
        <Input
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && search()}
          placeholder="Rechercher dans les échanges passés..."
          className="flex-1 text-xs"
        />
        <Button variant="secondary" size="sm" icon={<Search size={13} />} onClick={search} disabled={searching}>
          {searching ? '...' : 'Chercher'}
        </Button>
        {searchResults !== null && (
          <Button
            variant="ghost" size="sm" icon={<X size={13} />}
            onClick={() => { setSearchResults(null); setSearchQuery('') }}
            aria-label="Effacer la recherche"
          />
        )}
      </div>

      {/* Loading overlay */}
      {loading && (
        <p className="text-xs text-muted">Chargement...</p>
      )}

      {/* List */}
      {displayList.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 py-16 text-muted select-none">
          <Clock size={16} />
          <span className="text-sm">
            {searchResults !== null ? 'Aucun résultat.' : 'Aucune conversation enregistrée.'}
          </span>
        </div>
      ) : (
        <div className="space-y-2">
          {displayList.map(c => {
            const isSummary = 'apercu' in c
            const isSearch = 'extrait' in c
            return (
              <Card
                key={c.id}
                accent={isSearch ? 'secondary' : 'none'}
                className="cursor-pointer group hover:border-accent/30 transition-colors duration-150"
                onClick={() => openConversation(c.id)}
              >
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-primary truncate">{c.titre}</p>
                    <p className="text-xs font-mono text-muted mt-0.5 flex items-center gap-2">
                      {c.date}
                      <Badge variant="secondary" mono>{c.modèle.split(':').pop()}</Badge>
                      {isSummary && <span>{(c as ConvSummary).n_messages} msg</span>}
                    </p>
                    {isSummary && (c as ConvSummary).apercu && (
                      <p className="text-xs text-muted mt-1 truncate">
                        {(c as ConvSummary).apercu}
                      </p>
                    )}
                    {isSearch && (
                      <p className="text-xs text-secondary mt-1 line-clamp-2">
                        {(c as SearchResult).extrait}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={e => deleteConversation(c.id, e)}
                    title="Supprimer"
                    className="p-1 rounded-sm text-muted hover:text-error shrink-0 opacity-0 group-hover:opacity-100 transition-all duration-150"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}
