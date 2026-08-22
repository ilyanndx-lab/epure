import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, RefreshCw, Hammer } from 'lucide-react'
import { ATELIER_PRESENT } from '../atelier'
import { Button } from './ui'

interface Props {
  /** id du module rendu — sert de clé : changer de module réinitialise l'erreur. */
  moduleId: string
  /** navigation vers un autre module (ex. l'atelier pour corriger). */
  onNavigate?: (module: string) => void
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Capture les erreurs de rendu d'un module (surtout les modules générés par
 * l'atelier). Au lieu d'une « page noire » / écran blanc, affiche un message
 * d'erreur exploitable avec le détail et un accès direct à l'atelier pour
 * demander une correction à l'IA.
 *
 * NB : un ErrorBoundary ne capture QUE les erreurs de rendu/lifecycle, pas les
 * erreurs asynchrones (fetch, setTimeout) — celles-là restent à gérer dans les
 * modules eux-mêmes.
 */
export default class ModuleErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidUpdate(prev: Props) {
    // Changement de module → on oublie l'erreur précédente.
    if (prev.moduleId !== this.props.moduleId && this.state.error) {
      this.setState({ error: null })
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[module ${this.props.moduleId}] crash au rendu :`, error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    const { moduleId, onNavigate } = this.props
    return (
      <div className="flex flex-1 items-center justify-center p-8 overflow-auto">
        <div className="max-w-xl w-full rounded-lg border border-error/40 bg-error/5 p-5 space-y-3">
          <div className="flex items-center gap-2 text-error">
            <AlertTriangle size={18} className="shrink-0" />
            <h2 className="text-sm font-semibold">
              Le module « {moduleId} » a planté au chargement
            </h2>
          </div>
          <p className="text-xs text-secondary">
            Son interface contient une erreur et n'a pas pu s'afficher. Rien d'autre n'est
            cassé — vous pouvez réessayer
            {ATELIER_PRESENT ? " ou demander à l'atelier de corriger l'erreur." : '.'}
          </p>
          <pre className="text-xs font-mono text-error/90 bg-[#100e0a] border border-line rounded-sm p-3 max-h-48 overflow-auto whitespace-pre-wrap">
            {error.name}: {error.message}
            {error.stack ? `\n\n${error.stack}` : ''}
          </pre>
          <div className="flex items-center gap-2 pt-1">
            <Button
              variant="secondary"
              size="sm"
              icon={<RefreshCw size={13} />}
              onClick={() => this.setState({ error: null })}
            >
              Réessayer
            </Button>
            {ATELIER_PRESENT && onNavigate && (
              <Button
                variant="ghost"
                size="sm"
                icon={<Hammer size={13} />}
                onClick={() => onNavigate('workshop')}
              >
                Corriger dans l'atelier
              </Button>
            )}
          </div>
        </div>
      </div>
    )
  }
}
