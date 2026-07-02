import { useState } from 'react'
import {
  AlertTriangle, BookOpen, CalendarCheck, Check, Layers, Loader2, RefreshCw, Target,
} from 'lucide-react'
import { Badge, Button, Card, ProgressBar } from '../../components/ui'
import { API, apiFetch } from '../../api'
import { usePersistentState } from '../../usePersistentState'

// ── Types (miroir de GET /reviseur/plan) ─────────────────────────────────────

interface Bloc {
  matière: string
  objectif: string
  durée_min: number
  fiches: string[]
  cartes: string
  consigne: string
}

interface Plan {
  date: string
  cibles: string[]
  cartes_dues: number
  par_deck: { deck: string; n: number }[]
  blocs: Bloc[]
  fallback: boolean
  note: string
}

// État d'un bloc : null = à faire ; sinon le ressenti envoyé en observation.
type EtatBloc = null | 'acquis' | 'a_retravailler'

// Date locale YYYY-MM-DD (toISOString serait en UTC → mauvais jour le soir).
const today = () => new Date().toLocaleDateString('fr-CA')

export default function Component() {
  // Persistés : le plan du jour et l'avancement survivent au rechargement.
  const [plan, setPlan] = usePersistentState<Plan | null>('reviseur.plan', null)
  const [etats, setEtats] = usePersistentState<EtatBloc[]>('reviseur.etats', [])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const stale = plan !== null && plan.date !== today()
  const nDone = etats.filter(Boolean).length
  const nBlocs = plan?.blocs.length ?? 0

  const generatePlan = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await apiFetch(`${API}/reviseur/plan`)
      const data = await res.json()
      if (!res.ok) {
        setError(typeof data.detail === 'string' ? data.detail : 'Génération du plan échouée.')
        return
      }
      setPlan(data as Plan)
      setEtats(Array((data as Plan).blocs.length).fill(null))
    } catch {
      setError('Backend injoignable.')
    } finally {
      setLoading(false)
    }
  }

  // Termine un bloc : état local immédiat + observation en mémoire de session
  // (best-effort : un échec réseau n'annule pas la case cochée, il est signalé).
  const finishBloc = (i: number, ressenti: 'acquis' | 'a_retravailler') => {
    const bloc = plan?.blocs[i]
    if (!bloc || etats[i]) return
    setEtats(prev => prev.map((e, j) => (j === i ? ressenti : e)))
    void apiFetch(`${API}/reviseur/bloc/termine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ matière: bloc.matière, objectif: bloc.objectif, ressenti }),
    }).then(res => {
      if (!res.ok) setError("Bloc coché, mais l'observation n'a pas pu être enregistrée en mémoire.")
    }).catch(() => setError("Bloc coché, mais l'observation n'a pas pu être enregistrée (réseau)."))
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 space-y-6">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <CalendarCheck size={18} className="text-accent" /> Réviseur — plan du jour
      </h1>

      {/* ── Synthèse + génération ── */}
      <Card className="max-w-2xl space-y-3">
        {plan && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <Badge variant={stale ? 'warning' : 'success'}>{plan.date}</Badge>
              {plan.cibles.map(c => (
                <Badge key={c} variant="primary"><Target size={11} />{c}</Badge>
              ))}
              {plan.cartes_dues > 0 && (
                <Badge variant="secondary">
                  <Layers size={11} />{plan.cartes_dues} carte{plan.cartes_dues > 1 ? 's' : ''} due{plan.cartes_dues > 1 ? 's' : ''}
                </Badge>
              )}
            </div>
            {stale && (
              <p className="text-xs text-warning flex items-center gap-1.5">
                <AlertTriangle size={12} /> Ce plan date du {plan.date} — générez celui d'aujourd'hui.
              </p>
            )}
            {plan.fallback && (
              <p className="text-xs text-muted">
                Plan de repli (LLM indisponible ou réponse inexploitable) — structure simple lacune par lacune.
              </p>
            )}
            {plan.note && <p className="text-xs text-muted">{plan.note}</p>}
          </div>
        )}

        <div className="flex items-center gap-3">
          <Button variant="primary" icon={loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
            onClick={generatePlan} disabled={loading}>
            {loading ? 'Analyse du profil, des fiches et des cartes…'
              : plan ? 'Régénérer le plan' : 'Générer le plan du jour'}
          </Button>
          {nBlocs > 0 && (
            <span className="text-xs text-muted">{nDone}/{nBlocs} bloc{nBlocs > 1 ? 's' : ''} terminé{nDone > 1 ? 's' : ''}</span>
          )}
        </div>
        {nBlocs > 0 && <ProgressBar value={(nDone / nBlocs) * 100} color={nDone === nBlocs ? 'success' : 'gradient'} />}
        {error && <p className="text-xs text-error whitespace-pre-wrap">{error}</p>}
      </Card>

      {/* ── Blocs de la séance ── */}
      {plan?.blocs.map((bloc, i) => {
        const etat = etats[i] ?? null
        return (
          <Card key={i} className={`max-w-2xl space-y-2 ${etat ? 'opacity-70' : ''}`}>
            <div className="flex items-start justify-between gap-3">
              <label className="flex items-start gap-2.5 cursor-pointer">
                <input type="checkbox" checked={etat !== null} onChange={() => finishBloc(i, 'acquis')}
                  disabled={etat !== null}
                  className="accent-[--accent-primary] mt-0.5" />
                <span>
                  <span className={`text-sm font-medium ${etat ? 'line-through text-muted' : 'text-primary'}`}>
                    {bloc.objectif}
                  </span>
                  <span className="block text-xs text-muted mt-0.5">
                    {bloc.matière} · {bloc.durée_min} min
                  </span>
                </span>
              </label>
              <div className="flex items-center gap-2 shrink-0">
                {etat === 'acquis' && <Badge variant="success"><Check size={11} />acquis</Badge>}
                {etat === 'a_retravailler' && <Badge variant="warning">à retravailler</Badge>}
                {etat === null && (
                  <Button variant="ghost" size="sm" onClick={() => finishBloc(i, 'a_retravailler')}>
                    Fait, mais à retravailler
                  </Button>
                )}
              </div>
            </div>

            {bloc.fiches.length > 0 && (
              <p className="text-xs text-secondary flex items-center gap-1.5 flex-wrap">
                <BookOpen size={12} className="text-accent2 shrink-0" />
                {bloc.fiches.map(f => <span key={f} className="font-mono">{f}</span>)}
              </p>
            )}
            {bloc.cartes && (
              <p className="text-xs text-secondary flex items-center gap-1.5">
                <Layers size={12} className="text-accent2 shrink-0" />{bloc.cartes}
              </p>
            )}
            {bloc.consigne && <p className="text-xs text-muted">{bloc.consigne}</p>}
          </Card>
        )
      })}

      {!plan && !loading && (
        <p className="text-sm text-muted max-w-2xl">
          Le Réviseur croise vos lacunes (profil élève), vos fiches indexées et vos flashcards
          dues pour proposer une séance en blocs de 25 minutes. Chaque bloc terminé nourrit la
          mémoire de session — la consolidation en tient compte.
        </p>
      )}
    </main>
  )
}
