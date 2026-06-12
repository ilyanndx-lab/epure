import { forwardRef, type InputHTMLAttributes } from 'react'

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  mono?: boolean
}

const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { mono = false, className = '', ...props },
  ref,
) {
  return (
    <input
      ref={ref}
      className={`bg-elevated border border-line rounded-sm px-3 py-1.5 text-sm text-primary placeholder-muted focus:outline-none focus:border-accent transition-colors duration-150 ${mono ? 'font-mono' : ''} ${className}`}
      {...props}
    />
  )
})

export default Input
