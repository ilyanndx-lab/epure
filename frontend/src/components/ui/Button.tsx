import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  icon?: ReactNode
}

const VARIANTS: Record<Variant, string> = {
  primary:
    'bg-gradient-primary text-on-accent font-medium shadow-sm hover:opacity-90 active:opacity-80',
  secondary:
    'bg-elevated border border-line text-secondary hover:text-primary hover:border-accent/50',
  ghost:
    'text-secondary hover:text-primary hover:bg-elevated',
  danger:
    'bg-error/10 border border-error/30 text-error hover:bg-error/20',
}

const SIZES: Record<Size, string> = {
  sm: 'px-2.5 py-1 text-xs gap-1.5',
  md: 'px-4 py-2 text-sm gap-2',
}

export default function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  className = '',
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      className={`inline-flex items-center justify-center rounded-md transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed disabled:pointer-events-none ${VARIANTS[variant]} ${SIZES[size]} ${className}`}
      {...props}
    >
      {icon && <span className="shrink-0 inline-flex">{icon}</span>}
      {children}
    </button>
  )
}
