import { useState, useEffect } from 'react'
import { Circle } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { Button } from '../../../components/ui'

const API = 'http://localhost:8000'

interface Score {
  score: number
}

interface Ball {
  x: number
  y: number
  vx: number
  vy: number
  radius: number
}

interface Paddle {
  x: number
  y: number
  width: number
  height: number
  speed: number
}

const initialBall: Ball = {
  x: 250,
  y: 250,
  vx: 5,
  vy: 5,
  radius: 10,
}

const initialPaddle: Paddle = {
  x: 10,
  y: 250,
  width: 10,
  height: 100,
  speed: 5,
}

export default function PongModule(_props: SharedModuleProps) {
  const [score, setScore] = useState<Score>({ score: 0 })
  const [ball, setBall] = useState<Ball>(initialBall)
  const [paddle, setPaddle] = useState<Paddle>(initialPaddle)
  const [gameOver, setGameOver] = useState(false)

  useEffect(() => {
    const interval = setInterval(() => {
      if (!gameOver) {
        updateBall()
        updatePaddle()
      }
    }, 16)
    return () => clearInterval(interval)
  }, [gameOver])

  const updateBall = () => {
    const newBall = { ...ball }
    newBall.x += newBall.vx
    newBall.y += newBall.vy

    if (newBall.y < 0 || newBall.y > 500) {
      newBall.vy = -newBall.vy
    }

    if (newBall.x < 0) {
      setGameOver(true)
    } else if (newBall.x > 500) {
      newBall.vx = -newBall.vx
    }

    if (
      newBall.x < paddle.x + paddle.width &&
      newBall.y > paddle.y &&
      newBall.y < paddle.y + paddle.height
    ) {
      newBall.vx = -newBall.vx
    }

    setBall(newBall)
  }

  const updatePaddle = () => {
    const newPaddle = { ...paddle }
    if (newPaddle.y < ball.y) {
      newPaddle.y += newPaddle.speed
    } else if (newPaddle.y > ball.y) {
      newPaddle.y -= newPaddle.speed
    }
    setPaddle(newPaddle)
  }

  const resetGame = () => {
    setGameOver(false)
    setBall(initialBall)
    setPaddle(initialPaddle)
  }

  const ping = async () => {
    try {
      const res = await fetch(`${API}/pong/ping`)
      console.log(await res.json())
    } catch {
      console.log('erreur réseau')
    }
  }

  const updateScore = async () => {
    try {
      const res = await fetch(`${API}/pong/score`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(score.score + 1),
      })
      setScore(await res.json())
    } catch {
      console.log('erreur réseau')
    }
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
        <Circle size={18} className="text-accent" /> Pong
      </h1>
      <div className="flex items-center gap-3">
        <Button onClick={ping}>Ping</Button>
        <Button onClick={updateScore}>Update Score</Button>
        <Button onClick={resetGame}>Reset Game</Button>
      </div>
      <div className="relative w-500 h-500 border border-gray-400">
        <div
          className="absolute"
          style={{
            top: ball.y,
            left: ball.x,
            width: ball.radius * 2,
            height: ball.radius * 2,
            borderRadius: '50%',
            backgroundColor: 'black',
          }}
        />
        <div
          className="absolute"
          style={{
            top: paddle.y,
            left: paddle.x,
            width: paddle.width,
            height: paddle.height,
            backgroundColor: 'black',
          }}
        />
      </div>
      {gameOver && <p>Game Over ! Votre score est : {score.score}</p>}
    </main>
  )
}
