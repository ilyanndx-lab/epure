import React, { useRef, useEffect, useState } from "react";

const Component: React.FC = () => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [score, setScore] = useState(0);
  const [highScore, setHighScore] = useState(0);
  const [gameOver, setGameOver] = useState(false);
  const [level, setLevel] = useState(1);
  const [isPaused, setIsPaused] = useState(false);

  // ... (reste du code, sans les éléments interdits)

  const adjustSpeed = () => {
    // Réduire la vitesse du dinosaure ou la fréquence d'appel à la fonction de jeu
    dino.vy /= 2; // Réduire la vitesse du dinosaure par 2
    // ou
    // gameSpeed /= 2; // Réduire la vitesse de la frame par 2
  };

  useEffect(() => {
    // ... (reste du code, sans les éléments interdits)

    // Appel à la fonction de jeu
    const gameLoop = () => {
      // ... (reste du code, sans les éléments interdits)

      // Réduire la vitesse du dinosaure ou la fréquence d'appel à la fonction de jeu
      adjustSpeed();
    };

    // ... (reste du code, sans les éléments interdits)

    // Réinitialiser la vitesse si le jeu est réinitialisé
    if (gameOver) {
      dino.vy = 0;
      adjustSpeed();
      setGameOver(false);
    }

    // ... (reste du code, sans les éléments interdits)
  }, []);

  return (
    <div style={{ textAlign: "center" }}>
      <canvas
        ref={canvasRef}
        style={{
          border: "2px solid #333",
          display: "block",
          margin: "0 auto",
          background: "linear-gradient(to bottom, #87CEEB, #E0F7FA)"
        }}
      />
    </div>
  );
};

export default Component;
