import React, { useRef, useEffect, useState, useCallback } from "react";

const GRAVITY = 0.6;
const JUMP_FORCE = -12;
const GROUND_Y = 150;
const DINO_WIDTH = 40;
const DINO_HEIGHT = 50;
const OBSTACLE_WIDTH = 20;
const OBSTACLE_HEIGHT = 40;
const BASE_SPEED = 5;
const CANVAS_WIDTH = 600;
const CANVAS_HEIGHT = 200;

interface Dino {
  x: number;
  y: number;
  vy: number;
  jumping: boolean;
}

interface Obstacle {
  x: number;
  y: number;
  w: number;
  h: number;
}

const Component: React.FC = () => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dinoRef = useRef<Dino>({ x: 60, y: GROUND_Y, vy: 0, jumping: false });
  const obstaclesRef = useRef<Obstacle[]>([]);
  const speedRef = useRef(BASE_SPEED);
  const frameRef = useRef(0);
  const animRef = useRef<number>(0);

  const [score, setScore] = useState(0);
  const [highScore, setHighScore] = useState(0);
  const [gameOver, setGameOver] = useState(false);
  const [level, setLevel] = useState(1);
  const [started, setStarted] = useState(false);
  const [backendMsg, setBackendMsg] = useState("");

  const fetchGame = async () => {
    try {
      const res = await fetch("/dinosaure/game");
      const data = await res.json();
      setBackendMsg(data.message);
    } catch {
      // backend injoignable, jeu local uniquement
    }
  };

  const resetGame = useCallback(() => {
    dinoRef.current = { x: 60, y: GROUND_Y, vy: 0, jumping: false };
    obstaclesRef.current = [];
    speedRef.current = BASE_SPEED;
    frameRef.current = 0;
    setScore(0);
    setLevel(1);
    setGameOver(false);
    setStarted(true);
    fetch("/dinosaure/reset").catch(() => {});
  }, []);

  const jump = useCallback(() => {
    const dino = dinoRef.current;
    if (!dino.jumping) {
      dino.vy = JUMP_FORCE;
      dino.jumping = true;
    }
  }, []);

  // Keyboard controls
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code === "Space" || e.code === "ArrowUp") {
        e.preventDefault();
        if (gameOver) {
          resetGame();
        } else if (!started) {
          resetGame();
        } else {
          jump();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [gameOver, started, jump, resetGame]);

  // Fetch backend status on mount
  useEffect(() => {
    fetchGame();
  }, []);

  // Game loop
  useEffect(() => {
    if (!started || gameOver) return;

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const loop = () => {
      const dino = dinoRef.current;
      const obstacles = obstaclesRef.current;
      frameRef.current += 1;
      const speed = speedRef.current;

      // Physics
      dino.y += dino.vy;
      dino.vy += GRAVITY;
      if (dino.y >= GROUND_Y) {
        dino.y = GROUND_Y;
        dino.vy = 0;
        dino.jumping = false;
      }

      // Spawn obstacles
      if (frameRef.current % Math.max(40, 90 - level * 5) === 0) {
        obstacles.push({
          x: CANVAS_WIDTH,
          y: GROUND_Y + DINO_HEIGHT - OBSTACLE_HEIGHT,
          w: OBSTACLE_WIDTH,
          h: OBSTACLE_HEIGHT,
        });
      }

      // Move obstacles & collision
      for (let i = obstacles.length - 1; i >= 0; i--) {
        obstacles[i].x -= speed;

        // Collision (AABB)
        const dLeft = dino.x;
        const dRight = dino.x + DINO_WIDTH;
        const dTop = dino.y;
        const dBottom = dino.y + DINO_HEIGHT;
        const oLeft = obstacles[i].x;
        const oRight = obstacles[i].x + obstacles[i].w;
        const oTop = obstacles[i].y;
        const oBottom = obstacles[i].y + obstacles[i].h;

        if (dRight > oLeft && dLeft < oRight && dBottom > oTop && dTop < oBottom) {
          setGameOver(true);
          setStarted(false);
          if (score > highScore) setHighScore(score);
          return;
        }

        // Remove off-screen
        if (obstacles[i].x + obstacles[i].w < 0) {
          obstacles.splice(i, 1);
          const newScore = score + 10;
          setScore(newScore);
          if (newScore > highScore) setHighScore(newScore);
          // Level up every 100 points
          setLevel(Math.floor(newScore / 100) + 1);
        }
      }

      // Increase speed with level
      speedRef.current = BASE_SPEED + (level - 1) * 1.2;

      // Draw
      ctx.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT);

      // Ground line
      const groundLine = GROUND_Y + DINO_HEIGHT;
      ctx.beginPath();
      ctx.moveTo(0, groundLine);
      ctx.lineTo(CANVAS_WIDTH, groundLine);
      ctx.strokeStyle = "#555";
      ctx.lineWidth = 2;
      ctx.stroke();

      // Dino (green rectangle with eye)
      ctx.fillStyle = "#2e7d32";
      ctx.fillRect(dino.x, dino.y, DINO_WIDTH, DINO_HEIGHT);
      // Eye
      ctx.fillStyle = "white";
      ctx.fillRect(dino.x + DINO_WIDTH - 14, dino.y + 8, 8, 8);
      ctx.fillStyle = "black";
      ctx.fillRect(dino.x + DINO_WIDTH - 10, dino.y + 10, 4, 4);

      // Obstacles (red-brown)
      ctx.fillStyle = "#8d3b3b";
      for (const obs of obstacles) {
        ctx.fillRect(obs.x, obs.y, obs.w, obs.h);
        // Spikes
        ctx.fillStyle = "#6b2020";
        ctx.beginPath();
        ctx.moveTo(obs.x, obs.y);
        ctx.lineTo(obs.x + obs.w / 2, obs.y - 6);
        ctx.lineTo(obs.x + obs.w, obs.y);
        ctx.fill();
        ctx.fillStyle = "#8d3b3b";
      }

      // Score
      ctx.fillStyle = "#333";
      ctx.font = "16px monospace";
      ctx.fillText(`Score: ${score}`, 10, 20);
      ctx.fillText(`Niveau: ${level}`, 10, 40);

      animRef.current = requestAnimationFrame(loop);
    };

    animRef.current = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(animRef.current);
  }, [started, gameOver, score, highScore, level]);

  return (
    <div style={{ textAlign: "center", fontFamily: "monospace", padding: "1rem" }}>
      <h2>🦕 Dinosaure</h2>
      {backendMsg && <p style={{ color: "#666", fontSize: "0.9rem" }}>{backendMsg}</p>}

      <div style={{ display: "flex", justifyContent: "center", gap: "2rem", marginBottom: "0.5rem" }}>
        <span>Score: <strong>{score}</strong></span>
        <span>Record: <strong>{highScore}</strong></span>
        <span>Niveau: <strong>{level}</strong></span>
      </div>

      <canvas
        ref={canvasRef}
        width={CANVAS_WIDTH}
        height={CANVAS_HEIGHT}
        style={{
          border: "2px solid #333",
          background: "linear-gradient(to bottom, #87CEEB, #E0F7FA)",
          cursor: "pointer",
          borderRadius: 4,
        }}
        onClick={() => {
          if (gameOver || !started) {
            resetGame();
          } else {
            jump();
          }
        }}
      />

      {(gameOver || !started) && (
        <p style={{ marginTop: "0.75rem", color: "#c62828" }}>
          {gameOver ? "💀 Game Over !" : "Appuie sur Espace ou clique pour jouer"}
        </p>
      )}

      <p style={{ color: "#888", fontSize: "0.8rem", marginTop: "0.5rem" }}>
        Espace / ↑ / clic = sauter &nbsp;|&nbsp; Niveaux automatiques
      </p>
    </div>
  );
};

export default Component;
