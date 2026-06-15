import { useState, useEffect, useRef, useCallback } from 'react'
import {
  Hammer, AlertTriangle, Check, X, RefreshCw, Loader2, Play, Terminal,
  ShieldCheck, FilePlus2, FilePen, Bug, Cpu,
} from 'lucide-react'
import { Badge, Button, Card, Input, Select, Textarea } from './ui'
import { useInstanceConfig } from '../instance'
import { fetchModules } from '../modules'
import { usePersistentState } from '../usePersistentState'

const API = 'http://localhost:8000'
const WS_URL = 'ws://localhost:8000/ws/workshop'

type Engine = 'ollama' | 'claude_sub' | 'claude_gateway' | 'aider'
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

  // Persistés : le formulaire (description, moteur, modèle…) survit au reload
  // complet déclenché par Vite après l'approbation d'un module.
  const [kind, setKind] = usePersistentState<Kind>('epure.workshop.kind', 'new')
  const [newId, setNewId] = usePersistentState<string>('epure.workshop.newId', '')
  const [targetId, setTargetId] = usePersistentState<string>('epure.workshop.targetId', '')
  const [description, setDescription] = usePersistentState<string>('epure.workshop.description', '')
  const [engine, setEngine] = usePersistentState<Engine>('epure.workshop.engine', () => (config.atelier.moteur_defaut as Engine) || 'ollama')
  const [mode, setMode] = usePersistentState<Mode>('epure.workshop.mode', () => (config.atelier.mode_defaut as Mode) || 'headless')
  const [models, setModels] = useState<{ id: string; nom: string }[]>([])
  const [model, setModel] = usePersistentState<string>('epure.workshop.model', '')

  // Revue persistée : le code généré + le rapport survivent à un rechargement.
  const [phase, setPhase] = usePersistentState<Phase>('epure.workshop.phase', 'idle')
  const [log, setLog] = usePersistentState<string>('epure.workshop.log', '')
  const [terminalInfo, setTerminalInfo] = useState<{ cwd?: string; cmd?: string[] } | null>(null)
  const [staging, setStaging] = usePersistentState<Staging | null>('epure.workshop.staging', null)
  const [report, setReport] = usePersistentState<Report | null>('epure.workshop.report', null)
  const [activeTab, setActiveTab] = usePersistentState<string>('epure.workshop.activeTab', 'router.py')
  const [error, setError] = useState<string | null>(null)
  const [approveResult, setApproveResult] = useState<string | null>(null)

  const wsRef = useRef<WebSocket | null>(null)

  // Après un rechargement, la socket est fermée : une phase « en cours » devient
  // obsolète. On bascule sur la revue si du code généré subsiste, sinon idle.
  useEffect(() => {
    if (phase === 'generating' || phase === 'validating' || phase === 'terminal') {
      setPhase(staging ? 'review' : 'idle')
    }
    // au montage uniquement
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    fetch(`${API}/workshop/engines`).then(r => r.json()).then(setEngines).catch(() => {})
    fetch(`${API}/workshop/modules`).then(r => r.json())
      .then((d: { modules: ModuleRow[] }) => setModules(d.modules)).catch(() => {})
    // Modèles disponibles → sélecteur local (sans passer par les Réglages).
    fetch(`${API}/models`).then(r => r.json()).then((d) => {
      const all = [
        ...(d.local ?? []),
        ...(d.local_npu ?? []),
        ...(Object.values(d.cloud ?? {}).flat() as { id: string; nom: string }[]),
      ]
      setModels(all)
      const actif = (config as { providers?: { actif?: string } }).providers?.actif
      setModel(prev => prev || actif || all[0]?.id || '')
    }).catch(() => {})
    return () => wsRef.current?.close()
  }, [])

  const currentId = kind === 'new' ? newId.trim() : targetId
  const editingCore = kind === 'edit' && modules.find(m => m.id === targetId)?.core_module
  const editingSettings = kind === 'edit' && targetId === 'settings'

  const refreshStaging = useCallback(async (id: string) => {
    try {
      const res = await fetch(`${API}/workshop/staging/${id}`)
      if (res.ok) setStaging(await res.json())
    } catch { /* ignore */ }
  }, [])

  const startGeneration = useCallback(async (feedback?: string) => {
    setError(null); setApproveResult(null); setLog('')
    if (!feedback) { setStaging(null); setReport(null) }
    const id = currentId
    if (!id) { setError('Indiquez un identifiant de module.'); return }
    if (!description.trim()) { setError('Décrivez ce que le module doit faire.'); return }

    // 1) Prépare le staging (création ou édition).
    //    En correction d'erreur (feedback), le staging existe déjà — on ne le
    //    réinitialise pas, sinon on perdrait les fichiers à corriger.
    if (!feedback) {
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
    }

    // 2) Stream la génération via WebSocket.
    setPhase('generating')
    wsRef.current?.close()  // ferme une éventuelle session précédente
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws
    ws.onopen = () => ws.send(JSON.stringify({ type: 'generate', id, kind, description, engine, mode, ollama_model: (engine === 'ollama' || engine === 'aider') ? (model || null) : null, feedback }))
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
  }, [currentId, kind, description, engine, mode, model, refreshStaging])

  // Renvoie les erreurs de validation à l'IA pour qu'elle les corrige (même
  // staging, modèle éventuellement changé via le sélecteur).
  const fixError = useCallback(() => {
    const errs = [...(report?.errors ?? []), ...(report?.warnings ?? [])]
    const feedback = errs.length ? errs.join('\n') : (error ?? 'Le module a échoué.')
    void startGeneration(feedback)
  }, [report, error, startGeneration])

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
      // Rafraîchit le cache partagé (Sidebar/App) → le module apparaît sans reload,
      // + la liste locale de l'atelier (avec infos staging).
      void fetchModules()
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

        {editingSettings ? (
          <div className="flex items-start gap-2 px-3 py-2 rounded-sm border border-error/50 bg-error/10">
            <AlertTriangle size={15} className="text-error shrink-0 mt-0.5" />
            <p className="text-xs text-secondary">
              Vous modifiez le module <strong>Réglages</strong> lui-même. Une erreur peut vous
              <strong> verrouiller hors de cet écran</strong> (plus moyen de réactiver des modules ni
              de revenir en arrière depuis l'UI). Un backup horodaté est créé ; en cas de problème,
              restaurez-le depuis backend/modules/_backups/settings/ puis redémarrez le backend.
            </p>
          </div>
        ) : editingCore && (
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
              {(['ollama', 'aider', 'claude_sub', 'claude_gateway'] as Engine[]).map(en => {
                const info = engines?.[en]
                const ok = info?.disponible ?? (en === 'ollama')
                return (
                  <option key={en} value={en} disabled={!ok}>
                    {en === 'ollama' ? 'Ollama (local)'
                      : en === 'aider' ? 'aider (local/cloud)'
                      : en === 'claude_sub' ? 'Claude (abonnement)'
                      : 'Claude (passerelle)'}
                    {ok ? '' : ' — indisponible'}
                  </option>
                )
              })}
            </Select>
          </div>
          {(engine === 'ollama' || engine === 'aider') && (
            <div className="min-w-[12rem]">
              <label className="text-xs text-muted uppercase tracking-wide mb-1 flex items-center gap-1">
                <Cpu size={11} /> Modèle
              </label>
              <Select mono value={model} onChange={e => setModel(e.target.value)} className="w-full">
                {models.length === 0 && <option value="">(modèle par défaut)</option>}
                {models.map(m => (
                  <option key={m.id} value={m.id}>{m.nom}</option>
                ))}
              </Select>
            </div>
          )}
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
            onClick={() => startGeneration()}
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

          {staging.id === 'settings' ? (
            <div className="flex items-start gap-2 px-3 py-2 rounded-sm border border-error/50 bg-error/10">
              <AlertTriangle size={15} className="text-error shrink-0 mt-0.5" />
              <p className="text-xs text-secondary">
                <strong>Module Réglages.</strong> Approuver une version cassée peut vous verrouiller
                hors de cet écran. Vérifiez attentivement le diff ci-dessous avant d'approuver
                (backup dans _backups/settings/).
              </p>
            </div>
          ) : staging.is_core && (
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
            <Button variant="ghost" size="sm" icon={<RefreshCw size={13} />} onClick={() => startGeneration()}>Régénérer</Button>
            {!report?.ok && (
              <Button variant="secondary" size="sm" icon={<Bug size={13} />} onClick={fixError}>
                Corriger l'erreur (renvoyer à l'IA)
              </Button>
            )}
          </div>
          {!report?.ok && (
            <p className="text-xs text-muted">
              Validation échouée — le module reste en brouillon, activation impossible.
              Vous pouvez changer de modèle ci-dessus puis cliquer « Corriger l'erreur » : l'IA
              reçoit les messages d'erreur et tente de les résoudre.
            </p>
          )}
        </Card>
      )}
    </main>
  )
}
