import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Gamepad2,
  Play,
  Pause,
  RotateCcw,
  ArrowUp,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
} from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { Button } from '../../../components/ui'

const API = 'http://localhost:8000'

// ── Paramètres du plateau ────────────────────────────────────────────────────
const GRID = 20 // nombre de cellules par côté
const CELL = 22 // taille d'une cellule en pixels
const SIZE = GRID * CELL // côté du canvas en pixels
const BEST_KEY = 'snake.best'

type Point = { x: number; y: number }
type Dir = 'up' | 'down' | 'left' | 'right'
type Status = 'ready' | 'running' | 'paused' | 'over'

const DELTAS: Record<Dir, Point> = {
  up: { x: 0, y: -1 },
  down: { x: 0, y: 1 },
  left: { x: -1, y: 0 },
  right: { x: 1, y: 0 },
}

const OPPOSITE: Record<Dir, Dir> = {
  up: 'down',
  down: 'up',
  left: 'right',
  right: 'left',
}

const INITIAL_SNAKE: Point[] = [
  { x: 8, y: 10 },
  { x: 7, y: 10 },
  { x: 6, y: 10 },
]

/** Vitesse (délai entre deux pas) : décroît avec le score, plancher à 70 ms. */
function stepDelay(score: number): number {
  return Math.max(70, 150 - Math.floor(score / 4) * 10)
}

/** Place la nourriture sur une cellule libre (jamais sous le serpent). */
function placeFood(snake: Point[]): Point {
  const occupied = new Set(snake.map((s) => `${s.x},${s.y}`))
  const free: Point[] = []
  for (let x = 0; x < GRID; x++) {
    for (let y = 0; y < GRID; y++) {
      if (!occupied.has(`${x},${y}`)) free.push({ x, y })
    }
  }
  if (free.length === 0) return { x: 0, y: 0 }
  return free[Math.floor(Math.random() * free.length)]
}

export default function SnakeModule(_props: SharedModuleProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  const [status, setStatus] = useState<Status>('ready')
  const [score, setScore] = useState(0)
  const [best, setBest] = useState(0)

  // État du jeu vivant dans des refs : la boucle le lit sans dépendre des
  // fermetures (closures) de rendu — évite les bugs de « stale state ».
  const snakeRef = useRef<Point[]>(INITIAL_SNAKE)
  const foodRef = useRef<Point>({ x: 14, y: 10 })
  const dirRef = useRef<Dir>('right')
  const nextDirRef = useRef<Dir>('right')
  const statusRef = useRef<Status>('ready')
  const scoreRef = useRef(0)

  useEffect(() => {
    statusRef.current = status
  }, [status])

  // ── Dessin ─────────────────────────────────────────────────────────────────
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // Fond
    ctx.fillStyle = '#0f1117'
    ctx.fillRect(0, 0, SIZE, SIZE)

    // Quadrillage discret
    ctx.strokeStyle = 'rgba(255,255,255,0.04)'
    ctx.lineWidth = 1
    for (let i = 1; i < GRID; i++) {
      ctx.beginPath()
      ctx.moveTo(i * CELL, 0)
      ctx.lineTo(i * CELL, SIZE)
      ctx.moveTo(0, i * CELL)
      ctx.lineTo(SIZE, i * CELL)
      ctx.stroke()
    }

    // Nourriture
    const f = foodRef.current
    ctx.fillStyle = '#ef4444'
    ctx.beginPath()
    ctx.arc(f.x * CELL + CELL / 2, f.y * CELL + CELL / 2, CELL / 2 - 3, 0, Math.PI * 2)
    ctx.fill()

    // Serpent
    const snake = snakeRef.current
    snake.forEach((seg, i) => {
      ctx.fillStyle = i === 0 ? '#34d399' : '#10b981'
      const pad = i === 0 ? 1 : 2
      ctx.fillRect(seg.x * CELL + pad, seg.y * CELL + pad, CELL - pad * 2, CELL - pad * 2)
    })
  }, [])

  // ── Soumission du meilleur score (best-effort, ne bloque jamais le jeu) ──────
  const submitScore = useCallback((value: number) => {
    fetch(`${API}/snake/score`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ score: value }),
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data && typeof data.best === 'number') {
          setBest((prev) => Math.max(prev, data.best))
        }
      })
      .catch(() => {
        /* hors-ligne : on garde le meilleur score local */
      })
  }, [])

  // ── Un pas de jeu ────────────────────────────────────────────────────────────
  const step = useCallback(() => {
    const dir = nextDirRef.current
    dirRef.current = dir
    const delta = DELTAS[dir]
    const snake = snakeRef.current
    const head = { x: snake[0].x + delta.x, y: snake[0].y + delta.y }

    // Collision murs
    if (head.x < 0 || head.x >= GRID || head.y < 0 || head.y >= GRID) {
      endGame()
      return
    }

    const willGrow = head.x === foodRef.current.x && head.y === foodRef.current.y
    // Corps à tester : si on ne grandit pas, la queue libère sa case.
    const body = willGrow ? snake : snake.slice(0, -1)
    if (body.some((s) => s.x === head.x && s.y === head.y)) {
      endGame()
      return
    }

    const next = [head, ...snake]
    if (willGrow) {
      const newScore = scoreRef.current + 1
      scoreRef.current = newScore
      setScore(newScore)
      foodRef.current = placeFood(next)
    } else {
      next.pop()
    }
    snakeRef.current = next
    draw()
  }, [draw])

  const endGame = useCallback(() => {
    setStatus('over')
    statusRef.current = 'over'
    const value = scoreRef.current
    setBest((prev) => {
      const b = Math.max(prev, value)
      try {
        localStorage.setItem(BEST_KEY, String(b))
      } catch {
        /* localStorage indisponible : on ignore */
      }
      return b
    })
    submitScore(value)
  }, [submitScore])

  // ── Boucle de jeu : ré-arme un setTimeout dont le délai suit le score ────────
  useEffect(() => {
    if (status !== 'running') return
    let timer: number
    const loop = () => {
      step()
      if (statusRef.current === 'running') {
        timer = window.setTimeout(loop, stepDelay(scoreRef.current))
      }
    }
    timer = window.setTimeout(loop, stepDelay(scoreRef.current))
    return () => window.clearTimeout(timer)
  }, [status, step])

  // ── Démarrage / réinitialisation ─────────────────────────────────────────────
  const reset = useCallback(() => {
    snakeRef.current = INITIAL_SNAKE.map((p) => ({ ...p }))
    dirRef.current = 'right'
    nextDirRef.current = 'right'
    scoreRef.current = 0
    foodRef.current = placeFood(snakeRef.current)
    setScore(0)
    setStatus('ready')
    statusRef.current = 'ready'
    draw()
  }, [draw])

  const start = useCallback(() => {
    if (status === 'over' || status === 'ready') reset()
    setStatus('running')
  }, [status, reset])

  const togglePause = useCallback(() => {
    setStatus((s) => (s === 'running' ? 'paused' : s === 'paused' ? 'running' : s))
  }, [])

  // Changement de direction avec garde anti demi-tour.
  const turn = useCallback((dir: Dir) => {
    if (dir === OPPOSITE[dirRef.current]) return
    nextDirRef.current = dir
  }, [])

  // ── Clavier ───────────────────────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const map: Record<string, Dir> = {
        ArrowUp: 'up',
        ArrowDown: 'down',
        ArrowLeft: 'left',
        ArrowRight: 'right',
        w: 'up',
        s: 'down',
        a: 'left',
        d: 'right',
      }
      const dir = map[e.key]
      if (dir) {
        e.preventDefault()
        if (statusRef.current === 'ready') start()
        turn(dir)
        return
      }
      if (e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault()
        if (statusRef.current === 'running' || statusRef.current === 'paused') togglePause()
        else start()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [start, turn, togglePause])

  // ── Initialisation : meilleur score local + serveur, premier rendu ───────────
  useEffect(() => {
    try {
      const stored = Number(localStorage.getItem(BEST_KEY))
      if (Number.isFinite(stored) && stored > 0) setBest(stored)
    } catch {
      /* ignore */
    }
    fetch(`${API}/snake/highscore`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data && typeof data.best === 'number') {
          setBest((prev) => Math.max(prev, data.best))
        }
      })
      .catch(() => {
        /* hors-ligne */
      })
    foodRef.current = placeFood(snakeRef.current)
    draw()
  }, [draw])

  const overlayText =
    status === 'ready'
      ? 'Appuyez sur Démarrer ou une flèche'
      : status === 'paused'
        ? 'En pause'
        : status === 'over'
          ? 'Game Over'
          : null

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <Gamepad2 size={18} className="text-accent" /> Snake
      </h1>
      <p className="text-sm text-secondary max-w-lg leading-relaxed">
        Dirigez le serpent avec les flèches (ou ZQSD/WASD), barre d'espace pour
        mettre en pause. Le serpent accélère au fil des points.
      </p>

      <div className="flex flex-col items-center gap-4">
        <div className="flex items-center gap-6 text-sm">
          <span className="font-medium text-primary">
            Score&nbsp;: <span className="text-accent">{score}</span>
          </span>
          <span className="font-medium text-secondary">Record&nbsp;: {best}</span>
        </div>

        <div className="relative" style={{ width: SIZE, height: SIZE }}>
          <canvas
            ref={canvasRef}
            width={SIZE}
            height={SIZE}
            className="rounded-lg border border-line"
          />
          {overlayText && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 rounded-lg bg-black/55 text-center">
              <span
                className={`text-lg font-semibold ${
                  status === 'over' ? 'text-error' : 'text-white'
                }`}
              >
                {overlayText}
              </span>
              {status === 'over' && (
                <span className="text-sm text-white/80">Score final&nbsp;: {score}</span>
              )}
            </div>
          )}
        </div>

        <div className="flex gap-2">
          {status === 'running' ? (
            <Button variant="secondary" icon={<Pause size={16} />} onClick={togglePause}>
              Pause
            </Button>
          ) : (
            <Button variant="primary" icon={<Play size={16} />} onClick={start}>
              {status === 'paused' ? 'Reprendre' : 'Démarrer'}
            </Button>
          )}
          <Button variant="secondary" icon={<RotateCcw size={16} />} onClick={reset}>
            Réinitialiser
          </Button>
        </div>

        {/* Croix directionnelle (tactile / souris) */}
        <div className="grid grid-cols-3 gap-1.5 w-fit">
          <span />
          <Button variant="ghost" size="sm" onClick={() => turn('up')} aria-label="Haut">
            <ArrowUp size={16} />
          </Button>
          <span />
          <Button variant="ghost" size="sm" onClick={() => turn('left')} aria-label="Gauche">
            <ArrowLeft size={16} />
          </Button>
          <Button variant="ghost" size="sm" onClick={() => turn('down')} aria-label="Bas">
            <ArrowDown size={16} />
          </Button>
          <Button variant="ghost" size="sm" onClick={() => turn('right')} aria-label="Droite">
            <ArrowRight size={16} />
          </Button>
        </div>
      </div>
    </main>
  )
}
