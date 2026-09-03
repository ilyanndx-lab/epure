import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { usePersistentState } from '../../usePersistentState'
import { ChevronDown, Brain, Check, X, Circle, Loader2, Sparkles, Send, Play, Square, Globe, RotateCcw } from 'lucide-react'
import { Card, Textarea, Toggle } from '../../components/ui'
import RichMessage from '../../components/RichMessage'
import ModuleBar from '../../components/ModuleBar'
import type { EffortLevel, StepConfig } from '../../App'
import { API, apiFetch, wsUrl } from '../../api'
import { AT_COMMANDS, allSlashCommands, moduleCommands } from './commands'
import ConversationList from './ConversationList'
import { creerConversation, reprendreAncienChat } from './conversations'
import { liste, texte } from '../../normaliser'
import { useModules } from '../../modules'
import { metaAffichable, type MetaAffichable } from './metaMessage'
import { etapesDe, libelleBadgeCitations, resumeTrace, verifieeContreRecherche, type EtapeTrace } from './traceRecherche'

interface MsgStats {
  tps: number
  outputTokens: number
  promptTokens: number
  durationMs: number
}

interface PipelineStepData {
  role: string
  label: string
  model: string
  output: string
  stats?: { tps: number; tokens: number; duration_ms: number }
  status: 'pending' | 'running' | 'done' | 'error'
  errorMsg?: string
}

interface PipelineTotalStats {
  duration_ms: number
  steps: number
  total_tokens: number
}

interface ThinkingBlock {
  steps: PipelineStepData[]
  totalStats?: PipelineTotalStats
  done: boolean
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  stats?: MsgStats
  isError?: boolean
  thinking?: ThinkingBlock
  /**
   * Raisonnement du modele (Ollama : champ `thinking` du flux), distinct du
   * contenu final. Ne pas confondre avec `thinking` juste au-dessus, qui est le
   * deroule des ETAPES DU PIPELINE de l'orchestrateur — deux notions differentes
   * qui s'affichent toutes deux en bloc repliable, d'ou le nom francais pour
   * celle-ci plutot qu'un second `thinking` desambigue par un prefixe.
   *
   * Jamais envoye au modele au tour suivant : le backend ne met que le contenu
   * dans `history` (cf. modules/chat/router.py).
   */
  raisonnement?: string
  /**
   * Instant d'écriture, tel que le SERVEUR l'a posé (ISO local, à la seconde).
   *
   * Optionnel, et il le restera : les messages écrits avant ce champ n'en ont
   * pas, et rien ne permet de le reconstituer. L'interface affiche « non
   * disponible » plutôt que de deviner.
   */
  horodatage?: string
  /**
   * Modèle qui a produit CE message. Présent sur les réponses seulement.
   *
   * Un message tapé par l'utilisateur n'est produit par aucun modèle : son
   * absence ici est normale, pas une donnée manquante. C'est ce qui permet à
   * l'interface de distinguer « pas de modèle par nature » (message utilisateur)
   * de « on ne sait pas » (réponse d'avant ce champ).
   *
   * ⚠️ Ne JAMAIS combler depuis le `modèle` de la conversation : celui-ci dit le
   * dernier modèle utilisé, et il a pu changer plusieurs fois depuis.
   */
  modele?: string
  /**
   * Sources @web RÉELLEMENT citées par CE message — pas ce qui a été
   * récupéré, ce sur quoi la réponse s'appuie. Métadonnée séparée du
   * `content`, jamais un bloc de texte ajouté dedans : un bloc « Sources »
   * dans le contenu repartirait tel quel dans l'historique du prompt au
   * tour suivant, avec ses URLs complètes — exactement ce que le contrat de
   * citation (domaine seulement, cf. `core/websearch.py`) retire du
   * contexte. Rendu identique pendant la génération (événement `done`) et
   * après rechargement (`GET /chat/conversations/{id}`), les deux lisant la
   * même métadonnée persistée (`core/history.py`).
   */
  sources?: SourceCitee[]
  /**
   * Déroulé d'une recherche @web pour CE message — requête envoyée mot pour
   * mot, résultats, exclusions publicitaires, erreurs, citations invalides.
   * Même principe que `sources` : métadonnée séparée de `content`, jamais du
   * texte ajouté dedans (ce serait relu par le modèle au tour suivant). Rendu
   * identique en direct (événement `done`) et après rechargement — même
   * donnée persistée (`core/history.py`), même composant de rendu
   * (`TraceRechercheView`).
   *
   * Absent : ni bug ni recherche vide, juste un tour sans `@web` — ou un
   * message plus ancien que ce champ (cf. CLAUDE.md, convention `sources`).
   */
  traceRecherche?: EtapeTrace[]
}

interface SourceCitee {
  rang: number
  titre: string
  url: string
}

/**
 * `sources` normalisé à CHAQUE frontière `.json()`/WebSocket, comme `liste`
 * et `texte` (`../../normaliser`) : un champ absent ou de forme inattendue
 * ne doit jamais atteindre le `.map()` du rendu.
 */
function sourcesDe(v: unknown): SourceCitee[] {
  return liste<Record<string, unknown>>(v)
    .map(s => ({ rang: Number(s.rang) || 0, titre: texte(s.titre), url: texte(s.url) }))
    .filter(s => s.rang > 0 && s.url)
}

interface ChatProps {
  onAssistantDone?: (text: string) => void
  playSpeech?: (text: string) => void
  stopSpeech?: () => void
  synthesizingText?: string | null
  speakingText?: string | null
  // `string` et non une union fermée : l'union nommait `kholle` et
  // `flashcards`, deux modules du CATALOGUE, dans le type d'un composant du
  // cœur. App.tsx pilote de toute façon `activeModule: string` — la contrainte
  // n'était pas une garantie, seulement un couplage.
  onNavigate?: (module: string) => void
  ttsEnabled?: boolean
  onTtsToggle?: () => void
}

/**
 * Petit menu d'un message : date, heure, et le modèle qui l'a produit.
 *
 * Au CLIC et non au survol, et c'est un choix : le survol déclencherait le menu
 * en traversant la conversation à la souris, et sur un bloc de texte qu'on lit
 * ce serait du bruit permanent. Le clic est aussi ce qui rend la chose
 * atteignable au clavier.
 *
 * Positionné en `absolute` sous l'ancre, avec `z-20` : le message suivant est
 * rendu après, donc au-dessus dans l'ordre de peinture sans lui.
 */
function MenuMeta({ meta, onFermer }: { meta: MetaAffichable; onFermer: () => void }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const auClic = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onFermer()
    }
    const auClavier = (e: KeyboardEvent) => { if (e.key === 'Escape') onFermer() }
    // `mousedown` et non `click` : le clic qui a ouvert ce menu remonterait
    // jusqu'au document et le refermerait aussitôt.
    document.addEventListener('mousedown', auClic)
    document.addEventListener('keydown', auClavier)
    return () => {
      document.removeEventListener('mousedown', auClic)
      document.removeEventListener('keydown', auClavier)
    }
  }, [onFermer])

  return (
    <div ref={ref} role="dialog" aria-label="Détails du message"
         className="absolute z-20 mt-1 bg-elevated border border-line rounded-md shadow-md px-3 py-2 text-xs font-mono whitespace-nowrap">
      <div className="text-muted">date <span className="text-secondary">{meta.date}</span></div>
      <div className="text-muted">heure <span className="text-secondary">{meta.heure}</span></div>
      <div className="text-muted">{meta.libelleModele} <span className="text-secondary">{meta.modele}</span></div>
    </div>
  )
}

/**
 * Distance au-delà de laquelle un clic est en réalité un glisser de sélection.
 *
 * 4 px : assez pour absorber le tremblement d'un vrai clic, assez peu pour
 * qu'un début de sélection compte comme tel.
 */
const SEUIL_GLISSER = 4

/**
 * Ce clic est-il en fait une SÉLECTION de texte ?
 *
 * Le bug corrigé : la bulle entière ouvre le menu au clic, or c'est aussi la
 * zone où l'on sélectionne du texte pour le copier. Sélectionner ouvrait donc le
 * menu, qui recouvrait le texte au moment précis où l'on essayait de l'attraper.
 *
 * Deux garde-fous, parce qu'aucun ne suffit seul :
 *
 * 1. **le curseur a-t-il bougé** entre l'enfoncement et le relâchement — c'est
 *    ce qui attrape le glisser en cours, y compris quand la sélection finit
 *    vide (un glisser dans une marge) ;
 * 2. **une sélection non vide existe-t-elle** dans ce message — ce qui attrape
 *    le double-clic sur un mot, où le curseur n'a pas bougé d'un pixel.
 *
 * Le clic qui EFFACE une sélection existante (cliquer ailleurs pour
 * désélectionner) n'est pas concerné : le navigateur a déjà réduit la sélection
 * quand `click` se déclenche.
 */
function estUneSelectionDepuis(
  depart: { x: number; y: number } | null,
  arrivee: { x: number; y: number },
): boolean {
  if (depart) {
    const dx = Math.abs(arrivee.x - depart.x)
    const dy = Math.abs(arrivee.y - depart.y)
    if (dx > SEUIL_GLISSER || dy > SEUIL_GLISSER) return true
  }
  const selection = window.getSelection()
  return !!selection && !selection.isCollapsed && selection.toString().trim().length > 0
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s % 60)
  return `${m}m${rem}s`
}

/**
 * Raisonnement du modèle, en bloc repliable, pendant qu'il arrive.
 *
 * Pourquoi ça existe : sur `qwen3:8b`, mesuré sur le chemin réel, une question
 * d'arithmétique produisait **584 tokens en 78 s dont le premier caractère
 * visible à 76,5 s** — le raisonnement était reçu chunk par chunk et jeté dans
 * `core/llm.py`, donc le chat restait muet pendant 76 secondes avant de lâcher
 * 14 caractères. Ce bloc rend ces 76 secondes lisibles ; il ne rend pas la
 * réponse plus rapide.
 *
 * Volontairement PAS `ThinkingBlockView` : celui-là déroule les étapes du
 * pipeline de l'orchestrateur (une liste, avec statuts, modèles et stats par
 * étape). Ici c'est un seul flux de texte. Le langage visuel est le même
 * (`Card accent="secondary"`, icône `Brain`, chevron) parce que c'est la même
 * idée pour l'utilisateur ; la structure ne l'est pas.
 *
 * `max-h-52 overflow-y-auto` : le raisonnement fait couramment plusieurs
 * milliers de caractères — sans plafond, il pousse la réponse hors de l'écran
 * au moment précis où elle arrive.
 */
function RaisonnementView({ texte, enCours, collapsed, onToggle }: {
  texte: string
  enCours: boolean
  collapsed: boolean
  onToggle: () => void
}) {
  return (
    <Card accent="secondary" padded={false} className="mb-2 overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-elevated transition-colors duration-150"
      >
        <span className="text-xs text-secondary flex items-center gap-2">
          <Brain size={14} className={`text-accent2 shrink-0 ${enCours ? 'animate-pulse' : ''}`} />
          <span>{enCours ? 'Raisonnement...' : 'Raisonnement'}</span>
        </span>
        <ChevronDown
          size={14}
          className={`text-muted shrink-0 transition-transform duration-150 ${collapsed ? '' : 'rotate-180'}`}
        />
      </button>

      {!collapsed && (
        <div className="border-t border-line px-3 py-2">
          <p className="text-xs text-muted leading-relaxed whitespace-pre-wrap break-words m-0 max-h-52 overflow-y-auto">
            {texte}
            {enCours && <span className="animate-pulse text-accent2">▍</span>}
          </p>
        </div>
      )}
    </Card>
  )
}

function ThinkingBlockView({ thinking, collapsed, onToggle }: {
  thinking: ThinkingBlock
  collapsed: boolean
  onToggle: () => void
}) {
  const total = thinking.totalStats
  const label = total
    ? `${thinking.steps.length} étapes · ${fmtDuration(total.duration_ms)} · ${total.total_tokens} tokens`
    : thinking.steps.length > 0
    ? `${thinking.steps.filter(s => s.status === 'running').length > 0
        ? `étape ${thinking.steps.findIndex(s => s.status === 'running') + 1}/${thinking.steps.length}...`
        : `${thinking.steps.length} étapes`}`
    : 'Réflexion...'

  return (
    <Card accent="secondary" padded={false} className="mt-2 mb-1 overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-elevated transition-colors duration-150"
      >
        <span className="text-xs text-secondary flex items-center gap-2">
          <Brain size={14} className={`text-accent2 shrink-0 ${thinking.done ? '' : 'animate-pulse'}`} />
          <span>Réflexion · {label}</span>
        </span>
        <ChevronDown
          size={14}
          className={`text-muted shrink-0 transition-transform duration-150 ${collapsed ? '' : 'rotate-180'}`}
        />
      </button>

      {!collapsed && (
        <div className="border-t border-line divide-y divide-line">
          {thinking.steps.map((step, i) => (
            <div key={i} className="px-3 py-2">
              <div className="flex items-center gap-2 mb-1">
                <span className={`text-xs font-mono shrink-0 inline-flex items-center gap-1.5 ${
                  step.status === 'done' ? 'text-success'
                  : step.status === 'running' ? 'text-warning animate-pulse'
                  : step.status === 'error' ? 'text-error'
                  : 'text-muted'
                }`}>
                  {step.status === 'done' ? <Check size={12} /> : step.status === 'running' ? <Loader2 size={12} className="animate-spin" /> : step.status === 'error' ? <X size={12} /> : <Circle size={12} />}
                  {' '}{String(i + 1).padStart(2, '0')} {step.label}
                </span>
                <span className="text-xs font-mono text-muted shrink-0">
                  {step.model.split(':').pop()}
                </span>
                {step.stats && (
                  <span className="text-xs font-mono text-muted shrink-0">
                    {step.stats.tps.toFixed(1)} tok/s · {step.stats.tokens} tokens · {fmtDuration(step.stats.duration_ms)}
                  </span>
                )}
              </div>
              {step.errorMsg ? (
                <p className="text-xs font-mono text-error">{step.errorMsg}</p>
              ) : step.output ? (
                <div className="text-sm text-secondary max-h-40 overflow-y-auto">
                  <RichMessage content={step.output} streaming={step.status === 'running'} />
                </div>
              ) : step.status === 'running' ? (
                <span className="text-xs font-mono text-muted animate-pulse">▍</span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

/**
 * Rendu d'UNE étape, dans le panneau déplié.
 *
 * `default` n'est pas une erreur de couverture : le schéma de la trace est
 * délibérément extensible (§1 de la tâche, phase 4 ajoutera `page_recuperee`/
 * `passages_retenus`) — un type non reconnu s'affiche en repli plutôt que de
 * disparaître, pour qu'une étape future reste visible avant même qu'un rendu
 * dédié existe pour elle.
 */
function EtapeTraceView({ etape }: { etape: EtapeTrace }) {
  switch (etape.etape) {
    case 'recherche_debut':
      // LE point central de l'exigence de confidentialité : la requête part
      // ici mot pour mot, jamais résumée ni tronquée par le rendu lui-même
      // (elle peut déjà l'être côté serveur, cf. TRACE_TEXTE_MAX — mais visible
      // ici veut dire visible telle que réellement envoyée).
      return (
        <p className="m-0">
          Requête envoyée ({texte(etape.moteur)}) :{' '}
          <span className="font-mono text-secondary">« {texte(etape.requete)} »</span>
        </p>
      )
    case 'recherche_cache':
      return (
        <p className="m-0">
          Requête <span className="font-mono text-secondary">« {texte(etape.requete)} »</span> servie
          depuis le cache — aucune requête envoyée cette fois.
        </p>
      )
    case 'recherche_filtree':
      return (
        <p className="m-0">
          {Number(etape.nombre_ecarte) || 0} résultat(s) écarté(s) — raison : {texte(etape.raison) || 'inconnue'}.
        </p>
      )
    case 'recherche_resultats': {
      const resultats = liste<Record<string, unknown>>(etape.resultats)
      return (
        <div>
          <p className="m-0">
            {Number(etape.nombre) || 0} résultat(s) via {texte(etape.moteur)} en {Number(etape.ms) || 0} ms
          </p>
          {resultats.length > 0 && (
            <ul className="list-none m-0 mt-1 p-0 space-y-0.5">
              {resultats.map((r, i) => (
                <li key={i} className="truncate">
                  [{Number(r.rang) || i + 1}] {texte(r.titre)} —{' '}
                  <a href={texte(r.url)} target="_blank" rel="noreferrer"
                     className="text-accent2 hover:underline break-all">
                    {texte(r.url)}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )
    }
    case 'recherche_erreur':
      return <p className="m-0 text-error">Échec : {texte(etape.message)}</p>
    case 'citations_invalides': {
      const rangs = liste<number>(etape.rangs)
      const urls = liste<string>(etape.urls)
      // Deux affirmations de force différente (cf. `verifieeContreRecherche`) :
      // « hors sources » quand une vraie recherche dit que ce n'en est pas ;
      // « non vérifié(es) » quand il n'y avait simplement rien à comparer —
      // un badge qui crie au loup à chaque URL de mémoire finirait ignoré.
      const contreRecherche = verifieeContreRecherche(etape)
      return (
        <p className="m-0 text-warning">
          {contreRecherche ? 'Citation hors sources' : 'Lien non vérifié'}
          {rangs.length > 0 && <> — numéro(s) hors liste : {rangs.join(', ')}</>}
          {urls.length > 0 && (
            <> — URL(s) {contreRecherche ? 'non reconnue(s)' : 'non vérifiée(s)'} : {urls.join(', ')}</>
          )}
        </p>
      )
    }
    default:
      return (
        <p className="m-0 font-mono text-[11px] break-all">
          {etape.etape} — {JSON.stringify(etape)}
        </p>
      )
  }
}

/**
 * Panneau de trace @web, replié par défaut — même langage visuel que
 * `RaisonnementView` (`Card accent="secondary"`, chevron), pour la même
 * raison : c'est la même idée pour l'utilisateur (« voici ce qui s'est
 * passé pendant que tu attendais »), donc le même vocabulaire visuel.
 *
 * Sert DEUX usages avec le même composant — la trace transitoire pendant la
 * recherche (avant même que la bulle assistant existe) et la trace finale,
 * persistée, d'un message déjà terminé. « Même rendu » (tâche §3) n'est pas
 * qu'une intention : c'est littéralement le même composant appelé deux fois.
 */
function TraceRechercheView({ etapes, collapsed, onToggle }: {
  etapes: EtapeTrace[]
  collapsed: boolean
  onToggle: () => void
}) {
  const libelleBadge = libelleBadgeCitations(etapes)
  return (
    <Card accent="secondary" padded={false} className="mt-2 mb-1 overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-elevated transition-colors duration-150"
      >
        <span className="text-xs text-secondary flex items-center gap-2 min-w-0">
          <Globe size={14} className="text-accent2 shrink-0" />
          <span className="truncate">{resumeTrace(etapes)}</span>
          {libelleBadge && (
            <span className="shrink-0 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-warning/20 text-warning">
              {libelleBadge}
            </span>
          )}
        </span>
        <ChevronDown
          size={14}
          className={`text-muted shrink-0 transition-transform duration-150 ${collapsed ? '' : 'rotate-180'}`}
        />
      </button>
      {!collapsed && (
        <div className="border-t border-line divide-y divide-line">
          {etapes.map((e, i) => (
            <div key={i} className="px-3 py-2 text-xs text-muted">
              <EtapeTraceView etape={e} />
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export default function Chat({
  onAssistantDone,
  playSpeech,
  stopSpeech,
  synthesizingText,
  speakingText,
  onNavigate,
  ttsEnabled,
  onTtsToggle,
}: ChatProps) {
  // Modules réellement installés : source des commandes `/` d'ouverture.
  const modules = useModules()
  const [effort, setEffort] = usePersistentState<EffortLevel>('epure.chat.effort', 'direct')
  const [pipelineSteps, setPipelineSteps] = useState<StepConfig[]>([])
  /**
   * Les messages ne sont PLUS persistés dans localStorage.
   *
   * Ils l'étaient sous `epure.chat.messages`, et c'était un bug silencieux : au
   * rechargement, l'écran réaffichait la conversation pendant que le backend
   * repartait d'une liste vide (elle vivait dans la fermeture du handler
   * WebSocket). **Le modèle ne voyait plus les tours précédents alors que
   * l'utilisateur les avait sous les yeux**, sans le moindre signe.
   *
   * La source est désormais le disque, côté backend : `GET
   * /chat/conversations/{id}`. Écran et modèle lisent enfin la même chose.
   * L'ancienne clé est reprise une fois puis effacée (étape 7).
   */
  const [messages, setMessages] = useState<Message[]>([])
  /** Seule chose qui reste persistée : QUELLE conversation est ouverte. */
  const [conversationId, setConversationId] = usePersistentState<string>('epure.chat.conversationId', '')
  /** Index du message dont le menu de métadonnées est ouvert, ou `null`. */
  const [menuMetaOuvert, setMenuMetaOuvert] = useState<number | null>(null)
  /** Incrémenté pour forcer `ConversationList` à relire l'index. */
  const [rafraichirConvs, setRafraichirConvs] = useState(0)
  /**
   * Panneau des conversations replié.
   *
   * `usePersistentState` et non le serveur : c'est une préférence d'affichage de
   * CE navigateur, pas un état de l'instance. La faire voyager par
   * `instance_config.json` la rendrait partagée entre deux fenêtres et
   * impliquerait un aller-retour réseau pour un clic sur un chevron.
   */
  const [panneauReplie, setPanneauReplie] = usePersistentState<boolean>(
    'epure.chat.panneauReplie', false)
  const [input, setInput] = usePersistentState<string>('epure.chat.input', '')
  const [connected, setConnected] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)
  const [streamStats, setStreamStats] = useState<{ tps: number; count: number } | null>(null)
  const [collapsedThinking, setCollapsedThinking] = useState<Record<number, boolean>>({})
  /**
   * Repli du bloc de raisonnement, par index de message — et seulement quand
   * l'utilisateur a TRANCHÉ lui-même.
   *
   * Une entrée absente veut dire « automatique » : ouvert tant que le contenu
   * final n'a pas commencé, refermé dès qu'il commence (cf. le rendu). C'est
   * pour ça que le défaut n'est pas `false` — sinon il faudrait un `useEffect`
   * qui referme, qui écraserait le clic de quelqu'un en train de lire.
   */
  const [collapsedRaisonnement, setCollapsedRaisonnement] = useState<Record<number, boolean>>({})
  /**
   * Repli du panneau de trace @web, par index de message. Absent = REPLIÉ
   * (tâche §3) — contrairement à `collapsedRaisonnement`, qui s'ouvre tant
   * que le raisonnement coule : la trace n'a pas cette urgence de lecture.
   */
  const [collapsedTrace, setCollapsedTrace] = useState<Record<number, boolean>>({})
  /**
   * Trace @web du tour EN COURS, avant même qu'un message assistant existe :
   * la recherche a lieu avant le premier token (direct comme pipeline), donc
   * rien à indexer par message pendant qu'elle tourne. Vidée à l'envoi d'un
   * message et à `done`, où la trace définitive est fusionnée sur le message
   * assistant (cf. handler `trace_recherche_etape` et `done`).
   */
  const [traceEnCours, setTraceEnCours] = useState<EtapeTrace[]>([])
  const [traceEnCoursOuverte, setTraceEnCoursOuverte] = useState(false)

  // Recherche web : active = force une recherche avant la réponse.
  // Mode 'once' = réinitialisé après chaque message (défaut, non handicapant) ;
  // 'always' = reste actif jusqu'à désactivation explicite.
  const [webSearch, setWebSearch] = usePersistentState<boolean>('epure.chat.webSearch', false)
  const [webSearchMode, setWebSearchMode] = usePersistentState<'once' | 'always'>('epure.chat.webSearchMode', 'once')
  const [webMenuOpen, setWebMenuOpen] = useState(false)

  /** Position du bouton enfoncé, pour distinguer un clic d'un glisser. */
  const pointerDownRef = useRef<{ x: number; y: number } | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const webMenuRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const lastAssistantRef = useRef('')
  const tokenCountRef = useRef(0)
  const streamStartRef = useRef<number | null>(null)
  const pendingOllamaStatsRef = useRef<{ promptTokens: number; outputTokens: number; evalMs: number } | null>(null)
  const inPipelineRef = useRef(false)
  const pipelineUserMsgIdxRef = useRef(-1)
  // Arrêt : ignore les events de streaming entrants après un stop manuel.
  const cancelledRef = useRef(false)
  // Dernier message envoyé (pour « relancer »).
  const lastSentRef = useRef<Record<string, unknown> | null>(null)

  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(wsUrl('/ws/chat'))
      ws.onopen = () => setConnected(true)
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000) }
      ws.onmessage = (event) => {
        const data = JSON.parse(event.data)

        // Après un arrêt manuel : on ignore les tokens encore en vol, mais on
        // laisse passer done/error pour réinitialiser proprement l'état.
        if (cancelledRef.current && data.type !== 'done' && data.type !== 'error') return

        if (data.type === 'meta_message') {
          // Horodatage du message utilisateur, posé par le serveur (cf. le
          // commentaire du routeur : deux horloges donneraient deux heures pour
          // le même message selon qu'on le regarde avant ou après un rechargement).
          if (data.horodatage || data['modèle']) {
            const h = data.horodatage as string | undefined
            const mo = data['modèle'] as string | undefined
            setMessages(prev => {
              const dernier = prev.length - 1
              if (dernier < 0 || prev[dernier].role !== 'user') return prev
              const copie = [...prev]
              copie[dernier] = {
                ...copie[dernier],
                ...(h ? { horodatage: h } : {}),
                ...(mo ? { modele: mo } : {}),
              }
              return copie
            })
          }
          return
        }

        if (data.type === 'conversation') {
          // Création paresseuse côté serveur : le premier message d'un fil neuf
          // (ou un identifiant devenu inconnu) fait naître la conversation, et
          // le serveur nous dit laquelle. Sans ce recalage, le message suivant
          // repartirait sans identifiant et le client écrirait dans le vide.
          setConversationId(data.id)
          setRafraichirConvs(n => n + 1)
          return
        } else if (data.type === 'titre') {
          // Titrage automatique après le premier tour : la liste doit le montrer
          // sans attendre un rechargement de page.
          setRafraichirConvs(n => n + 1)
          return
        }

        if (data.type === 'pipeline_info') {
          inPipelineRef.current = true
          const steps: PipelineStepData[] = (data.steps ?? []).map((s: { role: string; label: string; model: string }) => ({
            role: s.role,
            label: s.label || s.role,
            model: s.model,
            output: '',
            status: 'pending' as const,
          }))
          const thinking: ThinkingBlock = { steps, done: false }
          setMessages(prev => {
            // Attach thinking to last user message
            const idx = [...prev].reverse().findIndex(m => m.role === 'user')
            if (idx === -1) return prev
            const realIdx = prev.length - 1 - idx
            pipelineUserMsgIdxRef.current = realIdx
            const updated = [...prev]
            updated[realIdx] = { ...updated[realIdx], thinking }
            return updated
          })
          setCollapsedThinking(prev => {
            const idx = pipelineUserMsgIdxRef.current
            return idx >= 0 ? { ...prev, [idx]: false } : prev
          })

        } else if (data.type === 'step_start') {
          const stepIdx: number = data.step
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            thinking.steps = thinking.steps.map((s, i) =>
              i === stepIdx ? { ...s, status: 'running' as const } : s
            )
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'token' && inPipelineRef.current) {
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            const runningIdx = thinking.steps.findIndex(s => s.status === 'running')
            if (runningIdx >= 0) {
              thinking.steps = thinking.steps.map((s, i) =>
                i === runningIdx ? { ...s, output: s.output + data.content } : s
              )
            }
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'step_end') {
          const stepIdx: number = data.step
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            thinking.steps = thinking.steps.map((s, i) =>
              i === stepIdx ? {
                ...s,
                output: data.output ?? s.output,
                stats: data.stats,
                status: 'done' as const,
              } : s
            )
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'step_error') {
          const stepIdx: number = data.step
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            if (msgIdx < 0 || !prev[msgIdx]?.thinking) return prev
            const updated = [...prev]
            const thinking = { ...updated[msgIdx].thinking! }
            thinking.steps = thinking.steps.map((s, i) =>
              i === stepIdx ? { ...s, status: 'error' as const, errorMsg: data.message } : s
            )
            updated[msgIdx] = { ...updated[msgIdx], thinking }
            return updated
          })

        } else if (data.type === 'pipeline_done') {
          inPipelineRef.current = false
          const finalOutput: string = data.final_output ?? ''
          const totalStats: PipelineTotalStats = data.total_stats
          setMessages(prev => {
            const msgIdx = pipelineUserMsgIdxRef.current
            const updated = [...prev]
            if (msgIdx >= 0 && updated[msgIdx]?.thinking) {
              const thinking = { ...updated[msgIdx].thinking!, done: true, totalStats }
              updated[msgIdx] = { ...updated[msgIdx], thinking }
            }
            if (finalOutput) {
              updated.push({ role: 'assistant', content: finalOutput })
              lastAssistantRef.current = finalOutput
            }
            return updated
          })
          setCollapsedThinking(prev => {
            const idx = pipelineUserMsgIdxRef.current
            return idx >= 0 ? { ...prev, [idx]: true } : prev
          })

        } else if (data.type === 'reasoning' && !inPipelineRef.current) {
          // Même aiguillage et même condition d'accumulation que `token` juste en
          // dessous : le raisonnement se colle au message assistant en cours,
          // celui-là même qui recevra ensuite le contenu. Les deux vivent donc
          // sur UN message, ce qui est ce que l'utilisateur voit.
          //
          // Mesuré sur qwen3:8b (3 formes de prompt) : la séquence est toujours
          // `thinking×N → content×N`, sans retour en arrière ni chunk portant les
          // deux. Ce code ne s'appuie PAS là-dessus — trois prompts sur un modèle
          // ne prouvent pas le cas général. Si du raisonnement revenait après du
          // contenu, il s'ajouterait au même bloc (replié), sans rien perdre et
          // sans réordonner la réponse.
          //
          // `lastAssistantRef` n'est PAS touché : il alimente la lecture à voix
          // haute et `onAssistantDone`. Faire lire le raisonnement à voix haute
          // serait absurde.
          setMessages(prev => {
            const last = prev[prev.length - 1]
            if (last?.role === 'assistant' && !last.thinking) {
              return [...prev.slice(0, -1),
                      { ...last, raisonnement: (last.raisonnement ?? '') + data.content }]
            }
            return [...prev, { role: 'assistant', content: '', raisonnement: data.content }]
          })

        } else if (data.type === 'token' && !inPipelineRef.current) {
          setMessages(prev => {
            const last = prev[prev.length - 1]
            if (last?.role === 'assistant' && !last.thinking) {
              const next = last.content + data.content
              lastAssistantRef.current = next
              return [...prev.slice(0, -1), { ...last, content: next }]
            }
            lastAssistantRef.current = data.content
            return [...prev, { role: 'assistant', content: data.content }]
          })
          tokenCountRef.current += 1
          if (streamStartRef.current === null) streamStartRef.current = Date.now()
          const elapsed = (Date.now() - (streamStartRef.current ?? Date.now())) / 1000
          if (elapsed > 0) setStreamStats({ tps: tokenCountRef.current / elapsed, count: tokenCountRef.current })

        } else if (data.type === 'trace_recherche_etape') {
          // Étape de recherche @web EN DIRECT (core/websearch.py, callback
          // `on_etape`) — remplit le panneau PENDANT la recherche, pas
          // seulement après (tâche §2). Indépendant de `inPipelineRef` et de
          // `messages` : la recherche a toujours lieu AVANT le premier token,
          // direct comme pipeline, donc aucune bulle assistant n'existe
          // encore forcément à ce stade. Fusionnée dans le message définitif
          // à `done`, comme `sources`.
          setTraceEnCours(prev => [...prev, data.etape as EtapeTrace])

        } else if (data.type === 'stats') {
          pendingOllamaStatsRef.current = {
            promptTokens: data.prompt_tokens as number,
            outputTokens: data.output_tokens as number,
            evalMs: data.eval_duration_ms as number,
          }

        } else if (data.type === 'done') {
          const pending = pendingOllamaStatsRef.current
          let finalStats: MsgStats | null = null
          if (pending && pending.outputTokens > 0 && pending.evalMs > 0) {
            finalStats = {
              tps: pending.outputTokens / (pending.evalMs / 1000),
              outputTokens: pending.outputTokens,
              promptTokens: pending.promptTokens,
              durationMs: pending.evalMs,
            }
          } else {
            const count = tokenCountRef.current
            const dur = streamStartRef.current !== null ? (Date.now() - streamStartRef.current) / 1000 : 0
            if (count > 0 && dur > 0) {
              finalStats = { tps: count / dur, outputTokens: count, promptTokens: 0, durationMs: Math.round(dur * 1000) }
            }
          }
          if (finalStats) {
            const s = finalStats
            setMessages(prev => {
              const last = prev[prev.length - 1]
              if (last?.role === 'assistant' && !last.thinking) return [...prev.slice(0, -1), { ...last, stats: s }]
              return prev
            })
          }
          // Métadonnées de la réponse, telles que le serveur vient de les écrire.
          // Évite de relire la conversation entière après chaque tour, et garde
          // l'heure affichée identique à celle du disque.
          //
          // `sources`/`trace_recherche` suivent le même chemin, pour la même
          // raison : sans ce merge, ils n'apparaîtraient qu'après un F5
          // (relecture de `GET /chat/conversations/{id}`), pas pendant la
          // génération — les deux doivent montrer la même chose, ils lisent
          // la même métadonnée persistée (`core/history.py`).
          const sourcesRecues = sourcesDe(data.sources)
          const traceRecue = etapesDe(data.trace_recherche)
          if (data.horodatage || data['modèle'] || sourcesRecues.length || traceRecue.length) {
            const h = data.horodatage as string | undefined
            const mo = data['modèle'] as string | undefined
            setMessages(prev => {
              const dernier = prev.length - 1
              if (dernier < 0 || prev[dernier].role !== 'assistant') return prev
              const copie = [...prev]
              copie[dernier] = {
                ...copie[dernier],
                ...(h ? { horodatage: h } : {}),
                ...(mo ? { modele: mo } : {}),
                ...(sourcesRecues.length ? { sources: sourcesRecues } : {}),
                ...(traceRecue.length ? { traceRecherche: traceRecue } : {}),
              }
              return copie
            })
          }
          // La trace TRANSITOIRE a fait son office (elle s'est affichée
          // pendant la recherche) ; la trace définitive vit désormais sur le
          // message lui-même, fusionnée juste au-dessus.
          setTraceEnCours([])
          setTraceEnCoursOuverte(false)
          pendingOllamaStatsRef.current = null
          setStreaming(false)
          setStreamStats(null)
          tokenCountRef.current = 0
          streamStartRef.current = null
          onAssistantDone?.(lastAssistantRef.current)
          lastAssistantRef.current = ''
          inPipelineRef.current = false
          cancelledRef.current = false

        } else if (data.type === 'error') {
          inPipelineRef.current = false
          cancelledRef.current = false
          setMessages(prev => [...prev, { role: 'assistant', content: data.content, isError: true }])
          setStreaming(false)
          setStreamStats(null)
          tokenCountRef.current = 0
          streamStartRef.current = null
          pendingOllamaStatsRef.current = null
          lastAssistantRef.current = ''
          setTraceEnCours([])
          setTraceEnCoursOuverte(false)
        }
      }
      wsRef.current = ws
    }
    connect()
    return () => wsRef.current?.close()
  }, [onAssistantDone])

  /**
   * Reprise UNIQUE de ce qui était à l'écran au moment de la mise à jour.
   *
   * Le chantier n'a rien à migrer côté serveur ; la seule chose qui
   * disparaîtrait est `localStorage['epure.chat.messages']`, que ce composant ne
   * lit plus. On la reverse en conversation, une fois, puis la clé est effacée.
   *
   * `dejaTente` protège du double montage de `StrictMode` en développement, qui
   * exécuterait l'effet deux fois et créerait deux conversations reprises. La
   * ref plutôt qu'un état : elle ne doit provoquer aucun rendu.
   */
  const repriseTenteeRef = useRef(false)
  useEffect(() => {
    if (conversationId || repriseTenteeRef.current) return
    repriseTenteeRef.current = true
    void (async () => {
      const id = await reprendreAncienChat()
      if (id) setConversationId(id)
    })()
  }, [conversationId, setConversationId])

  /**
   * Charge les messages d'une conversation depuis le DISQUE.
   *
   * C'est ici que se joue la correction du bug silencieux : avant, l'écran
   * repartait de `localStorage` et le backend d'une liste vide. Les deux lisent
   * désormais le même fichier.
   *
   * Toutes les frontières `.json()` sont normalisées (`liste`, `texte`) — un 401
   * avant appairage ou un 404 sur une conversation supprimée ailleurs rendent un
   * corps qui n'a pas de champ `messages`, et un `as` le laisserait passer
   * jusqu'au `.map()` du rendu.
   */
  useEffect(() => {
    if (!conversationId) { setMessages([]); return }
    let annule = false
    void (async () => {
      try {
        const res = await apiFetch(`${API}/chat/conversations/${conversationId}`)
        if (annule) return
        if (res.status === 404) {
          // Supprimée depuis un autre onglet : on repart à vide plutôt que
          // d'afficher une conversation fantôme.
          setConversationId('')
          setMessages([])
          return
        }
        if (!res.ok) return
        const d = await res.json() as Record<string, unknown>
        if (annule) return
        setMessages(liste<Record<string, unknown>>(d.messages).map(m => {
          const role = texte(m.role) === 'assistant' ? 'assistant' as const : 'user' as const
          const horodatage = texte(m['horodatage'])
          const modele = texte(m['modèle'])
          const sources = sourcesDe(m['sources'])
          const traceRecherche = etapesDe(m['trace_recherche'])
          // Les champs ABSENTS restent absents : `texte()` rend `''`, qu'on ne
          // recopie pas. Un `horodatage: ''` se distinguerait mal d'une vraie
          // valeur vide, et l'interface doit pouvoir dire « non disponible ».
          return {
            role,
            content: texte(m.content),
            ...(horodatage ? { horodatage } : {}),
            ...(modele ? { modele } : {}),
            ...(sources.length ? { sources } : {}),
            ...(traceRecherche.length ? { traceRecherche } : {}),
          }
        }))
      } catch { /* backend qui démarre : la liste reste vide */ }
    })()
    return () => { annule = true }
  }, [conversationId, setConversationId])

  const ouvrirConversation = useCallback((id: string) => {
    if (id === conversationId) return
    setConversationId(id)
    setMessages([])          // évite d'afficher l'ancien fil pendant le chargement
    setStreaming(false)
  }, [conversationId, setConversationId])

  /**
   * Nouvelle conversation : on la crée EXPLICITEMENT côté serveur.
   *
   * On ne se contente pas de vider `conversationId` : côté backend, « pas
   * d'identifiant » veut dire « poursuis ce que fait cette connexion » — le
   * message suivant repartirait donc dans le fil précédent.
   */
  const nouvelleConversation = useCallback(async () => {
    try {
      const id = await creerConversation()
      setConversationId(id)
      setMessages([])
      setRafraichirConvs(n => n + 1)
    } catch { /* le backend répondra au premier message : rien de bloquant */ }
  }, [setConversationId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Ferme le menu de recherche web au clic extérieur.
  useEffect(() => {
    if (!webMenuOpen) return
    const onDown = (e: MouseEvent) => {
      if (webMenuRef.current && !webMenuRef.current.contains(e.target as Node)) {
        setWebMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [webMenuOpen])

  // ── Autocomplete ──────────────────────────────────────────────────────────

  const suggestions = useMemo(() => {
    if (input.includes(' ')) return []
    if (input.startsWith('@')) return AT_COMMANDS.filter(c => c.trigger.startsWith(input))
    // Les commandes `/` incluent une entrée par module INSTALLÉ : on ne propose
    // jamais d'ouvrir quelque chose qui n'est pas là.
    if (input.startsWith('/')) return allSlashCommands(modules).filter(c => c.trigger.startsWith(input))
    return []
  }, [input, modules])

  useEffect(() => { setSelectedSuggestion(0) }, [suggestions])

  const applySuggestion = useCallback((trigger: string) => {
    setInput(trigger + ' ')
  }, [])

  // ── Skill handlers ────────────────────────────────────────────────────────

  const pushMsg = (role: Message['role'], content: string) =>
    setMessages(prev => [...prev, { role, content }])

  const streamSSE = useCallback(async (userText: string) => {
    pushMsg('user', userText)
    setStreaming(true)
    try {
      const res = await apiFetch(`${API}/skills/résumé`, { method: 'POST' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        pushMsg('assistant', `[erreur: ${(err as { detail?: string }).detail ?? res.status}]`)
        return
      }
      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue
          try {
            const ev = JSON.parse(part.slice(6))
            if (ev.type === 'token') {
              setMessages(prev => {
                const last = prev[prev.length - 1]
                if (last?.role === 'assistant') {
                  return [...prev.slice(0, -1), { ...last, content: last.content + ev.content }]
                }
                return [...prev, { role: 'assistant', content: ev.content }]
              })
            } else if (ev.type === 'error') {
              pushMsg('assistant', `[erreur: ${ev.content}]`)
            }
          } catch { /* skip */ }
        }
      }
    } catch {
      pushMsg('assistant', '[erreur réseau]')
    } finally {
      setStreaming(false)
    }
  }, [])

  const handleMémoire = useCallback(async (userText: string) => {
    pushMsg('user', userText)
    try {
      const res = await apiFetch(`${API}/memory/context`)
      const data = await res.json() as { context: string }
      pushMsg('assistant', data.context)
    } catch {
      pushMsg('assistant', '[erreur lecture mémoire]')
    }
  }, [])

  const handleModèle = useCallback(async (userText: string, nom: string) => {
    pushMsg('user', userText)
    try {
      await apiFetch(`${API}/context/settings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 'modèle_actif': nom }),
      })
      pushMsg('assistant', `Modèle → ${nom}`)
    } catch {
      pushMsg('assistant', '[erreur changement modèle]')
    }
  }, [])

  const handleLacunes = useCallback(async (userText: string) => {
    pushMsg('user', userText)
    try {
      const res = await apiFetch(`${API}/memory/lacunes`)
      const data = await res.json() as {
        lacunes: string[]
        erreurs_recentes: { date: string; erreur: string }[]
      }
      const lines: string[] = []
      if (data.lacunes.length > 0) {
        lines.push('LACUNES CONFIRMÉES')
        data.lacunes.forEach(l => lines.push(`  · ${l}`))
      } else {
        lines.push('Aucune lacune confirmée.')
      }
      if (data.erreurs_recentes.length > 0) {
        lines.push('')
        lines.push('ERREURS RÉCENTES (7j)')
        data.erreurs_recentes.forEach(e => lines.push(`  · [${e.date}] ${e.erreur}`))
      }
      pushMsg('assistant', lines.join('\n'))
    } catch {
      pushMsg('assistant', '[erreur lecture lacunes]')
    }
  }, [])

  const handleNavigate = useCallback(
    (userText: string, moduleId: string, label: string, param?: string) => {
      pushMsg('user', userText)
      onNavigate?.(moduleId)
      pushMsg('assistant', `→ ${label}${param ? ` — ${param}` : ''}`)
    },
    [onNavigate]
  )

  // ── Send ──────────────────────────────────────────────────────────────────

  // Envoi d'un message « normal » (hors commandes /…) — factorisé pour être
  // réutilisé par « relancer ».
  const sendUserText = useCallback((rawText: string) => {
    if (!connected) return
    cancelledRef.current = false

    let cleanText = rawText
    let ragOverride: string | undefined
    let strictOverride = false
    let webSearchOverride = webSearch

    let again = true
    while (again) {
      again = false
      if (cleanText === '@cours' || cleanText.startsWith('@cours ')) {
        ragOverride = 'all'; cleanText = cleanText.replace(/^@cours\s*/, '').trim(); again = true
      } else if (cleanText === '@strict' || cleanText.startsWith('@strict ')) {
        strictOverride = true; cleanText = cleanText.replace(/^@strict\s*/, '').trim(); again = true
      } else if (cleanText === '@web' || cleanText.startsWith('@web ')) {
        webSearchOverride = true; cleanText = cleanText.replace(/^@web\s*/, '').trim(); again = true
      }
    }

    pushMsg('user', rawText)
    setStreaming(true)
    tokenCountRef.current = 0
    streamStartRef.current = null
    pendingOllamaStatsRef.current = null
    setStreamStats(null)
    inPipelineRef.current = false
    pipelineUserMsgIdxRef.current = -1
    setTraceEnCours([])
    setTraceEnCoursOuverte(false)

    const wsMsg: Record<string, unknown> = { role: 'user', content: cleanText || rawText, effort }
    // Vide au tout premier message : le serveur ouvre alors une conversation et
    // nous renvoie son identifiant (`{"type": "conversation"}`). Côté serveur,
    // « pas d'identifiant » veut dire « poursuis ce que fait cette connexion »,
    // jamais « recommence » — on ne risque donc pas un fil neuf par message.
    if (conversationId) wsMsg.conversation_id = conversationId
    if (effort !== 'direct' && pipelineSteps.length > 0) wsMsg.steps = pipelineSteps
    if (ragOverride) wsMsg.rag_override = ragOverride
    if (strictOverride) wsMsg.strict_override = true
    if (webSearchOverride) wsMsg.web_search_override = true
    lastSentRef.current = wsMsg
    wsRef.current?.send(JSON.stringify(wsMsg))

    if (webSearch && webSearchMode === 'once') setWebSearch(false)
  }, [connected, conversationId, effort, pipelineSteps, webSearch, webSearchMode, pushMsg, setWebSearch])

  const send = useCallback(async () => {
    const rawText = input.trim()
    if (!rawText || streaming) return
    setInput('')

    if (rawText.startsWith('/')) {
      const [cmd, ...argParts] = rawText.slice(1).trim().split(/\s+/)
      const arg = argParts.join(' ')

      // Ouverture d'un module INSTALLÉ. Résolue avant le switch, et sur la
      // liste réelle : `/kholle` n'existe que si kholle est là. Avant, deux
      // `case` en dur répondaient toujours et faisaient naviguer vers un
      // module absent.
      const cible = moduleCommands(modules).find(
        c => c.trigger.slice(1).toLowerCase() === cmd?.toLowerCase()
      )
      if (cible) {
        const id = cible.trigger.slice(1)
        handleNavigate(rawText, id, modules.find(m => m.id === id)?.nom ?? id, arg || undefined)
        return
      }

      switch (cmd?.toLowerCase()) {
        case 'résumé':
          await streamSSE(rawText)
          return
        case 'modèle':
          if (arg) await handleModèle(rawText, arg)
          else { pushMsg('user', rawText); pushMsg('assistant', 'Usage : /modèle <nom>') }
          return
        case 'lacunes':
          await handleLacunes(rawText)
          return
        case 'direct': {
          if (!arg) {
            pushMsg('user', rawText)
            pushMsg('assistant', 'Usage : /direct [message] — envoie sans orchestrateur')
            return
          }
          if (!connected) return
          cancelledRef.current = false
          pushMsg('user', rawText)
          setStreaming(true)
          tokenCountRef.current = 0
          streamStartRef.current = null
          pendingOllamaStatsRef.current = null
          setStreamStats(null)
          inPipelineRef.current = false
          setTraceEnCours([])
          setTraceEnCoursOuverte(false)
          wsRef.current?.send(JSON.stringify({
            role: 'user', content: arg, effort: 'direct',
            ...(conversationId ? { conversation_id: conversationId } : {}),
          }))
          return
        }
      }
    }

    if (rawText === '@mémoire' || rawText.startsWith('@mémoire ')) {
      await handleMémoire(rawText)
      return
    }

    // Message normal : délégué à sendUserText (réutilisé par « relancer »).
    sendUserText(rawText)
  }, [
    input, connected, streaming, sendUserText, modules,
    streamSSE, handleMémoire, handleModèle, handleLacunes, handleNavigate,
  ])

  // ── Stop & relancer ─────────────────────────────────────────────────────────

  const stop = useCallback(() => {
    if (!streaming) return
    // Arrêt côté client : on cesse d'afficher les tokens et on débloque l'UI.
    // (Le backend termine sa génération en silence ; ses tokens sont ignorés
    // grâce à cancelledRef, et le 'done' final réinitialise l'état.)
    cancelledRef.current = true
    setStreaming(false)
    setStreamStats(null)
    inPipelineRef.current = false
  }, [streaming])

  const relancer = useCallback(() => {
    if (streaming || !connected) return
    const lastUser = [...messages].reverse().find(m => m.role === 'user')
    if (lastUser) sendUserText(lastUser.content)
  }, [streaming, connected, messages, sendUserText])

  const canResume = !streaming && messages.some(m => m.role === 'user')

  // ── Keyboard ──────────────────────────────────────────────────────────────

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (suggestions.length > 0) {
      if (e.key === 'Escape') { setInput(''); e.preventDefault(); return }
      if (e.key === 'ArrowUp') { setSelectedSuggestion(i => Math.max(0, i - 1)); e.preventDefault(); return }
      if (e.key === 'ArrowDown') { setSelectedSuggestion(i => Math.min(suggestions.length - 1, i + 1)); e.preventDefault(); return }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault()
        applySuggestion(suggestions[selectedSuggestion].trigger)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-1 overflow-hidden">
      <ConversationList
        courante={conversationId}
        onOuvrir={ouvrirConversation}
        onNouvelle={() => void nouvelleConversation()}
        rafraichir={rafraichirConvs}
        replie={panneauReplie}
        onBasculerRepli={() => setPanneauReplie(v => !v)}
      />
    <main className="flex flex-col flex-1 overflow-hidden">
      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-5">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center gap-2 h-full text-muted select-none">
            <Sparkles size={16} />
            <span className="text-sm">En attente d'un message</span>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex group ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`relative max-w-[78%] cursor-pointer ${
                msg.role === 'user'
                  ? 'px-4 py-3 rounded-lg bg-elevated border border-line text-sm leading-relaxed text-primary'
                  : 'text-sm leading-relaxed text-secondary'
              }`}
              role="button"
              tabIndex={0}
              aria-label="Détails du message"
              title="Détails du message"
              onMouseDown={e => { pointerDownRef.current = { x: e.clientX, y: e.clientY } }}
              onClick={e => {
                const depart = pointerDownRef.current
                pointerDownRef.current = null
                if (estUneSelectionDepuis(depart, { x: e.clientX, y: e.clientY })) return
                setMenuMetaOuvert(v => (v === i ? null : i))
              }}
              onKeyDown={e => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  setMenuMetaOuvert(v => (v === i ? null : i))
                }
              }}
            >
              {menuMetaOuvert === i && (
                <MenuMeta
                  meta={metaAffichable(msg.horodatage, msg.modele, msg.role === 'user')}
                  onFermer={() => setMenuMetaOuvert(null)}
                />
              )}
              {msg.role === 'user' ? (
                <>
                  <p className="whitespace-pre-wrap break-words m-0">{msg.content}</p>
                  {msg.thinking && (
                    <ThinkingBlockView
                      thinking={msg.thinking}
                      collapsed={collapsedThinking[i] ?? false}
                      onToggle={() => setCollapsedThinking(prev => ({ ...prev, [i]: !prev[i] }))}
                    />
                  )}
                </>
              ) : msg.isError ? (
                <p className="text-xs text-error whitespace-pre-wrap">{msg.content}</p>
              ) : (
                <>
                  {msg.raisonnement && (
                    <RaisonnementView
                      texte={msg.raisonnement}
                      // « En cours » = le raisonnement coule encore, c'est-à-dire
                      // qu'aucun contenu final n'a commencé sur ce message et
                      // qu'on est bien sur le dernier, en streaming.
                      enCours={streaming && i === messages.length - 1 && !msg.content}
                      // Le repli AUTOMATIQUE : ouvert tant qu'il n'y a pas de
                      // contenu, refermé dès le premier caractère de réponse —
                      // « refermé/remplacé dès que le vrai contenu commence ».
                      // Un clic de l'utilisateur (entrée présente dans
                      // `collapsedRaisonnement`) l'emporte et n'est plus écrasé :
                      // sans ça, quelqu'un qui ouvre le bloc pour relire le
                      // raisonnement se le voit refermer au token suivant.
                      collapsed={collapsedRaisonnement[i] ?? msg.content.length > 0}
                      onToggle={() => setCollapsedRaisonnement(prev => ({
                        ...prev,
                        [i]: !(prev[i] ?? msg.content.length > 0),
                      }))}
                    />
                  )}
                  <RichMessage content={msg.content} streaming={streaming && i === messages.length - 1} />
                </>
              )}
              {msg.role === 'assistant' && i === messages.length - 1 && streaming && streamStats && (
                <div className="mt-1 text-xs font-mono text-muted/70">
                  {streamStats.tps.toFixed(1)} tok/s · {streamStats.count} tokens
                </div>
              )}
              {msg.role === 'assistant' && msg.stats && (
                <div className="mt-1 text-xs font-mono text-muted/70">
                  {msg.stats.tps.toFixed(1)} tok/s · {msg.stats.durationMs}ms · {msg.stats.promptTokens}in / {msg.stats.outputTokens}out tokens
                </div>
              )}
              {msg.role === 'assistant' && msg.traceRecherche && msg.traceRecherche.length > 0 && (
                <TraceRechercheView
                  etapes={msg.traceRecherche}
                  collapsed={collapsedTrace[i] ?? true}
                  onToggle={() => setCollapsedTrace(prev => ({ ...prev, [i]: !(prev[i] ?? true) }))}
                />
              )}
              {msg.role === 'assistant' && msg.sources && msg.sources.length > 0 && (
                <div className="mt-2 text-xs text-muted space-y-0.5">
                  <div className="font-medium text-secondary">Sources</div>
                  {msg.sources.map(s => (
                    <div key={s.rang} className="truncate">
                      [{s.rang}] {s.titre} —{' '}
                      <a
                        href={s.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-accent2 hover:underline break-all"
                      >
                        {s.url}
                      </a>
                    </div>
                  ))}
                </div>
              )}
              {msg.role === 'assistant' && playSpeech && (
                <div className="mt-2 flex">
{/* Trois états. Le bouton restait sur « Lire » pendant toute la
                      synthèse — jusqu'à 49 s sur un message long : rien ne
                      signalait que le clic avait été pris en compte, et recliquer
                      lançait une deuxième synthèse aussi longue. L'icône reste
                      cliquable pendant la synthèse pour pouvoir l'abandonner. */}
                  <button
                    onClick={() =>
                      synthesizingText === msg.content || speakingText === msg.content
                        ? stopSpeech?.()
                        : playSpeech(msg.content)
                    }
                    className={`transition-colors duration-150
                      [@media(pointer:fine)]:opacity-0 [@media(pointer:fine)]:group-hover:opacity-100
                      [@media(pointer:coarse)]:opacity-100
                      ${synthesizingText === msg.content || speakingText === msg.content
                        ? 'text-accent2 hover:text-accent2-hover'
                        : 'text-muted hover:text-secondary'}`}
                    title={
                      synthesizingText === msg.content
                        ? 'Synthèse en cours — cliquer pour abandonner'
                        : speakingText === msg.content
                          ? 'Arrêter'
                          : 'Lire'
                    }
                  >
                    {synthesizingText === msg.content
                      ? <Loader2 size={13} className="animate-spin" />
                      : speakingText === msg.content
                        ? <Square size={13} fill="currentColor" />
                        : <Play size={13} />}
                  </button>
                </div>
              )}
            </div>
          </div>
        ))}
        {traceEnCours.length > 0 && (
          // Trace TRANSITOIRE du tour en cours — la recherche @web a lieu
          // AVANT le premier token (direct comme pipeline), donc avant même
          // qu'une bulle assistant existe. Remplacée par la trace PERSISTÉE,
          // attachée au message, dès que `done` arrive (cf. son handler) :
          // même composant, même rendu, juste une source de données différente
          // selon le moment (tâche §3).
          <div className="flex justify-start">
            <div className="max-w-[78%]">
              <TraceRechercheView
                etapes={traceEnCours}
                collapsed={!traceEnCoursOuverte}
                onToggle={() => setTraceEnCoursOuverte(v => !v)}
              />
            </div>
          </div>
        )}
        {streaming && messages[messages.length - 1]?.role !== 'assistant' && !inPipelineRef.current && (
          <div className="flex justify-start">
            <span className="text-xs font-mono text-accent2 animate-pulse">▍</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <ModuleBar
        module="chat"
        conversationId={conversationId}
        showFile
        showMic
        showSkills
        showModel
        showEffort
        onTranscribed={(t) => setInput(prev => prev + t)}
        ttsEnabled={ttsEnabled}
        onTtsToggle={onTtsToggle}
        synthesizingText={synthesizingText}
        speakingText={speakingText}
        effort={effort}
        onEffortChange={setEffort}
        pipelineSteps={pipelineSteps}
        onPipelineStepsChange={setPipelineSteps}
      />

      <div className="border-t border-line px-4 py-4 relative">
        {suggestions.length > 0 && (
          <div className="absolute bottom-full left-4 mb-2 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-10 min-w-60">
            {suggestions.map((s, i) => (
              <button
                key={s.trigger}
                onMouseDown={e => { e.preventDefault(); applySuggestion(s.trigger) }}
                className={`w-full text-left px-3 py-2 flex gap-3 items-baseline transition-colors duration-150 ${
                  i === selectedSuggestion ? 'bg-accent/10' : 'hover:bg-surface'
                }`}
              >
                <span className="text-xs font-mono text-accent2 shrink-0">{s.trigger}</span>
                <span className="text-xs text-muted truncate">{s.desc}</span>
              </button>
            ))}
          </div>
        )}

        <div className="flex gap-3 items-end">
          {/* ── Recherche web : icône cliquable + menu déroulable ── */}
          <div className="relative shrink-0" ref={webMenuRef}>
            <div
              className={`flex items-stretch rounded-md border transition-colors duration-150 ${
                webSearch ? 'border-accent/40 bg-accent/10' : 'border-line bg-elevated'
              }`}
            >
              <button
                type="button"
                onClick={() => setWebSearch(v => !v)}
                aria-pressed={webSearch}
                title={webSearch
                  ? 'Recherche web activée — forcée avant la réponse'
                  : 'Forcer une recherche web avant la réponse'}
                className={`relative p-2.5 rounded-l-md transition-colors duration-150 ${
                  webSearch ? 'text-accent' : 'text-muted hover:text-secondary'
                }`}
              >
                <Globe size={16} className={webSearch && streaming ? 'animate-pulse' : ''} />
                {webSearch && (
                  <span className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-accent text-on-accent text-[10px] font-mono leading-none flex items-center justify-center">
                    {webSearchMode === 'once' ? '1×' : '∞'}
                  </span>
                )}
              </button>
              <button
                type="button"
                onClick={() => setWebMenuOpen(v => !v)}
                aria-haspopup="menu"
                aria-expanded={webMenuOpen}
                title="Options de recherche web"
                className={`px-1 rounded-r-md border-l transition-colors duration-150 ${
                  webSearch
                    ? 'border-accent/30 text-accent hover:bg-accent/10'
                    : 'border-line text-muted hover:text-secondary hover:bg-elevated'
                }`}
              >
                <ChevronDown
                  size={13}
                  className={`transition-transform duration-150 ${webMenuOpen ? 'rotate-180' : ''}`}
                />
              </button>
            </div>

            {webMenuOpen && (
              <div className="absolute bottom-full left-0 mb-2 w-64 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20">
                <div className="flex items-center justify-between px-3 py-2.5 border-b border-line">
                  <span className="text-xs font-medium text-primary flex items-center gap-2">
                    <Globe size={13} className={webSearch ? 'text-accent' : 'text-muted'} />
                    Recherche web
                  </span>
                  <Toggle checked={webSearch} onChange={setWebSearch} label="Activer la recherche web" />
                </div>

                <div className="p-1.5 space-y-0.5">
                  <p className="px-2 py-1 text-xs text-muted uppercase tracking-wide">Mode</p>
                  {([
                    { id: 'once', label: 'Activer une fois', desc: 'Réinitialisé après chaque message' },
                    { id: 'always', label: 'Toujours activé', desc: "Reste actif jusqu'à désactivation" },
                  ] as const).map(opt => {
                    const selected = webSearchMode === opt.id
                    return (
                      <button
                        key={opt.id}
                        onClick={() => { setWebSearchMode(opt.id); setWebSearch(true) }}
                        className={`w-full text-left px-2.5 py-1.5 rounded-sm transition-colors duration-150 flex items-start gap-2 ${
                          selected ? 'bg-accent/10' : 'hover:bg-surface'
                        }`}
                      >
                        <span className="shrink-0 w-4 inline-flex justify-center pt-0.5">
                          {selected
                            ? <Check size={13} className="text-accent" />
                            : <span className="w-1.5 h-1.5 rounded-full bg-line inline-block mt-1" />}
                        </span>
                        <span className="flex-1 min-w-0">
                          <span className={`block text-xs ${selected ? 'text-accent font-medium' : 'text-secondary'}`}>
                            {opt.label}
                          </span>
                          <span className="block text-[11px] text-muted">{opt.desc}</span>
                        </span>
                      </button>
                    )
                  })}
                </div>

                <div className="px-3 py-2.5 border-t border-line space-y-1.5">
                  <p className="text-xs text-muted uppercase tracking-wide">Sources utilisées</p>
                  <div className="flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-accent2 shrink-0" />
                    <span className="text-xs text-secondary">DuckDuckGo</span>
                    <span className="text-[11px] font-mono text-muted ml-auto">Instant + HTML</span>
                  </div>
                </div>
              </div>
            )}
          </div>

          <Textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={streaming}
            placeholder={connected ? 'Message...' : 'Connexion au serveur...'}
            rows={1}
            className="flex-1"
            style={{ minHeight: '40px', maxHeight: '160px' }}
            onInput={e => {
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = `${Math.min(el.scrollHeight, 160)}px`
            }}
          />
          {streaming ? (
            <button
              onClick={stop}
              title="Arrêter la génération"
              className="p-2.5 rounded-md bg-error/90 text-on-accent shadow-sm hover:opacity-90 transition-all duration-150 shrink-0"
            >
              <Square size={16} fill="currentColor" />
            </button>
          ) : (
            <>
              {canResume && !input.trim() && (
                <button
                  onClick={relancer}
                  title="Relancer le dernier message"
                  className="p-2.5 rounded-md border border-line text-muted hover:text-secondary hover:bg-elevated transition-all duration-150 shrink-0"
                >
                  <RotateCcw size={16} />
                </button>
              )}
              <button
                onClick={() => { send() }}
                disabled={!input.trim()}
                title="Envoyer"
                className="p-2.5 rounded-md bg-gradient-primary text-on-accent shadow-sm hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 shrink-0"
              >
                <Send size={16} />
              </button>
            </>
          )}
        </div>
        {!connected && (
          <div className="mt-2 text-xs font-mono text-error">ws déconnecté — reconnexion...</div>
        )}
      </div>
    </main>
    </div>
  )
}
