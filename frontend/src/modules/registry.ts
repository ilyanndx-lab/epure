import { lazy, type ComponentType } from 'react'

/**
 * Registre frontend des modules — remplace les unions `Module` codées en dur et
 * la cascade `activeModule === ...` d'App.tsx.
 *
 * Résout un id de module vers son composant React :
 *  - modules core : import paresseux (lazy) des composants de src/components ;
 *  - modules ajoutés : découverts dans src/modules/generated/<id>/Component.tsx
 *    via import.meta.glob (aucun import à écrire à la main).
 *
 * Les métadonnées d'affichage (label, icône, ordre, status) restent pilotées par
 * les manifestes backend (GET /modules) — voir src/modules.ts. Ici on ne gère que
 * la résolution id → composant.
 */

/** Props partagées passées à tout module (les composants ignorent ce qu'ils n'utilisent pas). */
export interface SharedModuleProps {
  onAssistantDone?: (text: string) => void
  playSpeech?: (text: string) => void
  stopSpeech?: () => void
  speakingText?: string | null
  onNavigate?: (module: string) => void
  ttsEnabled?: boolean
  onTtsToggle?: () => void
}

export interface ModuleDef {
  id: string
  label: string
  icon: string  // nom d'icône lucide-react
  component: ComponentType<SharedModuleProps>
  core: boolean
}

type ModuleLoader = () => Promise<{ default: ComponentType<SharedModuleProps> }>

/** Charge un composant en lazy (cast localisé pour tolérer des props plus étroites). */
function lazyComponent(loader: ModuleLoader): ComponentType<SharedModuleProps> {
  return lazy(loader) as ComponentType<SharedModuleProps>
}

// ── Modules core (label/icône de repli ; le manifeste backend fait foi) ──────
//
// Ne restent ici que le CŒUR — les modules livrés avec Épure et jamais
// désinstallables. Les six installables (code, docs, flashcards, kholle,
// reviseur, rangement) sont partis dans `modules-catalogue/` : leur composant
// n'est plus dans le bundle, et une fois installé il atterrit dans
// `generated/<id>/Component.tsx` où le glob ci-dessous le découvre comme
// n'importe quel module ajouté. C'est ce qui donne son sens au catalogue : le
// code d'un module non installé n'est pas chez l'utilisateur.
const CORE_DEFS: ModuleDef[] = [
  { id: 'chat',       label: 'Chat',        icon: 'MessageSquare', core: true, component: lazyComponent(() => import('./chat/Component')) },
  { id: 'admin',      label: 'Admin',       icon: 'FolderCog',     core: true, component: lazyComponent(() => import('./admin/Component')) },
  { id: 'history',    label: 'Historique',  icon: 'Clock',         core: true, component: lazyComponent(() => import('./history/Component')) },
  { id: 'settings',   label: 'Réglages',    icon: 'Settings',      core: true, component: lazyComponent(() => import('./settings/Component')) },
  // Atelier : outil transverse (pas un module backend), composant dans components/.
  { id: 'workshop',   label: 'Atelier',     icon: 'Hammer',        core: true, component: lazyComponent(() => import('../components/Workshop')) },
]

// ── Modules ajoutés (composants générés) ─────────────────────────────────────
// chemin : './generated/<id>/Component.tsx'
// On EXCLUT les dossiers préfixés '_' (ex. _workshop_check_<id>, créé/supprimé
// par le type-check de l'atelier). Sinon leur apparition/disparition modifie
// l'ensemble du glob → Vite recharge toute la page (F5) en pleine revue.
const generatedLoaders = import.meta.glob(['./generated/**/*.tsx', '!./generated/_*/**'])
const GENERATED_DEFS: ModuleDef[] = Object.entries(generatedLoaders)
  .filter(([path]) => !(path.split('/')[2] ?? '').startsWith('_'))
  .map(([path, loader]) => {
  const id = path.split('/')[2] ?? path
  return {
    id,
    label: id,
    icon: 'Box',
    core: false,
    component: lazyComponent(loader as ModuleLoader),
  }
})

const REGISTRY: Record<string, ModuleDef> = {}
for (const def of [...CORE_DEFS, ...GENERATED_DEFS]) REGISTRY[def.id] = def

/** Composant + métadonnées de repli pour un id donné (undefined si inconnu). */
export function getModuleDef(id: string): ModuleDef | undefined {
  return REGISTRY[id]
}

/** Tous les modules connus du frontend (core + générés). */
export function allModuleDefs(): ModuleDef[] {
  return Object.values(REGISTRY)
}
