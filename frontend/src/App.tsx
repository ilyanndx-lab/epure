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
import { useVoix } from './voix'

export type EffortLevel = 'direct' | 'low' | 'medium' | 'high' | 'adaptive'
export interface StepConfig { role: string; model: string }

export default function App() {
  const config = useInstanceConfig()
  const modules = useModules()
  // Capacités vocales de CETTE machine. Sur un paquet ARM64 les paquets vocaux ne
  // sont pas installés (cf. voix.ts) : les contrôles doivent disparaître, pas
  // échouer au clic.
  const voix = useVoix()
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
  // DEUX états, pas un. `speakingText` servait aux deux et était posé dès AVANT
  // le fetch : sur un message long l'interface annonçait « lecture... » pendant
  // toute la synthèse. Mesuré côté backend sur le vrai moteur Piper : 0,3 s pour
  // 26 caractères, 14 s pour 3 700, 49 s pour 12 400. L'utilisateur voyait donc
  // une lecture en cours sans entendre quoi que ce soit — ce qui se lit comme une
  // panne et pousse à recliquer, déclenchant une deuxième synthèse aussi longue.
  //
  // `synthesizingText` : de l'envoi de la requête jusqu'au DÉBUT RÉEL de la
  // lecture (ou l'erreur). `speakingText` : seulement quand le navigateur a
  // effectivement commencé à jouer l'audio. Les deux ne sont jamais posés
  // ensemble.
  const [synthesizingText, setSynthesizingText] = useState<string | null>(null)
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

  // Numéro de la demande vocale courante. Une synthèse dure des dizaines de
  // secondes (mesuré) : pendant ce temps l'utilisateur peut cliquer « arrêter »
  // ou lancer la lecture d'un AUTRE message. Sans ce compteur, la réponse de la
  // requête abandonnée finit par arriver et rallume l'audio et l'état d'un texte
  // que plus personne n'attend — d'autant plus visible maintenant que « synthèse »
  // et « lecture » sont distincts. Chaque appel prend un numéro ; à chaque étape
  // asynchrone il vérifie qu'il est encore le dernier, sinon il se retire.
  const demandeVocale = useRef(0)

  const stopSpeech = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    // Les deux : « arrêter » doit aussi annuler l'affichage d'une synthèse en
    // cours, sinon le bouton stop laisse l'interface sur « synthèse... » pour un
    // audio qui n'arrivera jamais.
    demandeVocale.current += 1   // invalide la requête en vol (cf. playSpeech)
    setSynthesizingText(null)
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
    // Synthèse, PAS lecture : rien n'a encore été joué, et ça peut durer une
    // minute. `speakingText` ne sera posé qu'au démarrage réel de l'audio.
    const demande = demandeVocale.current + 1
    demandeVocale.current = demande
    const estCourante = () => demandeVocale.current === demande
    setSynthesizingText(text)
    setSpeakingText(null)
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
      // Arrêté ou remplacé pendant la synthèse : on ne joue rien et on ne touche
      // à aucun état — celui affiché appartient désormais à une autre demande.
      if (!estCourante()) return
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      audioRef.current = audio
      audio.onended = () => {
        URL.revokeObjectURL(url)
        setSpeakingText(null)
        audioRef.current = null
      }
      audio.onerror = () => {
        URL.revokeObjectURL(url)
        setSynthesizingText(null)
        setSpeakingText(null)
        audioRef.current = null
      }
      // LE point de bascule : `playing` est le seul signal qui dise « le
      // navigateur émet du son maintenant ». C'est donc ici — et nulle part
      // avant — que « synthèse » devient « lecture ».
      audio.onplaying = () => {
        if (!estCourante()) return
        setSynthesizingText(null)
        setSpeakingText(text)
      }
      // `play()` rend une Promise, et c'est tout l'enjeu : non attendue, son rejet
      // ne passe PAS par le `catch` ci-dessous (on est déjà sorti du `try` quand
      // elle se règle). Il partait donc en unhandled rejection, et surtout
      // `speakingText` restait posé — l'UI montrait un message en train d'être lu
      // pour toujours, sans qu'aucun son ne sorte et sans rien dans les logs.
      //
      // `err.name` est journalisé explicitement parce que c'est lui qui tranche :
      // `NotAllowedError` = blocage d'autoplay par le navigateur (il faut un geste
      // utilisateur), `NotSupportedError` = le blob n'est pas un audio décodable
      // (donc un problème côté backend, pas côté navigateur), `AbortError` = une
      // autre lecture a démarré entre-temps. Les trois demandent des correctifs
      // opposés, et le message seul ne les distingue pas.
      audio.play().catch((err: unknown) => {
        const nom = err instanceof Error ? err.name : typeof err
        const message = err instanceof Error ? err.message : String(err)
        console.error(`Erreur TTS (play) — ${nom} : ${message}`, err)
        URL.revokeObjectURL(url)
        audioRef.current = null
        if (!estCourante()) return
        // Les DEUX : le rejet arrive avant tout `playing`, donc c'est l'état
        // « synthèse » qui est encore affiché — le laisser posé rendrait
        // l'interface définitivement bloquée sur un travail terminé en échec.
        setSynthesizingText(null)
        setSpeakingText(null)
      })
    } catch (err) {
      console.error('Erreur TTS:', err)
      if (!estCourante()) return
      setSynthesizingText(null)
      setSpeakingText(null)
    }
  }, [confirmerModeleVocal])

  const onAssistantDone = useCallback(
    (text: string) => {
      // `voix.synthese` en plus de `ttsEnabled` : la lecture auto est déclenchée
      // par le backend qui finit de répondre, pas par un clic. Si la capacité
      // disparaît (paquet absent) alors que l'état était resté à `true`, personne
      // ne serait là pour l'empêcher.
      if (ttsEnabled && voix.synthese) playSpeech(text)
    },
    [ttsEnabled, voix.synthese, playSpeech]
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
  //
  // `playSpeech` et `onTtsToggle` sont OMIS quand la synthèse est indisponible, et
  // ça suffit à faire disparaître les contrôles : les composants les gardent déjà
  // derrière un `playSpeech && …` / `onTtsToggle && …` (chat, kholle, ModuleBar).
  // Couper à la source plutôt qu'ajouter une condition par bouton — un module
  // ajouté plus tard hérite du bon comportement sans rien savoir de la voix.
  const sharedProps: SharedModuleProps = {
    onAssistantDone,
    playSpeech: voix.synthese ? playSpeech : undefined,
    stopSpeech,
    synthesizingText,
    speakingText,
    onNavigate: setActiveModule,
    ttsEnabled: voix.synthese ? ttsEnabled : false,
    onTtsToggle: voix.synthese ? () => setTtsEnabled(v => !v) : undefined,
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
