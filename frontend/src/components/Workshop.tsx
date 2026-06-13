import { useState, useEffect, useRef, useCallback } from 'react'
import {
  Hammer, AlertTriangle, Check, X, RefreshCw, Loader2, Play, Terminal,
  ShieldCheck, FilePlus2, FilePen,
} from 'lucide-react'
import { Badge, Button, Card, Input, Select, Textarea } from './ui'
import { useInstanceConfig } from '../instance'

const API = 'http://localhost:8000'
const WS_URL = 'ws://localhost:8000/ws/workshop'

type Engine = 'ollama' | 'claude_sub' | 'claude_gateway'
type Mode = 'headless' | 'terminal'
type Kind = 'new' | 'edit'
type Phase = 'idle' | 'generating' | 'validating' | 'terminal' | 'review'

interface EngineInfo { disponible: boolean; raison: string; base_url?: string; model?: string; bin?: string }
interface ModuleRow { id: string; nom: string; core_module?: boolean; status?: string }
interface Report { ok: boolean; errors: string[]; warnings: string[] }
interface Staging {
  id: string
  files: Record<string, string>
  diff: Record<string, string>
  meta: { kind?: Kind; engine?: Engine; status?: string }
  is_core: boolean
}

const FILE_TABS = ['router.py', 'Component.tsx', 'manifest.json'] as const

export default function Workshop() {
  const config = useInstanceConfig()
  const [engines, setEngines] = useState<Record<Engine, EngineInfo> | null>(null)
  const [modules, setModules] = useState<ModuleRow[]>([])

  const [kind, setKind] = useState<Kind>('new')
  const [newId, setNewId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [description, setDescription] = useState('')
  const [engine, setEngine] = useState<Engine>(() => (config.atelier.moteur_defaut as Engine) || 'ollama')
  const [mode, setMode] = useState<Mode>(() => (config.atelier.mode_defaut as Mode) || 'headless')

  const [phase, setPhase] = useState<Phase>('idle')
  const [log, setLog] = useState('')
  const [terminalInfo, setTerminalInfo] = useState<{ cwd?: string; cmd?: string[] } | null>(null)
  const [staging, setStaging] = useState<Staging | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [activeTab, setActiveTab] = useState<string>('router.py')
  const [error, setError] = useState<string | null>(null)
  const [approveResult, setApproveResult] = useState<string | null>(null)

  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    fetch(`${API}/workshop/engines`).then(r => r.json()).then(setEngines).catch(() => {})
    fetch(`${API}/workshop/modules`).then(r => r.json())
      .then((d: { modules: ModuleRow[] }) => setModules(d.modules)).catch(() => {})
    return () => wsRef.current?.close()
  }, [])

  const currentId = kind === 'new' ? newId.trim() : targetId
  const editingCore = kind === 'edit' && modules.find(m => m.id === targetId)?.core_module

  const refreshStaging = useCallback(async (id: string) => {
    try {
      const res = await fetch(`${API}/workshop/staging/${id}`)
      if (res.ok) setStaging(await res.json())
    } catch { /* ignore */ }
  }, [])

  const startGeneration = useCallback(async () => {
    setError(null); setApproveResult(null); setStaging(null); setReport(null); setLog('')
    const id = currentId
    if (!id) { setError('Indiquez un identifiant de module.'); return }
    if (!description.trim()) { setError('Décrivez ce que le module doit faire.'); return }

    // 1) Prépare le staging (création ou édition).
    try {
      const url = kind === 'new' ? `${API}/workshop/generate` : `${API}/workshop/${id}/edit`
      const body = kind === 'new' ? { id, engine, mode } : { engine, mode }
      const res = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
      if (!res.ok) {
        const e = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        setError(typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail)); return
      }
    } catch { setError('Backend injoignable.'); return }

    // 2) Stream la génération via WebSocket.
    setPhase('generating')
    wsRef.current?.close()  // ferme une éventuelle session précédente
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws
    ws.onopen = () => ws.send(JSON.stringify({ type: 'generate', id, kind, description, engine, mode }))
    ws.onmessage = (ev) => {
      const data = JSON.parse(ev.data)
      if (data.type === 'token') {
        setLog(prev => prev + (data.content ?? ''))
      } else if (data.type === 'engine') {
        setLog(prev => prev + `[moteur ${data.engine}${data.model ? ' · ' + data.model : ''}]\n`)
      } else if (data.type === 'terminal_opened') {
        setTerminalInfo({ cwd: data.cwd, cmd: data.cmd })
        setPhase('terminal')
      } else if (data.type === 'file_written') {
        setLog(prev => prev + `\n✓ ${data.path}\n`)
      } else if (data.type === 'error') {
        setError(data.content ?? 'Erreur de génération')
      } else if (data.type === 'validating') {
        setPhase('validating')
      } else if (data.type === 'validated') {
        setReport(data.report)
        void refreshStaging(id).then(() => setPhase('review'))
      } else if (data.type === 'typecheck') {
        // tsc arrive APRÈS la revue : on ajoute ses warnings sans toucher à
        // report.ok (qui gouverne l'approbation).
        const warns: string[] = data.report?.warnings ?? []
        if (warns.length) setReport(prev => (prev ? { ...prev, warnings: [...prev.warnings, ...warns] } : prev))
      }
      // NB : pas de ws.close() sur "done" — on garde la socket ouverte pour
      // recevoir le {type:"typecheck"} de fond. Elle est fermée à l'approbation,
      // au rejet, à une nouvelle génération ou au démontage.
    }
    ws.onerror = () => setError('Erreur WebSocket atelier.')
  }, [currentId, kind, description, engine, mode, refreshStaging])

  const finishTerminal = useCallback(() => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      setPhase('generating')
      ws.send(JSON.stringify({ type: 'terminal_done', id: currentId }))
    }
  }, [currentId])

  const approve = useCallback(async () => {
    if (!staging) return
    try {
      const res = await fetch(`${API}/workshop/${staging.id}/approve`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) {
        setError(typeof data.detail === 'object' ? JSON.stringify(data.detail) : (data.detail ?? 'Activation refusée'))
        return
      }
      setApproveResult(
        `Module activé.${data.backup ? ` Sauvegarde : ${data.backup}.` : ''}` +
        (data.restart_required ? ' Redémarrage du backend requis pour appliquer les changements de routes.' : ' Routes montées à chaud.') +
        ` Composant copié dans src/modules/generated/${staging.id}/ — en dev Vite le charge à chaud ; en build, reconstruisez le frontend.`
      )
      wsRef.current?.close()
      setPhase('idle'); setStaging(null); setReport(null); setLog('')
      fetch(`${API}/workshop/modules`).then(r => r.json()).then((d: { modules: ModuleRow[] }) => setModules(d.modules)).catch(() => {})
    } catch { setError('Activation échouée (réseau).') }
  }, [staging])

  const reject = useCallback(async () => {
    if (!staging) return
    await fetch(`${API}/workshop/${staging.id}/reject`, { method: 'POST' }).catch(() => {})
    wsRef.current?.close()
    setPhase('idle'); setStaging(null); setReport(null); setLog('')
  }, [staging])

  const engineUnavailable = engines ? !engines[engine]?.disponible : false

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 space-y-6">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <Hammer size={18} className="text-accent" /> Atelier de modules
      </h1>

      {/* ── Configuration ── */}
      <Card className="max-w-2xl space-y-4">
        {/* Nouveau / Modifier */}
        <div className="flex gap-2">
          <Button variant={kind === 'new' ? 'primary' : 'ghost'} size="sm"
            icon={<FilePlus2 size={13} />} onClick={() => setKind('new')}>Nouveau</Button>
          <Button variant={kind === 'edit' ? 'primary' : 'ghost'} size="sm"
            icon={<FilePen size={13} />} onClick={() => setKind('edit')}>Modifier</Button>
        </div>

        {kind === 'new' ? (
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-1">Identifiant (minuscules)</label>
            <Input value={newId} onChange={e => setNewId(e.target.value.toLowerCase())}
              placeholder="ex : minuteur" className="w-full text-xs py-1.5 font-mono" />
          </div>
        ) : (
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-1">Module à modifier</label>
            <Select mono value={targetId} onChange={e => setTargetId(e.target.value)} className="w-full">
              <option value="" disabled>Choisir un module…</option>
              {modules.map(m => (
                <option key={m.id} value={m.id}>{m.id}{m.core_module ? ' (core)' : ''}</option>
              ))}
            </Select>
          </div>
        )}

        {editingCore && (
          <div className="flex items-start gap-2 px-3 py-2 rounded-sm border border-warning/40 bg-warning/10">
            <AlertTriangle size={15} className="text-warning shrink-0 mt-0.5" />
            <p className="text-xs text-secondary">
              Vous modifiez un <strong>module core</strong>. Une erreur peut casser l'application.
              Un backup horodaté sera créé et la modification ne sera appliquée qu'après votre validation.
            </p>
          </div>
        )}

        <div>
          <label className="text-xs text-muted uppercase tracking-wide block mb-1">Description</label>
          <Textarea value={description} onChange={e => setDescription(e.target.value)} rows={3}
            placeholder="Décrivez la fonctionnalité du module…" className="w-full" />
        </div>

        {/* Moteur + mode */}
        <div className="flex flex-wrap gap-4 items-end">
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-1">Moteur</label>
            <Select value={engine} onChange={e => setEngine(e.target.value as Engine)}>
              {(['ollama', 'claude_sub', 'claude_gateway'] as Engine[]).map(en => {
                const info = engines?.[en]
                const ok = info?.disponible ?? (en === 'ollama')
                return (
                  <option key={en} value={en} disabled={!ok}>
                    {en === 'ollama' ? 'Ollama (local)' : en === 'claude_sub' ? 'Claude (abonnement)' : 'Claude (passerelle)'}
                    {ok ? '' : ' — indisponible'}
                  </option>
                )
              })}
            </Select>
          </div>
          {engine.startsWith('claude') && (
            <div>
              <label className="text-xs text-muted uppercase tracking-wide block mb-1">Mode</label>
              <Select value={mode} onChange={e => setMode(e.target.value as Mode)}>
                <option value="headless">Headless (streamé)</option>
                <option value="terminal">Terminal (piloté)</option>
              </Select>
            </div>
          )}
          <Button variant="primary" icon={<Play size={14} />}
            onClick={startGeneration}
            disabled={phase === 'generating' || phase === 'validating' || engineUnavailable || !currentId || !description.trim()}>
            {phase === 'generating' ? 'Génération…' : phase === 'validating' ? 'Validation…' : 'Générer'}
          </Button>
        </div>

        {engineUnavailable && engines && (
          <p className="text-xs text-warning">{engines[engine]?.raison}</p>
        )}
        {error && <p className="text-xs text-error whitespace-pre-wrap">{error}</p>}
        {approveResult && <p className="text-xs text-success">{approveResult}</p>}
      </Card>

      {/* ── Terminal mode ── */}
      {phase === 'terminal' && (
        <Card className="max-w-2xl space-y-3">
          <p className="text-sm text-secondary flex items-center gap-2">
            <Terminal size={15} className="text-accent2" /> Session terminal ouverte
          </p>
          {terminalInfo?.cwd && <p className="text-xs font-mono text-muted">cwd : {terminalInfo.cwd}</p>}
          {terminalInfo?.cmd && (
            <pre className="text-xs font-mono text-secondary bg-elevated border border-line rounded-sm p-2 overflow-x-auto">
              {terminalInfo.cmd.join(' ')}
            </pre>
          )}
          <p className="text-xs text-muted">
            Pilotez la session dans le dossier confiné, puis cliquez ci-dessous : l'atelier
            re-scanne le staging et relance la validation.
          </p>
          <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={finishTerminal}>
            J'ai terminé — re-scanner
          </Button>
        </Card>
      )}

      {/* ── Stream de génération ── */}
      {(phase === 'generating' || phase === 'validating' || (log && phase !== 'review')) && (
        <Card className="max-w-2xl">
          <p className="text-xs text-muted uppercase tracking-wide mb-2 flex items-center gap-2">
            {(phase === 'generating' || phase === 'validating') && <Loader2 size={13} className="animate-spin text-accent2" />}
            {phase === 'validating' ? 'Validation' : 'Génération'}
          </p>
          <pre className="text-xs font-mono text-secondary max-h-56 overflow-y-auto whitespace-pre-wrap">{log || '…'}</pre>
        </Card>
      )}

      {/* ── Revue ── */}
      {phase === 'review' && staging && (
        <Card className="max-w-3xl space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold text-primary flex items-center gap-2">
              <ShieldCheck size={15} className={report?.ok ? 'text-success' : 'text-error'} />
              Revue — {staging.id} {staging.is_core && <Badge variant="warning">core</Badge>}
            </p>
            <Badge variant={report?.ok ? 'success' : 'error'}>
              {report?.ok ? 'pending_review' : 'draft (rejeté)'}
            </Badge>
          </div>

          {/* Rapport de validation */}
          {report && (report.errors.length > 0 || report.warnings.length > 0) && (
            <div className="space-y-1">
              {report.errors.map((e, i) => (
                <p key={`e${i}`} className="text-xs text-error flex items-start gap-1.5">
                  <X size={12} className="shrink-0 mt-0.5" />{e}</p>
              ))}
              {report.warnings.map((w, i) => (
                <p key={`w${i}`} className="text-xs text-warning flex items-start gap-1.5">
                  <AlertTriangle size={12} className="shrink-0 mt-0.5" />{w}</p>
              ))}
            </div>
          )}

          {staging.is_core && (
            <div className="flex items-start gap-2 px-3 py-2 rounded-sm border border-warning/40 bg-warning/10">
              <AlertTriangle size={15} className="text-warning shrink-0 mt-0.5" />
              <p className="text-xs text-secondary">
                Modifier un module core peut casser l'app. Vérifiez le diff ci-dessous avant d'approuver.
              </p>
            </div>
          )}

          {/* Onglets fichiers */}
          <div className="flex gap-1.5">
            {FILE_TABS.map(t => (
              <button key={t} onClick={() => setActiveTab(t)}
                className={`px-2.5 py-1 rounded-sm text-xs font-mono transition-colors duration-150 ${
                  activeTab === t ? 'bg-accent/15 text-accent' : 'text-muted hover:text-secondary hover:bg-elevated'
                }`}>{t}</button>
            ))}
          </div>

          {staging.meta.kind === 'edit' && staging.diff?.[activeTab] ? (
            <pre className="text-xs font-mono bg-elevated border border-line rounded-sm p-3 max-h-80 overflow-auto">
              {staging.diff[activeTab].split('\n').map((ln, i) => (
                <div key={i} className={
                  ln.startsWith('+') && !ln.startsWith('+++') ? 'text-success'
                  : ln.startsWith('-') && !ln.startsWith('---') ? 'text-error'
                  : 'text-muted'
                }>{ln || ' '}</div>
              ))}
            </pre>
          ) : (
            <pre className="text-xs font-mono text-secondary bg-elevated border border-line rounded-sm p-3 max-h-80 overflow-auto whitespace-pre-wrap">
              {staging.files[activeTab] || '(vide)'}
            </pre>
          )}

          {/* Actions — jamais d'import/exécution avant approbation explicite */}
          <div className="flex items-center gap-3">
            <Button variant="primary" icon={<Check size={14} />} onClick={approve} disabled={!report?.ok}>
              Approuver & activer
            </Button>
            <Button variant="ghost" size="sm" icon={<X size={13} />} onClick={reject}>Rejeter</Button>
            <Button variant="ghost" size="sm" icon={<RefreshCw size={13} />} onClick={startGeneration}>Régénérer</Button>
          </div>
          {!report?.ok && (
            <p className="text-xs text-muted">Validation échouée — le module reste en brouillon, activation impossible.</p>
          )}
        </Card>
      )}
    </main>
  )
}
