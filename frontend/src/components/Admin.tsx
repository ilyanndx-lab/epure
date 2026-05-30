import { useState, useEffect, useRef } from 'react'

const API = 'http://localhost:8000'
const FICHES_ROOT = 'C:\\Users\\Ilyan\\Fiches\\'

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

function confColor(c: number) {
  if (c > 0.8) return 'text-[#6a8a6a]'
  if (c > 0.5) return 'text-[#8a7a4a]'
  return 'text-[#7a4a4a]'
}

function filename(p: string) {
  return p.split('\\').pop() || p
}

function isRecent(dateStr: string) {
  return Date.now() - new Date(dateStr).getTime() < 24 * 60 * 60 * 1000
}

export default function Admin() {
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
      const res = await fetch(`${API}/admin/scan`, { method: 'POST', signal: ctrl.signal })
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
          destination = `${FICHES_ROOT}${r.matière_détectée}\\${r.nom_suggéré}`
          type = 'tri+renommage'
        } else if (isTri) {
          destination = `${FICHES_ROOT}${r.matière_détectée}\\${r.nom_actuel}`
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
      const res = await fetch(`${API}/admin/execute`, {
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
      const res = await fetch(`${API}/admin/duplicates`)
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
      await fetch(`${API}/admin/open?path=${encodeURIComponent(path)}`)
    } catch (err) {
      console.error('Erreur ouverture:', err)
    }
  }

  async function loadLog() {
    try {
      const res = await fetch(`${API}/admin/log`)
      const data = await res.json()
      setLog([...data.log].reverse())
    } catch (err) {
      console.error('Erreur log:', err)
    }
  }

  async function undoAction(id: string) {
    try {
      const res = await fetch(`${API}/admin/undo`, {
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
    <div className="flex-1 overflow-y-auto p-6 space-y-10 font-mono text-sm text-[#e0e0e0]">

      {/* ── Section 1 : Scan ── */}
      <section>
        <div className="flex items-center gap-4 mb-4">
          <h2 className="text-[10px] uppercase tracking-[0.2em] text-[#444]">Scan et analyse</h2>
        </div>

        <button
          onClick={startScan}
          className={`px-4 py-2 border rounded text-xs transition-colors ${
            scanning
              ? 'border-[#4a2a2a] text-[#aa5a5a] hover:border-[#7a3a3a]'
              : 'bg-[#1a1a1a] border-[#2a2a2a] hover:border-[#444]'
          }`}
        >
          {scanning ? 'Arrêter le scan' : 'Scanner les fiches'}
        </button>

        {scanProgress && (
          <div className="mt-3 space-y-1">
            <div className="flex items-center gap-3">
              <div className="flex-1 h-px bg-[#1a1a1a] relative overflow-hidden">
                <div
                  className="absolute left-0 top-0 h-full bg-[#3a5a3a] transition-all duration-300"
                  style={{ width: `${((scanProgress.index + 1) / scanProgress.total) * 100}%` }}
                />
              </div>
              <span className="text-[10px] text-[#555] shrink-0">
                {scanProgress.index + 1}/{scanProgress.total}
              </span>
            </div>
            <p className="text-[10px] text-[#3a3a3a] truncate">{scanProgress.file}</p>
          </div>
        )}

        {scanResults.length > 0 && (
          <div className="mt-5">
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[#3a3a3a] border-b border-[#1a1a1a]">
                    <th className="text-left pb-2 pr-4 font-normal">Fichier</th>
                    <th className="text-left pb-2 pr-4 font-normal">Dossier</th>
                    <th className="text-left pb-2 pr-4 font-normal">Matière</th>
                    <th className="text-left pb-2 pr-4 font-normal">Conf.</th>
                    <th className="text-left pb-2 pr-4 font-normal">Nom suggéré</th>
                    <th className="text-center pb-2 pr-2 font-normal">Déplacer</th>
                    <th className="text-center pb-2 font-normal">Renommer</th>
                  </tr>
                </thead>
                <tbody>
                  {scanResults.map(r => (
                    <tr key={r.path} className="border-b border-[#111] hover:bg-[#0f0f0f] group">
                      <td className="py-2 pr-4 text-[#bbb] max-w-[180px]">
                        <span className="truncate block" title={r.nom_actuel}>{r.nom_actuel}</span>
                      </td>
                      <td className="py-2 pr-4 text-[#666]">{r.dossier_actuel}</td>
                      <td className={`py-2 pr-4 ${r.matière_détectée === 'Inconnu' ? 'text-[#3a3a3a]' : 'text-[#888]'}`}>
                        {r.matière_détectée}
                      </td>
                      <td className={`py-2 pr-4 ${confColor(r.confiance)}`}>
                        {(r.confiance * 100).toFixed(0)}%
                      </td>
                      <td className="py-2 pr-4 text-[#666] max-w-[180px]">
                        {r.nom_actuel !== r.nom_suggéré
                          ? <span className="truncate block" title={r.nom_suggéré}>{r.nom_suggéré}</span>
                          : <span className="text-[#222]">—</span>
                        }
                      </td>
                      <td className="py-2 pr-2 text-center">
                        {r.action_tri ? (
                          <input
                            type="checkbox"
                            checked={selection[r.path]?.tri ?? false}
                            onChange={e => setSelection(s => ({
                              ...s,
                              [r.path]: { ...s[r.path], tri: e.target.checked }
                            }))}
                            className="accent-[#3a6a3a] cursor-pointer"
                          />
                        ) : <span className="text-[#222]">—</span>}
                      </td>
                      <td className="py-2 text-center">
                        {r.action_renommage ? (
                          <input
                            type="checkbox"
                            checked={selection[r.path]?.renommage ?? false}
                            onChange={e => setSelection(s => ({
                              ...s,
                              [r.path]: { ...s[r.path], renommage: e.target.checked }
                            }))}
                            className="accent-[#3a6a3a] cursor-pointer"
                          />
                        ) : <span className="text-[#222]">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-4 flex gap-3 items-center">
              <button
                onClick={() => setShowModal(true)}
                disabled={pendingCount === 0}
                className="px-4 py-2 bg-[#1a1a1a] border border-[#2a2a2a] rounded text-xs hover:border-[#444] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                Aperçu ({pendingCount} action{pendingCount !== 1 ? 's' : ''})
              </button>
              <button
                onClick={() => setShowModal(true)}
                disabled={executing || pendingCount === 0}
                className="px-4 py-2 bg-[#111a11] border border-[#1e3a1e] rounded text-xs hover:border-[#3a6a3a] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                {executing ? 'Exécution…' : 'Exécuter la sélection'}
              </button>
            </div>

            {execResults && (
              <div className="mt-3 space-y-1">
                {execResults.map((r, i) => (
                  <div key={i} className={`text-xs flex items-center gap-2 ${r.succès ? 'text-[#6a8a6a]' : 'text-[#8a4a4a]'}`}>
                    <span>{r.succès ? '✓' : '✗'}</span>
                    <span className="truncate">{filename(r.path)}</span>
                    {r.erreur && <span className="text-[#6a3a3a]">— {r.erreur}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      {/* ── Section 2 : Doublons ── */}
      <section>
        <h2 className="text-[10px] uppercase tracking-[0.2em] text-[#444] mb-4">Doublons</h2>

        <button
          onClick={loadDuplicates}
          disabled={loadingDups}
          className="px-4 py-2 bg-[#1a1a1a] border border-[#2a2a2a] rounded text-xs hover:border-[#444] disabled:opacity-50 transition-colors"
        >
          {loadingDups ? 'Analyse…' : 'Détecter les doublons'}
        </button>

        {duplicates !== null && (
          <div className="mt-4">
            {duplicates.length === 0 ? (
              <p className="text-xs text-[#3a3a3a]">Aucun doublon détecté.</p>
            ) : (
              <div className="space-y-3">
                {duplicates.map((grp, gi) => (
                  <div key={gi} className="border border-[#1e1e1e] rounded p-3 space-y-1">
                    <div className="text-[10px] text-[#444] mb-2">
                      similarité {(grp.similarité * 100).toFixed(1)}%
                    </div>
                    {grp.groupe.map((p, pi) => (
                      <div key={pi} className="flex items-center gap-3">
                        <span className="text-xs text-[#888] truncate flex-1" title={p}>
                          {filename(p)}
                        </span>
                        <span className="text-[10px] text-[#333] truncate max-w-[200px] hidden sm:block" title={p}>
                          {p}
                        </span>
                        <button
                          onClick={() => openFile(p)}
                          className="text-[10px] text-[#444] hover:text-[#888] shrink-0 transition-colors"
                        >
                          ouvrir
                        </button>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      {/* ── Section 3 : Historique ── */}
      <section>
        <div className="flex items-center gap-4 mb-4">
          <h2 className="text-[10px] uppercase tracking-[0.2em] text-[#444]">Historique</h2>
          <button
            onClick={loadLog}
            className="text-[10px] text-[#333] hover:text-[#666] transition-colors"
          >
            rafraîchir
          </button>
        </div>

        {log.length === 0 ? (
          <p className="text-xs text-[#2a2a2a]">Aucune action enregistrée.</p>
        ) : (
          <div className="space-y-0">
            {log.map(entry => (
              <div
                key={entry.id}
                className={`flex items-center gap-3 border-b border-[#111] py-2 text-xs ${entry.annulé ? 'opacity-30' : ''}`}
              >
                <span className="text-[#333] shrink-0 text-[10px]">
                  {new Date(entry.date).toLocaleString('fr-FR', {
                    day: '2-digit', month: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                  })}
                </span>
                <span className="text-[#444] shrink-0 text-[10px]">{entry.type}</span>
                <span className="text-[#666] truncate flex-1" title={`${entry.source} → ${entry.destination}`}>
                  {filename(entry.source)} → {filename(entry.destination)}
                </span>
                {!entry.annulé && isRecent(entry.date) && (
                  <button
                    onClick={() => undoAction(entry.id)}
                    className="text-[10px] text-[#5a3a3a] hover:text-[#aa5a5a] shrink-0 transition-colors"
                  >
                    annuler
                  </button>
                )}
                {entry.annulé && (
                  <span className="text-[10px] text-[#2a2a2a] shrink-0">annulé</span>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ── Modal aperçu ── */}
      {showModal && (
        <div
          className="fixed inset-0 bg-black/80 flex items-center justify-center z-50"
          onClick={e => { if (e.target === e.currentTarget) setShowModal(false) }}
        >
          <div className="bg-[#0f0f0f] border border-[#2a2a2a] rounded p-6 max-w-2xl w-full mx-4 max-h-[75vh] flex flex-col">
            <h3 className="text-[10px] uppercase tracking-[0.2em] text-[#444] mb-4 shrink-0">
              Aperçu — {pendingCount} action{pendingCount !== 1 ? 's' : ''}
            </h3>

            <div className="flex-1 overflow-y-auto space-y-2 min-h-0">
              {pendingActions.map((a, i) => (
                <div key={i} className="text-xs border-b border-[#1a1a1a] pb-2">
                  <span className="text-[#3a5a3a] mr-2">[{a.type}]</span>
                  <span className="text-[#777]">{filename(a.source)}</span>
                  <span className="text-[#333] mx-2">→</span>
                  <span className="text-[#aaa] break-all">{a.destination}</span>
                </div>
              ))}
            </div>

            <div className="mt-4 flex gap-3 shrink-0 pt-2 border-t border-[#1a1a1a]">
              <button
                onClick={executeActions}
                disabled={executing}
                className="px-4 py-2 bg-[#111a11] border border-[#1e3a1e] rounded text-xs hover:border-[#3a6a3a] disabled:opacity-50 transition-colors"
              >
                {executing ? 'Exécution…' : 'Confirmer et exécuter'}
              </button>
              <button
                onClick={() => setShowModal(false)}
                className="px-4 py-2 bg-[#1a1a1a] border border-[#2a2a2a] rounded text-xs hover:border-[#444] transition-colors"
              >
                Annuler
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
