interface ProgressBarProps {
  /** 0–100 */
  value: number
  /** Par défaut dégradé violet→turquoise ; sinon couleur sémantique */
  color?: 'gradient' | 'success' | 'warning' | 'error'
  className?: string
}

const COLORS = {
  gradient: 'bg-gradient-primary',
  success: 'bg-success',
  warning: 'bg-warning',
  error: 'bg-error',
}

export default function ProgressBar({ value, color = 'gradient', className = '' }: ProgressBarProps) {
  const clamped = Math.max(0, Math.min(100, value))
  return (
    <div className={`h-1.5 bg-elevated border border-line rounded-full overflow-hidden ${className}`}>
      <div
        className={`h-full rounded-full transition-all duration-300 ${COLORS[color]}`}
        style={{ width: `${clamped}%` }}
      />
    </div>
  )
}
