import { useState, useRef, useCallback, useEffect, Suspense } from 'react'
import { Loader2, ZoomIn, ZoomOut, RotateCcw } from 'lucide-react'
import Sidebar from './components/Sidebar'
import ModuleErrorBoundary from './components/ModuleErrorBoundary'
import { useInstanceConfig } from './instance'
import { useModules, orderedModules } from './modules'
import { getModuleDef, type SharedModuleProps } from './modules/registry'
import { usePersistentState } from './usePersistentState'
import { API, apiFetch, ensureToken, setToken } from './api'
import { ATELIER_PRESENT } from './atelier'

export type EffortLevel = 'direct' | 'low' | 'medium' | 'high' | 'adaptive'
export interface StepConfig { role: string; model: string }

export default function App() {
  const config = useInstanceConfig()
  const modules = useModules()
  // Appairage : auto via /pair (localhost). 'forbidden' = accès distant →
  // écran de saisie du code ; 'unreachable' traité comme ok (l'UX « backend
  // injoignable » existante s'applique, apiFetch ré-appairera au retour).
  const [pairing, setPairing] = useState<'pending' | 'ok' | 'forbidden'>('pending')
  useEffect(() => {
    ensureToken().then(r => setPairing(r === 'forbidden' ? 'forbidden' : 'ok'))
  }, [])
  const [activeModule, setActiveModule] = usePersistentState<string>('epure.activeModule', 'chat')
  // Modules déjà visités : on les garde MONTÉS (cachés) pour que leurs tâches
  // (streaming chat, génération…) continuent en arrière-plan quand on change de
  // module. On n'ajoute jamais, on ne retire pas → pas d'interruption.
  const [mountedIds, setMountedIds] = useState<string[]>([activeModule])
  const [ttsEnabled, setTtsEnabled] = useState(false)
  const [speakingText, setSpeakingText] = useState<string | null>(null)
  // Zoom par module : la prop CSS `zoom` reflue le layout (Chromium/Electron) →
  // le contenu agrandi devient scrollable au lieu d'être coupé. Persisté et borné.
  const [zoomByModule, setZoomByModule] = usePersistentState<Record<string, number>>('epure.moduleZoom', {})
  const zoom = zoomByModule[activeModule] ?? 1
  const setZoom = useCallback((next: number) => {
    const z = Math.min(2, Math.max(0.5, Math.round(next * 10) / 10))
    setZoomByModule(prev => ({ ...prev, [activeModule]: z }))
  }, [activeModule, setZoomByModule])

  const audioRef = useRef<HTMLAudioElement | null>(null)

  const stopSpeech = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setSpeakingText(null)
  }, [])

  // Le modèle de synthèse (~77 Mo) n'est plus versionné : il est téléchargé au
  // premier usage de la voix. On demande avant — 77 Mo tirés sans prévenir, sur
  // une connexion de fortune, ce n'est pas acceptable. Le ref évite de reposer
  // la question à chaque phrase : une fois le modèle sur le disque, le backend
  // le sert sans réseau.
  const modeleVocalPret = useRef(false)
  const voixSignalee = useRef(false)

  const confirmerModeleVocal = useCallback(async (): Promise<boolean> => {
    if (modeleVocalPret.current) return true
    try {
      const res = await apiFetch(`${API}/voice/model`)
      // État inconnu (endpoint absent, backend qui redémarre) : on n'empêche pas
      // d'essayer. Bloquer sur une incertitude coûterait la voix à quelqu'un qui
      // a déjà son modèle.
      if (!res.ok) return true
      const etat = await res.json()
      if (etat['présent'] || etat['téléchargement_en_cours']) {
        modeleVocalPret.current = true
        return true
      }
      const mo = etat['taille_attendue_mo'] ?? 77
      const ok = window.confirm(
        `La synthèse vocale doit d'abord télécharger son modèle (~${mo} Mo).\n` +
        `C'est une seule fois : il reste ensuite sur le disque.\n\n` +
        `Lancer le téléchargement ?`
      )
      if (ok) modeleVocalPret.current = true
      return ok
    } catch {
      return true
    }
  }, [])

  const playSpeech = useCallback(async (text: string) => {
    if (!text.trim()) return
    if (!(await confirmerModeleVocal())) return
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setSpeakingText(text)
    try {
      const res = await apiFetch(`${API}/voice/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      })
      if (res.status === 503) {
        // Voix indisponible (hors ligne, empreinte divergente…). Le backend
        // envoie un message utile — le jeter dans la console serait le perdre.
        // Une seule fois par session : la lecture auto (ttsEnabled) rejouerait
        // l'alerte à chaque réponse de l'assistant.
        const detail = await res.json().then(d => d?.detail).catch(() => null)
        if (!voixSignalee.current) {
          voixSignalee.current = true
          window.alert(detail || 'Synthèse vocale indisponible.')
        }
        // Le modèle n'est pas arrivé : reposer la question au prochain essai.
        modeleVocalPret.current = false
        throw new Error(detail || 'Synthèse vocale indisponible')
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      audioRef.current = audio
      audio.onended = () => {
        URL.revokeObjectURL(url)
        setSpeakingText(null)
        audioRef.current = null
      }
      audio.onerror = () => {
        setSpeakingText(null)
        audioRef.current = null
      }
      audio.play()
    } catch (err) {
      console.error('Erreur TTS:', err)
      setSpeakingText(null)
    }
  }, [confirmerModeleVocal])

  const onAssistantDone = useCallback(
    (text: string) => {
      if (ttsEnabled) playSpeech(text)
    },
    [ttsEnabled, playSpeech]
  )

  // Modules réellement accessibles (settings toujours inclus).
  // orderedModules et non un `includes` sur modules_activés : une liste VIDE
  // signifie « tous les modules installés » (cf. sa docstring), pas « aucun ».
  const ordre = orderedModules(modules, config.modules_activés)
  const visibleIds = new Set<string>([
    'settings',
    ...(ATELIER_PRESENT ? ['workshop'] : []),
    ...ordre.map(m => m.id),
  ])

  // Si le module courant devient inaccessible, bascule vers le premier visible.
  useEffect(() => {
    if (!visibleIds.has(activeModule)) {
      const first = ordre.map(m => m.id).find(id => visibleIds.has(id))
      setActiveModule(first ?? 'settings')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeModule, config.modules_activés, modules])

  // Monte le module actif s'il ne l'est pas déjà (et le garde monté ensuite).
  useEffect(() => {
    setMountedIds(prev => (prev.includes(activeModule) ? prev : [...prev, activeModule]))
  }, [activeModule])

  // Props partagées passées à tout module rendu.
  const sharedProps: SharedModuleProps = {
    onAssistantDone,
    playSpeech,
    stopSpeech,
    speakingText,
    onNavigate: setActiveModule,
    ttsEnabled,
    onTtsToggle: () => setTtsEnabled(v => !v),
  }

  const activeHasInterface = !!getModuleDef(activeModule)

  // Zoom au clavier : Ctrl/Cmd + / − / 0 (réinitialiser).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      if (e.key === '+' || e.key === '=') { e.preventDefault(); setZoom(zoom + 0.1) }
      else if (e.key === '-' || e.key === '_') { e.preventDefault(); setZoom(zoom - 0.1) }
      else if (e.key === '0') { e.preventDefault(); setZoom(1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [zoom, setZoom])

  // Appairage : rien tant qu'on ne sait pas (évite un flash), écran de saisie
  // du code si l'accès est distant. Placé après tous les hooks (règle React).
  if (pairing === 'pending') {
    return <div className="h-screen w-full bg-base" />
  }
  if (pairing === 'forbidden') {
    return <PairingGate onSubmit={code => { setToken(code); setPairing('ok') }} />
  }

  return (
    <div className="flex h-screen w-full bg-base text-primary overflow-hidden">
      <Sidebar activeModule={activeModule} onModuleChange={setActiveModule} />
      <div className="relative flex flex-col flex-1 overflow-hidden">
        {/* Tous les modules visités restent montés ; seul l'actif est affiché.
            Leurs tâches (WebSocket, streaming) ne sont donc pas interrompues. */}
        {mountedIds.map(id => {
          const def = getModuleDef(id)
          if (!def) return null
          const C = def.component
          const isActive = id === activeModule
          const z = zoomByModule[id] ?? 1
          return (
            <div
              key={id}
              className="flex flex-col flex-1 min-h-0 overflow-hidden"
              style={{ display: isActive ? 'flex' : 'none' }}
            >
              <ModuleErrorBoundary moduleId={id} onNavigate={setActiveModule}>
                <Suspense
                  fallback={
                    <div className="flex flex-1 items-center justify-center text-muted">
                      <Loader2 size={18} className="animate-spin" />
                    </div>
                  }
                >
                  {/* Conteneur de zoom : `overflow-auto` pour scroller quand le
                      contenu agrandi dépasse ; zoom omis à 1 pour ne rien changer. */}
                  <div
                    className="flex flex-col flex-1 min-h-0 overflow-auto"
                    style={z !== 1 ? { zoom: z } : undefined}
                  >
                    <C {...sharedProps} />
                  </div>
                </Suspense>
              </ModuleErrorBoundary>
            </div>
          )
        })}
        {!activeHasInterface && (
          <div className="flex flex-1 items-center justify-center text-sm text-muted">
            Module « {activeModule} » sans interface frontend
          </div>
        )}
        {activeHasInterface && (
          <div className="absolute bottom-3 right-3 z-20 flex items-center gap-0.5 rounded-lg border border-base bg-base/90 px-1 py-0.5 shadow-lg opacity-40 hover:opacity-100 transition-opacity backdrop-blur-sm">
            <button
              type="button"
              onClick={() => setZoom(zoom - 0.1)}
              disabled={zoom <= 0.5}
              title="Dézoomer (Ctrl -)"
              className="p-1 rounded text-muted hover:text-primary hover:bg-white/5 disabled:opacity-30 disabled:hover:text-muted"
            >
              <ZoomOut size={15} />
            </button>
            <button
              type="button"
              onClick={() => setZoom(1)}
              title="Réinitialiser (Ctrl 0)"
              className="px-1 w-11 text-center text-xs tabular-nums text-muted hover:text-primary"
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              type="button"
              onClick={() => setZoom(zoom + 0.1)}
              disabled={zoom >= 2}
              title="Zoomer (Ctrl +)"
              className="p-1 rounded text-muted hover:text-primary hover:bg-white/5 disabled:opacity-30 disabled:hover:text-muted"
            >
              <ZoomIn size={15} />
            </button>
            <button
              type="button"
              onClick={() => setZoom(1)}
              title="Réinitialiser le zoom"
              className="p-1 rounded text-muted hover:text-primary hover:bg-white/5"
            >
              <RotateCcw size={13} />
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

/** Écran d'appairage (accès depuis un autre poste que la machine hôte). */
function PairingGate({ onSubmit }: { onSubmit: (code: string) => void }) {
  const [code, setCode] = useState('')
  return (
    <div className="flex h-screen w-full items-center justify-center bg-base text-primary">
      <div className="w-[26rem] max-w-[90vw] space-y-4 rounded-xl border border-base p-6">
        <h1 className="text-lg font-semibold">Appairage requis</h1>
        <p className="text-sm text-muted">
          Épure tourne sur un autre ordinateur. Sur celui-ci, ouvre{' '}
          <code className="text-primary">http://localhost:8000/pair</code> dans un
          navigateur, puis colle ici le code affiché (champ « token »).
        </p>
        <input
          value={code}
          onChange={e => setCode(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && code.trim()) onSubmit(code) }}
          placeholder="Code d'appairage"
          className="w-full rounded-lg border border-base bg-transparent px-3 py-2 text-sm outline-none focus:border-white/30"
          autoFocus
        />
        <button
          type="button"
          disabled={!code.trim()}
          onClick={() => onSubmit(code)}
          className="w-full rounded-lg bg-white/10 px-3 py-2 text-sm hover:bg-white/15 disabled:opacity-40"
        >
          Se connecter
        </button>
      </div>
    </div>
  )
}
