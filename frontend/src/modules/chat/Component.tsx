import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { usePersistentState } from '../../usePersistentState'
import { ChevronDown, Brain, Check, X, Circle, Loader2, Sparkles, Send, Play, Square, Globe, Columns3, Settings, SearchCheck } from 'lucide-react'
import { Button, Card, Textarea, Toggle } from '../../components/ui'
import RichMessage from '../../components/RichMessage'
import ModuleBar from '../../components/ModuleBar'
import { EFFORT_LABELS } from '../../effort'
import { EXTENSIONS_ACCEPTEES } from '../../fileTypes'
import { capacitesRaisonnement } from '../../raisonnement'
import type { EffortLevel, StepConfig } from '../../App'
import { API, apiFetch, wsUrl } from '../../api'
import { AT_COMMANDS, allSlashCommands, moduleCommands } from './commands'
import ConversationList from './ConversationList'
import { creerConversation, reprendreAncienChat } from './conversations'
import { liste, texte, modelesDisponibles, type ModeleDisponible } from '../../normaliser'
import { useModules } from '../../modules'
import { metaAffichable, type MetaAffichable } from './metaMessage'
import { budgetRechercheApprofondie, infoBulleRechercheApprofondie } from './rechercheApprofondie'
import { etapesDe, libelleBadgeAbandon, libelleBadgeCitations, libelleBadgeLimite, resumeTrace, verifieeContreRecherche, type EtapeTrace } from './traceRecherche'
import CamembertContexte from './CamembertContexte'
import { useFluxEnCours } from '../../redemarrage'

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

/** État d'UN modèle dans une comparaison en cours — mêmes canaux que le
 * chemin mono-modèle (`content`, `raisonnement`), mais tenus séparément par
 * modèle, cf. `ComparaisonBlock`. */
interface CompareModelState {
  content: string
  raisonnement: string
  termine: boolean
  erreur?: string
}

/**
 * Comparaison multi-modèles EN COURS, attachée au message UTILISATEUR qui l'a
 * déclenchée — même principe que `ThinkingBlock` : c'est ce message qui a
 * demandé la comparaison, donc c'est lui qui la porte.
 *
 * `resolutionEnCours` : posé au clic sur « Garder cette réponse », AVANT même
 * la réponse du serveur — désactive tous les boutons de cette comparaison
 * pour éviter un double envoi (double clic). Remis à `false` si le serveur
 * répond par une erreur (état périmé) plutôt que par `done`, pour que
 * l'utilisateur puisse réessayer sur une autre réponse au lieu de rester
 * bloqué. Le champ disparaît (comparaison mise à `undefined` sur le message)
 * dès qu'un choix aboutit — cf. le handler `done`.
 */
interface ComparaisonBlock {
  modeles: string[]
  parModele: Record<string, CompareModelState>
  resolutionEnCours: boolean
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
  /**
   * Tokens occupés dans la fenêtre de contexte par CE tour, tel que le
   * fournisseur l'a rapporté (`contexte_tokens` de la sentinelle `__stats__`).
   *
   * À ne pas confondre avec `stats.promptTokens`, qui est une SOMME sur tous
   * les rounds d'appel d'outil — c'est ce qui se facture, pas ce qui occupe la
   * fenêtre. Seul le DERNIER round mesure le contexte ; c'est cette valeur-ci
   * qui alimente `CamembertContexte`, et elle seule.
   *
   * Même statut que `sources` / `traceRecherche` : métadonnée de présentation,
   * persistée par `core/history.py` et jamais réinjectée dans le prompt. Absent
   * veut dire « pas rapporté » (fournisseur muet, message antérieur au champ,
   * comparaison multi-modèles) — jamais « zéro », qui décrirait un tour vide.
   */
  contexteTokens?: number
  /**
   * Comparaison multi-modèles déclenchée par CE message utilisateur, tant
   * qu'elle n'est pas résolue. Absent : soit ce message n'a jamais déclenché
   * de comparaison, soit elle vient d'être résolue (le champ est effacé au
   * moment où le texte choisi devient un message assistant normal — cf.
   * handler `done`). Jamais posé sur un message assistant.
   */
  comparaison?: ComparaisonBlock
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
 * Longueur maximale de la consigne d'un fil — miroir de `MAX_INSTRUCTION` côté
 * backend, qui REFUSE au-delà plutôt que de tronquer. Affichée en compteur et
 * utilisée pour désactiver le bouton : sans elle, le refus n'arriverait
 * qu'après l'envoi, sur une consigne déjà écrite. Migré depuis `ModuleBar.tsx`
 * avec le reste du panneau de paramètres (§ défaut 3, itération 2).
 */
const MAX_INSTRUCTION_FIL = 4000

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
    <Card accent="secondary" padded={false} className="mb-2 overflow-hidden !bg-accent2/5 !border-accent2/25">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-accent2/10 transition-colors duration-150"
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
        <div className="border-t border-accent2/25 px-3 py-2">
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
    <Card accent="secondary" padded={false} className="mt-2 mb-1 overflow-hidden !bg-accent2/5 !border-accent2/25">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-accent2/10 transition-colors duration-150"
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
        <div className="border-t border-accent2/25 divide-y divide-line">
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
    case 'tool_call_abandonne':
      // Formulation NON technique : l'utilisateur n'a pas à savoir ce qu'est
      // un JSON d'arguments — seulement que la recherche n'a pas eu lieu et
      // sur quoi repose donc la réponse.
      return (
        <p className="m-0 text-warning">
          Recherche abandonnée : le modèle n'a pas formulé sa recherche correctement.
          La réponse s'appuie sur ses connaissances, qui peuvent être datées.
        </p>
      )
    case 'tool_call_plafond_atteint':
      return (
        <p className="m-0 text-warning">
          Limite de recherches atteinte : le modèle a conclu sans nouvelle recherche.
        </p>
      )
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
  // Visible sans déplier le panneau, comme le badge des citations : c'est ce
  // qui distingue une réponse sourcée d'une réponse donnée de mémoire.
  const libelleAbandon = libelleBadgeAbandon(etapes)
  const libelleLimite = libelleBadgeLimite(etapes)
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
          {libelleAbandon && (
            <span className="shrink-0 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-warning/20 text-warning">
              {libelleAbandon}
            </span>
          )}
          {libelleLimite && (
            <span className="shrink-0 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-warning/20 text-warning">
              {libelleLimite}
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

/**
 * Panneaux de comparaison multi-modèles, côte à côte (empilés verticalement
 * si l'écran est étroit — jamais de scroll horizontal forcé).
 *
 * Même langage visuel que `ThinkingBlockView`/`RaisonnementView` (`Card`,
 * `RichMessage` en mode streaming) : c'est la même idée pour l'utilisateur
 * (« voici ce qui arrive, modèle par modèle »), donc les mêmes briques.
 *
 * `msgIdx` sert uniquement à préfixer les clés de `collapsedRaisonnement`
 * (partagé entre tous les modèles de tous les messages) — le composant ne
 * lit ni n'écrit rien d'autre à travers cet index.
 */
function ComparaisonView({
  msgIdx, comparaison, onGarder, collapsedRaisonnement, onToggleRaisonnement,
}: {
  msgIdx: number
  comparaison: ComparaisonBlock
  onGarder: (model: string) => void
  collapsedRaisonnement: Record<string, boolean>
  onToggleRaisonnement: (model: string) => void
}) {
  return (
    <div className="mt-2 grid grid-cols-1 md:grid-cols-3 gap-3">
      {comparaison.modeles.map(model => {
        const etat = comparaison.parModele[model]
        if (!etat) return null
        const gardable = etat.termine && !etat.erreur && etat.content.length > 0
        const cle = `${msgIdx}-${model}`
        return (
          <Card key={model} className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-mono text-secondary truncate">{model}</span>
              {!etat.termine && <Loader2 size={13} className="animate-spin text-accent2 shrink-0" />}
            </div>
            {etat.raisonnement && (
              <RaisonnementView
                texte={etat.raisonnement}
                enCours={!etat.termine && !etat.content}
                collapsed={collapsedRaisonnement[cle] ?? etat.content.length > 0}
                onToggle={() => onToggleRaisonnement(model)}
              />
            )}
            {etat.erreur ? (
              <p className="text-xs text-error whitespace-pre-wrap break-words m-0">{etat.erreur}</p>
            ) : (
              <div className="text-sm text-secondary min-w-0">
                <RichMessage content={etat.content} streaming={!etat.termine} />
              </div>
            )}
            <button
              type="button"
              disabled={!gardable || comparaison.resolutionEnCours}
              onClick={() => onGarder(model)}
              className="mt-auto text-xs rounded-md px-2 py-1.5 border border-line text-secondary
                         hover:bg-elevated disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-150"
            >
              Garder cette réponse
            </button>
          </Card>
        )
      })}
    </div>
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
  /** Miroir d'affichage de `procheDuBasRef`, pour le bouton « reprendre le
   * suivi » — la ref seule ne redéclenche pas de rendu. */
  const [procheDuBas, setProcheDuBas] = useState(true)
  const [connected, setConnected] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)
  const [streamStats, setStreamStats] = useState<{ tps: number; count: number } | null>(null)
  /**
   * Le modèle LM Studio actif est-il encore en train de charger ?
   *
   * `null` = on ne sait pas, et c'est un état À PART ENTIÈRE, pas un `false`
   * poli : LM Studio rend un 500 transitoire à l'instant précis où un
   * chargement démarre (observé une fois sur 79 sondes le 2026-09-06), et il
   * peut être éteint. Le confondre avec « pas en train de charger » ferait
   * disparaître l'indicateur en plein chargement — d'où la règle appliquée
   * dans le sondage ci-dessous : **un `null` ne remplace jamais un `true`
   * déjà affiché**, il est ignoré.
   *
   * Il n'y a délibérément AUCUN pourcentage ici. LM Studio n'en expose pas sur
   * son API HTTP (mesuré, cf. `core/models.py:etat_modele_lmstudio` — le corps
   * de la route de chargement refuse `stream` en 400, donc son schéma est
   * fermé et il n'y a pas de drapeau qu'on aurait manqué). En fabriquer un à
   * partir du temps écoulé serait un chiffre inventé, et les durées mesurées
   * (12,1 s pour 3 Go, 119,4 s pour 17,7 Go) ne se laissent de toute façon pas
   * estimer.
   */
  const [chargementLmStudio, setChargementLmStudio] = useState<boolean | null>(null)
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

  /**
   * Analyse vision d'une image attachée, EN COURS ou en échec.
   *
   * Elle a lieu avant le premier token et prend 6 a 26 s par image (mesure du
   * poste, cf. CLAUDE.md §3.3 bis) : sans cette ligne, le chat resterait muet
   * une demi-minute sur une question parfaitement normale. Le silence pendant
   * un chargement est un mode d'echec deja paye ici (la webview coupait un flux
   * SSE muet), donc l'afficher n'est pas de la decoration.
   *
   * Un ECHEC survit a `done`, contrairement a `en_cours` : « l'image n'a pas pu
   * etre lue » est une information sur la reponse qu'on vient de recevoir, et
   * l'effacer avec le curseur de frappe la rendrait invisible. Efface au
   * message suivant.
   */
  const [visionAnalyse, setVisionAnalyse] = useState<
    { etat: string; fichier: string; index: number; total: number; reste: number } | null
  >(null)

  // Recherche web : active = force une recherche avant la réponse.
  // Mode 'once' = réinitialisé après chaque message (défaut, non handicapant) ;
  // 'always' = reste actif jusqu'à désactivation explicite.
  const [webSearch, setWebSearch] = usePersistentState<boolean>('epure.chat.webSearch', false)
  const [webSearchMode, setWebSearchMode] = usePersistentState<'once' | 'always'>('epure.chat.webSearchMode', 'once')
  /**
   * Recherche approfondie — armée pour le PROCHAIN message uniquement, jamais
   * persistée (pas de `usePersistentState`, contrairement à `webSearch`) :
   * jusqu'à `budgetApprofondie` requêtes web enchaînées par le modèle
   * (tool-calling natif, `recherche_approfondie` dans `core/llm.py` ; budget
   * réglable de 1 à 10 dans Réglages › Tool calling), donc un mode qui resterait
   * collant referait ce coût à chaque message — même raisonnement que
   * `visionOverride` ci-dessus pour l'analyse d'image. Réinitialisé après
   * l'envoi (cf. `sendUserText`).
   */
  const [deepSearch, setDeepSearch] = useState(false)
  /**
   * Budget RÉELLEMENT configuré pour `recherche_approfondie` (cf.
   * `rechercheApprofondie.ts`), affiché dans l'info-bulle du bouton. `null`
   * tant qu'inconnu : l'info-bulle ne cite alors aucun nombre. Relu à CHAQUE
   * ouverture du menu web, pas seulement au montage : un module visité reste
   * monté (`App.tsx`, `mountedIds`), donc un budget changé dans les Réglages
   * entre-temps serait sinon affiché périmé.
   */
  const [budgetApprofondie, setBudgetApprofondie] = useState<number | null>(null)
  /**
   * Un seul panneau du header ouvert à la fois — modèle, recherche web,
   * comparaison, ou le popover « Paramètres de la conversation ». Une valeur
   * UNIQUE plutôt que quatre booléens indépendants : ouvrir l'un ferme l'autre
   * par construction, sans code de coordination séparé. Ne couvre QUE les
   * panneaux propriété de ce composant — pas ceux de `ModuleBar` (`activePanel`,
   * privé à ce composant ; il ne lui reste plus que le panel fichiers et le
   * panel modèle, cf. rapport final).
   */
  const [headerMenuOuvert, setHeaderMenuOuvert] = useState<'model' | 'web' | 'compare' | 'params' | null>(null)

  /**
   * Comparaison multi-modèles — sélection courante (2 à 3 id, non persistée :
   * préférence de tour, pas de session). Une comparaison est « active » dès
   * que 2 modèles au moins sont cochés (§ tâche 2026-09-03) ; il n'y a pas de
   * bascule séparée, le plafond de 3 est appliqué en désactivant les cases non
   * cochées plutôt qu'en acceptant puis rejetant (double protection avec la
   * validation serveur, cf. `router.py:_valider_compare_models`).
   */
  const [compareModeles, setCompareModeles] = useState<string[]>([])
  const [modelesDisponiblesListe, setModelesDisponiblesListe] = useState<ModeleDisponible[]>([])
  /**
   * Modèle actif, reporté par `ModuleBar` (`onModelChange`) plutôt que relu
   * indépendamment ici : `selectedModel` est un état PRIVÉ de `ModuleBar`,
   * synchronisé avec le serveur (chargement initial + sélection). Une
   * seconde lecture ici diverger­ait dès que l'utilisateur change de modèle
   * depuis le panneau de `ModuleBar`, sans qu'aucun événement ne l'annonce.
   */
  const [modeleActifId, setModeleActifId] = useState('')
  /** Cible du portail du panneau modèle complet de `ModuleBar` (cf. le chip
   * du header) — un `useState`, pas un simple `useRef`, parce que `ModuleBar`
   * doit être RE-RENDU une fois le nœud DOM de l'ancre disponible pour que
   * son portail ait une cible. */
  const [modelPanelAncre, setModelPanelAncre] = useState<HTMLDivElement | null>(null)
  /**
   * Portails du bouton "Fichiers"/son panneau et du bouton micro — même
   * principe que `modelPanelAncre` : le bouton "Fichiers" va à gauche des
   * pilules d'effort, le micro à droite, dans l'îlot du composer. Ce sont les
   * VRAIS boutons de `ModuleBar` (avec leur état — badge de fichiers
   * attachés, icône micro selon `recording`/`transcribing`), pas une copie :
   * une fois ces trois ancres montées, la barre de `ModuleBar` n'a plus rien
   * à montrer pour Chat et ne se rend plus du tout (`hasAnyContent` côté
   * `ModuleBar.tsx`).
   */
  const [fileButtonAncre, setFileButtonAncre] = useState<HTMLDivElement | null>(null)
  const [filePanelAncre, setFilePanelAncre] = useState<HTMLDivElement | null>(null)
  const [micButtonAncre, setMicButtonAncre] = useState<HTMLDivElement | null>(null)
  /** Ouverture du panneau fichiers, désormais pilotée depuis le composer. */
  const [filesPanelOuvert, setFilesPanelOuvert] = useState(false)
  /**
   * Sortie de `uploadFiles` de `ModuleBar` — un collage (Ctrl+V) d'image ou de
   * fichier dans le `<Textarea>` du composer vise ce même point d'entrée que
   * le glisser-déposer et le sélecteur du panneau "Fichiers", pour profiter
   * de l'indexation RAG/vision existante sans la dupliquer.
   */
  const uploadFilesRef = useRef<((files: File[], opts?: { generateSummary?: boolean }) => void) | null>(null)
  /** Titre de la conversation affichée, reporté par `ConversationList` — seule
   * source de l'index des conversations (cf. sa prop `onTitreActif`). */
  const [titreConversationActive, setTitreConversationActive] = useState('Nouvelle conversation')

  /**
   * Paramètres de conversation — migrés depuis `ModuleBar.tsx` (son panneau
   * « Paramètres de session »), supprimé de ce fichier une fois la migration
   * faite : `showSkills` n'avait que Chat comme consommateur, donc le garder
   * vivant à deux endroits aurait été du code mort côté `ModuleBar`, pas du
   * partage. Rendus désormais dans le popover du header (§ défaut 3).
   */
  const [strictMode, setStrictMode] = useState(false)
  // Défaut `true` = comportement historique : si `/context` ne répond pas, on
  // n'éteint pas une capacité qu'on n'a pas pu lire.
  const [raisonnement, setRaisonnement] = useState(true)
  const [sessionInstruction, setSessionInstruction] = useState('')
  const [instructionDraft, setInstructionDraft] = useState('')
  /**
   * État des préfixes intégrés (Réglages › Préfixes & commandes) — `null` tant
   * que `/context` n'a pas répondu, ce que `atCommandsActifs` traite comme
   * « tout activé » (défaut serveur, cf. `core.memory._CONTEXT_DEFAULT`), pas
   * comme « tout désactivé ». On ne garde QUE `enabled` ici : le `trigger`
   * personnalisé n'est pas affiché (`AT_COMMANDS` reste la liste EN DUR
   * consommée par `sendUserText`, cf. son commentaire — un libellé renommé ici
   * mentirait sur ce qu'il faut réellement taper).
   */
  const [prefixesIntegresActifs, setPrefixesIntegresActifs] = useState<Record<string, boolean> | null>(null)
  /** Préfixes personnalisés à déclenchement MANUEL actifs (`prefixe_actif` +
   * `trigger`) — jamais affichés nulle part dans le chat avant cette
   * itération. Purement informationnel : le déclenchement réel est déjà géré
   * côté serveur indépendamment de cet affichage (`modules/chat/router.py`). */
  const [prefixesPersonnalisesActifs, setPrefixesPersonnalisesActifs] = useState<
    Array<{ id: string; trigger: string; desc: string }>
  >([])
  /** Consigne libre DE CETTE CONVERSATION, distincte de la consigne de session
   * juste au-dessus — même distinction de portée que dans `ModuleBar.tsx`
   * avant sa migration ici. */
  const [instructionFil, setInstructionFil] = useState('')
  const [instructionFilDraft, setInstructionFilDraft] = useState('')
  /** Repli du raisonnement PAR PANNEAU de comparaison, clé `${msgIdx}-${model}` —
   * même sémantique que `collapsedRaisonnement`, dupliquée plutôt que partagée
   * parce que les clés ne vivent pas dans le même espace (un index de message
   * seul ne suffit plus à identifier UN panneau). */
  const [collapsedRaisonnementCompare, setCollapsedRaisonnementCompare] = useState<Record<string, boolean>>({})

  /** Position du bouton enfoncé, pour distinguer un clic d'un glisser. */
  const pointerDownRef = useRef<{ x: number; y: number } | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const modelMenuRef = useRef<HTMLDivElement>(null)
  const webMenuRef = useRef<HTMLDivElement>(null)
  const compareMenuRef = useRef<HTMLDivElement>(null)
  const paramsMenuRef = useRef<HTMLDivElement>(null)
  /** Conteneur du bouton "Fichiers" + son panneau, dans le composer — clic
   * extérieur = fermeture, même patron que les panneaux du header. */
  const filesMenuRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  /** Le conteneur défilant lui-même — nécessaire pour lire sa position de
   * scroll (cf. l'effet de suivi automatique plus bas). */
  const containerRef = useRef<HTMLDivElement>(null)
  /**
   * Position de scroll SUIVIE EN CONTINU par l'écouteur `scroll` du
   * conteneur, pas recalculée après coup depuis `messages` : au moment où
   * l'effet sur `messages` tourne, le DOM a déjà grandi du nouveau contenu,
   * donc mesurer « suis-je proche du bas » à cet instant confondrait « le
   * bas a reculé parce qu'un message est arrivé » avec « l'utilisateur a
   * remonté ». Cette ref porte la dernière position connue AVANT l'arrivée
   * du nouveau contenu — c'est elle qui répond à la vraie question : est-ce
   * que l'utilisateur suivait la conversation ?
   */
  const procheDuBasRef = useRef(true)
  /** Force le prochain défilement, quelle que soit la position — posé par
   * `pushMsg('user', …)` : un message que l'utilisateur vient d'envoyer doit
   * toujours ramener en bas, même s'il avait remonté pour relire. */
  const forcerDefilementRef = useRef(false)
  const lastAssistantRef = useRef('')
  const tokenCountRef = useRef(0)
  const streamStartRef = useRef<number | null>(null)
  /**
   * Stats de la trame `stats` du tour en cours. `contexteTokens` y est le
   * contexte du DERNIER round d'outil, pas la somme `promptTokens` — les deux
   * sortent de la même trame mais ne mesurent pas la même chose, et seul le
   * premier alimente la jauge (cf. `Message.contexteTokens`).
   */
  const pendingOllamaStatsRef = useRef<{ promptTokens: number; outputTokens: number; evalMs: number; contexteTokens: number | null } | null>(null)
  const inPipelineRef = useRef(false)
  const pipelineUserMsgIdxRef = useRef(-1)
  /** Index, dans `messages`, du message UTILISATEUR qui porte une comparaison
   * EN COURS — `-1` si aucune. Même rôle que `pipelineUserMsgIdxRef`, mais
   * posé côté CLIENT dès l'envoi (aucun événement serveur n'annonce le début
   * d'une comparaison : c'est le client qui a demandé `compare_models`, donc
   * qui connaît déjà la liste — cf. handler `done` pour la remise à `-1`). */
  const comparaisonUserMsgIdxRef = useRef(-1)
  /**
   * Miroir SYNCHRONE du texte accumulé par modèle pendant une comparaison,
   * tenu en parallèle de l'état React (`messages[i].comparaison`).
   *
   * Nécessaire précisément parce qu'un updater passé à `setMessages` n'est
   * PAS exécuté immédiatement à l'appel : React le rejoue plus tard, à son
   * prochain rendu. Le reste de ce fichier s'appuie sur ce délai sans jamais
   * le nommer (ex. `lastAssistantRef`, posé dans l'updater de `token` puis lu
   * seulement au `done` SUIVANT — un événement séparé, dont le rendu du
   * précédent a déjà eu lieu). Ici, `done` doit lire le texte choisi et le
   * repousser dans CE MÊME appel : lire une ref qu'on vient de poser DANS un
   * updater, plus bas dans le même appel synchrone, verrait encore l'ancienne
   * valeur. D'où ce second miroir, écrit en dehors de tout `setState`.
   */
  const compareAccumRef = useRef<Record<string, string>>({})
  /** Miroir synchrone de `comparaison.resolutionEnCours`, pour la même
   * raison que `compareAccumRef` : le handler `error` doit savoir, DANS le
   * même appel, si l'erreur reçue répond à un `compare_choix` (auquel cas on
   * réactive les boutons) ou rejette la comparaison avant tout streaming
   * (auquel cas rien ne viendra jamais peupler le panneau — on le referme). */
  const resolutionEnCoursRef = useRef(false)
  // Arrêt : ignore les events de streaming entrants après un stop manuel.
  const cancelledRef = useRef(false)
  // Dernier message envoyé.
  const lastSentRef = useRef<Record<string, unknown> | null>(null)

  /**
   * Miroir SYNCHRONE de `conversationId`, lu par `ws.onmessage` — LE bug de
   * fuite entre conversations.
   *
   * `ws.onmessage` (ci-dessous) est créé UNE SEULE FOIS : son effet ne dépend
   * que d'`onAssistantDone`, délibérément (une seule connexion WebSocket sert
   * TOUTES les conversations, cf. CLAUDE.md — on ne la ferme/rouvre pas à
   * chaque bascule de fil). Un `conversationId` lu directement depuis cette
   * fermeture resterait donc figé à sa valeur du MONTAGE, jamais mis à jour
   * par un changement d'état React ultérieur. Seule une ref, réassignée EN
   * DEHORS du rendu (dans `definirConversationAffichee` ci-dessous, appelée
   * partout où `conversationId` change), donne au handler une valeur
   * réellement à jour de « quel fil est affiché EN CE MOMENT ».
   */
  const conversationIdRef = useRef(conversationId)
  /**
   * Conversations pour lesquelles au moins un événement de streaming a été
   * REJETÉ par le filtre ci-dessous (cf. `ws.onmessage`) parce qu'un autre
   * fil était affiché au moment de sa réception.
   *
   * Sert à distinguer, dans le handler `done`, deux cas au retour sur un fil
   * qui a continué de générer pendant l'absence :
   *  - retour APRÈS son `done` : la conversation a déjà été rechargée depuis
   *    le disque par l'effet de bascule de fil (plus bas), qui lit toujours
   *    la version COMPLÈTE — rien à faire de plus ;
   *  - retour AVANT son `done` : les tokens reçus pendant l'absence ont été
   *    jetés, donc le texte reconstruit à l'écran (uniquement la suite reçue
   *    après le retour) est TRONQUÉ par rapport à ce que le serveur a
   *    réellement écrit sur le disque. Le `done` correspondant déclenche
   *    alors une relecture complète plutôt que de faire confiance à l'écran.
   */
  const conversationsSuspectesRef = useRef<Set<string>>(new Set())

  /**
   * Pose `conversationId` ET sa ref synchrone d'un seul geste — tout code qui
   * change le fil affiché doit passer par ICI, jamais par `setConversationId`
   * directement, sous peine de laisser `conversationIdRef` (donc le filtre
   * anti-fuite de `ws.onmessage`) périmé.
   */
  const definirConversationAffichee = useCallback((id: string) => {
    conversationIdRef.current = id
    setConversationId(id)
  }, [setConversationId])
  // Filet de sécurité, pour tout changement de `conversationId` qui
  // échapperait malgré tout à `definirConversationAffichee` (hydratation
  // initiale de `usePersistentState` notamment, dont la ref ci-dessus ne peut
  // pas connaître la valeur avant le premier rendu).
  useEffect(() => { conversationIdRef.current = conversationId }, [conversationId])

  /**
   * Recharge UNE conversation depuis le disque et remplace `messages`.
   *
   * Extraite pour être appelée par DEUX chemins : l'effet de bascule de fil
   * (plus bas), et le handler `done`, qui doit relire le disque au lieu de
   * faire confiance à l'accumulation locale quand des tokens de CE tour ont
   * été jetés pendant que ce fil n'était pas affiché (cf.
   * `conversationsSuspectesRef`) — sans ça, le texte affiché resterait
   * tronqué à la seule suite reçue après le retour sur le fil.
   *
   * Revérifie `conversationIdRef` APRÈS la réponse réseau, pas seulement
   * avant l'appel : si l'utilisateur a de nouveau changé de fil pendant que
   * cette requête était en vol, son résultat ne doit plus rien écraser.
   */
  const chargerConversationDepuisDisque = useCallback(async (id: string): Promise<boolean> => {
    try {
      const res = await apiFetch(`${API}/chat/conversations/${id}`)
      if (id !== conversationIdRef.current) return false
      if (res.status === 404) {
        // Supprimée depuis un autre onglet : on repart à vide plutôt que
        // d'afficher une conversation fantôme.
        definirConversationAffichee('')
        setMessages([])
        comparaisonUserMsgIdxRef.current = -1
        return false
      }
      if (!res.ok) return false
      const d = await res.json() as Record<string, unknown>
      if (id !== conversationIdRef.current) return false
      setMessages(liste<Record<string, unknown>>(d.messages).map(m => {
        const role = texte(m.role) === 'assistant' ? 'assistant' as const : 'user' as const
        const horodatage = texte(m['horodatage'])
        const modele = texte(m['modèle'])
        const sources = sourcesDe(m['sources'])
        const traceRecherche = etapesDe(m['trace_recherche'])
        // Entier STRICTEMENT positif, même règle que côté backend : un
        // `contexte_tokens: 0` persisté décrirait un tour vide, ce qui n'arrive
        // jamais — c'est une absence déguisée. Le test est numérique et non
        // `texte()`, qui ne sert qu'aux chaînes.
        const contexteBrut = m['contexte_tokens']
        const contexteTokens = typeof contexteBrut === 'number' && contexteBrut > 0 ? contexteBrut : null
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
          ...(contexteTokens !== null ? { contexteTokens } : {}),
        }
      }))
      return true
    } catch {
      // Backend qui démarre, ou requête interrompue : la liste reste ce
      // qu'elle était, rien de plus à faire ici.
      return false
    }
  }, [definirConversationAffichee])

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

        /**
         * Filtre anti-fuite — LE bug corrigé ici.
         *
         * Chaque événement de streaming porte désormais un `conversation_id`
         * (backend/modules/chat/router.py) : tout ce qui ne correspond pas au
         * fil AFFICHÉ ici est ignoré AVANT de toucher `messages`, `streaming`
         * ou n'importe quel autre état visible. Sans ce filtre, basculer vers
         * une conversation B pendant qu'une conversation A générait encore
         * laissait les tokens de A continuer à s'accumuler dans l'écran de B.
         *
         * `conversationIdRef`, pas `conversationId` : cf. sa déclaration plus
         * haut — ce handler est créé une seule fois, à la connexion.
         *
         * Un `conversation_id` ABSENT ou VIDE est PERMISSIF (on applique quand
         * même) : c'est le cas d'une poignée d'événements où le serveur ne
         * peut identifier la conversation avec certitude (ex. une comparaison
         * déjà résolue avant ce choix) — les faire disparaître serait une
         * nouvelle classe d'échec silencieux, pas une correction.
         *
         * `conversation` et `titre` ont leur propre traitement plus bas (l'un
         * annonce un identifiant qui n'existait pas encore côté client, l'autre
         * ne touche jamais `messages`) : ils ne passent pas par ce filtre.
         */
        const idEvt = typeof data.conversation_id === 'string' ? data.conversation_id : ''
        const estPourLeFilAffiche = !idEvt || idEvt === conversationIdRef.current
        if (data.type !== 'conversation' && data.type !== 'titre' && !estPourLeFilAffiche) {
          // Le `done`/`error` de ce tour est peut-être encore à venir : sans
          // cette marque, une relecture ultérieure du disque (cf. handler
          // `done`) ne saurait pas qu'elle doit remplacer l'accumulation
          // locale au lieu de lui faire confiance.
          if (idEvt) conversationsSuspectesRef.current.add(idEvt)
          return
        }

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
          //
          // MAIS seulement si l'utilisateur n'a pas, entre-temps, basculé vers
          // un AUTRE fil existant (`conversationIdRef` porterait alors un id
          // non vide, différent de celui qu'on vient de créer) : sinon cette
          // annonce, tardive, ramènerait de force l'écran sur la conversation
          // qu'il vient de quitter. La liste, elle, doit montrer le nouveau fil
          // dans tous les cas.
          if (conversationIdRef.current === '') definirConversationAffichee(data.id)
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

        } else if (data.type === 'vision_analyse') {
          // Emis par `_analyser_images_du_tour` (backend), un par image et par
          // transition d'etat. On ne garde que le DERNIER : c'est une ligne
          // d'etat, pas un journal — le deroule complet, lui, est dans les logs
          // du backend, qui portent la duree et le modele.
          setVisionAnalyse({
            etat: String(data['état'] ?? ''),
            fichier: String(data.fichier ?? ''),
            index: Number(data.index ?? 0),
            total: Number(data.total ?? 0),
            reste: Number(data.reste ?? 0),
          })

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
            // Absent de la trame pour les fournisseurs qui ne partagent pas leur
            // fenêtre : `null`, jamais 0 — la jauge doit disparaître, pas
            // afficher « 100 % restants » sur un tour dont on ne sait rien.
            contexteTokens: (data.contexte_tokens as number | undefined) ?? null,
          }

        // ── Comparaison multi-modèles ────────────────────────────────────
        //
        // Vocabulaire d'événements disjoint du mono-modèle (cf. router.py) :
        // un `compare_*` ne peut arriver que pour un tour lancé avec
        // `compare_models`, donc uniquement pendant que
        // `comparaisonUserMsgIdxRef.current` pointe un message valide. Chaque
        // handler met à jour SON modèle dans `parModele`, jamais les autres —
        // c'est ce qui permet un streaming réellement indépendant par panneau.

        } else if (data.type === 'compare_token') {
          const modele: string = data.model
          compareAccumRef.current[modele] = (compareAccumRef.current[modele] ?? '') + data.content
          setMessages(prev => {
            const idx = comparaisonUserMsgIdxRef.current
            const bloc = prev[idx]?.comparaison
            if (!bloc || !bloc.parModele[modele]) return prev
            const updated = [...prev]
            updated[idx] = {
              ...updated[idx],
              comparaison: {
                ...bloc,
                parModele: {
                  ...bloc.parModele,
                  [modele]: { ...bloc.parModele[modele], content: bloc.parModele[modele].content + data.content },
                },
              },
            }
            return updated
          })

        } else if (data.type === 'compare_reasoning') {
          const modele: string = data.model
          setMessages(prev => {
            const idx = comparaisonUserMsgIdxRef.current
            const bloc = prev[idx]?.comparaison
            if (!bloc || !bloc.parModele[modele]) return prev
            const updated = [...prev]
            updated[idx] = {
              ...updated[idx],
              comparaison: {
                ...bloc,
                parModele: {
                  ...bloc.parModele,
                  [modele]: { ...bloc.parModele[modele], raisonnement: bloc.parModele[modele].raisonnement + data.content },
                },
              },
            }
            return updated
          })

        } else if (data.type === 'compare_error') {
          const modele: string = data.model
          setMessages(prev => {
            const idx = comparaisonUserMsgIdxRef.current
            const bloc = prev[idx]?.comparaison
            if (!bloc || !bloc.parModele[modele]) return prev
            const updated = [...prev]
            updated[idx] = {
              ...updated[idx],
              comparaison: {
                ...bloc,
                parModele: { ...bloc.parModele, [modele]: { ...bloc.parModele[modele], erreur: data.content } },
              },
            }
            return updated
          })

        } else if (data.type === 'compare_done') {
          const modele: string = data.model
          setMessages(prev => {
            const idx = comparaisonUserMsgIdxRef.current
            const bloc = prev[idx]?.comparaison
            if (!bloc || !bloc.parModele[modele]) return prev
            const updated = [...prev]
            updated[idx] = {
              ...updated[idx],
              comparaison: {
                ...bloc,
                parModele: { ...bloc.parModele, [modele]: { ...bloc.parModele[modele], termine: true } },
              },
            }
            return updated
          })
          // `compare_all_done` (un seul événement, tous modèles terminés)
          // n'a rien de plus à faire ici : `tousTermines` se dérive déjà de
          // `parModele` à chaque rendu, jamais un flag séparé à maintenir.

        } else if (data.type === 'done') {
          // Résolution d'une comparaison multi-modèles : le serveur répond par
          // ce même événement `done` (CLAUDE.md, protocole de comparaison),
          // avec `modèle` portant l'ID CHOISI par l'utilisateur. Le texte,
          // lui, n'est JAMAIS renvoyé par le serveur — il vit déjà côté
          // client, accumulé au fil des `compare_token` (même principe que
          // l'horodatage serveur-fait-foi ailleurs : ici c'est le CONTENU qui
          // fait foi côté client, le serveur n'ayant fait que le persister).
          // On le pousse comme un message assistant normal AVANT de laisser
          // le reste de ce handler faire ce qu'il fait déjà pour un tour
          // mono-modèle (stats, sources, trace — tous visent « le dernier
          // message assistant », qui est désormais celui-ci).
          const idxComparaison = comparaisonUserMsgIdxRef.current
          // Ce tour a-t-il perdu des événements en route (cf. le filtre
          // anti-fuite plus haut) ? Si oui, le texte accumulé côté client
          // (`lastAssistantRef`/`messages`) n'est que la SUITE reçue depuis le
          // retour sur ce fil — tronqué par rapport à ce que le serveur a
          // réellement écrit. `_enregistrer_reponse` (backend) persiste
          // AVANT d'envoyer ce `done` : le disque, lui, est déjà complet.
          //
          // ⚠️ PAS de repli sur `conversationIdRef.current` ici, contrairement
          // au filtre plus haut : un `done` sans `conversation_id` (défensif —
          // le backend en pose toujours un, cf. router.py) ne doit jamais
          // faire disparaître la marque « suspecte » du fil réellement
          // affiché. Un tel `done` serait attribué au fil courant par
          // erreur, effacerait sa marque SANS jamais relire le disque, et
          // laisserait la troncature que ce correctif vise à éliminer se
          // réinstaller en silence.
          const idConvDone = typeof data.conversation_id === 'string' ? data.conversation_id : ''
          const etaitSuspecte = idConvDone !== '' && conversationsSuspectesRef.current.has(idConvDone)
          if (idConvDone) conversationsSuspectesRef.current.delete(idConvDone)

          if (idxComparaison >= 0) {
            const modeleChoisi = data['modèle'] as string | undefined
            const texteChoisi = (modeleChoisi && compareAccumRef.current[modeleChoisi]) || ''
            lastAssistantRef.current = texteChoisi
            setMessages(prev => {
              if (!prev[idxComparaison]?.comparaison) return prev
              const updated = [...prev]
              updated[idxComparaison] = { ...updated[idxComparaison], comparaison: undefined }
              updated.push({ role: 'assistant', content: texteChoisi })
              return updated
            })
            comparaisonUserMsgIdxRef.current = -1
            compareAccumRef.current = {}
          } else if (etaitSuspecte) {
            // Relit le disque plutôt que de construire quoi que ce soit à
            // partir de l'accumulation locale — stats comprises : elles ne
            // compteraient que les tokens reçus APRÈS le retour, pas ceux
            // générés pendant l'absence. La relecture ne les fournit pas non
            // plus (`GET /chat/conversations/{id}` ne porte pas de débit),
            // et c'est très bien : pas de chiffre plutôt qu'un chiffre faux.
            //
            // Non couvert par ce chemin : une comparaison multi-modèles
            // interrompue de la même façon (`idxComparaison >= 0` ci-dessus
            // prime toujours). Rare — une comparaison se résout en restant
            // sur l'écran — et laissé pour une prochaine occurrence plutôt
            // que d'alourdir ce correctif.
            void chargerConversationDepuisDisque(idConvDone)
          } else {
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
            // Le contexte du tour monte sur le MÊME message, mais sous une
            // condition INDÉPENDANTE de `finalStats` : un fournisseur peut
            // rapporter `contexte_tokens` sans les durées d'évaluation qui
            // composent les tok/s (c'est le cas du cloud), et l'inverse. Deux
            // ajouts conditionnels plutôt qu'un test unique, qui perdrait l'un
            // des deux cas.
            const contexteDuTour = pending?.contexteTokens ?? null
            if (contexteDuTour !== null) {
              setMessages(prev => {
                const last = prev[prev.length - 1]
                if (last?.role !== 'assistant' || last.thinking) return prev
                return [...prev.slice(0, -1), { ...last, contexteTokens: contexteDuTour }]
              })
            }
            // Métadonnées de la réponse, telles que le serveur vient de les
            // écrire. Évite de relire la conversation entière après chaque
            // tour, et garde l'heure affichée identique à celle du disque.
            //
            // `sources`/`trace_recherche` suivent le même chemin, pour la même
            // raison : sans ce merge, ils n'apparaîtraient qu'après un F5
            // (relecture de `GET /chat/conversations/{id}`), pas pendant la
            // génération — les deux doivent montrer la même chose, ils lisent
            // la même métadonnée persistée (`core/history.py`).
            //
            // Sauté dans le cas SUSPECT ci-dessus : `chargerConversationDepuisDisque`
            // remplace `messages` en entier avec ces mêmes métadonnées déjà
            // dessus — fusionner ici porterait sur le tableau TRONQUÉ, pour un
            // résultat de toute façon remplacé dès que la relecture répond.
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
          }
          // La trace TRANSITOIRE a fait son office (elle s'est affichée
          // pendant la recherche) ; la trace définitive vit désormais sur le
          // message lui-même, fusionnée juste au-dessus.
          setTraceEnCours([])
          setTraceEnCoursOuverte(false)
          // `en_cours` disparait avec le curseur de frappe ; un `échec` RESTE
          // affiche — il dit que la reponse qu'on vient de lire a ete
          // construite SANS l'image, ce qui change comment la lire. Efface au
          // message suivant, pas ici.
          setVisionAnalyse(v => (v && v.etat === 'échec' ? v : null))
          pendingOllamaStatsRef.current = null
          setStreaming(false)
          setStreamStats(null)
          tokenCountRef.current = 0
          streamStartRef.current = null
          // Pas de lecture à voix haute d'un texte TRONQUÉ : dans le cas
          // suspect, `lastAssistantRef` ne porte que la suite reçue après le
          // retour sur ce fil, jamais la réponse complète.
          if (!etaitSuspecte) onAssistantDone?.(lastAssistantRef.current)
          lastAssistantRef.current = ''
          inPipelineRef.current = false
          cancelledRef.current = false

        } else if (data.type === 'error') {
          inPipelineRef.current = false
          cancelledRef.current = false
          const idxComparaisonErr = comparaisonUserMsgIdxRef.current
          if (idxComparaisonErr >= 0) {
            if (resolutionEnCoursRef.current) {
              // Réponse à un `compare_choix` périmé (double clic, comparaison
              // déjà résolue) : la génération, elle, a bien eu lieu — on
              // réactive juste les boutons pour laisser choisir une autre
              // réponse, sans toucher au reste du panneau.
              resolutionEnCoursRef.current = false
              setMessages(prev => {
                const bloc = prev[idxComparaisonErr]?.comparaison
                if (!bloc) return prev
                const updated = [...prev]
                updated[idxComparaisonErr] = { ...updated[idxComparaisonErr], comparaison: { ...bloc, resolutionEnCours: false } }
                return updated
              })
            } else {
              // Rejetée AVANT tout streaming (compare_models invalide côté
              // serveur) : aucun `compare_token` ne viendra jamais peupler ce
              // panneau, on le referme plutôt que de le laisser figé.
              setMessages(prev => {
                if (!prev[idxComparaisonErr]?.comparaison) return prev
                const updated = [...prev]
                updated[idxComparaisonErr] = { ...updated[idxComparaisonErr], comparaison: undefined }
                return updated
              })
              comparaisonUserMsgIdxRef.current = -1
              compareAccumRef.current = {}
            }
          }
          setMessages(prev => [...prev, { role: 'assistant', content: data.content, isError: true }])
          setStreaming(false)
          setStreamStats(null)
          tokenCountRef.current = 0
          streamStartRef.current = null
          pendingOllamaStatsRef.current = null
          lastAssistantRef.current = ''
          setTraceEnCours([])
          setTraceEnCoursOuverte(false)
          setVisionAnalyse(v => (v && v.etat === 'échec' ? v : null))
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
      if (id) definirConversationAffichee(id)
    })()
  }, [conversationId, definirConversationAffichee])

  /**
   * Charge les messages d'une conversation depuis le DISQUE, à chaque
   * bascule de fil.
   *
   * C'est ici que se joue la correction du bug silencieux d'origine : avant,
   * l'écran repartait de `localStorage` et le backend d'une liste vide. Les
   * deux lisent désormais le même fichier — via `chargerConversationDepuisDisque`,
   * partagée avec le handler `done` (cf. sa déclaration plus haut), qui
   * revérifie lui-même `conversationIdRef` avant d'écrire quoi que ce soit :
   * plus besoin du drapeau `annule` d'une fermeture d'effet ici.
   */
  useEffect(() => {
    if (!conversationId) {
      setMessages([])
      comparaisonUserMsgIdxRef.current = -1
      return
    }
    void chargerConversationDepuisDisque(conversationId)
  }, [conversationId, chargerConversationDepuisDisque])

  const ouvrirConversation = useCallback((id: string) => {
    if (id === conversationId) return
    definirConversationAffichee(id)
    setMessages([])          // évite d'afficher l'ancien fil pendant le chargement
    setStreaming(false)
    comparaisonUserMsgIdxRef.current = -1
    // Tout état attaché au STREAMING du fil qu'on QUITTE doit être remis à
    // zéro ici : un `done`/`error` qui arrive plus tard pour cet ancien fil
    // sera de toute façon filtré (cf. `ws.onmessage`), mais sans ce nettoyage
    // ces compteurs/indicateurs resteraient visuellement collés au nouveau
    // fil qu'on vient d'ouvrir (pipeline « en cours » fantôme, stats d'un
    // autre tour, image « en cours d'analyse » d'une conversation qu'on ne
    // regarde plus).
    tokenCountRef.current = 0
    streamStartRef.current = null
    pendingOllamaStatsRef.current = null
    setStreamStats(null)
    inPipelineRef.current = false
    pipelineUserMsgIdxRef.current = -1
    compareAccumRef.current = {}
    setTraceEnCours([])
    setTraceEnCoursOuverte(false)
    setVisionAnalyse(null)
  }, [conversationId, definirConversationAffichee])

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
      definirConversationAffichee(id)
      setMessages([])
      comparaisonUserMsgIdxRef.current = -1
      setRafraichirConvs(n => n + 1)
      // Même nettoyage qu'`ouvrirConversation`, cf. son commentaire.
      tokenCountRef.current = 0
      streamStartRef.current = null
      pendingOllamaStatsRef.current = null
      setStreamStats(null)
      inPipelineRef.current = false
      pipelineUserMsgIdxRef.current = -1
      compareAccumRef.current = {}
      setTraceEnCours([])
      setTraceEnCoursOuverte(false)
      setVisionAnalyse(null)
    } catch { /* le backend répondra au premier message : rien de bloquant */ }
  }, [definirConversationAffichee])

  /**
   * Suit la position de scroll EN CONTINU, pas seulement entre deux rendus
   * de `messages` : l'utilisateur peut remonter à tout moment pendant un
   * streaming, sans qu'aucun message ne change entre-temps.
   *
   * Le seuil (quelques dizaines de px) absorbe l'arrondi de
   * `scrollIntoView({ behavior: 'smooth' })`, qui ne pose pas toujours
   * `scrollTop` pile sur la valeur exacte du bas.
   */
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const SEUIL_BAS_PX = 64
    const majPosition = () => {
      const proche = el.scrollHeight - el.scrollTop - el.clientHeight < SEUIL_BAS_PX
      procheDuBasRef.current = proche
      setProcheDuBas(proche)
    }
    majPosition()
    el.addEventListener('scroll', majPosition, { passive: true })
    return () => el.removeEventListener('scroll', majPosition)
  }, [])

  /**
   * Défilement automatique — plus JAMAIS inconditionnel (bug rapporté : un
   * token reçu pendant qu'on relit un message plus haut ramenait la vue en
   * bas de force). Deux cas ramènent en bas :
   *
   *   1. l'utilisateur SUIVAIT déjà la conversation (`procheDuBasRef`, posée
   *      par l'écouteur de scroll AVANT que ce nouveau contenu n'arrive —
   *      pas recalculée ici, où le DOM a déjà grandi et fausserait la
   *      mesure) ;
   *   2. l'utilisateur vient d'envoyer un message (`forcerDefilementRef`,
   *      posée par `pushMsg('user', …)`) — l'action qui justifie de suivre,
   *      qu'il ait remonté ou non.
   *
   * Sinon : rien. On ne le ramène jamais de force pendant qu'une réponse
   * s'écrit.
   */
  useEffect(() => {
    const doitDefiler = forcerDefilementRef.current || procheDuBasRef.current
    forcerDefilementRef.current = false
    if (doitDefiler) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  /** Ramène en bas et reprend le suivi — action du bouton affiché quand
   * l'utilisateur a remonté. */
  const reprendreLeSuivi = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    procheDuBasRef.current = true
    setProcheDuBas(true)
  }, [])

  // Ferme le panneau du header ouvert (modèle, recherche web, comparaison,
  // paramètres de la conversation) au clic extérieur à SON conteneur.
  useEffect(() => {
    if (!headerMenuOuvert) return
    const ref = headerMenuOuvert === 'model' ? modelMenuRef
      : headerMenuOuvert === 'web' ? webMenuRef
      : headerMenuOuvert === 'compare' ? compareMenuRef
      : paramsMenuRef
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setHeaderMenuOuvert(null)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [headerMenuOuvert])

  // Ferme le panneau fichiers du composer au clic extérieur — même patron
  // que les panneaux du header, dans son propre conteneur (l'îlot n'est pas
  // le header : un état séparé plutôt qu'une valeur de plus dans
  // `headerMenuOuvert`, qui ne couvre que les panneaux du header).
  useEffect(() => {
    if (!filesPanelOuvert) return
    const onDown = (e: MouseEvent) => {
      if (filesMenuRef.current && !filesMenuRef.current.contains(e.target as Node)) {
        setFilesPanelOuvert(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [filesPanelOuvert])

  /**
   * Modèles disponibles pour la comparaison — même route que le panneau
   * modèle de `ModuleBar`/Réglages (`GET /models`), aplatie par
   * `modelesDisponibles` (`../../normaliser`) plutôt qu'une variante de plus
   * du même parsing. Échec silencieux : le picker reste vide, pas bloquant.
   */
  useEffect(() => {
    let annule = false
    void (async () => {
      try {
        const res = await apiFetch(`${API}/models`)
        if (!res.ok || annule) return
        const d = await res.json()
        if (annule) return
        setModelesDisponiblesListe(modelesDisponibles(d).filter(m => m.disponible))
      } catch { /* backend qui démarre : le picker reste vide */ }
    })()
    return () => { annule = true }
  }, [])

  // Budget de `recherche_approfondie`, relu à chaque ouverture du menu web —
  // seul endroit où l'info-bulle qui le cite est visible (cf. la déclaration
  // de `budgetApprofondie`). Une réponse de refus (401, 500…) a une autre
  // forme : `budgetRechercheApprofondie` rend alors `null`, jamais un nombre.
  useEffect(() => {
    if (headerMenuOuvert !== 'web') return
    let annule = false
    apiFetch(`${API}/context`)
      .then(r => r.json())
      .then((d: unknown) => { if (!annule) setBudgetApprofondie(budgetRechercheApprofondie(d)) })
      .catch(() => { if (!annule) setBudgetApprofondie(null) })
    return () => { annule = true }
  }, [headerMenuOuvert])

  /**
   * Réglages de session — migrés depuis `ModuleBar.tsx`. Requête séparée de
   * celle que `ModuleBar` fait encore sur `/context` (pour son propre
   * `selectedModel`) : même endpoint lu deux fois par deux composants
   * frères, comme `/models` l'était déjà avant cette itération — accepté
   * plutôt que de faire remonter un état de plus par callback.
   */
  useEffect(() => {
    apiFetch(`${API}/context`)
      .then(r => r.json())
      .then((d: Record<string, unknown>) => {
        setStrictMode((d['strict_mode'] as boolean) ?? false)
        // `?? true` et non `?? false` : la clé est absente des
        // `context_session.json` écrits avant ce réglage, et son absence doit
        // valoir « activé », pas « désactivé ».
        setRaisonnement((d['raisonnement'] as boolean) ?? true)
        const instr = (d['instruction_générale'] as string) ?? ''
        setSessionInstruction(instr)
        setInstructionDraft(instr)

        const prefixes = d['prefixes']
        const integres = prefixes && typeof prefixes === 'object'
          ? (prefixes as Record<string, unknown>)['integres']
          : null
        if (integres && typeof integres === 'object' && !Array.isArray(integres)) {
          const actifs: Record<string, boolean> = {}
          for (const [cle, val] of Object.entries(integres as Record<string, unknown>)) {
            actifs[cle] = !!(val && typeof val === 'object' && (val as Record<string, unknown>)['enabled'])
          }
          setPrefixesIntegresActifs(actifs)
        }
        const personnalises = prefixes && typeof prefixes === 'object'
          ? (prefixes as Record<string, unknown>)['personnalises']
          : null
        setPrefixesPersonnalisesActifs(
          Array.isArray(personnalises)
            ? personnalises
              .filter((o): o is Record<string, unknown> => !!o && typeof o === 'object')
              .filter(o => o['prefixe_actif'] && typeof o['trigger'] === 'string' && o['trigger'])
              .map(o => ({
                id: String(o['id'] ?? o['trigger']),
                trigger: o['trigger'] as string,
                desc: typeof o['description'] === 'string' && o['description'] ? o['description'] as string : String(o['nom'] ?? ''),
              }))
            : []
        )
      })
      .catch(() => {})
  }, [])

  /** Consigne DE CETTE CONVERSATION — relue à chaque changement de fil, comme
   * le faisait `chargerAttachements` dans `ModuleBar.tsx` avant la migration.
   * Le `useCallback` (plutôt qu'un `setState` en tête de l'effet lui-même)
   * suit le même patron que `chargerAttachements` : un `setState` posé
   * directement dans le corps d'un effet est signalé par
   * `react-hooks/set-state-in-effect`, pas quand il est atteint via une
   * fonction appelée depuis l'effet. */
  const chargerInstructionFil = useCallback(async () => {
    if (!conversationId) {
      setInstructionFil(''); setInstructionFilDraft('')
      return
    }
    try {
      const res = await apiFetch(`${API}/chat/conversations/${conversationId}`)
      if (!res.ok) return
      const d = await res.json() as Record<string, unknown>
      // Absente sur les conversations d'avant ce champ : `texte()` rend `''`,
      // ce qui est exactement le bon défaut — pas d'invention.
      const consigne = texte(d.instruction)
      setInstructionFil(consigne)
      setInstructionFilDraft(consigne)
    } catch { /* backend qui démarre : panneau vide, sans gravité */ }
  }, [conversationId])

  useEffect(() => { void chargerInstructionFil() }, [chargerInstructionFil])

  const pushContextSettings = useCallback((patch: Record<string, unknown>) => {
    apiFetch(`${API}/context/settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).catch(() => {})
  }, [])

  const handleStrictToggle = useCallback(() => {
    const next = !strictMode
    setStrictMode(next)
    pushContextSettings({ strict_mode: next })
  }, [strictMode, pushContextSettings])

  const handleRaisonnementToggle = useCallback(() => {
    const next = !raisonnement
    setRaisonnement(next)
    pushContextSettings({ raisonnement: next })
  }, [raisonnement, pushContextSettings])

  const handleInstructionSave = useCallback(() => {
    setSessionInstruction(instructionDraft)
    pushContextSettings({ 'instruction_générale': instructionDraft })
  }, [instructionDraft, pushContextSettings])

  const enregistrerInstructionFil = useCallback(async () => {
    if (!conversationId) return
    try {
      const res = await apiFetch(`${API}/chat/conversations/${conversationId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction: instructionFilDraft }),
      })
      // 400 = consigne trop longue (le backend refuse plutôt que de tronquer).
      // On ne touche pas à l'état : le brouillon reste tel quel, donc le bouton
      // reste actif et l'utilisateur peut raccourcir sans avoir rien perdu.
      if (!res.ok) return
      setInstructionFil(instructionFilDraft)
    } catch { /* le brouillon reste : l'utilisateur peut réessayer */ }
  }, [conversationId, instructionFilDraft])

  const providerActif = modelesDisponiblesListe.find(m => m.id === modeleActifId)?.provider
  const { nonSupporte: raisonnementNonSupporte, budgetSeul: raisonnementBudgetSeul } =
    capacitesRaisonnement(providerActif)

  /**
   * Contexte du DERNIER tour mesuré, et seulement s'il a été mesuré par le
   * modèle ACTIF.
   *
   * Ce garde-fou n'est pas une précaution de style : la fenêtre décrit le modèle
   * sélectionné, le numérateur vient du modèle qui a RÉPONDU. Quand
   * l'utilisateur change de modèle au milieu d'un fil, les deux ne mesurent plus
   * la même chose et le rapport serait faux. On n'affiche alors rien — même
   * règle que pour une fenêtre inconnue.
   *
   * L'égalité est littérale parce que les deux valeurs viennent de la MÊME
   * chaîne : `ModuleBar` passe le même identifiant à `onModelChange` (qui remplit
   * `modeleActifId`) et à `pushSettings({'modèle_actif': …})` (que le backend
   * persiste tel quel dans `modèle`).
   *
   * Déclaré AVANT la fenêtre ci-dessous, et pas après, parce que celle-ci en
   * dépend : c'est le numérateur qui dit QUAND la fenêtre devient connaissable
   * (cf. le commentaire de l'effet). Un `useMemo` consommé plus haut qu'il n'est
   * déclaré serait une zone morte temporelle, pas un simple avertissement.
   */
  const contexteDuFil = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.role !== 'assistant' || m.contexteTokens === undefined) continue
      return m.modele === modeleActifId ? m.contexteTokens : null
    }
    return null
  }, [messages, modeleActifId])

  /**
   * Fenêtre de contexte du modèle ACTIF — le DÉNOMINATEUR.
   *
   * Elle vient d'un endpoint (`GET /models/contexte`) et non de la trame
   * `stats`, parce que l'en-tête doit pouvoir l'afficher AVANT le premier
   * message d'une conversation — or la trame n'arrive qu'à la fin d'un tour.
   * Une seule source par valeur, donc rien à faire diverger entre avant et
   * après le premier tour. Le backend ne sonde que le modèle demandé, jamais
   * toute la surface (`core/fenetre_contexte.py`).
   *
   * `null` couvre les trois cas où l'on ne sait pas : fournisseur muet, modèle
   * Ollama pas encore chargé, backend injoignable. L'échec est SILENCIEUX — on
   * ne pose simplement rien, et la jauge disparaît. Un indicateur qui
   * empêcherait le chat de répondre serait un bug ; un indicateur qui s'efface
   * est un désagrément.
   *
   * **La valeur est estampillée de son modèle**, et c'est la seule protection
   * nécessaire : une réponse qui arrive après un changement de modèle porte
   * l'ancien nom, donc ne décrit plus le modèle actif et n'est pas lue. Un
   * drapeau d'annulation ferait le même travail de façon impérative, et une
   * remise à `null` synchrone dans le corps de l'effet déclencherait un rendu
   * en cascade pour rien.
   *
   * ── Quand elle est relue : au changement de modèle ET à chaque tour mesuré ──
   *
   * Le second déclencheur n'est pas un confort. Un modèle Ollama n'est pas
   * résident au repos — `/api/ps` ne liste que les modèles CHARGÉS, et c'est le
   * premier message envoyé qui le charge (mesuré : `qwen2.5:7b` annonce
   * `context_length: 32768` une fois résident, et rien du tout avant). La fenêtre
   * est donc inconnue au montage, et elle devient connaissable à l'instant
   * précis où le premier tour MESURÉ arrive — à aucun autre.
   *
   * Ne relire qu'au changement de modèle laissait la jauge invisible pour tout
   * le premier tour d'une session, puis pour tous les suivants : elle
   * n'apparaissait qu'après un aller-retour de modèle ou un F5, c'est-à-dire en
   * perdant la conversation qui la justifiait. C'est précisément le cas d'usage
   * de l'indicateur, donc le seul qu'il ne faut pas rater.
   *
   * D'où `contexteDuFil` en dépendance. Le coût reste borné : une relecture par
   * tour mesuré, donc aucune pour un fournisseur muet (aucun numérateur n'arrive
   * jamais) et aucune rafale en cours de génération — `contexteDuFil` est un
   * `useMemo` sur `messages`, il ne change pas à chaque morceau de texte reçu.
   */
  const [fenetreLue, setFenetreLue] = useState<{ modele: string; valeur: number | null; source: string | null } | null>(null)

  useEffect(() => {
    if (!modeleActifId) return
    const lire = async () => {
      try {
        const res = await apiFetch(`${API}/models/contexte?modele=${encodeURIComponent(modeleActifId)}`)
        if (!res.ok) return
        const d = await res.json() as { fenetre?: unknown; source?: unknown }
        setFenetreLue({
          modele: modeleActifId,
          valeur: typeof d.fenetre === 'number' && d.fenetre > 0 ? d.fenetre : null,
          source: typeof d.source === 'string' ? d.source : null,
        })
      } catch { /* backend injoignable : pas de fenêtre, donc pas de jauge */ }
    }
    void lire()
    // `contexteDuFil` : relire quand un tour MESURÉ arrive, parce que c'est le
    // moment où le modèle vient d'être chargé et où la fenêtre devient lisible.
    // Cf. le commentaire de la docstring ci-dessus — ce n'est pas une dépendance
    // de confort.
  }, [modeleActifId, contexteDuFil])

  const fenetreContexte = fenetreLue?.modele === modeleActifId ? fenetreLue : null

  /**
   * Contexte du DERNIER tour mesuré, et seulement s'il a été mesuré par le
   * modèle ACTIF.
   *
   * Ce garde-fou n'est pas une précaution de style : la fenêtre décrit le modèle
   * sélectionné, le numérateur vient du modèle qui a RÉPONDU. Quand
   * l'utilisateur change de modèle au milieu d'un fil, les deux ne mesurent plus
   * la même chose et le rapport serait faux. On n'affiche alors rien — même
   * règle que pour une fenêtre inconnue.
   *
   * L'égalité est littérale parce que les deux valeurs viennent de la MÊME
   * chaîne : `ModuleBar` passe le même identifiant à `onModelChange` (qui remplit
   * `modeleActifId`) et à `pushSettings({'modèle_actif': …})` (que le backend
  /** `AT_COMMANDS` filtré sur les préfixes intégrés ACTIFS (Réglages ›
   * Préfixes & commandes), plus les préfixes personnalisés à déclenchement
   * manuel — consommé par l'autocomplete `@` et par le popover « Préfixes @ »
   * ci-dessous, pour que les deux affichent la MÊME liste. `?? true` : tant
   * que `/context` n'a pas répondu (ou pour `@mémoire`, `cle: null`), un
   * préfixe reste affiché plutôt que de disparaître le temps du chargement. */
  const atCommandsActifs = useMemo(() => {
    const integres = AT_COMMANDS
      .filter(c => c.cle === null || (prefixesIntegresActifs?.[c.cle] ?? true))
      .map(c => ({ trigger: c.trigger as string, desc: c.desc as string }))
    const perso = prefixesPersonnalisesActifs.map(p => ({ trigger: p.trigger, desc: p.desc }))
    return [...integres, ...perso]
  }, [prefixesIntegresActifs, prefixesPersonnalisesActifs])

  // ── Autocomplete ──────────────────────────────────────────────────────────

  const suggestions = useMemo(() => {
    if (input.includes(' ')) return []
    if (input.startsWith('@')) return atCommandsActifs.filter(c => c.trigger.startsWith(input))
    // Les commandes `/` incluent une entrée par module INSTALLÉ : on ne propose
    // jamais d'ouvrir quelque chose qui n'est pas là.
    if (input.startsWith('/')) return allSlashCommands(modules).filter(c => c.trigger.startsWith(input))
    return []
  }, [input, modules, atCommandsActifs])

  useEffect(() => { setSelectedSuggestion(0) }, [suggestions])

  const applySuggestion = useCallback((trigger: string) => {
    setInput(trigger + ' ')
  }, [setInput])

  // ── Skill handlers ────────────────────────────────────────────────────────

  const pushMsg = (role: Message['role'], content: string) => {
    // Un message UTILISATEUR est l'action qui justifie de suivre la
    // conversation, même si on avait remonté pour relire — cf. l'effet de
    // défilement plus bas, seul lecteur de cette ref.
    if (role === 'user') forcerDefilementRef.current = true
    setMessages(prev => [...prev, { role, content }])
  }

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

  // ── Comparaison multi-modèles ───────────────────────────────────────────────

  /**
   * Une comparaison est en attente de résolution — dérivé de `messages` et de
   * la ref, jamais un état séparé à maintenir en double (même principe que
   * `thinking.done` plus haut). Bloque l'envoi d'un nouveau message : la
   * conversation ne reprend qu'une fois une réponse choisie, cf. `sendUserText`
   * et la zone de saisie (rendu).
   */
  const comparaisonEnCours = (() => {
    const idx = comparaisonUserMsgIdxRef.current
    return idx >= 0 && !!messages[idx]?.comparaison
  })()
  // Un redémarrage du backend (changement de modules) coupe /ws/chat : le
  // bandeau « Redémarrage requis » prévient d'abord si une réponse est en cours
  // ou si une comparaison attend encore son choix (src/redemarrage.ts).
  useFluxEnCours('une réponse du chat', streaming || comparaisonEnCours)

  const toggleCompareModel = useCallback((id: string) => {
    setCompareModeles(prev => {
      if (prev.includes(id)) return prev.filter(m => m !== id)
      if (prev.length >= 3) return prev
      return [...prev, id]
    })
  }, [])

  /**
   * Résout la comparaison EN COURS en gardant la réponse d'un modèle.
   *
   * Désactive IMMÉDIATEMENT tous les boutons de ce panneau (avant même la
   * réponse du serveur), pour éviter un double envoi sur double clic — cf.
   * `resolutionEnCoursRef`, son miroir synchrone lu par le handler `error`.
   */
  const garderReponse = useCallback((idxMsg: number, modele: string) => {
    // Le garde-fou anti-double-clic vit sur la REF, pas sur l'état React : la
    // ref est lue/posée de façon synchrone, l'état ne l'est pas (cf. les
    // commentaires de `resolutionEnCoursRef`) — un second clic très rapproché
    // doit être arrêté ICI, avant même la première mise à jour d'état.
    if (resolutionEnCoursRef.current) return
    resolutionEnCoursRef.current = true
    setMessages(prev => {
      const bloc = prev[idxMsg]?.comparaison
      if (!bloc) return prev
      const updated = [...prev]
      updated[idxMsg] = { ...updated[idxMsg], comparaison: { ...bloc, resolutionEnCours: true } }
      return updated
    })
    wsRef.current?.send(JSON.stringify({ type: 'compare_choix', conv_id: conversationId, model: modele }))
  }, [conversationId])

  // ── Send ──────────────────────────────────────────────────────────────────

  // Envoi d'un message « normal » (hors commandes /…).
  const sendUserText = useCallback((rawText: string) => {
    if (!connected || comparaisonEnCours) return
    cancelledRef.current = false

    let cleanText = rawText
    let ragOverride: string | undefined
    let strictOverride = false
    let webSearchOverride = webSearch
    // `@image` : force la RÉANALYSE des images attachées pour cette question.
    // Pas de bascule d'interface associée, contrairement à `webSearch` — c'est
    // un geste ponctuel, jamais un mode : une analyse vision coûte 6 à 26 s
    // par image (mesuré), et un mode collant la referait à chaque message.
    let visionOverride = false

    let again = true
    while (again) {
      again = false
      if (cleanText === '@cours' || cleanText.startsWith('@cours ')) {
        ragOverride = 'all'; cleanText = cleanText.replace(/^@cours\s*/, '').trim(); again = true
      } else if (cleanText === '@strict' || cleanText.startsWith('@strict ')) {
        strictOverride = true; cleanText = cleanText.replace(/^@strict\s*/, '').trim(); again = true
      } else if (cleanText === '@web' || cleanText.startsWith('@web ')) {
        webSearchOverride = true; cleanText = cleanText.replace(/^@web\s*/, '').trim(); again = true
      } else if (cleanText === '@image' || cleanText.startsWith('@image ')) {
        visionOverride = true; cleanText = cleanText.replace(/^@image\s*/, '').trim(); again = true
      }
    }

    // Comparaison : active dès 2 modèles cochés au moins (pas de bascule
    // séparée — cf. le commentaire de `compareModeles`). `@web`/`@cours`/
    // `@strict` restent traités exactement comme ci-dessus, indépendamment :
    // ils pilotent la construction du contexte en amont, pas le choix entre
    // mono-modèle et comparaison.
    const compareActive = compareModeles.length >= 2

    if (compareActive) {
      const modeles = [...compareModeles]
      const parModele: Record<string, CompareModelState> = Object.fromEntries(
        modeles.map(m => [m, { content: '', raisonnement: '', termine: false }])
      )
      compareAccumRef.current = {}
      forcerDefilementRef.current = true
      setMessages(prev => {
        comparaisonUserMsgIdxRef.current = prev.length
        return [...prev, { role: 'user', content: rawText, comparaison: { modeles, parModele, resolutionEnCours: false } }]
      })
    } else {
      pushMsg('user', rawText)
      setStreaming(true)
    }
    tokenCountRef.current = 0
    streamStartRef.current = null
    pendingOllamaStatsRef.current = null
    setStreamStats(null)
    inPipelineRef.current = false
    pipelineUserMsgIdxRef.current = -1
    setTraceEnCours([])
    setTraceEnCoursOuverte(false)
    // Un echec d'analyse d'image survit a `done` (il parle de la reponse
    // precedente) ; il ne doit pas survivre a la question SUIVANTE, sinon il
    // parlerait d'un tour que l'utilisateur a deja quitte.
    setVisionAnalyse(null)

    const wsMsg: Record<string, unknown> = { role: 'user', content: cleanText || rawText, effort }
    // Vide au tout premier message : le serveur ouvre alors une conversation et
    // nous renvoie son identifiant (`{"type": "conversation"}`). Côté serveur,
    // « pas d'identifiant » veut dire « poursuis ce que fait cette connexion »,
    // jamais « recommence » — on ne risque donc pas un fil neuf par message.
    if (conversationId) wsMsg.conversation_id = conversationId
    if (effort !== 'direct' && pipelineSteps.length > 0) wsMsg.steps = pipelineSteps
    if (ragOverride) wsMsg.rag_override = ragOverride
    if (strictOverride) wsMsg.strict_override = true
    // `deepSearch` pose TOUJOURS `web_search_override`, quel que soit le
    // provider : la recherche heuristique du classifieur (@web) part donc à
    // chaque recherche approfondie, EN PLUS de l'outil `recherche_approfondie`
    // là où le tool-calling natif est câblé (Ollama et LM Studio, cf.
    // `core/llm.py::stream`), et seule ailleurs — c'est ce qui évite au bouton
    // de rester sans effet sur les autres providers. Les résultats des deux
    // origines sont renumérotés à la suite (`rang_web_existant`).
    if (webSearchOverride || deepSearch) wsMsg.web_search_override = true
    if (deepSearch) wsMsg.deep_search_override = true
    if (visionOverride) wsMsg.vision_override = true
    if (compareActive) wsMsg.compare_models = compareModeles
    lastSentRef.current = wsMsg
    wsRef.current?.send(JSON.stringify(wsMsg))

    if (webSearch && webSearchMode === 'once') setWebSearch(false)
    // Toujours réinitialisé après l'envoi : jamais un mode collant, cf. le
    // commentaire de `deepSearch` à sa déclaration.
    if (deepSearch) setDeepSearch(false)
  }, [connected, comparaisonEnCours, compareModeles, conversationId, effort, pipelineSteps, webSearch, webSearchMode, deepSearch, pushMsg, setWebSearch])

  const send = useCallback(async () => {
    const rawText = input.trim()
    if (!rawText || streaming || comparaisonEnCours) return
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

    // Message normal : délégué à sendUserText.
    sendUserText(rawText)
  }, [
    input, connected, streaming, comparaisonEnCours, sendUserText, modules,
    streamSSE, handleMémoire, handleModèle, handleLacunes, handleNavigate,
  ])

  // ── Attente du premier token : LM Studio charge-t-il ? ──────────────────────

  /**
   * On attend le premier token de l'assistant — la fenêtre pendant laquelle
   * LM Studio peut être en train de charger son modèle.
   *
   * **Volontairement SANS `inPipelineRef.current`**, alors que le curseur
   * clignotant du fil (plus bas) l'exclut, lui. Deux raisons, dans cet ordre :
   * lire un `ref.current` pendant le rendu pour le faire entrer dans une
   * dépendance de hook est précisément ce que `react-hooks/refs` refuse (six
   * avertissements en cascade, mesurés), et la valeur d'un ref ne redéclenche
   * de toute façon aucun rendu — la dépendance serait un mensonge. Ensuite,
   * l'écart est sans conséquence : sonder pendant un pipeline ne fait
   * qu'alimenter un état que le rendu, lui, n'affiche pas dans ce cas.
   */
  const attenteToken = streaming && messages[messages.length - 1]?.role !== 'assistant'

  useEffect(() => {
    if (!attenteToken) return
    let vivant = true
    // 1 s : le chargement se compte en dizaines de secondes (12,1 s pour 3 Go,
    // 119,4 s pour 17,7 Go, mesurés le 2026-09-06), et la sonde côté LM Studio
    // répond en 4 ms de médiane MÊME pendant un chargement (max 164 ms sur
    // 392 sondes). Sonder plus vite n'apprendrait rien de plus.
    const sonder = async () => {
      try {
        const res = await apiFetch(`${API}/models/lmstudio/chargement`)
        // `res.ok` AVANT de lire : un corps d'erreur n'a pas de champ
        // `chargement`, et l'affirmer par un `as` donnerait `undefined` — le
        // piège que `ModuleBar.test.tsx` verrouille ailleurs dans ce dépôt.
        if (!vivant || !res.ok) return
        const d = await res.json() as { chargement?: unknown }
        if (!vivant) return
        // Seul un booléen franc met l'état à jour. Tout le reste (`null` du
        // backend, champ absent, corps inattendu) veut dire « aucune
        // information nouvelle » et laisse l'affichage tel quel.
        if (typeof d.chargement === 'boolean') setChargementLmStudio(d.chargement)
      } catch {
        // Panne réseau : on ne sait pas, donc on ne change rien.
      }
    }
    void sonder()
    const t = setInterval(() => { void sonder() }, 1000)
    return () => {
      vivant = false
      clearInterval(t)
      // Remise à zéro dans le NETTOYAGE, pas dans le corps de l'effet : un
      // `setState` synchrone dans un corps d'effet déclenche des rendus en
      // cascade (`react-hooks/set-state-in-effect`). Sans elle, le libellé
      // survivrait au premier token et se rallumerait sur la réponse suivante
      // avant le premier sondage.
      setChargementLmStudio(null)
    }
  }, [attenteToken])

  // ── Stop ──────────────────────────────────────────────────────────────────

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
        onTitreActif={setTitreConversationActive}
      />
    <main className="flex flex-col flex-1 overflow-hidden relative">
      {/* ── Header de conversation ──
          Trois zones flex (gauche / centre / droite) et non un flux linéaire :
          le chip modèle doit rester au centre VISUEL de la barre quelle que
          soit la longueur du titre à gauche, pas seulement « après le titre ».
          Les deux zones latérales partagent `flex-1` à parts égales, ce qui
          centre la zone du milieu par construction — pas de calcul de largeur. */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-line shrink-0">
        <div className="flex-1 min-w-0 flex items-center">
          <span className="truncate text-sm font-medium text-primary">
            {titreConversationActive}
          </span>
        </div>

        {/* ── Chip modèle : vrai bouton + chevron, ouvre la liste des modèles ── */}
        <div className="relative shrink-0" ref={modelMenuRef}>
          <button
            type="button"
            onClick={() => setHeaderMenuOuvert(v => (v === 'model' ? null : 'model'))}
            aria-haspopup="menu"
            aria-expanded={headerMenuOuvert === 'model'}
            title="Changer de modèle"
            className={`flex items-center gap-1 px-2.5 py-1 rounded-md border transition-colors duration-150 max-w-48 ${
              headerMenuOuvert === 'model'
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-line bg-elevated text-muted hover:text-secondary'
            }`}
          >
            <span className="truncate text-xs font-mono">
              {(() => {
                if (!modeleActifId) return 'Modèle'
                const info = modelesDisponiblesListe.find(m => m.id === modeleActifId)
                return info?.nom?.split(' ')[0] ?? modeleActifId.split(':').pop() ?? modeleActifId
              })()}
            </span>
            <ChevronDown
              size={12}
              className={`shrink-0 transition-transform duration-150 ${headerMenuOuvert === 'model' ? 'rotate-180' : ''}`}
            />
          </button>

          {/*
            Le CONTENU de ce menu (recommandations, verdicts matériel,
            contrôles FLM, mémoire Ollama...) n'est pas rendu ici : cette `div`
            n'est qu'une ANCRE, ciblée par un portail React que `ModuleBar`
            ouvre dans son propre panneau modèle (`modelPanelPortalTarget`
            ci-dessous). C'est le menu déroulant COMPLET qu'on avait avant,
            pas une liste simplifiée — son état et son JSX restent définis à
            un seul endroit (`ModuleBar.tsx`), pour tous les modules qui
            l'utilisent, et le portail ne fait que déplacer où il s'affiche.
          */}
          {headerMenuOuvert === 'model' && (
            <div
              ref={setModelPanelAncre}
              className="absolute top-full left-1/2 -translate-x-1/2 mt-2 w-[30rem] max-w-[90vw] bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20"
            />
          )}
        </div>

        <div className="flex-1 min-w-0 flex items-center justify-end gap-2">
        {/* ── Contexte restant : jauge permanente de la conversation active ──
            N'affiche RIEN quand la fenêtre ou le dernier tour mesuré manquent :
            le composant porte lui-même cette règle, pour qu'il n'y ait qu'un
            endroit à relire (et qu'un test puisse l'éprouver isolément). */}
        <CamembertContexte
          fenetre={fenetreContexte?.valeur ?? null}
          utilise={contexteDuFil}
          source={fenetreContexte?.source ?? null}
        />

        {/* ── Recherche web : icône cliquable + menu déroulable (déplacée du composer) ── */}
        <div className="relative shrink-0" ref={webMenuRef}>
          <div
            className={`flex items-stretch rounded-md border transition-colors duration-150 ${
              webSearch || deepSearch ? 'border-accent/40 bg-accent/10' : 'border-line bg-elevated'
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
                webSearch || deepSearch ? 'text-accent' : 'text-muted hover:text-secondary'
              }`}
            >
              <Globe size={16} className={webSearch && streaming ? 'animate-pulse' : ''} />
              {deepSearch
                ? (
                  <span
                    className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-accent text-on-accent text-[10px] font-mono leading-none flex items-center justify-center"
                    title="Recherche approfondie armée pour le prochain message"
                  >
                    <SearchCheck size={9} />
                  </span>
                )
                : webSearch && (
                  <span className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-accent text-on-accent text-[10px] font-mono leading-none flex items-center justify-center">
                    {webSearchMode === 'once' ? '1×' : '∞'}
                  </span>
                )}
            </button>
            <button
              type="button"
              onClick={() => setHeaderMenuOuvert(v => (v === 'web' ? null : 'web'))}
              aria-haspopup="menu"
              aria-expanded={headerMenuOuvert === 'web'}
              title="Options de recherche web"
              className={`px-1 rounded-r-md border-l transition-colors duration-150 ${
                webSearch || deepSearch
                  ? 'border-accent/30 text-accent hover:bg-accent/10'
                  : 'border-line text-muted hover:text-secondary hover:bg-elevated'
              }`}
            >
              <ChevronDown
                size={13}
                className={`transition-transform duration-150 ${headerMenuOuvert === 'web' ? 'rotate-180' : ''}`}
              />
            </button>
          </div>

          {headerMenuOuvert === 'web' && (
            <div className="absolute top-full right-0 mt-2 w-64 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20">
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

              <div className="p-1.5 border-t border-line">
                <button
                  type="button"
                  onClick={() => setDeepSearch(v => !v)}
                  aria-pressed={deepSearch}
                  title={infoBulleRechercheApprofondie(budgetApprofondie)}
                  className={`w-full text-left px-2.5 py-1.5 rounded-sm transition-colors duration-150 flex items-start gap-2 ${
                    deepSearch ? 'bg-accent/10' : 'hover:bg-surface'
                  }`}
                >
                  <span className="shrink-0 w-4 inline-flex justify-center pt-0.5">
                    <SearchCheck size={13} className={deepSearch ? 'text-accent' : 'text-muted'} />
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className={`block text-xs ${deepSearch ? 'text-accent font-medium' : 'text-secondary'}`}>
                      Recherche approfondie
                    </span>
                    <span className="block text-[11px] text-muted">
                      Plusieurs requêtes enchaînées, pour ce message uniquement
                    </span>
                  </span>
                  {deepSearch && <Check size={13} className="text-accent shrink-0 mt-0.5" />}
                </button>
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

        {/* ── Comparaison multi-modèles : icône + menu à cocher (déplacée du composer) ── */}
        <div className="relative shrink-0" ref={compareMenuRef}>
          <button
            type="button"
            onClick={() => setHeaderMenuOuvert(v => (v === 'compare' ? null : 'compare'))}
            aria-haspopup="menu"
            aria-expanded={headerMenuOuvert === 'compare'}
            aria-pressed={compareModeles.length >= 2}
            title={compareModeles.length >= 2
              ? `Comparaison active — ${compareModeles.length} modèles`
              : 'Comparer plusieurs modèles côte à côte'}
            className={`relative p-2.5 rounded-md border transition-colors duration-150 ${
              compareModeles.length >= 2
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-line bg-elevated text-muted hover:text-secondary'
            }`}
          >
            <Columns3 size={16} />
            {compareModeles.length > 0 && (
              <span className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-accent text-on-accent text-[10px] font-mono leading-none flex items-center justify-center">
                {compareModeles.length}
              </span>
            )}
          </button>

          {headerMenuOuvert === 'compare' && (
            <div className="absolute top-full right-0 mt-2 w-64 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20">
              <div className="px-3 py-2.5 border-b border-line">
                <span className="text-xs font-medium text-primary flex items-center gap-2">
                  <Columns3 size={13} className={compareModeles.length >= 2 ? 'text-accent' : 'text-muted'} />
                  Comparer des modèles
                </span>
                <p className="text-[11px] text-muted mt-1">2 à 3 modèles, réponses côte à côte.</p>
              </div>
              <div className="p-1.5 space-y-0.5 max-h-64 overflow-y-auto">
                {modelesDisponiblesListe.length === 0 ? (
                  <p className="px-2.5 py-2 text-xs text-muted">Aucun modèle disponible.</p>
                ) : modelesDisponiblesListe.map(m => {
                  const checked = compareModeles.includes(m.id)
                  const disabled = !checked && compareModeles.length >= 3
                  return (
                    <label
                      key={m.id}
                      className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-sm ${
                        disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer hover:bg-surface'
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={disabled}
                        onChange={() => toggleCompareModel(m.id)}
                        className="shrink-0"
                      />
                      <span className="text-xs text-secondary truncate">{m.nom}</span>
                    </label>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        {/* ── Paramètres de la conversation : effort + rappel des commandes ── */}
        <div className="relative shrink-0" ref={paramsMenuRef}>
          <button
            type="button"
            onClick={() => setHeaderMenuOuvert(v => (v === 'params' ? null : 'params'))}
            aria-haspopup="menu"
            aria-expanded={headerMenuOuvert === 'params'}
            title="Paramètres de la conversation"
            className={`p-2.5 rounded-md border transition-colors duration-150 ${
              headerMenuOuvert === 'params'
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-line bg-elevated text-muted hover:text-secondary'
            }`}
          >
            <Settings size={16} />
          </button>

          {headerMenuOuvert === 'params' && (
            <div className="absolute top-full right-0 mt-2 w-80 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20 max-h-[70vh] overflow-y-auto">
              {/* ── Trois toggles — migrés depuis le panel « skills » de
                  ModuleBar, fusionnés ici pour ne plus exister à deux endroits. ── */}
              <div className="p-3 border-b border-line space-y-3">
                <div className="flex items-center gap-5 flex-wrap">
                  {onTtsToggle && (
                    <div className="flex items-center gap-2">
                      <Toggle checked={!!ttsEnabled} onChange={onTtsToggle} label="Lecture auto" />
                      {/* Trois états distincts, et l'ordre compte : la synthèse précède
                          toujours la lecture. Annoncer « lecture... » pendant une synthèse
                          de 49 s (mesuré sur un message long) donnait une interface qui
                          prétend jouer un son qu'on n'entend pas. */}
                      <span className="text-xs text-secondary">
                        {synthesizingText ? 'synthèse...' : speakingText ? 'lecture...' : 'lecture auto'}
                      </span>
                    </div>
                  )}
                  <div className="flex items-center gap-2">
                    <Toggle checked={strictMode} onChange={handleStrictToggle} label="Mode strict" />
                    <span className="text-xs text-secondary">mode strict</span>
                  </div>
                </div>

                {/* Trois variantes selon `providerActif` : gemini n'a pas de toggle
                    du tout (aucun effet à annoncer), les cinq fournisseurs cloud
                    OpenAI-compatibles gardent un toggle qui agit réellement (le
                    budget de tokens) mais un libellé et un texte qui ne prétendent
                    plus à une réflexion visible qu'ils ne produisent jamais, et
                    ollama/flm gardent le texte d'origine, inchangé. */}
                <div className="space-y-1.5">
                  {raisonnementNonSupporte ? (
                    <p className="text-xs text-muted leading-relaxed">
                      Réflexion du modèle — non disponible sur Gemini : ce réglage
                      n'a aucun effet sur ce fournisseur, il n'apparaît donc pas ici.
                    </p>
                  ) : (
                    <>
                      <div className="flex items-center gap-2">
                        <Toggle checked={raisonnement} onChange={handleRaisonnementToggle}
                                label={raisonnementBudgetSeul ? 'Budget de réflexion (tokens)' : 'Réflexion du modèle'} />
                        <span className="text-xs text-secondary">
                          {raisonnementBudgetSeul ? 'budget de réflexion (tokens)' : 'réflexion du modèle'}
                        </span>
                      </div>
                      <p className="text-xs text-muted leading-relaxed">
                        {raisonnementBudgetSeul
                          ? (raisonnement
                              ? "Ce fournisseur n'affiche pas sa réflexion : ce réglage relève "
                                + 'seulement le plafond de tokens de la réponse, au cas où le '
                                + 'modèle réfléchit en interne sans le montrer.'
                              : 'Plafond de tokens standard. Aucun fournisseur cloud (hors NPU '
                                + 'local) ne montre sa réflexion ici de toute façon.')
                          : (raisonnement
                              ? 'Le modèle réfléchit avant de répondre : les réponses sont plus '
                                + 'sûres sur les questions difficiles, mais arrivent beaucoup plus '
                                + "tard. Sa réflexion s'affiche pendant l'attente."
                              : 'Le modèle répond directement, sans réfléchir à voix haute : '
                                + 'beaucoup plus rapide, mais moins fiable sur un calcul ou un '
                                + 'raisonnement en plusieurs étapes.')}
                      </p>
                    </>
                  )}
                </div>
              </div>

              <div className="p-3 border-b border-line space-y-1">
                <p className="text-xs text-muted uppercase tracking-wide mb-1">Préfixes @</p>
                {atCommandsActifs.map((c, i) => (
                  // Index en tête de clé : un préfixe personnalisé peut porter
                  // le même trigger littéral qu'un intégré ou qu'un autre
                  // personnalisé — `normaliser_prefixes` valide la FORME,
                  // jamais l'unicité entre triggers (cf. sa docstring, backend).
                  <div key={`${i}-${c.trigger}`} className="flex items-baseline gap-2">
                    <span className="text-xs font-mono text-accent2 shrink-0">{c.trigger}</span>
                    <span className="text-xs text-muted truncate">{c.desc}</span>
                  </div>
                ))}
              </div>

              <div className="p-3 border-b border-line space-y-1">
                <p className="text-xs text-muted uppercase tracking-wide mb-1">Commandes /</p>
                {allSlashCommands(modules).map(c => (
                  <div key={c.trigger} className="flex items-baseline gap-2">
                    <span className="text-xs font-mono text-accent2 shrink-0">{c.trigger}</span>
                    <span className="text-xs text-muted truncate">{c.desc}</span>
                  </div>
                ))}
              </div>

              {/* ── Consignes — migrées depuis le panel « skills » de ModuleBar. ── */}
              <div className="p-3 border-b border-line space-y-2">
                <p className="text-xs text-muted uppercase tracking-wide">Consigne générale</p>
                <Textarea
                  value={instructionDraft}
                  onChange={e => setInstructionDraft(e.target.value)}
                  placeholder="Ex : répondre en LaTeX, ne pas utiliser de métaphores..."
                  rows={2}
                  className="w-full text-xs"
                />
                <p className="text-xs text-muted">S'applique à toutes les conversations, et se garde.</p>
                <Button variant="secondary" size="sm" onClick={handleInstructionSave} disabled={instructionDraft === sessionInstruction}>
                  Sauvegarder
                </Button>
              </div>

              {/* Consigne du fil — juste sous l'instruction de session, exprès : leur
                  différence est une différence de PORTÉE, et elle ne se voit que si
                  les deux sont côte à côte au moment de choisir laquelle remplir. */}
              <div className="p-3 space-y-2">
                <p className="text-xs text-muted uppercase tracking-wide">Consigne de cette conversation</p>
                {conversationId ? (
                  <>
                    <Textarea
                      value={instructionFilDraft}
                      onChange={e => setInstructionFilDraft(e.target.value)}
                      placeholder="Ex : dans ce fil, réponds en anglais et cite tes sources..."
                      rows={3}
                      className="w-full text-xs"
                    />
                    <p className="text-xs text-muted">
                      Ne s'applique qu'à ce fil, et le suit tant qu'il existe.
                      {' '}{instructionFilDraft.length}/{MAX_INSTRUCTION_FIL}
                    </p>
                    <Button variant="secondary" size="sm"
                            onClick={() => void enregistrerInstructionFil()}
                            disabled={instructionFilDraft === instructionFil
                                      || instructionFilDraft.length > MAX_INSTRUCTION_FIL}>
                      Sauvegarder
                    </Button>
                  </>
                ) : (
                  <p className="text-xs text-muted">
                    Aucune conversation ouverte : écrivez un message pour en commencer une.
                  </p>
                )}
              </div>
            </div>
          )}
        </div>
        </div>
      </div>

      <div ref={containerRef} className="flex-1 overflow-y-auto px-6 py-6 space-y-5">
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
                  ? 'px-4 py-3 rounded-tl-lg rounded-tr-lg rounded-bl-lg rounded-br-[4px] bg-elevated border border-line text-sm leading-relaxed text-primary'
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
                  {msg.comparaison && (
                    <ComparaisonView
                      msgIdx={i}
                      comparaison={msg.comparaison}
                      onGarder={modele => garderReponse(i, modele)}
                      collapsedRaisonnement={collapsedRaisonnementCompare}
                      onToggleRaisonnement={modele => setCollapsedRaisonnementCompare(prev => ({
                        ...prev,
                        [`${i}-${modele}`]: !(prev[`${i}-${modele}`] ?? (msg.comparaison?.parModele[modele]?.content.length ?? 0) > 0),
                      }))}
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
        {visionAnalyse && visionAnalyse.etat !== 'terminée' && (
          /* Analyse vision d'une image attachee : 6 a 26 s par image, avant le
             premier token. Le cas `terminée` n'affiche RIEN — l'analyse est
             alors dans le prompt, et le resultat sera visible dans la reponse
             elle-meme ; une ligne « fait » de plus serait du bruit. */
          <div className="flex justify-start items-center gap-2">
            {visionAnalyse.etat === 'échec' ? (
              <span className="text-xs font-mono text-warning">
                image non lue ({visionAnalyse.fichier}) — reponse construite sans elle
              </span>
            ) : (
              <>
                <Loader2 size={13} className="animate-spin text-accent2" />
                <span className="text-xs font-mono text-muted">
                  lecture de l’image {visionAnalyse.fichier}
                  {visionAnalyse.total > 1 && ` (${visionAnalyse.index}/${visionAnalyse.total})`}
                  {visionAnalyse.reste > 0 && ` · ${visionAnalyse.reste} au tour suivant`}
                  …
                </span>
              </>
            )}
          </div>
        )}
        {streaming && messages[messages.length - 1]?.role !== 'assistant' && !inPipelineRef.current && (
          <div className="flex justify-start items-center gap-2">
            <span className="text-xs font-mono text-accent2 animate-pulse">▍</span>
            {/* Le libellé n'apparaît QUE sur un `true` franc : `null` (LM Studio
                injoignable, 500 transitoire, modèle non LM Studio) laisse le
                curseur seul, comme avant ce lot. Et il n'y a pas de
                pourcentage — LM Studio n'en expose aucun sur son API HTTP
                (mesuré, cf. `core/models.py:etat_modele_lmstudio`). */}
            {chargementLmStudio === true && (
              <span className="text-xs font-mono text-muted">
                chargement du modèle en cours…
              </span>
            )}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {!procheDuBas && (
        <button
          type="button"
          onClick={reprendreLeSuivi}
          title="Reprendre le suivi de la conversation"
          className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-1
                     rounded-full bg-accent px-3 py-1.5 text-xs text-on-accent shadow-lg
                     hover:opacity-90"
        >
          <ChevronDown size={13} />
          Reprendre le suivi
        </button>
      )}

      {/*
        Chat ne demande plus rien à la barre VISIBLE de `ModuleBar` : le
        bouton "Fichiers" et le micro sont portés dans l'îlot du composer
        (`fileButtonAncre`/`filePanelAncre`/`micButtonAncre`, à gauche des
        pilules d'effort et à droite d'elles), le modèle se pilote depuis le
        chip du header (`modelPanelAncre`), les pilules d'effort vivent dans
        l'îlot, et `showSkills` a migré vers le popover fusionné. `ModuleBar`
        garde `showFile`/`showMic`/`showModel`/`showEffort` à `true` — c'est ce
        qui fait tourner ses effets de chargement (fichiers, modèles, matériel,
        pipeline) — mais ne rend plus RIEN en place pour Chat une fois ces
        portails montés (`hasAnyContent` côté `ModuleBar.tsx`) : `<ModuleBar>`
        n'apparaît donc plus du tout comme une barre séparée.
      */}
      <ModuleBar
        module="chat"
        conversationId={conversationId}
        showFile
        contextFenetre={fenetreContexte?.valeur ?? null}
        contextTokensUtilises={contexteDuFil}
        contextSource={fenetreContexte?.source ?? null}
        fileButtonPortalTarget={fileButtonAncre}
        filePanelOpen={filesPanelOuvert}
        onFilePanelOpenChange={setFilesPanelOuvert}
        filePanelPortalTarget={filePanelAncre}
        showMic
        micButtonPortalTarget={micButtonAncre}
        showModel
        hideModelButton
        modelPanelOpen={headerMenuOuvert === 'model'}
        onModelPanelOpenChange={ouvert => setHeaderMenuOuvert(ouvert ? 'model' : null)}
        modelPanelPortalTarget={modelPanelAncre}
        showEffort
        hideEffortPills
        onModelChange={setModeleActifId}
        onTranscribed={(t) => setInput(prev => prev + t)}
        effort={effort}
        onEffortChange={setEffort}
        pipelineSteps={pipelineSteps}
        onPipelineStepsChange={setPipelineSteps}
        uploadFilesRef={uploadFilesRef}
      />

      <div className="border-t border-line px-4 py-4 relative">
        {suggestions.length > 0 && (
          <div className="absolute bottom-full left-4 mb-2 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-10 min-w-60">
            {suggestions.map((s, i) => (
              <button
                key={`${i}-${s.trigger}`}
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

        {/*
          L'« îlot » : un seul conteneur arrondi pour le texte ET la rangée de
          contrôles, au lieu d'un `<Textarea>` bordé posé à côté d'un bouton
          d'envoi séparé. `bare` retire le cadre propre du `Textarea` — sinon
          son cadre et celui de l'îlot se cumuleraient en un double cadre.
        */}
        <div className="rounded-2xl border border-line bg-elevated/60 focus-within:border-accent/40 transition-colors duration-150 px-4 pt-3 pb-2">
          <Textarea
            bare
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={e => {
              const brut = Array.from(e.clipboardData?.files ?? [])
              if (brut.length === 0) return
              // Un screenshot collé arrive souvent sans extension reconnue
              // (nom vide ou générique) — on la complète depuis le type MIME
              // avant de filtrer, sinon `EXTENSIONS_ACCEPTEES` le rejette à tort.
              const renommes = brut.map(f =>
                /\.[a-z0-9]+$/i.test(f.name)
                  ? f
                  : new File([f], `collage-${Date.now()}.${f.type.split('/').pop() || 'png'}`, { type: f.type })
              )
              const supportes = renommes.filter(f => {
                const ext = f.name.split('.').pop()?.toLowerCase() ?? ''
                return (EXTENSIONS_ACCEPTEES as readonly string[]).includes(ext)
              })
              // Rien de supporté (ex. un .zip copié) : on laisse le collage de
              // texte normal du presse-papier suivre son cours, PAS avant —
              // `preventDefault` ne doit avaler le collage que si on l'utilise
              // réellement.
              if (supportes.length === 0) return
              e.preventDefault()
              // Le point d'entrée est celui du panneau "Fichiers"
              // (`uploadFiles`), pour l'indexation RAG/vision déjà en place —
              // pas un chemin parallèle. `generateSummary: false` : coller vite
              // veut attacher vite, pas déclencher le résumé automatique.
              uploadFilesRef.current?.(supportes, { generateSummary: false })
            }}
            disabled={streaming || comparaisonEnCours}
            placeholder={
              !connected ? 'Connexion au serveur...'
              : comparaisonEnCours ? 'Choisissez une réponse pour continuer...'
              : 'Écrivez à Épure... (@web pour forcer une recherche)'
            }
            rows={1}
            className="w-full bg-transparent px-0"
            style={{ minHeight: '40px', maxHeight: '160px' }}
            onInput={e => {
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = `${Math.min(el.scrollHeight, 160)}px`
            }}
          />

          <div className="flex items-center gap-1.5 mt-2">
            {/*
              Bouton "Fichiers" — le VRAI bouton de `ModuleBar` (badge du
              nombre de fichiers attachés compris), porté ici par portail
              (`fileButtonAncre`), pas recréé. Le panneau s'ouvre au-dessus
              (`bottom-full`), comme les menus du composer avant qu'ils ne
              migrent dans le header (§ itération 2) — l'îlot est en bas de
              l'écran, donc ses panneaux s'ouvrent vers le haut.
            */}
            <div className="relative shrink-0" ref={filesMenuRef}>
              <div ref={setFileButtonAncre} />
              {filesPanelOuvert && (
                <div
                  ref={setFilePanelAncre}
                  className="absolute bottom-full left-0 mb-2 w-80 bg-elevated border border-line rounded-md shadow-md overflow-hidden z-20 max-h-72 overflow-y-auto"
                />
              )}
            </div>

            {/* Niveaux d'effort — déplacés depuis le popover « Paramètres de
                la conversation » (itération 3) : ils vivent maintenant ici,
                dans l'îlot du composer, pas dans les deux endroits à la fois. */}
            {(['direct', 'low', 'medium', 'high', 'adaptive'] as EffortLevel[]).map(e => (
              <button
                key={e}
                onClick={() => setEffort(e)}
                className={`px-3 py-1 rounded-full text-xs font-medium transition-colors duration-150 ${
                  effort === e
                    ? 'bg-gradient-primary text-on-accent'
                    : 'text-muted hover:text-secondary hover:bg-elevated'
                }`}
              >
                {EFFORT_LABELS[e]}
              </button>
            ))}

            {/* Micro — même principe que le bouton "Fichiers" ci-dessus :
                le vrai bouton de `ModuleBar` (icône selon `recording`/
                `transcribing`), porté par portail (`micButtonAncre`). */}
            <div ref={setMicButtonAncre} className="shrink-0" />

            <div className="flex-1" />

            {streaming ? (
              <button
                onClick={stop}
                title="Arrêter la génération"
                className="p-2.5 rounded-md bg-error/90 text-on-accent shadow-sm hover:opacity-90 transition-all duration-150 shrink-0"
              >
                <Square size={16} fill="currentColor" />
              </button>
            ) : comparaisonEnCours ? (
              <button
                disabled
                title="Choisissez une réponse pour continuer"
                className="p-2.5 rounded-md border border-line text-muted opacity-40 cursor-not-allowed shrink-0"
              >
                <Send size={16} />
              </button>
            ) : (
              <button
                onClick={() => { send() }}
                disabled={!input.trim()}
                title="Envoyer"
                className="p-2.5 rounded-full bg-gradient-primary text-on-accent shadow-sm hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 shrink-0"
              >
                <Send size={16} />
              </button>
            )}
          </div>
        </div>
        {!connected && (
          <div className="mt-2 text-xs font-mono text-error">ws déconnecté — reconnexion...</div>
        )}
      </div>
    </main>
    </div>
  )
}
