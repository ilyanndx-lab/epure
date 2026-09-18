import { forwardRef, type TextareaHTMLAttributes } from 'react'

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  mono?: boolean
  /**
   * Sans fond ni bordure propres — pour un composeur qui fournit déjà son
   * propre conteneur (l'« îlot » du chat, cf. `modules/chat/Component.tsx`) :
   * sinon la bordure de ce composant ET celle du conteneur se cumuleraient
   * en un double cadre.
   */
  bare?: boolean
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { mono = false, bare = false, className = '', ...props },
  ref,
) {
  return (
    <textarea
      ref={ref}
      className={`${bare ? '' : 'bg-elevated border border-line rounded-sm'} px-3 py-2 text-sm text-primary placeholder-muted focus:outline-none ${bare ? '' : 'focus:border-accent'} transition-colors duration-150 resize-none ${mono ? 'font-mono' : ''} ${className}`}
      {...props}
    />
  )
})

export default Textarea
