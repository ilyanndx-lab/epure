import { useState, useEffect, useCallback } from 'react'

const API = 'http://localhost:8000'

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
          <span className="flex-1 text-xs font-mono text-[#888]">{item}</span>
          <button
            onClick={() => onChange(items.filter((_, j) => j !== i))}
            className="text-xs font-mono text-[#333] hover:text-[#888] transition-colors"
          >
            ✕
          </button>
        </div>
      ))}
      <div className="flex gap-2 mt-1">
        <input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && add()}
          placeholder={placeholder}
          className="flex-1 bg-[#141414] border border-[#242424] rounded px-2 py-1 text-xs font-mono text-[#e0e0e0] placeholder-[#333] focus:outline-none focus:border-[#383838]"
        />
        <button
          onClick={add}
          className="px-2 py-1 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#555] hover:text-[#aaa] transition-colors"
        >
          +
        </button>
      </div>
    </div>
  )
}

export default function Settings() {
  const [profile, setProfile] = useState<Profile | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [selectedDates, setSelectedDates] = useState<string[]>([])

  useEffect(() => {
    fetch(`${API}/memory/profile`)
      .then(r => r.json())
      .then(setProfile)
      .catch(err => console.error('GET /memory/profile:', err))

    fetch(`${API}/memory/sessions`)
      .then(r => r.json())
      .then((d: { sessions: Session[] }) => setSessions(d.sessions))
      .catch(err => console.error('GET /memory/sessions:', err))
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

  if (!profile) {
    return (
      <main className="flex flex-col flex-1 overflow-hidden items-center justify-center">
        <span className="text-xs font-mono text-[#2a2a2a]">Chargement...</span>
      </main>
    )
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 space-y-10">

      {/* ── Profile ── */}
      <section className="max-w-lg space-y-6">
        <p className="text-xs font-mono text-[#444] uppercase tracking-widest">Profil</p>

        <div className="space-y-4">
          {/* Identité */}
          <div className="space-y-3">
            {(['niveau', 'établissement', 'objectif'] as const).map(key => (
              <div key={key}>
                <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest block mb-1">
                  {key}
                </label>
                <input
                  value={profile.identité[key]}
                  onChange={e =>
                    setProfile(p => p && {
                      ...p,
                      identité: { ...p.identité, [key]: e.target.value },
                    })
                  }
                  className="w-full bg-[#141414] border border-[#242424] rounded px-3 py-1.5 text-xs font-mono text-[#e0e0e0] placeholder-[#333] focus:outline-none focus:border-[#383838]"
                />
              </div>
            ))}
          </div>

          {/* Style */}
          <div>
            <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest block mb-1">
              Style d'interaction
            </label>
            <input
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
              className="w-full bg-[#141414] border border-[#242424] rounded px-3 py-1.5 text-xs font-mono text-[#e0e0e0] focus:outline-none focus:border-[#383838]"
            />
          </div>

          {/* Ne pas faire */}
          <div>
            <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest block mb-2">
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
            <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest block mb-2">
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
            <label className="text-[10px] font-mono text-[#333] uppercase tracking-widest block mb-2">
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
          <button
            onClick={saveProfile}
            disabled={saving}
            className="px-5 py-2 bg-[#141414] border border-[#242424] rounded text-xs font-mono text-[#888] hover:border-[#383838] hover:text-[#ccc] disabled:opacity-30 transition-colors"
          >
            {saving ? 'Sauvegarde...' : 'Sauvegarder'}
          </button>
          {saveMsg && (
            <span className="text-xs font-mono text-[#5a9a5a]">{saveMsg}</span>
          )}
        </div>
      </section>

      {/* ── Sessions ── */}
      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <p className="text-xs font-mono text-[#444] uppercase tracking-widest">
            Sessions kholle
          </p>
          {selectedDates.length > 0 && (
            <button
              onClick={archiveSelected}
              className="text-xs font-mono text-[#555] hover:text-[#aaa] transition-colors"
            >
              Archiver la sélection ({selectedDates.length})
            </button>
          )}
        </div>

        {sessions.length === 0 ? (
          <p className="text-xs font-mono text-[#2a2a2a]">Aucune session enregistrée.</p>
        ) : (
          <div className="space-y-2">
            {[...sessions].reverse().map((s, i) => (
              <div
                key={i}
                className={`border rounded px-4 py-3 space-y-1 ${
                  s.archivée ? 'border-[#1a1a1a] opacity-40' : 'border-[#1e1e1e]'
                }`}
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
                      className="mt-0.5 accent-[#555] shrink-0"
                    />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-3 flex-wrap">
                      <span className="text-xs font-mono text-[#666]">{s.date}</span>
                      <span className="text-xs font-mono text-[#888]">{s.matière}</span>
                      <span className="text-xs font-mono text-[#5a9a5a]">
                        {s.réussies}✓
                      </span>
                      <span className="text-xs font-mono text-[#9a5a5a]">
                        {s.ratées}✗
                      </span>
                      {s.archivée && (
                        <span className="text-[10px] font-mono text-[#333]">archivée</span>
                      )}
                    </div>
                    {s.fichier && (
                      <p className="text-[10px] font-mono text-[#2a2a2a] truncate mt-0.5">
                        {s.fichier.split(/[/\\]/).pop()}
                      </p>
                    )}
                    {s.erreurs.length > 0 && (
                      <div className="mt-1 space-y-0.5">
                        {s.erreurs.slice(0, 3).map((err, j) => (
                          <p key={j} className="text-[10px] font-mono text-[#555] leading-relaxed">
                            · {err}
                          </p>
                        ))}
                        {s.erreurs.length > 3 && (
                          <p className="text-[10px] font-mono text-[#333]">
                            +{s.erreurs.length - 3} erreurs
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </main>
  )
}
