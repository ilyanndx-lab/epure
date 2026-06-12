import { useState, useEffect, useCallback, useRef } from 'react'
import { ArrowLeft, Check, Layers, Plus, Sparkles, Trash2, X } from 'lucide-react'
import { Badge, Button, Card, Input, ProgressBar, Select } from '../../components/ui'

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
          className="absolute inset-0 border border-line rounded-lg shadow-sm flex items-center justify-center px-8 bg-surface"
        >
          <p className="text-base text-primary text-center leading-relaxed">
            {question}
          </p>
        </div>
        {/* Back — réponse */}
        <div
          style={{
            backfaceVisibility: 'hidden',
            transform: 'rotateY(180deg)',
          }}
          className="absolute inset-0 border border-accent2/30 rounded-lg shadow-sm flex items-center justify-center px-8 bg-elevated overflow-y-auto"
        >
          <p className="text-sm text-primary text-center leading-relaxed whitespace-pre-wrap">
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
          <div className="max-w-xl space-y-5">
            <div className="flex items-center justify-between">
              <h1 className="text-lg font-semibold text-primary flex items-center gap-2">
                <Layers size={18} className="text-accent" />
                Flashcards
              </h1>
              <Button
                variant="primary"
                size="sm"
                icon={<Plus size={14} />}
                onClick={() => { setGenText(''); setGenError(null); setView('generate') }}
              >
                Nouveau deck
              </Button>
            </div>

            {loadingDecks && (
              <p className="text-xs text-muted">Chargement...</p>
            )}

            {!loadingDecks && decks.length === 0 && (
              <div className="flex flex-col items-center justify-center gap-2 py-16 text-muted select-none">
                <Layers size={16} />
                <span className="text-sm">Aucun deck. Créez-en un depuis une fiche PDF.</span>
              </div>
            )}

            {decks.map(deck => (
              <Card key={deck.id} className="space-y-3">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-primary truncate">{deck.nom}</p>
                    <p className="text-xs font-mono text-muted truncate mt-0.5">
                      {basename(deck.source)}
                    </p>
                  </div>
                  <button
                    onClick={() => deleteDeck(deck.id)}
                    className="p-1 rounded-sm text-muted hover:text-error transition-colors duration-150 shrink-0"
                    title="Supprimer"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>

                <div className="flex items-center gap-2">
                  <Badge variant="neutral" mono>{deck.n_cartes} cartes</Badge>
                  {deck.n_dues > 0 && (
                    <Badge variant="secondary" mono>{deck.n_dues} dues</Badge>
                  )}
                </div>

                <div className="flex gap-2">
                  {deck.n_dues > 0 && (
                    <Button variant="primary" size="sm" onClick={() => startReview(deck.id, true)}>
                      Réviser ({deck.n_dues})
                    </Button>
                  )}
                  <Button variant="secondary" size="sm" onClick={() => startReview(deck.id, false)}>
                    Tout réviser
                  </Button>
                </div>
              </Card>
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
          <div className="max-w-lg space-y-5">
            <div className="flex items-center gap-3">
              <Button variant="ghost" size="sm" icon={<ArrowLeft size={14} />} onClick={() => setView('list')}>
                retour
              </Button>
              <h1 className="text-lg font-semibold text-primary">Nouveau deck</h1>
            </div>

            <Card className="space-y-5">
              {/* Source */}
              <div>
                <label className="text-xs text-muted uppercase tracking-wide block mb-2">
                  Fichier source
                </label>
                <Select
                  value={genSource}
                  onChange={e => setGenSource(e.target.value)}
                  disabled={generating}
                  className="w-full py-2 disabled:opacity-40"
                >
                  <option value="">Choisir un fichier indexé...</option>
                  {availableFiles.map(f => (
                    <option key={f} value={f}>{basename(f)}</option>
                  ))}
                </Select>
              </div>

              {/* Nom */}
              <div>
                <label className="text-xs text-muted uppercase tracking-wide block mb-2">
                  Nom du deck
                </label>
                <Input
                  value={genNom}
                  onChange={e => setGenNom(e.target.value)}
                  disabled={generating}
                  placeholder="ex : Mécanique S1"
                  className="w-full disabled:opacity-40"
                />
              </div>

              {/* N cartes */}
              <div>
                <div className="flex items-center gap-3 mb-3">
                  <label className="text-xs text-muted uppercase tracking-wide">
                    Nombre de cartes
                  </label>
                  <button
                    onClick={() => setGenAuto(v => !v)}
                    disabled={generating}
                    className={`px-2 py-0.5 rounded-full text-xs border transition-colors duration-150 ${
                      genAuto
                        ? 'bg-accent2/15 border-accent2/30 text-accent2'
                        : 'border-line text-muted hover:text-secondary'
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
                      className="flex-1 accent-[--accent-primary]"
                    />
                    <span className="text-xs font-mono text-secondary w-8 text-right">{genN}</span>
                  </div>
                )}
              </div>

              <Button
                variant="primary"
                onClick={startGenerate}
                disabled={!canGenerate}
                icon={<Sparkles size={14} />}
              >
                {generating ? 'Génération...' : 'Générer'}
              </Button>
            </Card>

            {/* Streaming output */}
            {(genText || generating) && (
              <Card accent="secondary" className="max-h-56 overflow-y-auto">
                <p className="text-xs text-muted uppercase tracking-wide mb-2">
                  {generating ? 'Génération en cours...' : 'Terminé'}
                </p>
                <pre className="text-xs font-mono text-secondary whitespace-pre-wrap break-words leading-relaxed">
                  {genText}
                  {generating && <span className="animate-pulse text-accent2">▍</span>}
                </pre>
              </Card>
            )}

            {genError && (
              <p className="text-xs text-error">{genError}</p>
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
        <div className="max-w-sm w-full space-y-5 text-center">
          <p className="text-sm font-semibold text-primary">Session terminée</p>
          <Card className="px-8 py-6 space-y-3">
            <p className="text-2xl font-mono text-gradient font-semibold">{taux}%</p>
            <div className="flex justify-center gap-3">
              <Badge variant="success" mono>{sessionSu} su</Badge>
              <Badge variant="error" mono>{sessionPasSu} pas su</Badge>
            </div>
          </Card>
          <Button variant="primary" onClick={() => { setView('list'); fetchDecks() }}>
            Retour aux decks
          </Button>
        </div>
      </main>
    )
  }

  return (
    <main className="flex flex-col flex-1 overflow-hidden">
      {/* Progress header */}
      <div className="border-b border-line px-8 py-4 shrink-0 flex items-center justify-between bg-surface">
        <span className="text-xs font-mono text-muted">
          {reviewIndex + 1} / {total}
        </span>
        <div className="flex items-center gap-3">
          <Badge variant="success" mono>{sessionSu}</Badge>
          <Badge variant="error" mono>{sessionPasSu}</Badge>
          <Button variant="ghost" size="sm" onClick={() => setView('list')}>
            quitter
          </Button>
        </div>
      </div>

      {/* Progress bar */}
      <div className="px-8 py-1 shrink-0 bg-surface border-b border-line">
        <ProgressBar value={(progress / total) * 100} />
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
              <Button variant="primary" onClick={() => setFlipped(true)}>
                Voir la réponse
              </Button>
            ) : (
              <div className="flex gap-4">
                <Button
                  variant="danger"
                  icon={<X size={14} />}
                  onClick={() => submitReview('pas_su')}
                  disabled={submitting}
                >
                  Pas su
                </Button>
                <Button
                  variant="secondary"
                  icon={<Check size={14} className="text-success" />}
                  onClick={() => submitReview('su')}
                  disabled={submitting}
                  className="border-success/30 text-success hover:border-success/50 hover:text-success"
                >
                  Su
                </Button>
              </div>
            )}

            <p className="text-xs font-mono text-muted">
              niveau {card.niveau} · prochaine révision dans{' '}
              {[1, 3, 7, 14, 30, 60][Math.min(card.niveau, 5)]}j
            </p>
          </>
        )}
      </div>
    </main>
  )
}
