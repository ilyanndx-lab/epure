import { forwardRef, type SelectHTMLAttributes } from 'react'

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  mono?: boolean
}

const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { mono = false, className = '', children, ...props },
  ref,
) {
  return (
    <select
      ref={ref}
      className={`bg-elevated border border-line rounded-sm px-2 py-1 text-xs text-secondary focus:outline-none focus:border-accent transition-colors duration-150 cursor-pointer ${mono ? 'font-mono' : ''} ${className}`}
      {...props}
    >
      {children}
    </select>
  )
})

export default Select
