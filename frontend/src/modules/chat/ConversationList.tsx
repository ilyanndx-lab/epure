import { useCallback, useEffect, useState } from 'react'
import { MessageSquarePlus, PanelLeftClose, PanelLeftOpen, Pencil, Trash2 } from 'lucide-react'
import { Button } from '../../components/ui'
import {
  chargerConversations, renommerConversation, supprimerConversation,
  type ConvEntry,
} from './conversations'

/**
 * Liste des conversations du chat — étape 6 de docs/conversations-persistees.md.
 *
 * ── Pourquoi ce composant vit ICI et pas dans `components/Sidebar.tsx` ────────
 *
 * La Sidebar est la navigation ENTRE MODULES. Y loger les conversations
 * mélangerait deux axes : on ne navigue pas « vers une conversation » comme on
 * navigue vers l'Atelier ou les Réglages. Rendu dans le module chat, ce panneau
 * disparaît naturellement quand on en sort, et aucun autre module n'hérite d'une
 * notion qui ne le concerne pas.
 *
 * ── Toutes les frontières `.json()` sont normalisées ──────────────────────────
 *
 * `liste()` / `texte()` de `src/normaliser.ts`, à chaque réponse. Ce n'est pas
 * de la prudence décorative : `GET /chat/conversations` répond 401 tant que le
 * token n'est pas appairé (au premier rendu, c'est fréquent), et un
 * `as {conversations: X[]}` sur ce corps donnerait `undefined`, que le
 * `.map()` du rendu ferait planter au tour suivant — dans un chunk minifié.
 */

interface Props {
  /** Conversation ouverte, ou `''` si aucune (rien n'a encore été envoyé). */
  courante: string
  onOuvrir: (id: string) => void
  onNouvelle: () => void
  /** Incrémenté par le parent pour forcer un rechargement (titre, nouveau tour). */
  rafraichir: number
  /** Replié : le panneau se réduit à un rail, la largeur est rendue au chat. */
  replie: boolean
  onBasculerRepli: () => void
  /**
   * Reporte le libellé de la conversation ACTIVE (`courante`) — c'est ce
   * composant qui tient l'index (`conversations`), et donc la seule source du
   * titre. Le header de conversation de `Component.tsx` s'en sert plutôt que
   * de relire l'index de son côté, ce qui l'aurait fait diverger de la même
   * liste au moindre titrage automatique.
   */
  onTitreActif?: (titre: string) => void
}

/** Libellé d'une conversation sans titre : le titrage arrive après le 1er tour. */
function libelle(c: ConvEntry): string {
  if (c.titre) return c.titre
  if (c.apercu) return c.apercu.slice(0, 60)
  return 'Nouvelle conversation'
}

export default function ConversationList({
  courante, onOuvrir, onNouvelle, rafraichir, replie, onBasculerRepli, onTitreActif,
}: Props) {
  const [conversations, setConversations] = useState<ConvEntry[]>([])
  const [enEdition, setEnEdition] = useState<string>('')
  const [brouillon, setBrouillon] = useState<string>('')

  const recharger = useCallback(async () => {
    setConversations(await chargerConversations())
  }, [])

  useEffect(() => { void recharger() }, [recharger, rafraichir])

  useEffect(() => {
    const active = conversations.find(c => c.id === courante)
    onTitreActif?.(active ? libelle(active) : 'Nouvelle conversation')
  }, [conversations, courante, onTitreActif])

  const renommer = useCallback(async (id: string) => {
    const titre = brouillon.trim()
    setEnEdition('')
    if (!titre) return
    await renommerConversation(id, titre)
    await recharger()
  }, [brouillon, recharger])

  const supprimer = useCallback(async (id: string) => {
    await supprimerConversation(id)
    // Recharger DANS TOUS LES CAS : si la conversation avait déjà disparu côté
    // serveur (404), la retirer de l'écran est justement ce qu'il faut faire.
    await recharger()
    if (id === courante) onNouvelle()
  }, [courante, onNouvelle, recharger])

  /**
   * Replié : un RAIL, pas un panneau caché.
   *
   * La largeur est réellement rendue au chat (224 px → 32 px) — ce n'est pas un
   * `hidden` qui garderait la boîte. Un rail plutôt qu'une disparition totale :
   * le bouton qui ramène le panneau doit rester là où le panneau était, sinon il
   * faut le chercher. C'est le motif des barres latérales d'éditeurs, et il
   * évite d'ajouter une barre d'en-tête au chat, qui coûterait de la hauteur en
   * permanence pour un réglage qu'on touche rarement.
   */
  if (replie) {
    return (
      <aside className="w-8 shrink-0 border-r border-subtle flex flex-col items-center py-2 gap-2 h-full">
        <button className="text-muted hover:text-primary p-1"
                title="Afficher les conversations"
                aria-label="Afficher les conversations"
                aria-expanded={false}
                onClick={onBasculerRepli}>
          <PanelLeftOpen size={16} />
        </button>
        <button className="text-muted hover:text-primary p-1"
                title="Nouvelle conversation"
                aria-label="Nouvelle conversation"
                onClick={onNouvelle}>
          <MessageSquarePlus size={16} />
        </button>
      </aside>
    )
  }

  return (
    <aside className="w-56 shrink-0 border-r border-subtle flex flex-col h-full">
      <div className="p-2 border-b border-subtle flex items-center gap-1">
        <Button variant="secondary" size="sm" className="flex-1 justify-start gap-2"
                onClick={onNouvelle}>
          <MessageSquarePlus size={14} />
          Nouvelle conversation
        </Button>
        <button className="text-muted hover:text-primary p-1 shrink-0"
                title="Réduire les conversations"
                aria-label="Réduire les conversations"
                aria-expanded={true}
                onClick={onBasculerRepli}>
          <PanelLeftClose size={16} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {conversations.length === 0 && (
          <p className="px-3 py-4 text-xs text-muted">
            Aucune conversation. Écrivez un message pour en commencer une.
          </p>
        )}
        {conversations.map((c, i) => (
          <div key={c.id}>
            {/* Intercalaire entre deux conversations — un simple repère visuel,
                pas une séparation fonctionnelle : delibérément discret (1px,
                turquoise à faible opacité) pour ne pas rivaliser avec le
                surlignage de la conversation active ci-dessous. */}
            {i > 0 && (
              <div className="flex justify-center py-0.5" aria-hidden="true">
                <span className="block w-8 h-px rounded-full bg-accent2/25" />
              </div>
            )}
            <div className={`group flex items-center gap-1 mx-1 my-0.5 px-2 py-1.5 text-sm cursor-pointer rounded-md border-l-2 ${
                 c.id === courante
                   ? 'border-accent bg-accent/10 text-primary'
                   : 'border-transparent text-secondary hover:bg-elevated'
               }`}>
            {enEdition === c.id ? (
              <input
                autoFocus
                className="flex-1 min-w-0 bg-transparent border-b border-accent text-sm outline-none"
                value={brouillon}
                onChange={e => setBrouillon(e.target.value)}
                onBlur={() => void renommer(c.id)}
                onKeyDown={e => {
                  if (e.key === 'Enter') void renommer(c.id)
                  if (e.key === 'Escape') setEnEdition('')
                }}
              />
            ) : (
              <button className="flex-1 min-w-0 text-left truncate"
                      title={libelle(c)}
                      onClick={() => onOuvrir(c.id)}>
                {libelle(c)}
              </button>
            )}
            <button
              className="opacity-0 group-hover:opacity-100 text-muted hover:text-primary p-0.5"
              title="Renommer"
              onClick={() => { setEnEdition(c.id); setBrouillon(c.titre) }}>
              <Pencil size={12} />
            </button>
            <button
              className="opacity-0 group-hover:opacity-100 text-muted hover:text-danger p-0.5"
              title="Supprimer"
              onClick={() => void supprimer(c.id)}>
              <Trash2 size={12} />
            </button>
            </div>
          </div>
        ))}
      </div>
    </aside>
  )
}
