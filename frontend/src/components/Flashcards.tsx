import { useState, useEffect, useCallback, useRef } from 'react'

const API = 'http://localhost:8000'

// ── Types ──────────────────────────────────────────────────────────────────

interface Deck {
  id: string
  nom: string
  source: string
  créé_le: string
  n_cartes: number
  n_dues: number
}

interface Carte {
  id: string
  question: string
  réponse: string
  niveau: number
  dernière_révision: string | null
  prochaine_révision: string | null
}

interface DeckFull extends Omit<Deck, 'n_cartes' | 'n_dues'> {
  cartes: Carte[]
}

type View = 'list' | 'generate' | 'review'

const basename = (p: string) => p.split(/[/\\]/).pop() ?? p

// ── Flip Card component ────────────────────────────────────────────────────

function FlipCard({
  question,
  réponse,
  flipped,
}: {
  question: string
  réponse: string
  flipped: boolean
}) {
  return (
    <div style={{ perspective: '1200px' }} className="w-full">
      <div
        style={{
          transformStyle: 'preserve-3d',
          transition: 'transform 0.55s ease',
          transform: flipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
          position: 'relative',
          height: '220px',
        }}
      >
        {/* Front — question */}
        <div
          style={{ backfaceVisibility: 'hidden' }}
          className="absolute inset-0 border border-[#1e1e1e] rounded-lg flex items-center justify-center px-8 bg-[#0d0d0d]"
        >
          <p className="text-base font-mono text-[#e0e0e0] text-center leading-relaxed">
            {question}
          </p>
        </div>
        {/* Back — réponse */}
        <div
          style={{
            backfaceVisibility: 'hidden',
            transform: 'rotateY(180deg)',
          }}
          className="absolute inset-0 border border-[#2a3a2a] rounded-lg flex items-center justify-center px-8 bg-[#0a120a] overflow-y-auto"
        >
          <p className="text-sm font-mono text-[#b8d8b8] text-center leading-relaxed whitespace-pre-wrap">
            {réponse}
          </p>
        </div>
      </div>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────

export default function Flashcards() {
  const [view, setView] = useState<View>('list')

  // ── List state ──────────────────────────────────────────────────────────
  const [decks, setDecks] = useState<Deck[]>([])
  const [loadingDecks, setLoadingDecks] = useState(false)

  // ── Generate state ──────────────────────────────────────────────────────
  const [availableFiles, setAvailableFiles] = useState<string[]>([])
  const [genSource, setGenSource] = useState('')
  const [genNom, setGenNom] = useState('')
  const [genN, setGenN] = useState(20)
  const [genAuto, setGenAuto] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [genText, setGenText] = useState('')
  const genTextRef = useRef('')
  const [genError, setGenError] = useState<string | null>(null)

  // ── Review state ────────────────────────────────────────────────────────
  const [reviewDeckId, setReviewDeckId] = useState('')
  const [reviewCards, setReviewCards] = useState<Carte[]>([])
  const [reviewIndex, setReviewIndex] = useState(0)
  const [flipped, setFlipped] = useState(false)
  const [sessionSu, setSessionSu] = useState(0)
  const [sessionPasSu, setSessionPasSu] = useState(0)
  const [reviewDone, setReviewDone] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  // ── Fetch decks ─────────────────────────────────────────────────────────

  const fetchDecks = useCallback(async () => {
    setLoadingDecks(true)
    try {
      const res = await fetch(`${API}/flashcards/decks`)
      const d: { decks: Deck[] } = await res.json()
      setDecks(d.decks)
    } catch (err) {
      console.error('GET /flashcards/decks:', err)
    } finally {
      setLoadingDecks(false)
    }
  }, [])

  useEffect(() => {
    fetchDecks()
    fetch(`${API}/rag/files`)
      .then(r => r.json())
      .then((d: { files: string[] }) => setAvailableFiles(d.files))
      .catch(err => console.error('GET /rag/files:', err))
  }, [fetchDecks])

  // ── Generate ─────────────────────────────────────────────────────────────

  const startGenerate = useCallback(async () => {
    if (!genSource || !genNom.trim()) return
    setGenerating(true)
    setGenText('')
    setGenError(null)
    genTextRef.current = ''

    try {
      const res = await fetch(`${API}/flashcards/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: genSource,
          nom: genNom.trim(),
          n_cartes: genAuto ? null : genN,
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)

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
              genTextRef.current += ev.content
              setGenText(genTextRef.current)
            } else if (ev.type === 'done') {
              await fetchDecks()
              setView('list')
            } else if (ev.type === 'error') {
              setGenError(ev.content)
            }
          } catch {
            // skip malformed
          }
        }
      }
    } catch (err) {
      console.error('POST /flashcards/generate:', err)
      setGenError(String(err))
    } finally {
      setGenerating(false)
    }
  }, [genSource, genNom, genN, genAuto, fetchDecks])

  // ── Start review ─────────────────────────────────────────────────────────

  const startReview = useCallback(async (deckId: string, dueOnly: boolean) => {
    try {
      const res = await fetch(`${API}/flashcards/decks/${deckId}`)
      const deck: DeckFull = await res.json()
      const today = new Date().toISOString().slice(0, 10)
      const cards = dueOnly
        ? deck.cartes.filter(c => (c.prochaine_révision ?? '0000-00-00') <= today)
        : deck.cartes
      if (cards.length === 0) return
      setReviewDeckId(deckId)
      setReviewCards(cards)
      setReviewIndex(0)
      setFlipped(false)
      setSessionSu(0)
      setSessionPasSu(0)
      setReviewDone(false)
      setView('review')
    } catch (err) {
      console.error('GET /flashcards/decks/:id:', err)
    }
  }, [])

  // ── Review action ─────────────────────────────────────────────────────────

  const submitReview = useCallback(async (resultat: 'su' | 'pas_su') => {
    const card = reviewCards[reviewIndex]
    if (!card || submitting) return
    setSubmitting(true)
    try {
      await fetch(
        `${API}/flashcards/decks/${reviewDeckId}/cartes/${card.id}/review`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resultat }),
        }
      )
      if (resultat === 'su') setSessionSu(p => p + 1)
      else setSessionPasSu(p => p + 1)

      const next = reviewIndex + 1
      if (next >= reviewCards.length) {
        setReviewDone(true)
        // Save session to memory
        const su = resultat === 'su' ? sessionSu + 1 : sessionSu
        const pas = resultat === 'pas_su' ? sessionPasSu + 1 : sessionPasSu
        const deck = decks.find(d => d.id === reviewDeckId)
        fetch(`${API}/memory/sessions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            matiere: 'flashcards',
            fichier: deck?.source ?? '',
            erreurs: [],
            reussies: su,
            ratees: pas,
          }),
        }).catch(err => console.error('POST /memory/sessions:', err))
      } else {
        setReviewIndex(next)
        setFlipped(false)
      }
    } catch (err) {
      console.error('POST review:', err)
    } finally {
      setSubmitting(false)
    }
  }, [reviewCards, reviewIndex, reviewDeckId, submitting, sessionSu, sessionPasSu, decks])

  // ── Delete deck ───────────────────────────────────────────────────────────

  const deleteDeck = useCallback(async (id: string) => {
    try {
      await fetch(`${API}/flashcards/decks/${id}`, { method: 'DELETE' })
      setDecks(prev => prev.filter(d => d.id !== id))
    } catch (err) {
      console.error('DELETE /flashcards/decks/:id:', err)
    }
  }, [])

  // ── Renders ───────────────────────────────────────────────────────────────

  // LIST VIEW
  if (view === 'list') {
    return (
      <main className="flex flex-col flex-1 overflow-hidden">
        <div className="flex-1 overflow-y-auto px-8 py-8">
          <div className="max-w-xl space-y-6">
            <div className="flex items-center justify-between">
              <p className="text-xs font-mono text-[#444] uppercase tracking-widest">
                Flashcards
              </p>
              <button
                onClick={() => { setGenText(''); setGenError(null); setView('generate') }}
                className="px-3 py-1.5 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] transition-colors"
              >
                + Nouveau deck
              </button>
            </div>

            {loadingDecks && (
              <p className="text-xs font-mono text-[#2a2a2a]">Chargement...</p>
            )}

            {!loadingDecks && decks.length === 0 && (
              <p className="text-xs font-mono text-[#2a2a2a]">
                Aucun deck. Créez-en un depuis une fiche PDF.
              </p>
            )}

            {decks.map(deck => (
              <div key={deck.id} className="border border-[#1e1e1e] rounded-lg px-5 py-4 space-y-3">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <p className="text-sm font-mono text-[#e0e0e0] truncate">{deck.nom}</p>
                    <p className="text-[10px] font-mono text-[#2a2a2a] truncate mt-0.5">
                      {basename(deck.source)}
                    </p>
                  </div>
                  <button
                    onClick={() => deleteDeck(deck.id)}
                    className="text-xs font-mono text-[#2a2a2a] hover:text-[#6a3a3a] transition-colors shrink-0"
                    title="Supprimer"
                  >
                    ✕
                  </button>
                </div>

                <div className="flex items-center gap-4">
                  <span className="text-xs font-mono text-[#444]">{deck.n_cartes} cartes</span>
                  {deck.n_dues > 0 && (
                    <span className="text-xs font-mono text-[#5a9a5a]">
                      {deck.n_dues} dues
                    </span>
                  )}
                </div>

                <div className="flex gap-2">
                  {deck.n_dues > 0 && (
                    <button
                      onClick={() => startReview(deck.id, true)}
                      className="px-3 py-1.5 bg-[#1a2a1a] border border-[#2a4a2a] rounded text-xs font-mono text-[#5a9a5a] hover:border-[#3a6a3a] hover:text-[#7aba7a] transition-colors"
                    >
                      Réviser ({deck.n_dues})
                    </button>
                  )}
                  <button
                    onClick={() => startReview(deck.id, false)}
                    className="px-3 py-1.5 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#666] hover:border-[#383838] hover:text-[#aaa] transition-colors"
                  >
                    Tout réviser
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </main>
    )
  }

  // GENERATE VIEW
  if (view === 'generate') {
    const canGenerate = genSource && genNom.trim() && !generating

    return (
      <main className="flex flex-col flex-1 overflow-hidden">
        <div className="flex-1 overflow-y-auto px-8 py-8">
          <div className="max-w-lg space-y-6">
            <div className="flex items-center gap-4">
              <button
                onClick={() => setView('list')}
                className="text-xs font-mono text-[#444] hover:text-[#888] transition-colors"
              >
                ← retour
              </button>
              <p className="text-xs font-mono text-[#444] uppercase tracking-widest">
                Nouveau deck
              </p>
            </div>

            {/* Source */}
            <div>
              <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest block mb-2">
                Fichier source
              </label>
              <select
                value={genSource}
                onChange={e => setGenSource(e.target.value)}
                disabled={generating}
                className="w-full bg-[#141414] border border-[#242424] rounded px-3 py-2 text-xs font-mono text-[#e0e0e0] focus:outline-none focus:border-[#383838] disabled:opacity-40"
              >
                <option value="">Choisir un fichier indexé...</option>
                {availableFiles.map(f => (
                  <option key={f} value={f}>{basename(f)}</option>
                ))}
              </select>
            </div>

            {/* Nom */}
            <div>
              <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest block mb-2">
                Nom du deck
              </label>
              <input
                value={genNom}
                onChange={e => setGenNom(e.target.value)}
                disabled={generating}
                placeholder="ex : Mécanique S1"
                className="w-full bg-[#141414] border border-[#242424] rounded px-3 py-2 text-xs font-mono text-[#e0e0e0] placeholder-[#333] focus:outline-none focus:border-[#383838] disabled:opacity-40"
              />
            </div>

            {/* N cartes */}
            <div>
              <div className="flex items-center gap-3 mb-3">
                <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest">
                  Nombre de cartes
                </label>
                <button
                  onClick={() => setGenAuto(v => !v)}
                  disabled={generating}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono border transition-colors ${
                    genAuto
                      ? 'bg-[#1a2a1a] border-[#2a4a2a] text-[#5a9a5a]'
                      : 'bg-[#141414] border-[#242424] text-[#444] hover:text-[#888]'
                  }`}
                >
                  IA choisit
                </button>
              </div>
              {!genAuto && (
                <div className="flex items-center gap-3">
                  <input
                    type="range"
                    min={5}
                    max={50}
                    value={genN}
                    onChange={e => setGenN(Number(e.target.value))}
                    disabled={generating}
                    className="flex-1 accent-[#555]"
                  />
                  <span className="text-xs font-mono text-[#888] w-8 text-right">{genN}</span>
                </div>
              )}
            </div>

            <button
              onClick={startGenerate}
              disabled={!canGenerate}
              className="px-5 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              {generating ? 'Génération...' : 'Générer'}
            </button>

            {/* Streaming output */}
            {(genText || generating) && (
              <div className="border border-[#1e1e1e] rounded px-4 py-3 max-h-56 overflow-y-auto">
                <p className="text-[10px] font-mono text-[#333] uppercase tracking-widest mb-2">
                  {generating ? 'Génération en cours...' : 'Terminé'}
                </p>
                <pre className="text-xs font-mono text-[#555] whitespace-pre-wrap break-words leading-relaxed">
                  {genText}
                  {generating && <span className="animate-pulse text-[#2a2a2a]">▍</span>}
                </pre>
              </div>
            )}

            {genError && (
              <p className="text-xs font-mono text-[#7a3333]">{genError}</p>
            )}
          </div>
        </div>
      </main>
    )
  }

  // REVIEW VIEW
  const card = reviewCards[reviewIndex]
  const total = reviewCards.length
  const progress = reviewIndex + (reviewDone ? 1 : 0)
  const taux = total > 0 ? Math.round((sessionSu / (sessionSu + sessionPasSu || 1)) * 100) : 0

  if (reviewDone) {
    return (
      <main className="flex flex-col flex-1 overflow-hidden items-center justify-center px-8">
        <div className="max-w-sm w-full space-y-6 text-center">
          <p className="text-xs font-mono text-[#444] uppercase tracking-widest">Session terminée</p>
          <div className="border border-[#1e1e1e] rounded-lg px-8 py-6 space-y-3">
            <p className="text-3xl font-mono text-[#e0e0e0]">{taux}%</p>
            <div className="flex justify-center gap-6">
              <span className="text-xs font-mono text-[#5a9a5a]">✓ {sessionSu} su</span>
              <span className="text-xs font-mono text-[#9a5a5a]">✗ {sessionPasSu} pas su</span>
            </div>
          </div>
          <button
            onClick={() => { setView('list'); fetchDecks() }}
            className="px-5 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] transition-colors"
          >
            Retour aux decks
          </button>
        </div>
      </main>
    )
  }

  return (
    <main className="flex flex-col flex-1 overflow-hidden">
      {/* Progress header */}
      <div className="border-b border-[#1e1e1e] px-8 py-4 shrink-0 flex items-center justify-between">
        <span className="text-xs font-mono text-[#3a3a3a]">
          {reviewIndex + 1} / {total}
        </span>
        <div className="flex items-center gap-4">
          <span className="text-xs font-mono text-[#5a9a5a]">✓ {sessionSu}</span>
          <span className="text-xs font-mono text-[#9a5a5a]">✗ {sessionPasSu}</span>
          <button
            onClick={() => setView('list')}
            className="text-xs font-mono text-[#2a2a2a] hover:text-[#666] transition-colors"
          >
            quitter
          </button>
        </div>
      </div>

      {/* Progress bar */}
      <div className="h-0.5 bg-[#111] shrink-0">
        <div
          className="h-full bg-[#2a4a2a] transition-all"
          style={{ width: `${(progress / total) * 100}%` }}
        />
      </div>

      {/* Card */}
      <div className="flex-1 flex flex-col items-center justify-center px-8 gap-6">
        {card && (
          <>
            <FlipCard
              question={card.question}
              réponse={card.réponse}
              flipped={flipped}
            />

            {!flipped ? (
              <button
                onClick={() => setFlipped(true)}
                className="px-6 py-2.5 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] transition-colors"
              >
                Voir la réponse
              </button>
            ) : (
              <div className="flex gap-4">
                <button
                  onClick={() => submitReview('pas_su')}
                  disabled={submitting}
                  className="px-6 py-2.5 bg-[#1a0a0a] border border-[#4a2a2a] rounded text-xs font-mono text-[#9a5a5a] hover:border-[#6a3a3a] hover:text-[#bb7a7a] disabled:opacity-30 transition-colors"
                >
                  ✗ Pas su
                </button>
                <button
                  onClick={() => submitReview('su')}
                  disabled={submitting}
                  className="px-6 py-2.5 bg-[#0a1a0a] border border-[#2a4a2a] rounded text-xs font-mono text-[#5a9a5a] hover:border-[#3a6a3a] hover:text-[#7aba7a] disabled:opacity-30 transition-colors"
                >
                  ✓ Su
                </button>
              </div>
            )}

            <p className="text-[10px] font-mono text-[#2a2a2a]">
              niveau {card.niveau} · prochaine révision dans{' '}
              {[1, 3, 7, 14, 30, 60][Math.min(card.niveau, 5)]}j
            </p>
          </>
        )}
      </div>
    </main>
  )
}
