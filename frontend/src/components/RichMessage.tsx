import { useState, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeHighlight from 'rehype-highlight'

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
      <pre
        ref={preRef}
        className="overflow-x-auto p-3 rounded text-xs bg-[#080808] border border-[#1a1a1a] leading-relaxed"
      >
        {children}
      </pre>
      <button
        onClick={copy}
        className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 px-2 py-0.5 text-[10px] font-mono bg-[#141414] border border-[#2a2a2a] rounded text-[#555] hover:text-[#aaa] transition-all duration-150"
      >
        {copied ? 'copié' : 'copier'}
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

export default function RichMessage({ content, streaming = false }: RichMessageProps) {
  return (
    <div className="text-sm font-mono text-[#b8b8b8] leading-relaxed">
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
              <code className="px-1 py-0.5 bg-[#141414] border border-[#1e1e1e] rounded text-[#c8c8c8] text-xs not-italic">
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
            return <li className="text-[#b8b8b8]">{children}</li>
          },
          strong({ children }) {
            return <strong className="text-[#e0e0e0] font-semibold">{children}</strong>
          },
          em({ children }) {
            return <em className="text-[#c8c8c8] italic">{children}</em>
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
              <th className="border border-[#2a2a2a] px-3 py-1.5 text-left text-[#555] font-normal">
                {children}
              </th>
            )
          },
          td({ children }) {
            return <td className="border border-[#1e1e1e] px-3 py-1.5 text-[#777]">{children}</td>
          },
          h1({ children }) {
            return <h1 className="text-base font-semibold text-[#e0e0e0] mt-3 mb-1 first:mt-0">{children}</h1>
          },
          h2({ children }) {
            return <h2 className="text-sm font-semibold text-[#e0e0e0] mt-3 mb-1 first:mt-0">{children}</h2>
          },
          h3({ children }) {
            return <h3 className="text-xs font-semibold text-[#ddd] mt-2 mb-1 first:mt-0">{children}</h3>
          },
          blockquote({ children }) {
            return (
              <blockquote className="border-l-2 border-[#2a2a2a] pl-3 text-[#666] my-2">
                {children}
              </blockquote>
            )
          },
          hr() {
            return <hr className="border-[#1e1e1e] my-3" />
          },
          a({ href, children }) {
            return (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className="text-[#5a7a9a] hover:text-[#7a9aaa] underline"
              >
                {children}
              </a>
            )
          },
        }}
      />
      {streaming && <span className="animate-pulse text-[#3a3a3a]">▍</span>}
    </div>
  )
}
