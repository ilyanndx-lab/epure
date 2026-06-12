import { forwardRef, type TextareaHTMLAttributes } from 'react'

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  mono?: boolean
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { mono = false, className = '', ...props },
  ref,
) {
  return (
    <textarea
      ref={ref}
      className={`bg-elevated border border-line rounded-sm px-3 py-2 text-sm text-primary placeholder-muted focus:outline-none focus:border-accent transition-colors duration-150 resize-none ${mono ? 'font-mono' : ''} ${className}`}
      {...props}
    />
  )
})

export default Textarea
