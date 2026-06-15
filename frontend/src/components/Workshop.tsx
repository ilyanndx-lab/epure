import { useState, useEffect, useRef, useCallback } from 'react'
import {
  Hammer, AlertTriangle, Check, X, RefreshCw, Loader2, Play, Terminal,
  ShieldCheck, FilePlus2, FilePen, Bug, Cpu,
} from 'lucide-react'
import { Badge, Button, Card, Input, Select, Textarea } from './ui'
import { useInstanceConfig } from '../instance'
import { usePersistentState } from '../usePersistentState'

const API = 'http://localhost:8000'
const WS_URL = 'ws://localhost:8000/ws/workshop'

type Engine = 'ollama' | 'claude_sub' | 'claude_gateway' | 'aider'
type Mode = 'headless' | 'terminal'
type Kind = 'new' | 'edit'
type Phase = 'idle' | 'generating' | 'validating' | 'terminal' | 'review' | 'paused' | 'chatting'

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
  // Champ éditable d'erreur/consigne envoyé à « Corriger l'erreur » (persistant :
  // survit au F5 comme le reste de la revue).
  const [feedbackText, setFeedbackText] = usePersistentState<string>('epure.workshop.feedback', '')
  const [revalidating, setRevalidating] = useState(false)
  const [aiderArchitect, setAiderArchitect] = useState(false)
  // Auto-reprise après une pause aider (timeout) : nb max de reprises automatiques.
  const [autoMax, setAutoMax] = usePersistentState<number>('epure.workshop.autoMax', 0)
  const autoLeft = useRef(0)

  // Conversation aider (Plan/Construire) : fil de bulles + saisie + mode + accès lecture.
  const [turns, setTurns] = useState<{ role: 'user' | 'aider', text: string }[]>([])
  const [chatInput, setChatInput] = useState('')
  const [aiderMode, setAiderMode] = useState<'plan' | 'build'>('plan')
  const [grantPath, setGrantPath] = useState('')
  const [grantMsg, setGrantMsg] = useState<string | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  // true pendant une conversation aider → les tokens vont dans `turns` (bulles),
  // pas dans `log` (flux one-shot ollama/claude).
  const chatRef = useRef(false)
  // Ref vers resumeGeneration : permet à bindSocket de la rappeler (auto-reprise)
  // sans dépendance circulaire entre les deux useCallback.
  const resumeRef = useRef<() => void>(() => {})

  // Après un rechargement, la socket est fermée : une phase « en cours » devient
  // obsolète. On bascule sur la revue si du code généré subsiste, sinon idle.
  useEffect(() => {
    if (phase === 'generating' || phase === 'validating' || phase === 'terminal') {
      setPhase(staging ? 'review' : 'idle')
    }
    // au montage uniquement
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Pré-remplit le champ de correction avec les dernières erreurs de validation
  // (l'utilisateur peut ensuite l'éditer / y coller une erreur d'exécution).
  useEffect(() => {
    const errs = [...(report?.errors ?? []), ...(report?.warnings ?? [])]
    if (errs.length) setFeedbackText(errs.join('\n'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report])

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

  // Handlers WS partagés par la génération ET la reprise, pour réagir aux mêmes
  // events (dont 'paused'). Sur 'paused', auto-reprise tant que autoLeft > 0.
  const bindSocket = useCallback((ws: WebSocket, id: string) => {
    ws.onmessage = (ev) => {
      const data = JSON.parse(ev.data)
      if (data.type === 'token') {
        if (chatRef.current) {
          // Conversation : on accumule dans la dernière bulle aider.
          setTurns(prev => {
            const last = prev[prev.length - 1]
            if (last?.role === 'aider') return [...prev.slice(0, -1), { ...last, text: last.text + (data.content ?? '') }]
            return [...prev, { role: 'aider', text: data.content ?? '' }]
          })
        } else {
          setLog(prev => prev + (data.content ?? ''))
        }
      } else if (data.type === 'engine') {
        if (chatRef.current) {
          // Nouveau tour → nouvelle bulle aider (mode/architect en méta visuelle).
          setPhase('chatting')
          setTurns(prev => [...prev, { role: 'aider', text: '' }])
        } else {
          setLog(prev => prev + `[moteur ${data.engine}${data.model ? ' · ' + data.model : ''}${data.architect ? ' · architect' : ''}${data.resumed ? ' · reprise' : ''}]\n`)
        }
      } else if (data.type === 'read_granted') {
        setGrantMsg(data.ok ? `Accès accordé en lecture : ${data.path}` : `Refusé (introuvable ou non autorisé) : ${data.path}`)
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
      } else if (data.type === 'paused') {
        setPhase('paused')
        if (autoLeft.current > 0) { autoLeft.current--; resumeRef.current() }
      } else if (data.type === 'typecheck') {
        const warns: string[] = data.report?.warnings ?? []
        if (warns.length) setReport(prev => (prev ? { ...prev, warnings: [...prev.warnings, ...warns] } : prev))
      }
      // Pas de ws.close() sur "done" : socket gardée pour le {type:"typecheck"} de fond.
    }
    ws.onerror = () => setError('Erreur WebSocket atelier.')
  }, [refreshStaging])

  const startGeneration = useCallback(async (feedback?: string) => {
    setError(null); setApproveResult(null); setLog('')
    chatRef.current = false  // flux one-shot ollama/claude → tokens dans le log
    if (!feedback) { setStaging(null); setReport(null) }
    const id = currentId
    if (!id) { setError('Indiquez un identifiant de module.'); return }
    // En correction (feedback fourni), la consigne EST le feedback : on n'exige
    // pas de description (utile pour « Corriger l'erreur » sur un brouillon repris).
    if (!feedback && !description.trim()) { setError('Décrivez ce que le module doit faire.'); return }

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
    ws.onopen = () => {
      autoLeft.current = autoMax  // budget d'auto-reprises pour CETTE génération
      ws.send(JSON.stringify({ type: 'generate', id, kind, description, engine, mode, ollama_model: (engine === 'ollama' || engine === 'aider') ? (model || null) : null, aider_architect: engine === 'aider' ? aiderArchitect : false, feedback }))
    }
    bindSocket(ws, id)
  }, [currentId, kind, description, engine, mode, model, aiderArchitect, autoMax, bindSocket])

  // Reprend une génération aider en pause. Réutilise la socket si encore ouverte,
  // sinon la rouvre (cas d'un rechargement) en réattachant les mêmes handlers.
  const resumeGeneration = useCallback(() => {
    const id = currentId
    if (!id) return
    setError(null); setApproveResult(null); setPhase('generating')
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resume', id }))
    } else {
      const nws = new WebSocket(WS_URL)
      wsRef.current = nws
      nws.onopen = () => nws.send(JSON.stringify({ type: 'resume', id }))
      bindSocket(nws, id)
    }
  }, [currentId, bindSocket])
  resumeRef.current = resumeGeneration

  // Envoie un tour de conversation aider (workshop_chat). Réutilise la socket si
  // ouverte, sinon la rouvre (cas reload). Pousse la bulle utilisateur.
  const sendChat = useCallback((message: string, chatMode: 'plan' | 'build') => {
    const id = currentId
    if (!id || !message.trim()) return
    chatRef.current = true
    setError(null)
    setTurns(prev => [...prev, { role: 'user', text: message }])
    setChatInput('')
    setPhase('chatting')
    const payload = JSON.stringify({ type: 'workshop_chat', id, message, mode: chatMode, kind, model: model || null, architect: aiderArchitect })
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(payload)
    } else {
      const nws = new WebSocket(WS_URL)
      wsRef.current = nws
      nws.onopen = () => nws.send(payload)
      bindSocket(nws, id)
    }
  }, [currentId, kind, model, aiderArchitect, bindSocket])

  // « Générer » en mode aider : prépare le staging (new/edit) puis lance le 1er
  // tour en mode Plan avec la description comme message.
  const startAiderChat = useCallback(async () => {
    const id = currentId
    if (!id) { setError('Indiquez un identifiant de module.'); return }
    if (!description.trim()) { setError('Décrivez ce que le module doit faire.'); return }
    setError(null); setApproveResult(null); setStaging(null); setReport(null); setTurns([]); setLog('')
    try {
      const url = kind === 'new' ? `${API}/workshop/generate` : `${API}/workshop/${id}/edit`
      const body = kind === 'new' ? { id, engine, mode } : { engine, mode }
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (!res.ok) {
        const e = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        setError(typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail)); return
      }
    } catch { setError('Backend injoignable.'); return }
    // Socket fraîche liée au bon id (évite une closure d'id obsolète).
    wsRef.current?.close(); wsRef.current = null
    sendChat(description, 'plan')
  }, [currentId, description, kind, engine, mode, sendChat])

  // Autorise un dossier/fichier en lecture pour les prochains tours.
  const grantRead = useCallback(() => {
    const id = currentId
    const path = grantPath.trim()
    if (!id || !path) return
    const payload = JSON.stringify({ type: 'grant_read', id, path })
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(payload)
    } else {
      const nws = new WebSocket(WS_URL)
      wsRef.current = nws
      nws.onopen = () => nws.send(payload)
      bindSocket(nws, id)
    }
    setGrantPath('')
  }, [currentId, grantPath, bindSocket])

  // Renvoie le contenu du champ éditable (erreurs de validation pré-remplies, ou
  // erreur d'exécution collée par l'utilisateur) à l'IA. Le staging actuel est
  // CONSERVÉ → reprise, pas de régénération depuis zéro.
  const fixError = useCallback(() => {
    const fb = feedbackText.trim() || error || 'Le module a échoué — corrige-le.'
    void startGeneration(fb)
  }, [feedbackText, error, startGeneration])

  const finishTerminal = useCallback(() => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      setPhase('generating')
      ws.send(JSON.stringify({ type: 'terminal_done', id: currentId }))
    }
  }, [currentId])

  const approve = useCallback(async (force = false) => {
    if (!staging) return
    if (force && !window.confirm(
      "Forcer l'activation malgré la validation échouée ?\n\n"
      + "Le contrôle de sécurité/AST est ignoré : le module peut être cassé ou dangereux. "
      + "Un backup de l'existant est créé. Continuer ?"
    )) return
    try {
      const res = await fetch(`${API}/workshop/${staging.id}/approve${force ? '?force=true' : ''}`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) {
        setError(typeof data.detail === 'object' ? JSON.stringify(data.detail) : (data.detail ?? 'Activation refusée'))
        return
      }
      const sid = staging.id
      setApproveResult(
        `Module « ${sid} » activé${data.backup ? ` (backup créé)` : ''}.`
        + (data.restart_required ? ' ⚠️ routes non montées à chaud — un redémarrage backend peut être nécessaire.' : '')
        + ' Rechargement de l\'interface…'
      )
      wsRef.current?.close()
      // IMPORTANT : on vide l'état de revue PERSISTÉ avant de recharger, sinon la
      // revue (sur un staging maintenant supprimé) réapparaîtrait après le reload.
      setPhase('idle'); setStaging(null); setReport(null); setLog(''); setFeedbackText('')
      // Recharge l'interface pour garantir l'affichage du composant à jour.
      // Le backend a déjà monté les routes à chaud (aucun redémarrage backend).
      // Côté frontend, Vite ne propage PAS fiablement le HMR d'un composant généré
      // (lazy + import.meta.glob) déjà monté lors d'une MODIFICATION (le set de
      // fichiers ne change pas) → on force un rechargement qui ré-importe le
      // composant à jour. L'état du formulaire (description, moteur…) est persisté.
      setTimeout(() => window.location.reload(), 1000)
    } catch { setError('Activation échouée (réseau).') }
  }, [staging])

  const reject = useCallback(async () => {
    if (!staging) return
    await fetch(`${API}/workshop/${staging.id}/reject`, { method: 'POST' }).catch(() => {})
    wsRef.current?.close()
    setPhase('idle'); setStaging(null); setReport(null); setLog(''); setFeedbackText('')
  }, [staging])

  // Re-valide le brouillon actuel SANS régénérer : recalcule le rapport (donc
  // réactive « Approuver » si le code est devenu valide). Utile sur un brouillon
  // repris après F5, ou après une correction manuelle des fichiers en staging.
  const revalidate = useCallback(async () => {
    if (!staging) return
    setRevalidating(true)
    try {
      const res = await fetch(`${API}/workshop/${staging.id}/validate`, { method: 'POST' })
      const data = await res.json()
      if (res.ok && data.report) {
        setReport(data.report)
        setError(null)
        await refreshStaging(staging.id)
      } else {
        setError(typeof data.detail === 'string' ? data.detail : 'Re-validation échouée.')
      }
    } catch {
      setError('Re-validation échouée (réseau).')
    } finally {
      setRevalidating(false)
    }
  }, [staging, refreshStaging])

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
          {engine === 'aider' && (
            <label className="flex items-center gap-2 text-xs text-secondary">
              <input type="checkbox" checked={aiderArchitect}
                onChange={e => setAiderArchitect(e.target.checked)}
                className="accent-[--accent-primary]" />
              Mode architect (v4-pro plan + v4-flash édition)
            </label>
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
            onClick={() => engine === 'aider' ? startAiderChat() : startGeneration()}
            disabled={phase === 'generating' || phase === 'validating' || phase === 'chatting' || engineUnavailable || !currentId || !description.trim()}>
            {phase === 'generating' ? 'Génération…' : phase === 'validating' ? 'Validation…'
              : phase === 'chatting' ? 'Conversation…'
              : engine === 'aider' ? 'Démarrer (Plan)' : 'Générer'}
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

      {/* ── Stream de génération (ollama/claude one-shot) ── */}
      {(phase === 'generating' || phase === 'validating' || (log && phase !== 'review' && phase !== 'paused' && phase !== 'chatting')) && (
        <Card className="max-w-2xl">
          <p className="text-xs text-muted uppercase tracking-wide mb-2 flex items-center gap-2">
            {(phase === 'generating' || phase === 'validating') && <Loader2 size={13} className="animate-spin text-accent2" />}
            {phase === 'validating' ? 'Validation' : 'Génération'}
          </p>
          <pre className="text-xs font-mono text-secondary max-h-56 overflow-y-auto whitespace-pre-wrap">{log || '…'}</pre>
        </Card>
      )}

      {/* ── Conversation aider (Plan / Construire) ── */}
      {phase === 'chatting' && (
        <Card className="max-w-2xl space-y-3">
          <p className="text-sm text-secondary flex items-center gap-2">
            <Bug size={15} className="text-accent2" /> Conversation aider — {currentId}
          </p>

          {/* Fil des bulles */}
          <div className="space-y-2 max-h-80 overflow-y-auto">
            {turns.length === 0 && <p className="text-xs text-muted">Démarrage…</p>}
            {turns.map((t, i) => (
              <div key={i} className={t.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
                <pre className={`text-xs font-mono whitespace-pre-wrap rounded-sm p-2 max-w-[85%] ${
                  t.role === 'user' ? 'bg-accent/10 text-primary' : 'bg-elevated border border-line text-secondary'
                }`}>{t.text || '…'}</pre>
              </div>
            ))}
          </div>

          {/* Mode + saisie */}
          <div className="flex items-center gap-2">
            <Select value={aiderMode} onChange={e => setAiderMode(e.target.value as 'plan' | 'build')}>
              <option value="plan">Plan (discuter)</option>
              <option value="build">Construire</option>
            </Select>
            <Button variant="secondary" size="sm" icon={<Play size={13} />}
              onClick={() => sendChat('Construis maintenant les 3 fichiers selon le plan validé.', 'build')}>
              Construire maintenant
            </Button>
          </div>
          <div className="flex gap-2 items-end">
            <Textarea value={chatInput} onChange={e => setChatInput(e.target.value)} rows={2}
              placeholder="Votre message à aider (questions, précisions, corrections)…" className="flex-1" />
            <Button variant="primary" size="sm" onClick={() => sendChat(chatInput, aiderMode)}
              disabled={!chatInput.trim()}>Envoyer</Button>
          </div>

          {/* Accès lecture */}
          <div className="flex gap-2 items-center">
            <Input value={grantPath} onChange={e => setGrantPath(e.target.value)}
              placeholder="Autoriser en lecture (dossier/fichier, ex : backend/modules/hello)"
              className="flex-1 text-xs py-1.5 font-mono" />
            <Button variant="ghost" size="sm" onClick={grantRead} disabled={!grantPath.trim()}>Autoriser</Button>
          </div>
          {grantMsg && <p className="text-xs text-muted">{grantMsg}</p>}
        </Card>
      )}

      {/* ── En pause (timeout aider) ── */}
      {phase === 'paused' && (
        <Card className="max-w-2xl space-y-3">
          <p className="text-sm text-secondary flex items-center gap-2">
            <AlertTriangle size={15} className="text-warning" /> En pause (délai atteint)
          </p>
          <p className="text-xs text-muted">
            Le travail est conservé. « Continuer » reprend là où aider s'est arrêté (sans tout réécrire).
          </p>
          <pre className="text-xs font-mono text-secondary max-h-56 overflow-y-auto whitespace-pre-wrap bg-elevated border border-line rounded-sm p-2">{log || '…'}</pre>
          <div className="flex items-center gap-3 flex-wrap">
            <Button variant="primary" size="sm" icon={<Play size={13} />} onClick={resumeGeneration}>
              Continuer
            </Button>
            <label className="flex items-center gap-2 text-xs text-secondary">
              Auto-continuer (max
              <Input type="number" min={0} value={String(autoMax)}
                onChange={e => setAutoMax(Math.max(0, parseInt(e.target.value, 10) || 0))}
                className="w-16 text-xs py-1" />
              fois)
            </label>
            <Button variant="ghost" size="sm" icon={<X size={13} />} onClick={reject}>Abandonner</Button>
          </div>
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

          {/* Champ erreur/consigne éditable : prérempli avec les erreurs de
              validation ; collez-y l'erreur vue à l'ouverture du module si besoin.
              Transmis tel quel à l'IA par « Corriger l'erreur ». */}
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-1">
              Erreur / consigne de correction (transmise à l'IA)
            </label>
            <Textarea value={feedbackText} onChange={e => setFeedbackText(e.target.value)} rows={3}
              placeholder="Erreurs de validation pré-remplies — ou collez l'erreur d'exécution / une consigne…"
              className="w-full" />
          </div>

          {/* Actions — jamais d'import/exécution avant approbation explicite */}
          <div className="flex items-center gap-3 flex-wrap">
            <Button variant="primary" icon={<Check size={14} />} onClick={() => approve(false)} disabled={!report?.ok}>
              Approuver & activer
            </Button>
            <Button variant="secondary" size="sm" icon={<Bug size={13} />} onClick={fixError}
              disabled={phase === 'generating' || phase === 'validating'}>
              Corriger l'erreur (reprend le code actuel)
            </Button>
            <Button variant="ghost" size="sm"
              icon={<RefreshCw size={13} className={revalidating ? 'animate-spin' : ''} />}
              onClick={revalidate} disabled={revalidating}>
              Re-valider
            </Button>
            <Button variant="ghost" size="sm" icon={<RefreshCw size={13} />} onClick={() => startGeneration()}>
              Régénérer (repart de zéro)
            </Button>
            <Button variant="ghost" size="sm" icon={<X size={13} />} onClick={reject}>Rejeter</Button>
            {!report?.ok && (
              <Button variant="danger" size="sm" icon={<AlertTriangle size={13} />} onClick={() => approve(true)}>
                Forcer l'activation
              </Button>
            )}
          </div>
          {!report?.ok && (
            <p className="text-xs text-muted">
              Validation échouée — « <strong>Corriger l'erreur</strong> » renvoie le texte ci-dessus à l'IA
              en gardant le code actuel (pas de reprise à zéro) ; « <strong>Re-valider</strong> » recalcule
              après correction ; « <strong>Forcer l'activation</strong> » active malgré tout
              (⚠️ module potentiellement cassé/non sûr, un backup est créé).
            </p>
          )}
        </Card>
      )}
    </main>
  )
}
