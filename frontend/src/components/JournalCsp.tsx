/**
 * Journal des violations de la CSP (Réglages › Sécurité).
 *
 * `img-src` est BLOQUANT, le reste en `Report-Only` (backend/core/csp.py) : le
 * mode de chaque directive vient de `GET /instance/csp` (`directives`), lu
 * dans les en-têtes réellement servis — jamais supposé ici. Chaque entrée
 * porte sa `disposition` telle que le navigateur l'a rapportée (`enforce` :
 * bloquée ; `report` : seulement signalée). Ce journal sert à décider si une
 * autre directive peut devenir bloquante, et ce que ça casserait. Il est
 * agrégé par directive × origine bloquée × document × disposition, côté
 * backend.
 *
 * Les entrées viennent d'un point de collecte sans token : n'importe quelle
 * page locale peut y écrire. Affichées comme du texte (React échappe), jamais
 * interprétées.
 */
import { useCallback, useEffect, useState } from 'react'
import { ShieldAlert, RefreshCw, Trash2 } from 'lucide-react'
import { Button, Card } from './ui'
import { API, apiFetch } from '../api'

interface Entree {
  directive: string
  bloqué: string
  document: string
  source?: string
  /** `enforce` | `report` (brut, tel que rapporté) ; `?` si absent. */
  disposition?: string
  nombre: number
  premier?: number
  dernier?: number
}

interface Directive {
  directive: string
  mode: string
  valeur: string
}

interface Journal {
  entrees: Entree[]
  directives: Directive[]
}

function date(ts?: number): string {
  return ts ? new Date(ts * 1000).toLocaleString('fr-FR') : '—'
}

/** Lit le journal ; rejette avec un message affichable. */
async function lireJournal(): Promise<Journal> {
  let res: Response
  try {
    res = await apiFetch(`${API}/instance/csp`)
  } catch {
    throw new Error('Backend injoignable.')
  }
  if (!res.ok) throw new Error(`Le backend a refusé la demande (HTTP ${res.status}).`)
  const data = await res.json() as { entrées?: Entree[]; directives?: Directive[] }
  return {
    entrees: Array.isArray(data.entrées) ? data.entrées : [],
    directives: Array.isArray(data.directives) ? data.directives : [],
  }
}

export default function JournalCsp() {
  const [entrees, setEntrees] = useState<Entree[] | null>(null)
  const [directives, setDirectives] = useState<Directive[]>([])
  const [erreur, setErreur] = useState<string | null>(null)

  const appliquer = useCallback((j: Journal) => {
    setEntrees(j.entrees)
    setDirectives(j.directives)
  }, [])

  const charger = useCallback(() => lireJournal()
    .then(j => { appliquer(j); setErreur(null) })
    .catch((e: Error) => setErreur(e.message)), [appliquer])

  const vider = useCallback(async () => {
    try {
      const res = await apiFetch(`${API}/instance/csp`, { method: 'DELETE' })
      if (!res.ok) { setErreur(`Le backend a refusé la demande (HTTP ${res.status}).`); return }
      await charger()
    } catch {
      setErreur('Backend injoignable.')
    }
  }, [charger])

  useEffect(() => {
    lireJournal().then(appliquer).catch((e: Error) => setErreur(e.message))
  }, [appliquer])

  return (
    <Card className="max-w-3xl space-y-3">
      <h2 className="text-sm font-semibold text-primary flex items-center gap-2">
        <span className="text-muted"><ShieldAlert size={15} /></span>
        Sécurité — CSP
      </h2>
      <p className="text-xs text-muted/70">
        Une directive « bloquant » refuse la ressource ; une directive « report-only »
        la laisse passer et la signale seulement. La CSP limite l'envoi de données vers
        l'extérieur, pas l'accès local d'un composant au backend.
      </p>
      {directives.length > 0 && (
        <ul aria-label="Mode par directive" className="text-xs font-mono text-secondary space-y-0.5">
          {directives.map(d => (
            <li key={`${d.directive}|${d.mode}`}>
              {d.directive} — <span className={d.mode === 'bloquant' ? 'text-error' : 'text-muted'}>{d.mode}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <Button variant="ghost" size="sm" icon={<RefreshCw size={13} />} onClick={() => void charger()}>
          Actualiser
        </Button>
        <Button variant="ghost" size="sm" icon={<Trash2 size={13} />} onClick={() => void vider()}
          disabled={!entrees || entrees.length === 0}>
          Vider le journal
        </Button>
      </div>
      {erreur && <p className="text-xs text-error">{erreur}</p>}
      {entrees && entrees.length === 0 && (
        <p className="text-xs text-muted">Aucune violation signalée.</p>
      )}
      {entrees && entrees.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-muted text-left">
              <tr>
                <th className="py-1 pr-3 font-normal">Directive</th>
                <th className="py-1 pr-3 font-normal">Bloqué</th>
                <th className="py-1 pr-3 font-normal">Page</th>
                <th className="py-1 pr-3 font-normal">Disposition</th>
                <th className="py-1 pr-3 font-normal">Nombre</th>
                <th className="py-1 font-normal">Dernière fois</th>
              </tr>
            </thead>
            <tbody className="font-mono text-secondary">
              {entrees.map(e => (
                <tr key={`${e.directive}|${e.bloqué}|${e.document}|${e.disposition ?? '?'}`} className="border-t border-line">
                  <td className="py-1 pr-3">{e.directive}</td>
                  <td className="py-1 pr-3 break-all">{e.bloqué}</td>
                  <td className="py-1 pr-3 break-all">{e.document}</td>
                  <td className="py-1 pr-3">{e.disposition ?? '?'}</td>
                  <td className="py-1 pr-3">{e.nombre}</td>
                  <td className="py-1">{date(e.dernier)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
