import { Sun, Moon } from 'lucide-react'
import { useTheme } from '../../theme'

interface ThemeToggleProps {
  /** Affiche le libellé du thème à côté de l'icône */
  withLabel?: boolean
  className?: string
}

/** Bouton soleil/lune — bascule et persiste le thème (localStorage). */
export default function ThemeToggle({ withLabel = false, className = '' }: ThemeToggleProps) {
  const { theme, toggleTheme } = useTheme()
  const dark = theme === 'dark'
  return (
    <button
      onClick={toggleTheme}
      title={dark ? 'Passer au thème clair' : 'Passer au thème sombre'}
      className={`inline-flex items-center gap-2 p-1.5 rounded-sm text-muted hover:text-accent2 hover:bg-elevated transition-colors duration-150 ${className}`}
    >
      {dark ? <Sun size={15} /> : <Moon size={15} />}
      {withLabel && <span className="text-sm">{dark ? 'Sombre' : 'Clair'}</span>}
    </button>
  )
}
