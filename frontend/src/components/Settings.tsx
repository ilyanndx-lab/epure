import { useState, useEffect, useCallback } from 'react'
import {
  Archive, Boxes, Brain, Check, ChevronDown, ChevronUp, Cpu, Eye, EyeOff,
  FolderTree, Gauge, GripVertical, KeyRound, Palette, Plus, RefreshCw,
  RotateCcw, Trash2, User, X,
} from 'lucide-react'
import { Badge, Button, Card, Input, ProgressBar, Select, Toggle } from './ui'
import { useTheme } from '../theme'
import { useInstanceConfig, updateInstance } from '../instance'
import { useModules, resolveIcon } from '../modules'

const API = 'http://localhost:8000'

interface ModelOption { id: string; nom: string; disponible: boolean }

interface ModelsResponse {
  local?: ModelOption[]
  local_npu?: ModelOption[]
  cloud?: { rapide?: ModelOption[]; puissant?: ModelOption[]; long_contexte?: ModelOption[] }
}

interface Profile {
  identité: { niveau: string; établissement: string; objectif: string }
  préférences_interaction: { style: string; ne_pas_faire: string[] }
  forces: string[]
  lacunes_confirmées: string[]
}

interface Session {
  date: string
  matière: string
  fichier: string
  erreurs: string[]
  réussies: number
  ratées: number
  archivée: boolean
}

interface QuotaEntry {
  tokens_input: number
  tokens_output: number
  requests: number
  reset_date: string
  limite: number | null
  type_limite: string
  période: string
  label_limite: string
  utilisé: number
  pourcentage: number | null
}

const PROVIDER_LABELS: Record<string, string> = {
  gemini: 'Google Gemini', groq: 'Groq', cerebras: 'Cerebras',
  nvidia: 'NVIDIA NIM', mistral: 'Mistral',
}

function quotaBarColor(pct: number): 'gradient' | 'warning' | 'error' {
  if (pct < 70) return 'gradient'
  if (pct <= 90) return 'warning'
  return 'error'
}

function formatCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function EditableList({
  items,
  onChange,
  placeholder,
}: {
  items: string[]
  onChange: (items: string[]) => void
  placeholder: string
}) {
  const [draft, setDraft] = useState('')

  const add = () => {
    const v = draft.trim()
    if (!v || items.includes(v)) return
    onChange([...items, v])
    setDraft('')
  }

  return (
    <div className="space-y-1">
      {items.map((item, i) => (
        <div key={i} className="flex items-center gap-2">
          <span className="flex-1 text-xs text-secondary">{item}</span>
          <button
            onClick={() => onChange(items.filter((_, j) => j !== i))}
            title="Retirer"
            className="p-0.5 rounded-sm text-muted hover:text-error transition-colors duration-150"
          >
            <X size={12} />
          </button>
        </div>
      ))}
      <div className="flex gap-2 mt-1">
        <Input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && add()}
          placeholder={placeholder}
          className="flex-1 text-xs py-1"
        />
        <Button variant="ghost" size="sm" icon={<Plus size={13} />} onClick={add} aria-label="Ajouter" />
      </div>
    </div>
  )
}

const defaultProfile: Profile = {
  identité: { niveau: '', établissement: '', objectif: '' },
  préférences_interaction: { style: '', ne_pas_faire: [] },
  forces: [],
  lacunes_confirmées: [],
}

function mergeProfile(raw: Record<string, unknown>): Profile {
  return {
    ...defaultProfile,
    ...raw,
    identité: {
      ...defaultProfile.identité,
      ...((raw.identité as Record<string, string>) ?? {}),
    },
    préférences_interaction: {
      ...defaultProfile.préférences_interaction,
      ...((raw.préférences_interaction as Record<string, unknown>) ?? {}),
    },
    forces: (raw.forces as string[]) ?? [],
    lacunes_confirmées: (raw.lacunes_confirmées as string[]) ?? [],
  }
}

function SectionTitle({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <h2 className="text-sm font-semibold text-primary flex items-center gap-2">
      <span className="text-muted">{icon}</span>
      {children}
    </h2>
  )
}

export default function Settings() {
  const { theme, toggleTheme } = useTheme()
  const config = useInstanceConfig()
  const modules = useModules()
  const [models, setModels] = useState<ModelOption[]>([])
  const [addModuleOpen, setAddModuleOpen] = useState(false)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [selectedDates, setSelectedDates] = useState<string[]>([])

  // Consolidation state
  const [consolidCloud, setConsolidCloud] = useState(false)
  const [consolidating, setConsolidating] = useState(false)
  const [consolidResult, setConsolidResult] = useState<string | null>(null)
  const [consolidLog, setConsolidLog] = useState<Record<string, unknown>[]>([])

  // Quota usage state
  const [quotas, setQuotas] = useState<Record<string, QuotaEntry>>({})
  const [quotasLoading, setQuotasLoading] = useState(false)

  // API keys state
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({
    GEMINI_API_KEY: '', GROQ_API_KEY: '', CEREBRAS_API_KEY: '', MISTRAL_API_KEY: '', NVIDIA_API_KEY: '',
  })
  const [showKey, setShowKey] = useState<Record<string, boolean>>({})
  const [savingKeys, setSavingKeys] = useState(false)
  const [keysMsg, setKeysMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [keyStatus, setKeyStatus] = useState<Record<string, boolean>>({})

  useEffect(() => {
    fetch(`${API}/memory/profile`)
      .then(r => r.json())
      .then((raw: Record<string, unknown>) => setProfile(mergeProfile(raw)))
      .catch(err => console.error('GET /memory/profile:', err))

    fetch(`${API}/memory/sessions`)
      .then(r => r.json())
      .then((d: { sessions: Session[] }) => setSessions(d.sessions))
      .catch(err => console.error('GET /memory/sessions:', err))

    fetch(`${API}/settings/api-keys`)
      .then(r => r.json())
      .then((d: Record<string, boolean>) => setKeyStatus(d))
      .catch(err => console.error('GET /settings/api-keys:', err))

    fetch(`${API}/context`)
      .then(r => r.json())
      .then((d: Record<string, unknown>) => setConsolidCloud(Boolean(d['consolidation_cloud'])))
      .catch(() => {})

    fetch(`${API}/memory/consolidation-log`)
      .then(r => r.json())
      .then((d: { log: Record<string, unknown>[] }) => setConsolidLog(d.log?.slice(0, 10) ?? []))
      .catch(() => {})

    fetch(`${API}/models`)
      .then(r => r.json())
      .then((d: ModelsResponse) => {
        const flat = [
          ...(d.local ?? []),
          ...(d.local_npu ?? []),
          ...(d.cloud?.rapide ?? []),
          ...(d.cloud?.puissant ?? []),
          ...(d.cloud?.long_contexte ?? []),
        ]
        setModels(flat)
      })
      .catch(() => {})

    loadQuotas()
  }, [])

  const addModule = useCallback((id: string) => {
    if (config.modules_activés.includes(id)) return
    void updateInstance({ modules_activés: [...config.modules_activés, id] })
    setAddModuleOpen(false)
  }, [config.modules_activés])

  const removeModule = useCallback((id: string) => {
    void updateInstance({ modules_activés: config.modules_activés.filter(m => m !== id) })
  }, [config.modules_activés])

  const moveModule = useCallback((id: string, dir: -1 | 1) => {
    const arr = [...config.modules_activés]
    const i = arr.indexOf(id)
    const j = i + dir
    if (i < 0 || j < 0 || j >= arr.length) return
    const tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp
    void updateInstance({ modules_activés: arr })
  }, [config.modules_activés])

  const loadQuotas = () => {
    setQuotasLoading(true)
    fetch(`${API}/quota/usage`)
      .then(r => r.json())
      .then((d: Record<string, QuotaEntry>) => setQuotas(d))
      .catch(err => console.error('GET /quota/usage:', err))
      .finally(() => setQuotasLoading(false))
  }

  const resetQuota = useCallback(async (provider: string) => {
    try {
      await fetch(`${API}/quota/reset/${provider}`, { method: 'POST' })
      loadQuotas()
    } catch (err) {
      console.error(`POST /quota/reset/${provider}:`, err)
    }
  }, [])

  const saveProfile = useCallback(async () => {
    if (!profile) return
    setSaving(true)
    setSaveMsg(null)
    try {
      const res = await fetch(`${API}/memory/profile`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(profile),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setSaveMsg('Profil sauvegardé')
      setTimeout(() => setSaveMsg(null), 2000)
    } catch (err) {
      console.error('PUT /memory/profile:', err)
      setSaveMsg('Erreur sauvegarde')
    } finally {
      setSaving(false)
    }
  }, [profile])

  const toggleConsolidCloud = useCallback(async () => {
    const next = !consolidCloud
    setConsolidCloud(next)
    await fetch(`${API}/context/settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ consolidation_cloud: next }),
    }).catch(() => {})
  }, [consolidCloud])

  const consolidateNow = useCallback(async () => {
    setConsolidating(true)
    setConsolidResult(null)
    try {
      const res = await fetch(`${API}/memory/consolidate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ use_cloud: consolidCloud }),
      })
      const data: Record<string, unknown> = await res.json()
      const ch = (data.changements as Record<string, unknown[]>) ?? {}
      const parts: string[] = []
      if ((ch.lacunes_ajoutées as string[])?.length) parts.push(`+${(ch.lacunes_ajoutées as string[]).length} lacune(s)`)
      if ((ch.forces_ajoutées as string[])?.length) parts.push(`+${(ch.forces_ajoutées as string[]).length} force(s)`)
      if (ch.style_màj) parts.push('style mis à jour')
      setConsolidResult(parts.length ? parts.join(' · ') : 'Aucun changement')
      const logRes = await fetch(`${API}/memory/consolidation-log`)
      const logData: { log: Record<string, unknown>[] } = await logRes.json()
      setConsolidLog(logData.log?.slice(0, 10) ?? [])
    } catch {
      setConsolidResult('Erreur')
    } finally {
      setConsolidating(false)
    }
  }, [consolidCloud])

  const saveApiKeys = useCallback(async () => {
    setSavingKeys(true)
    setKeysMsg(null)
    try {
      const body: Record<string, string> = {}
      Object.entries(apiKeys).forEach(([k, v]) => { if (v.trim()) body[k] = v.trim() })
      const res = await fetch(`${API}/settings/api-keys`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const status: Record<string, boolean> = await fetch(`${API}/settings/api-keys`).then(r => r.json())
      setKeyStatus(status)
      setApiKeys({ GEMINI_API_KEY: '', GROQ_API_KEY: '', CEREBRAS_API_KEY: '', MISTRAL_API_KEY: '', NVIDIA_API_KEY: '' })
      setKeysMsg({ ok: true, text: 'Clés sauvegardées' })
      setTimeout(() => setKeysMsg(null), 2000)
    } catch (err) {
      console.error('PUT /settings/api-keys:', err)
      setKeysMsg({ ok: false, text: 'Erreur sauvegarde' })
    } finally {
      setSavingKeys(false)
    }
  }, [apiKeys])

  const archiveSelected = useCallback(async () => {
    if (selectedDates.length === 0) return
    try {
      await fetch(`${API}/memory/sessions/archive`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dates: selectedDates }),
      })
      setSessions(prev =>
        prev.map(s => (selectedDates.includes(s.date) ? { ...s, archivée: true } : s))
      )
      setSelectedDates([])
    } catch (err) {
      console.error('POST /memory/sessions/archive:', err)
    }
  }, [selectedDates])

  // Options du sélecteur de modèle : dédupliquées, avec le modèle actif garanti
  // présent même s'il n'est pas (ou plus) listé par /models.
  const modelOptions: ModelOption[] = (() => {
    const seen = new Map<string, ModelOption>()
    for (const m of models) if (!seen.has(m.id)) seen.set(m.id, m)
    const actif = config.providers.actif
    if (actif && !seen.has(actif)) seen.set(actif, { id: actif, nom: actif, disponible: true })
    return [...seen.values()]
  })()

  if (!profile) {
    return (
      <main className="flex flex-col flex-1 overflow-hidden items-center justify-center">
        <span className="text-sm text-muted">Chargement...</span>
      </main>
    )
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 space-y-6">

      {/* ── Apparence ── */}
      <Card className="max-w-lg space-y-4">
        <SectionTitle icon={<Palette size={15} />}>Apparence</SectionTitle>
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-secondary">Thème</p>
            <p className="text-xs text-muted mt-0.5">
              {theme === 'dark' ? 'Sombre — gris chaud' : 'Clair — crème'}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted">Sombre</span>
            <Toggle checked={theme === 'light'} onChange={toggleTheme} label="Thème clair" />
            <span className="text-xs text-muted">Clair</span>
          </div>
        </div>
      </Card>

      {/* ── Instance ── */}
      <Card className="max-w-lg space-y-5">
        <SectionTitle icon={<Boxes size={15} />}>Instance</SectionTitle>

        {/* Nom affiché */}
        <div>
          <label className="text-xs text-muted uppercase tracking-wide block mb-1">Nom affiché</label>
          <Input
            value={config.nom_affiché}
            onChange={e => void updateInstance({ nom_affiché: e.target.value })}
            className="w-full text-xs py-1.5"
            placeholder="Épure"
          />
        </div>

        {/* Modules visibles — ordonnés, réordonnables, supprimables */}
        {(() => {
          const byId = new Map(modules.map(m => [m.id, m]))
          const ordered = config.modules_activés.filter(id => id !== 'settings')
          const addable = modules.filter(
            m => m.id !== 'settings' && !config.modules_activés.includes(m.id)
          )
          return (
            <div className="space-y-2">
              <div className="flex items-center justify-between relative">
                <label className="text-xs text-muted uppercase tracking-wide">Modules visibles</label>
                <div className="relative">
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={<Plus size={13} />}
                    onClick={() => setAddModuleOpen(v => !v)}
                    disabled={addable.length === 0}
                    aria-label="Ajouter un module"
                  >
                    Ajouter
                  </Button>
                  {addModuleOpen && addable.length > 0 && (
                    <div className="absolute right-0 top-full mt-1 w-60 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20">
                      <p className="px-3 py-2 text-xs text-muted uppercase tracking-wide border-b border-line">
                        Modules présents sur l'appareil
                      </p>
                      {addable.map(m => {
                        const Icon = resolveIcon(m.icon)
                        return (
                          <button
                            key={m.id}
                            onClick={() => addModule(m.id)}
                            className="w-full text-left px-3 py-2 flex items-center gap-2.5 hover:bg-surface transition-colors duration-150"
                          >
                            <Icon size={15} className="text-muted shrink-0" />
                            <span className="flex-1 min-w-0">
                              <span className="block text-sm text-secondary">{m.nom}</span>
                              <span className="block text-xs text-muted truncate">{m.description}</span>
                            </span>
                            {!m.core_module && <Badge variant="neutral">{m.origin}</Badge>}
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
              </div>

              {ordered.length === 0 ? (
                <p className="text-xs text-muted">Aucun module activé. Utilisez « Ajouter ».</p>
              ) : (
                <div className="space-y-1">
                  {ordered.map((id, idx) => {
                    const m = byId.get(id)
                    const Icon = resolveIcon(m?.icon ?? 'Box')
                    const inactive = m?.status !== 'active'
                    return (
                      <div
                        key={id}
                        className="flex items-center gap-2 bg-elevated border border-line rounded-sm px-2 py-1.5"
                      >
                        <GripVertical size={14} className="text-muted/50 shrink-0" />
                        <Icon size={15} className={`shrink-0 ${inactive ? 'text-muted' : 'text-accent'}`} />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm text-secondary truncate">{m?.nom ?? id}</p>
                          {inactive && <p className="text-xs text-warning">désactivé au catalogue</p>}
                        </div>
                        <button
                          onClick={() => moveModule(id, -1)}
                          disabled={idx === 0}
                          title="Monter"
                          className="p-1 rounded-sm text-muted hover:text-secondary disabled:opacity-30 disabled:cursor-not-allowed transition-colors duration-150"
                        >
                          <ChevronUp size={14} />
                        </button>
                        <button
                          onClick={() => moveModule(id, 1)}
                          disabled={idx === ordered.length - 1}
                          title="Descendre"
                          className="p-1 rounded-sm text-muted hover:text-secondary disabled:opacity-30 disabled:cursor-not-allowed transition-colors duration-150"
                        >
                          <ChevronDown size={14} />
                        </button>
                        <button
                          onClick={() => removeModule(id)}
                          title="Retirer de la barre"
                          className="p-1 rounded-sm text-muted hover:text-error transition-colors duration-150"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })()}

        {/* Modèle actif */}
        <div>
          <label className="text-xs text-muted uppercase tracking-wide mb-1 flex items-center gap-1.5">
            <Cpu size={12} /> Modèle actif
          </label>
          <Select
            mono
            value={config.providers.actif}
            onChange={e => void updateInstance({ providers: { actif: e.target.value } })}
            className="w-full"
          >
            {modelOptions.map(m => (
              <option key={m.id} value={m.id} disabled={!m.disponible}>
                {m.nom}{m.disponible ? '' : ' — indisponible'}
              </option>
            ))}
          </Select>
        </div>

        {/* Dossiers de fiches */}
        <div className="space-y-3">
          <label className="text-xs text-muted uppercase tracking-wide flex items-center gap-1.5">
            <FolderTree size={12} /> Dossiers de fiches
          </label>
          <div>
            <p className="text-xs text-muted mb-1">Racine</p>
            <Input
              mono
              key={config.fiches.racine}
              defaultValue={config.fiches.racine}
              onBlur={e => {
                const v = e.target.value
                if (v !== config.fiches.racine) void updateInstance({ fiches: { racine: v } })
              }}
              onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
              className="w-full text-xs py-1.5"
              placeholder="/data/fiches"
            />
          </div>
          <div>
            <p className="text-xs text-muted mb-1">Sous-dossiers surveillés</p>
            <EditableList
              items={config.fiches.watch_folders}
              onChange={items => void updateInstance({ fiches: { watch_folders: items } })}
              placeholder="Ajouter un dossier (ex : Maths)..."
            />
          </div>
          <p className="text-xs text-muted/70">
            Le retrait d'un dossier prend effet au redémarrage du backend.
          </p>
        </div>
      </Card>

      {/* ── Profile ── */}
      <Card className="max-w-lg space-y-5">
        <SectionTitle icon={<User size={15} />}>Profil</SectionTitle>

        <div className="space-y-4">
          {/* Identité */}
          <div className="space-y-3">
            {(['niveau', 'établissement', 'objectif'] as const).map(key => (
              <div key={key}>
                <label className="text-xs text-muted uppercase tracking-wide block mb-1">
                  {key}
                </label>
                <Input
                  value={profile.identité[key]}
                  onChange={e =>
                    setProfile(p => p && {
                      ...p,
                      identité: { ...p.identité, [key]: e.target.value },
                    })
                  }
                  className="w-full text-xs py-1.5"
                />
              </div>
            ))}
          </div>

          {/* Style */}
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-1">
              Style d'interaction
            </label>
            <Input
              value={profile.préférences_interaction.style}
              onChange={e =>
                setProfile(p => p && {
                  ...p,
                  préférences_interaction: {
                    ...p.préférences_interaction,
                    style: e.target.value,
                  },
                })
              }
              className="w-full text-xs py-1.5"
            />
          </div>

          {/* Ne pas faire */}
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-2">
              À éviter
            </label>
            <EditableList
              items={profile.préférences_interaction.ne_pas_faire}
              onChange={items =>
                setProfile(p => p && {
                  ...p,
                  préférences_interaction: {
                    ...p.préférences_interaction,
                    ne_pas_faire: items,
                  },
                })
              }
              placeholder="Ajouter un comportement à éviter..."
            />
          </div>

          {/* Forces */}
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-2">
              Forces
            </label>
            <EditableList
              items={profile.forces}
              onChange={items => setProfile(p => p && { ...p, forces: items })}
              placeholder="Ajouter une force..."
            />
          </div>

          {/* Lacunes confirmées */}
          <div>
            <label className="text-xs text-muted uppercase tracking-wide block mb-2">
              Lacunes confirmées
            </label>
            <EditableList
              items={profile.lacunes_confirmées}
              onChange={items =>
                setProfile(p => p && { ...p, lacunes_confirmées: items })
              }
              placeholder="Ajouter une lacune..."
            />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <Button variant="primary" onClick={saveProfile} disabled={saving}>
            {saving ? 'Sauvegarde...' : 'Sauvegarder'}
          </Button>
          {saveMsg && (
            <span className="text-xs text-success">{saveMsg}</span>
          )}
        </div>
      </Card>

      {/* ── Consolidation ── */}
      <Card className="max-w-lg space-y-4">
        <SectionTitle icon={<Brain size={15} />}>Consolidation mémoire</SectionTitle>

        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-secondary">Consolidation cloud après kholle</p>
            <p className="text-xs text-muted mt-0.5">
              {consolidCloud ? 'Groq Llama 3.3 70B (envoi erreurs + scores uniquement)' : 'LLM local (défaut)'}
            </p>
          </div>
          <Toggle checked={consolidCloud} onChange={toggleConsolidCloud} label="Consolidation cloud" />
        </div>

        <div className="flex items-center gap-3">
          <Button variant="secondary" size="sm" onClick={consolidateNow} disabled={consolidating}>
            {consolidating ? 'Consolidation...' : 'Consolider maintenant'}
          </Button>
          {consolidResult && (
            <span className="text-xs text-success">{consolidResult}</span>
          )}
        </div>

        {consolidLog.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs text-muted uppercase tracking-wide">Historique</p>
            {consolidLog.map((entry, i) => {
              const lacunes = (entry.lacunes_ajoutées as string[]) ?? []
              const forces = (entry.forces_ajoutées as string[]) ?? []
              const summary = [
                lacunes.length ? `+${lacunes.length} lacune` : '',
                forces.length ? `+${forces.length} force` : '',
                entry.style_màj ? 'style' : '',
              ].filter(Boolean).join(' · ') || 'aucun changement'
              return (
                <div key={i} className="flex items-center gap-3 text-xs font-mono border-b border-line last:border-b-0 py-1">
                  <span className="text-muted shrink-0">
                    {String(entry.date ?? '').slice(0, 10)}
                  </span>
                  <span className="text-muted shrink-0">{String(entry.type ?? '')}</span>
                  <span className="text-secondary truncate flex-1">{String(entry.source ?? '')}</span>
                  <span className="text-accent2 shrink-0">{summary}</span>
                </div>
              )
            })}
          </div>
        )}
      </Card>

      {/* ── Quotas & Usage ── */}
      <Card className="max-w-lg space-y-4">
        <div className="flex items-center justify-between">
          <SectionTitle icon={<Gauge size={15} />}>Quotas & Usage</SectionTitle>
          <Button
            variant="ghost"
            size="sm"
            icon={<RefreshCw size={12} className={quotasLoading ? 'animate-spin' : ''} />}
            onClick={loadQuotas}
            disabled={quotasLoading}
          >
            Actualiser
          </Button>
        </div>

        {Object.keys(quotas).length === 0 ? (
          <p className="text-xs text-muted">Aucune donnée d'usage.</p>
        ) : (
          <div className="space-y-4">
            {Object.entries(quotas).map(([provider, q]) => (
              <div key={provider} className="space-y-1">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-secondary font-medium">
                    {PROVIDER_LABELS[provider] ?? provider}
                  </span>
                  <div className="flex items-center gap-2">
                    <span className="text-muted font-mono">
                      {q.pourcentage !== null
                        ? `${formatCount(q.utilisé)} / ${formatCount(q.limite ?? 0)} ${q.type_limite === 'requests' ? 'req' : 'tok'} · ${q.pourcentage}%`
                        : `${formatCount(q.utilisé)} ${q.type_limite === 'requests' ? 'req' : 'tok'} · ${q.label_limite}`}
                    </span>
                    <button
                      onClick={() => resetQuota(provider)}
                      className="p-0.5 rounded-sm text-muted hover:text-secondary transition-colors duration-150"
                      title={`Reset compteurs ${provider}`}
                    >
                      <RotateCcw size={11} />
                    </button>
                  </div>
                </div>
                {q.pourcentage !== null && (
                  <ProgressBar
                    value={Math.max(q.pourcentage, q.utilisé > 0 ? 2 : 0)}
                    color={quotaBarColor(q.pourcentage)}
                  />
                )}
                <div className="flex items-center gap-3 text-xs font-mono text-muted/70">
                  <span>in {formatCount(q.tokens_input)} tok</span>
                  <span>out {formatCount(q.tokens_output)} tok</span>
                  <span>{q.requests} req</span>
                  <span>période {q.période === 'day' ? 'jour' : 'mois'} ({q.reset_date})</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ── API Keys ── */}
      <Card className="max-w-lg space-y-5">
        <SectionTitle icon={<KeyRound size={15} />}>Clés API</SectionTitle>

        {[
          { key: 'GEMINI_API_KEY',   label: 'Google Gemini', placeholder: 'AIza...' },
          { key: 'GROQ_API_KEY',     label: 'Groq',          placeholder: 'gsk_...' },
          { key: 'CEREBRAS_API_KEY', label: 'Cerebras',      placeholder: 'csk-...' },
          { key: 'MISTRAL_API_KEY',  label: 'Mistral',       placeholder: 'sk-...' },
          { key: 'NVIDIA_API_KEY',   label: 'NVIDIA NIM',    placeholder: 'nvapi-...' },
        ].map(({ key, label, placeholder }) => (
          <div key={key} className="space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted uppercase tracking-wide">{label}</span>
              {keyStatus[key] ? (
                <Badge variant="success"><Check size={10} /> configurée</Badge>
              ) : (
                <Badge variant="neutral">non configurée</Badge>
              )}
            </div>
            <div className="flex gap-2">
              <Input
                mono
                type={showKey[key] ? 'text' : 'password'}
                value={apiKeys[key] ?? ''}
                onChange={e => setApiKeys(prev => ({ ...prev, [key]: e.target.value }))}
                placeholder={placeholder}
                className="flex-1 text-xs py-1.5"
              />
              <Button
                variant="ghost"
                size="sm"
                icon={showKey[key] ? <EyeOff size={13} /> : <Eye size={13} />}
                onClick={() => setShowKey(prev => ({ ...prev, [key]: !prev[key] }))}
                aria-label={showKey[key] ? 'Masquer la clé' : 'Voir la clé'}
                className="shrink-0"
              />
            </div>
          </div>
        ))}

        <div className="flex items-center gap-3 pt-1">
          <Button
            variant="primary"
            onClick={saveApiKeys}
            disabled={savingKeys || !Object.values(apiKeys).some(v => v.trim())}
          >
            {savingKeys ? 'Sauvegarde...' : 'Sauvegarder'}
          </Button>
          {keysMsg && (
            <span className={`text-xs ${keysMsg.ok ? 'text-success' : 'text-error'}`}>
              {keysMsg.text}
            </span>
          )}
        </div>
      </Card>

      {/* ── Sessions ── */}
      <Card className="space-y-4">
        <div className="flex items-center justify-between">
          <SectionTitle icon={<Archive size={15} />}>Sessions kholle</SectionTitle>
          {selectedDates.length > 0 && (
            <Button variant="ghost" size="sm" icon={<Archive size={12} />} onClick={archiveSelected}>
              Archiver la sélection ({selectedDates.length})
            </Button>
          )}
        </div>

        {sessions.length === 0 ? (
          <p className="text-xs text-muted">Aucune session enregistrée.</p>
        ) : (
          <div className="space-y-2">
            {[...sessions].reverse().map((s, i) => (
              <Card
                key={i}
                elevated
                className={`space-y-1 ${s.archivée ? 'opacity-50' : ''}`}
              >
                <div className="flex items-start gap-3">
                  {!s.archivée && (
                    <input
                      type="checkbox"
                      checked={selectedDates.includes(s.date)}
                      onChange={e =>
                        setSelectedDates(prev =>
                          e.target.checked
                            ? [...prev, s.date]
                            : prev.filter(d => d !== s.date)
                        )
                      }
                      className="mt-0.5 accent-[--accent-primary] shrink-0"
                    />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs font-mono text-muted">{s.date}</span>
                      <span className="text-xs text-secondary">{s.matière}</span>
                      <Badge variant="success" mono>{s.réussies} ✓</Badge>
                      <Badge variant="error" mono>{s.ratées} ✗</Badge>
                      {s.archivée && (
                        <Badge variant="neutral">archivée</Badge>
                      )}
                    </div>
                    {s.fichier && (
                      <p className="text-xs font-mono text-muted truncate mt-0.5">
                        {s.fichier.split(/[/\\]/).pop()}
                      </p>
                    )}
                    {s.erreurs.length > 0 && (
                      <div className="mt-1 space-y-0.5">
                        {s.erreurs.slice(0, 3).map((err, j) => (
                          <p key={j} className="text-xs text-secondary leading-relaxed">
                            · {err}
                          </p>
                        ))}
                        {s.erreurs.length > 3 && (
                          <p className="text-xs text-muted">
                            +{s.erreurs.length - 3} erreurs
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </Card>
    </main>
  )
}
