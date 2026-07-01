import { useState, useEffect, useRef, useCallback } from 'react'
import { Button } from '../../../components/ui'
import { API, apiFetch } from '../../../api'

const BASE_URL = `${API}/pong`

interface Ball {
  x: number
  y: number
  radius: number
}

interface Paddle {
  x: number
  y: number
  width: number
  height: number
  score: number
}

interface GameState {
  ball: Ball
  paddleLeft: Paddle
  paddleRight: Paddle
  gameOver: boolean
  winner: string | null
}

const CANVAS_WIDTH = 800
const CANVAS_HEIGHT = 600
const PADDLE_HEIGHT = 100
const TICK_INTERVAL = 50 // ms

export default function PongModule() {
  const [gameState, setGameState] = useState<GameState | null>(null)
  const [paddleY, setPaddleY] = useState(() => CANVAS_HEIGHT / 2 - PADDLE_HEIGHT / 2)
  const [gameOver, setGameOver] = useState(false)
  const [speedFactor, setSpeedFactor] = useState<number>(1)
  const [error, setError] = useState<string | null>(null)

  // refs pour la boucle de jeu (pas de dépendances sur l'état)
  const paddleYRef = useRef(paddleY)
  const speedFactorRef = useRef(speedFactor)
  const gameOverRef = useRef(gameOver)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // synchroniser les refs avec l'état
  useEffect(() => { paddleYRef.current = paddleY }, [paddleY])
  useEffect(() => { speedFactorRef.current = speedFactor }, [speedFactor])
  useEffect(() => { gameOverRef.current = gameOver }, [gameOver])

  // Arrêter la boucle
  const stopLoop = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }, [])

  // Fonction de tick : envoie la position et reçoit l'état
  const tick = useCallback(async () => {
    if (gameOverRef.current) return
    try {
      const res = await apiFetch(`${BASE_URL}/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paddleLeftY: paddleYRef.current, speedFactor: speedFactorRef.current }),
      })
      if (!res.ok) return
      const data: GameState = await res.json()
      setGameState(data)
      if (data.gameOver) {
        setGameOver(true)
        gameOverRef.current = true
        stopLoop()
      }
    } catch {
      // silencieux : un tick raté n'interrompt pas la partie
    }
  }, [stopLoop])

  // Démarrer la boucle de jeu
  const startLoop = useCallback(() => {
    if (intervalRef.current) clearInterval(intervalRef.current)
    intervalRef.current = setInterval(tick, TICK_INTERVAL)
  }, [tick])

  // Nouvelle partie
  const resetGame = useCallback(async () => {
    stopLoop()
    setError(null)
    try {
      const res = await apiFetch(`${BASE_URL}/new-game`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speedFactor: speedFactorRef.current }),
      })
      if (!res.ok) {
        setError(`Impossible de démarrer la partie (HTTP ${res.status}).`)
        return
      }
      const data: GameState = await res.json()
      setGameState(data)
      setPaddleY(CANVAS_HEIGHT / 2 - PADDLE_HEIGHT / 2)
      setGameOver(false)
      gameOverRef.current = false
      startLoop()
    } catch {
      setError('Connexion au serveur impossible. Réessayez.')
    }
  }, [startLoop, stopLoop])

  // Initialisation au montage
  useEffect(() => {
    resetGame()
    return () => stopLoop()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Contrôle clavier (flèches)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (gameOverRef.current) return
      const step = 30
      if (e.key === 'ArrowUp') {
        setPaddleY(prev => Math.max(0, prev - step))
      } else if (e.key === 'ArrowDown') {
        setPaddleY(prev => Math.min(CANVAS_HEIGHT - PADDLE_HEIGHT, prev + step))
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [])

  // Espace pour recommencer
  useEffect(() => {
    const handleSpace = (e: KeyboardEvent) => {
      if (e.code === 'Space') {
        e.preventDefault()
        if (gameOverRef.current) {
          resetGame()
        }
      }
    }
    window.addEventListener('keydown', handleSpace)
    return () => window.removeEventListener('keydown', handleSpace)
  }, [resetGame])

  // Contrôle souris
  const containerRef = useRef<HTMLDivElement>(null)
  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (gameOverRef.current) return
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect) return
    const localY = e.clientY - rect.top
    const clamped = Math.max(0, Math.min(CANVAS_HEIGHT - PADDLE_HEIGHT, localY - PADDLE_HEIGHT / 2))
    setPaddleY(clamped)
  }

  // Écran de chargement / erreur : ne reste jamais bloqué grâce au bouton de relance
  if (!gameState) {
    return (
      <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4 items-center justify-center h-full">
        {error ? (
          <>
            <p className="text-red-600">{error}</p>
            <Button onClick={resetGame}>Réessayer</Button>
          </>
        ) : (
          <p>Chargement de la partie...</p>
        )}
      </main>
    )
  }

  const { ball, paddleLeft, paddleRight, winner } = gameState

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4 items-center">
      <h1 className="text-xl font-semibold text-primary">Pong</h1>
      <div className="flex gap-8">
        <Button onClick={resetGame}>Nouvelle Partie</Button>
        <div className="flex flex-col items-center gap-1 ml-4">
          <label htmlFor="speed-slider" className="text-xs text-muted-foreground">
            Vitesse : {speedFactor.toFixed(1)}x
          </label>
          <input
            id="speed-slider"
            type="range"
            min={0.5}
            max={20.0}
            step={0.5}
            value={speedFactor}
            onChange={(e) => setSpeedFactor(Number(e.target.value))}
            className="w-28"
          />
        </div>
      </div>

      {/* Zone de jeu */}
      <div
        ref={containerRef}
        onMouseMove={handleMouseMove}
        className="relative border border-gray-400"
        style={{ width: CANVAS_WIDTH, height: CANVAS_HEIGHT, cursor: 'none' }}
      >
        {/* Ligne centrale */}
        <div
          className="absolute border-l border-dashed border-gray-400"
          style={{ left: CANVAS_WIDTH / 2, height: CANVAS_HEIGHT, top: 0 }}
        />

        {/* Scores */}
        <div className="absolute top-2 left-4 text-primary text-2xl font-bold">
          {paddleLeft.score}
        </div>
        <div className="absolute top-2 right-4 text-primary text-2xl font-bold">
          {paddleRight.score}
        </div>

        {/* Balle */}
        <div
          className="absolute"
          style={{
            top: ball.y - ball.radius,
            left: ball.x - ball.radius,
            width: ball.radius * 2,
            height: ball.radius * 2,
            borderRadius: '50%',
            backgroundColor: 'white',
          }}
        />

        {/* Raquette gauche */}
        <div
          className="absolute"
          style={{
            top: paddleLeft.y,
            left: paddleLeft.x,
            width: paddleLeft.width,
            height: paddleLeft.height,
            backgroundColor: 'white',
          }}
        />

        {/* Raquette droite */}
        <div
          className="absolute"
          style={{
            top: paddleRight.y,
            left: paddleRight.x,
            width: paddleRight.width,
            height: paddleRight.height,
            backgroundColor: 'white',
          }}
        />
      </div>

      {gameOver && (
        <p className="text-lg font-semibold text-red-600">
          Partie terminée ! {winner === 'left' ? 'Vous avez gagné !' : "L'ordinateur a gagné."}
        </p>
      )}
    </main>
  )
}
