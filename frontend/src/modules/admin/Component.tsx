import { useState, useEffect, useRef } from 'react'
import { Check, Copy, FolderCog, RefreshCw, Scan, Square, Undo2, X } from 'lucide-react'
import { Badge, Button, Card, Modal, ProgressBar } from '../../components/ui'
import { API, apiFetch } from '../../api'
import { useInstanceConfig } from '../../instance'

/** Joint la racine des fiches à un chemin relatif, sans doubler le séparateur.
 *
 * Remplace un `const FICHES_ROOT = 'C:\\Users\\Ilyan\\Fiches\\'` en dur : la
 * racine est un réglage d'instance, et ce chemin-là n'existait que sur le poste
 * de l'auteur. Il avait survécu au nettoyage des chemins absolus parce que la
 * source JS échappe ses antislashs — `C:\\Users\\Ilyan` ne correspond pas au
 * motif `C:\Users\Ilyan` qu'on cherchait.
 */
function sousRacine(racine: string, ...parties: string[]): string {
  const sep = racine.includes('/') && !racine.includes('\\') ? '/' : '\\'
  const base = racine.replace(/[\\/]+$/, '')
  return [base, ...parties].join(sep)
}

interface ScanResult {
  path: string
  nom_actuel: string
  dossier_actuel: string
  matière_détectée: string
  nom_suggéré: string
  confiance: number
  action_tri: boolean
  action_renommage: boolean
}

interface ActionSelection {
  tri: boolean
  renommage: boolean
}

interface DuplicateGroup {
  groupe: string[]
  similarité: number
}

interface LogEntry {
  id: string
  date: string
  type: string
  source: string
  destination: string
  annulé: boolean
}

interface PendingAction {
  type: string
  source: string
  destination: string
  label: string
}

function confBadge(c: number): 'success' | 'warning' | 'error' {
  if (c > 0.8) return 'success'
  if (c > 0.5) return 'warning'
  return 'error'
}

function filename(p: string) {
  return p.split('\\').pop() || p
}

function isRecent(dateStr: string) {
  return Date.now() - new Date(dateStr).getTime() < 24 * 60 * 60 * 1000
}

export default function Admin() {
  // Racine des fiches : réglage d'instance, jamais un chemin en dur.
  const racineFiches = useInstanceConfig().fiches.racine
  // Scan
  const [scanning, setScanning] = useState(false)
  const [scanProgress, setScanProgress] = useState<{ file: string; index: number; total: number } | null>(null)
  const [scanResults, setScanResults] = useState<ScanResult[]>([])
  const [selection, setSelection] = useState<Record<string, ActionSelection>>({})
  const [showModal, setShowModal] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [execResults, setExecResults] = useState<{ path: string; succès: boolean; erreur: string | null }[] | null>(null)
  const scanAbortRef = useRef<AbortController | null>(null)

  // Duplicates
  const [loadingDups, setLoadingDups] = useState(false)
  const [duplicates, setDuplicates] = useState<DuplicateGroup[] | null>(null)

  // Log
  const [log, setLog] = useState<LogEntry[]>([])

  useEffect(() => {
    loadLog()
  }, [])

  async function startScan() {
    if (scanning) {
      scanAbortRef.current?.abort()
      return
    }
    setScanning(true)
    setScanProgress(null)
    setScanResults([])
    setSelection({})
    setExecResults(null)
    const ctrl = new AbortController()
    scanAbortRef.current = ctrl

    try {
      const res = await apiFetch(`${API}/admin/scan`, { method: 'POST', signal: ctrl.signal })
      const reader = res.body!.getReader()
      const dec = new TextDecoder()
      let buf = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const data = JSON.parse(line.slice(6))
          if (data.type === 'progress') {
            setScanProgress({ file: data.file, index: data.index, total: data.total })
          } else if (data.type === 'done') {
            const results: ScanResult[] = data.résultats
            setScanResults(results)
            const init: Record<string, ActionSelection> = {}
            for (const r of results) {
              init[r.path] = {
                tri: r.action_tri && r.confiance > 0.8,
                renommage: r.action_renommage && r.confiance > 0.8,
              }
            }
            setSelection(init)
            setScanProgress(null)
          } else if (data.type === 'error') {
            console.error('Erreur scan:', data.content)
            setScanProgress(null)
          }
        }
      }
    } catch (err: unknown) {
      if (err instanceof Error && err.name !== 'AbortError') console.error('Erreur scan:', err)
    } finally {
      setScanning(false)
    }
  }

  function buildPendingActions(): PendingAction[] {
    return scanResults
      .filter(r => selection[r.path]?.tri || selection[r.path]?.renommage)
      .map(r => {
        const isTri = selection[r.path]?.tri
        const isRen = selection[r.path]?.renommage
        const curDir = r.path.substring(0, r.path.lastIndexOf('\\'))
        let destination: string
        let type: string
        if (isTri && isRen) {
          destination = sousRacine(racineFiches, r.matière_détectée, r.nom_suggéré)
          type = 'tri+renommage'
        } else if (isTri) {
          destination = sousRacine(racineFiches, r.matière_détectée, r.nom_actuel)
          type = 'tri'
        } else {
          destination = `${curDir}\\${r.nom_suggéré}`
          type = 'renommage'
        }
        return {
          type,
          source: r.path,
          destination,
          label: `${r.nom_actuel} → ${filename(destination)}`,
        }
      })
  }

  async function executeActions() {
    const actions = buildPendingActions()
    if (actions.length === 0) return
    setExecuting(true)
    setShowModal(false)
    try {
      const res = await apiFetch(`${API}/admin/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ actions: actions.map(({ type, source, destination }) => ({ type, source, destination })) }),
      })
      const data = await res.json()
      setExecResults(data.résultats)
      setScanResults([])
      setSelection({})
      await loadLog()
    } catch (err) {
      console.error('Erreur exécution:', err)
    } finally {
      setExecuting(false)
    }
  }

  async function loadDuplicates() {
    setLoadingDups(true)
    try {
      const res = await apiFetch(`${API}/admin/duplicates`)
      const data = await res.json()
      setDuplicates(data.groupes)
    } catch (err) {
      console.error('Erreur doublons:', err)
    } finally {
      setLoadingDups(false)
    }
  }

  async function openFile(path: string) {
    try {
      await apiFetch(`${API}/admin/open?path=${encodeURIComponent(path)}`)
    } catch (err) {
      console.error('Erreur ouverture:', err)
    }
  }

  async function loadLog() {
    try {
      const res = await apiFetch(`${API}/admin/log`)
      const data = await res.json()
      setLog([...data.log].reverse())
    } catch (err) {
      console.error('Erreur log:', err)
    }
  }

  async function undoAction(id: string) {
    try {
      const res = await apiFetch(`${API}/admin/undo`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: id }),
      })
      const data = await res.json()
      if (data.succès) {
        await loadLog()
      } else {
        console.error('Annulation échouée:', data.erreur)
      }
    } catch (err) {
      console.error('Erreur annulation:', err)
    }
  }

  const pendingActions = buildPendingActions()
  const pendingCount = pendingActions.length

  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-8">

      <h1 className="text-lg font-semibold text-primary flex items-center gap-2">
        <FolderCog size={18} className="text-accent" />
        Administration des fiches
      </h1>

      {/* ── Section 1 : Scan ── */}
      <Card className="space-y-4">
        <h2 className="text-sm font-semibold text-primary flex items-center gap-2">
          <Scan size={15} className="text-muted" />
          Scan et analyse
        </h2>

        <Button
          variant={scanning ? 'danger' : 'primary'}
          onClick={startScan}
          icon={scanning ? <Square size={14} /> : <Scan size={14} />}
        >
          {scanning ? 'Arrêter le scan' : 'Scanner les fiches'}
        </Button>

        {scanProgress && (
          <div className="space-y-1">
            <div className="flex items-center gap-3">
              <ProgressBar
                value={((scanProgress.index + 1) / scanProgress.total) * 100}
                className="flex-1"
              />
              <span className="text-xs font-mono text-muted shrink-0">
                {scanProgress.index + 1}/{scanProgress.total}
              </span>
            </div>
            <p className="text-xs font-mono text-muted truncate">{scanProgress.file}</p>
          </div>
        )}

        {scanResults.length > 0 && (
          <div>
            <div className="overflow-x-auto rounded-md border border-line">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-muted uppercase tracking-wide border-b border-line bg-elevated/50">
                    <th className="text-left px-3 py-2 font-medium">Fichier</th>
                    <th className="text-left px-3 py-2 font-medium">Dossier</th>
                    <th className="text-left px-3 py-2 font-medium">Dossier cible</th>
                    <th className="text-left px-3 py-2 font-medium">Conf.</th>
                    <th className="text-left px-3 py-2 font-medium">Nom suggéré</th>
                    <th className="text-center px-3 py-2 font-medium">Déplacer</th>
                    <th className="text-center px-3 py-2 font-medium">Renommer</th>
                  </tr>
                </thead>
                <tbody>
                  {scanResults.map((r, ri) => (
                    <tr
                      key={r.path}
                      className={`border-b border-line last:border-b-0 hover:bg-accent/5 transition-colors duration-150 ${
                        ri % 2 === 1 ? 'bg-elevated/30' : ''
                      }`}
                    >
                      <td className="px-3 py-2 text-primary max-w-[180px]">
                        <span className="truncate block font-mono" title={r.nom_actuel}>{r.nom_actuel}</span>
                      </td>
                      <td className="px-3 py-2 text-secondary font-mono">{r.dossier_actuel}</td>
                      <td className={`px-3 py-2 ${r.matière_détectée === 'Inconnu' ? 'text-muted' : 'text-secondary'}`}>
                        {r.matière_détectée}
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant={confBadge(r.confiance)} mono>
                          {(r.confiance * 100).toFixed(0)}%
                        </Badge>
                      </td>
                      <td className="px-3 py-2 text-secondary max-w-[180px]">
                        {r.nom_actuel !== r.nom_suggéré
                          ? <span className="truncate block font-mono" title={r.nom_suggéré}>{r.nom_suggéré}</span>
                          : <span className="text-muted/50">—</span>
                        }
                      </td>
                      <td className="px-3 py-2 text-center">
                        {r.action_tri ? (
                          <input
                            type="checkbox"
                            checked={selection[r.path]?.tri ?? false}
                            onChange={e => setSelection(s => ({
                              ...s,
                              [r.path]: { ...s[r.path], tri: e.target.checked }
                            }))}
                            className="accent-[--accent-primary] cursor-pointer"
                          />
                        ) : <span className="text-muted/50">—</span>}
                      </td>
                      <td className="px-3 py-2 text-center">
                        {r.action_renommage ? (
                          <input
                            type="checkbox"
                            checked={selection[r.path]?.renommage ?? false}
                            onChange={e => setSelection(s => ({
                              ...s,
                              [r.path]: { ...s[r.path], renommage: e.target.checked }
                            }))}
                            className="accent-[--accent-primary] cursor-pointer"
                          />
                        ) : <span className="text-muted/50">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-4 flex gap-3 items-center">
              <Button
                variant="primary"
                onClick={() => setShowModal(true)}
                disabled={executing || pendingCount === 0}
              >
                {executing ? 'Exécution…' : `Exécuter la sélection (${pendingCount})`}
              </Button>
            </div>

            {execResults && (
              <div className="mt-3 space-y-1">
                {execResults.map((r, i) => (
                  <div key={i} className={`text-xs flex items-center gap-2 ${r.succès ? 'text-success' : 'text-error'}`}>
                    {r.succès ? <Check size={12} /> : <X size={12} />}
                    <span className="truncate font-mono">{filename(r.path)}</span>
                    {r.erreur && <span className="text-error/80">— {r.erreur}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── Section 2 : Doublons ── */}
      <Card className="space-y-4">
        <h2 className="text-sm font-semibold text-primary flex items-center gap-2">
          <Copy size={15} className="text-muted" />
          Doublons
        </h2>

        <Button variant="secondary" onClick={loadDuplicates} disabled={loadingDups}>
          {loadingDups ? 'Analyse…' : 'Détecter les doublons'}
        </Button>

        {duplicates !== null && (
          <div>
            {duplicates.length === 0 ? (
              <p className="text-xs text-muted">Aucun doublon détecté.</p>
            ) : (
              <div className="space-y-3">
                {duplicates.map((grp, gi) => (
                  <Card key={gi} elevated className="space-y-1">
                    <div className="mb-2">
                      <Badge variant="secondary" mono>
                        similarité {(grp.similarité * 100).toFixed(1)}%
                      </Badge>
                    </div>
                    {grp.groupe.map((p, pi) => (
                      <div key={pi} className="flex items-center gap-3">
                        <span className="text-xs font-mono text-secondary truncate flex-1" title={p}>
                          {filename(p)}
                        </span>
                        <span className="text-xs font-mono text-muted truncate max-w-[200px] hidden sm:block" title={p}>
                          {p}
                        </span>
                        <Button variant="ghost" size="sm" onClick={() => openFile(p)}>
                          ouvrir
                        </Button>
                      </div>
                    ))}
                  </Card>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── Section 3 : Historique ── */}
      <Card className="space-y-4">
        <div className="flex items-center gap-4">
          <h2 className="text-sm font-semibold text-primary flex items-center gap-2">
            <RefreshCw size={15} className="text-muted" />
            Historique
          </h2>
          <Button variant="ghost" size="sm" icon={<RefreshCw size={12} />} onClick={loadLog}>
            rafraîchir
          </Button>
        </div>

        {log.length === 0 ? (
          <p className="text-xs text-muted">Aucune action enregistrée.</p>
        ) : (
          <div>
            {log.map((entry, ei) => (
              <div
                key={entry.id}
                className={`flex items-center gap-3 border-b border-line last:border-b-0 py-2 px-2 text-xs hover:bg-accent/5 transition-colors duration-150 ${
                  ei % 2 === 1 ? 'bg-elevated/30' : ''
                } ${entry.annulé ? 'opacity-40' : ''}`}
              >
                <span className="text-muted font-mono shrink-0">
                  {new Date(entry.date).toLocaleString('fr-FR', {
                    day: '2-digit', month: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                  })}
                </span>
                <Badge variant="neutral" mono>{entry.type}</Badge>
                <span className="text-secondary font-mono truncate flex-1" title={`${entry.source} → ${entry.destination}`}>
                  {filename(entry.source)} → {filename(entry.destination)}
                </span>
                {!entry.annulé && isRecent(entry.date) && (
                  <Button variant="ghost" size="sm" icon={<Undo2 size={12} />} onClick={() => undoAction(entry.id)}>
                    annuler
                  </Button>
                )}
                {entry.annulé && (
                  <Badge variant="neutral">annulé</Badge>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ── Modal aperçu ── */}
      <Modal
        open={showModal}
        onClose={() => setShowModal(false)}
        title={`Aperçu — ${pendingCount} action${pendingCount !== 1 ? 's' : ''}`}
        width="max-w-2xl"
      >
        <div className="max-h-[55vh] overflow-y-auto space-y-2">
          {pendingActions.map((a, i) => (
            <div key={i} className="text-xs border-b border-line last:border-b-0 pb-2">
              <Badge variant="primary" mono className="mr-2">{a.type}</Badge>
              <span className="text-secondary font-mono">{filename(a.source)}</span>
              <span className="text-muted mx-2">→</span>
              <span className="text-primary font-mono break-all">{a.destination}</span>
            </div>
          ))}
        </div>

        <div className="mt-4 flex gap-3 pt-3 border-t border-line">
          <Button variant="primary" onClick={executeActions} disabled={executing}>
            {executing ? 'Exécution…' : 'Confirmer et exécuter'}
          </Button>
          <Button variant="ghost" onClick={() => setShowModal(false)}>
            Annuler
          </Button>
        </div>
      </Modal>
    </div>
  )
}
