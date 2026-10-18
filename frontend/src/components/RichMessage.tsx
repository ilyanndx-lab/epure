import { useContext, useState, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeHighlight from 'rehype-highlight'
import { Copy, Check } from 'lucide-react'
import { ImagesAutoriseesContext } from './imagesAutorisees'

function PreBlock({ children }: { children: React.ReactNode }) {
  const [copied, setCopied] = useState(false)
  const preRef = useRef<HTMLPreElement>(null)

  const copy = async () => {
    const text = preRef.current?.innerText ?? ''
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // clipboard unavailable
    }
  }

  return (
    <div className="relative group my-2">
      {/* Fond sombre fixe : le thème highlight.js (github-dark) est global, le bloc
          de code reste donc sombre même en thème light */}
      <pre
        ref={preRef}
        className="overflow-x-auto p-3 rounded-md text-xs font-mono bg-[#16140f] border border-line leading-relaxed"
      >
        {children}
      </pre>
      <button
        onClick={copy}
        title={copied ? 'Copié' : 'Copier'}
        className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 p-1.5 bg-elevated border border-line rounded-sm text-muted hover:text-primary transition-all duration-150"
      >
        {copied ? <Check size={12} className="text-success" /> : <Copy size={12} />}
      </button>
    </div>
  )
}

function preprocessLatex(content: string): string {
  return content
    .replace(/\\begin\{equation\}([\s\S]*?)\\end\{equation\}/g, (_, inner) => `$$${inner}$$`)
    .replace(/\\begin\{align\*\}([\s\S]*?)\\end\{align\*\}/g, (_, inner) => `$$\\begin{align*}${inner}\\end{align*}$$`)
    .replace(/\\begin\{align\}([\s\S]*?)\\end\{align\}/g, (_, inner) => `$$\\begin{align}${inner}\\end{align}$$`)
    .replace(/\\\[([\s\S]*?)\\\]/g, (_, inner) => `$$${inner}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_, inner) => `$${inner}$`)
}

interface RichMessageProps {
  content: string
  streaming?: boolean
}

/**
 * Image refusée : son URL en lien TEXTE, cliquable mais jamais chargée
 * d'office — cf. `ImagesAutoriseesContext` pour la menace. Le lien reste
 * visible plutôt que d'effacer l'image : l'utilisateur voit ce que le modèle
 * a tenté d'afficher, et peut l'ouvrir lui-même s'il le juge sûr.
 */
function ImageNonChargee({ src, alt }: { src: string; alt: string }) {
  return (
    <span className="text-muted text-xs">
      [image non chargée{alt ? ` « ${alt} »` : ''} :{' '}
      <a href={src} target="_blank" rel="noreferrer" className="text-accent2 underline break-all">{src}</a>]
    </span>
  )
}

export default function RichMessage({ content, streaming = false }: RichMessageProps) {
  // `undefined` : aucune restriction (tour sans lecture d'URL). Une liste,
  // même VIDE, restreint : « rien d'autorisé » n'est pas « tout autorisé ».
  const imagesAutorisees = useContext(ImagesAutoriseesContext)
  const imageAutorisee = (src: string) => imagesAutorisees === undefined || imagesAutorisees.includes(src)
  return (
    <div className="text-sm text-secondary leading-relaxed">
      <ReactMarkdown
        children={preprocessLatex(content)}
        remarkPlugins={[remarkMath]}
        rehypePlugins={[
          [rehypeKatex, {
            throwOnError: false,
            strict: false,
            trust: true,
            macros: {
              '\\R': '\\mathbb{R}',
              '\\N': '\\mathbb{N}',
              '\\Z': '\\mathbb{Z}',
            },
          }],
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          [rehypeHighlight as any, { detect: true, ignoreMissing: true }],
        ]}
        components={{
          // Block code wrapper → adds dark bg + copy button
          pre({ children }) {
            return <PreBlock>{children}</PreBlock>
          },
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          code({ children, className }: any) {
            if (className) {
              // Highlighted block code (inside <pre>)
              return <code className={className}>{children}</code>
            }
            // Inline code
            return (
              <code className="px-1 py-0.5 bg-elevated border border-line rounded-sm text-primary font-mono text-xs not-italic">
                {children}
              </code>
            )
          },
          p({ children }) {
            return <p className="mb-2 last:mb-0 whitespace-pre-wrap">{children}</p>
          },
          ul({ children }) {
            return <ul className="list-disc pl-5 mb-2 space-y-0.5">{children}</ul>
          },
          ol({ children }) {
            return <ol className="list-decimal pl-5 mb-2 space-y-0.5">{children}</ol>
          },
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          li({ children }: any) {
            return <li className="text-secondary">{children}</li>
          },
          strong({ children }) {
            return <strong className="text-primary font-semibold">{children}</strong>
          },
          em({ children }) {
            return <em className="text-secondary italic">{children}</em>
          },
          table({ children }) {
            return (
              <div className="overflow-x-auto my-2">
                <table className="border-collapse text-xs w-full">{children}</table>
              </div>
            )
          },
          th({ children }) {
            return (
              <th className="border border-line px-3 py-1.5 text-left text-muted font-medium">
                {children}
              </th>
            )
          },
          td({ children }) {
            return <td className="border border-line px-3 py-1.5 text-secondary">{children}</td>
          },
          h1({ children }) {
            return <h1 className="text-base font-semibold text-primary mt-3 mb-1 first:mt-0">{children}</h1>
          },
          h2({ children }) {
            return <h2 className="text-sm font-semibold text-primary mt-3 mb-1 first:mt-0">{children}</h2>
          },
          h3({ children }) {
            return <h3 className="text-xs font-semibold text-primary mt-2 mb-1 first:mt-0">{children}</h3>
          },
          blockquote({ children }) {
            return (
              <blockquote className="border-l-2 border-accent2/50 pl-3 text-secondary my-2">
                {children}
              </blockquote>
            )
          },
          hr() {
            return <hr className="border-line my-3" />
          },
          // Couvre AUSSI `\includegraphics{https://…}` de KaTeX (`trust: true`
          // ci-dessus) : `rehype-katex` produit des éléments HAST, qui passent
          // par ce même `img` — vérifié par RichMessage.test.tsx.
          img({ src, alt }) {
            const url = typeof src === 'string' ? src : ''
            if (!imageAutorisee(url)) return <ImageNonChargee src={url} alt={alt ?? ''} />
            return <img src={url} alt={alt ?? ''} className="max-w-full" />
          },
          a({ href, children }) {
            return (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className="text-accent2 hover:text-accent2-hover underline"
              >
                {children}
              </a>
            )
          },
        }}
      />
      {streaming && <span className="animate-pulse text-accent2">▍</span>}
    </div>
  )
}
