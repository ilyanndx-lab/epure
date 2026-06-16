from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel
from typing import Optional
import random
from core.runtime import llm

router = APIRouter()


class UpdateRequest(BaseModel):
    paddleLeftY: float
    speedFactor: float = 1.0


class NewGameRequest(BaseModel):
    speedFactor: float = 1.0


class GameState:
    width = 800
    height = 600
    paddle_width = 12
    paddle_height = 100
    ball_radius = 10
    speed = 5
    ai_speed = 6
    max_speed = 12
    acceleration = 1.05

    def __init__(self, speed_factor: float = 1.0):
        self.speed_factor = speed_factor
        self.reset()

    def reset(self):
        base = self.speed * self.speed_factor
        vx = base * random.choice([-1, 1])
        vy = base * random.uniform(-0.5, 0.5)
        self.ball = {
            "x": self.width / 2,
            "y": self.height / 2,
            "vx": vx,
            "vy": vy,
            "radius": self.ball_radius,
        }
        self.paddle_left = {
            "x": 20,
            "y": self.height / 2 - self.paddle_height / 2,
            "width": self.paddle_width,
            "height": self.paddle_height,
            "score": 0,
        }
        self.paddle_right = {
            "x": self.width - 20 - self.paddle_width,
            "y": self.height / 2 - self.paddle_height / 2,
            "width": self.paddle_width,
            "height": self.paddle_height,
            "score": 0,
        }
        self.game_over = False
        self.winner = None

    def to_dict(self):
        return {
            "ball": self.ball,
            "paddleLeft": self.paddle_left,
            "paddleRight": self.paddle_right,
            "gameOver": self.game_over,
            "winner": self.winner,
        }

    def update(self, player_left_y: float, speedFactor: float = 1.0):
        self.speed_factor = speedFactor
        effective_max = self.max_speed * self.speed_factor
        self.paddle_left["y"] = max(
            0,
            min(self.height - self.paddle_height, player_left_y),
        )

        # IA adversaire déterministe : suit la balle (aucun accès réseau / LLM)
        paddle_center = self.paddle_right["y"] + self.paddle_height / 2
        if self.ball["y"] < paddle_center - self.ai_speed:
            self.paddle_right["y"] = max(
                0, self.paddle_right["y"] - self.ai_speed
            )
        elif self.ball["y"] > paddle_center + self.ai_speed:
            self.paddle_right["y"] = min(
                self.height - self.paddle_height,
                self.paddle_right["y"] + self.ai_speed,
            )

        self.ball["x"] += self.ball["vx"]
        self.ball["y"] += self.ball["vy"]

        if self.ball["y"] - self.ball_radius <= 0:
            self.ball["y"] = self.ball_radius
            self.ball["vy"] = -self.ball["vy"]
        if self.ball["y"] + self.ball_radius >= self.height:
            self.ball["y"] = self.height - self.ball_radius
            self.ball["vy"] = -self.ball["vy"]

        # collision raquette gauche
        if (
            self.ball["vx"] < 0
            and self.ball["x"] - self.ball_radius
            <= self.paddle_left["x"] + self.paddle_left["width"]
            and self.paddle_left["y"]
            <= self.ball["y"] + self.ball_radius
            <= self.paddle_left["y"] + self.paddle_height
        ):
            self.ball["vx"] = -self.ball["vx"]
            self.ball["vy"] *= 1 + random.uniform(-0.2, 0.2)
            # accélération
            vx_sign = 1 if self.ball["vx"] > 0 else -1
            vy_sign = 1 if self.ball["vy"] > 0 else -1
            self.ball["vx"] = vx_sign * min(effective_max, abs(self.ball["vx"]) * self.acceleration)
            self.ball["vy"] = vy_sign * min(effective_max, abs(self.ball["vy"]) * self.acceleration)

        # collision raquette droite
        elif (
            self.ball["vx"] > 0
            and self.ball["x"] + self.ball_radius >= self.paddle_right["x"]
            and self.paddle_right["y"]
            <= self.ball["y"] + self.ball_radius
            <= self.paddle_right["y"] + self.paddle_height
        ):
            self.ball["vx"] = -self.ball["vx"]
            self.ball["vy"] *= 1 + random.uniform(-0.2, 0.2)
            # accélération
            vx_sign = 1 if self.ball["vx"] > 0 else -1
            vy_sign = 1 if self.ball["vy"] > 0 else -1
            self.ball["vx"] = vx_sign * min(effective_max, abs(self.ball["vx"]) * self.acceleration)
            self.ball["vy"] = vy_sign * min(effective_max, abs(self.ball["vy"]) * self.acceleration)

        # but
        if self.ball["x"] - self.ball_radius <= 0:
            self.paddle_right["score"] += 1
            self.game_over = True
            self.winner = "right"
        elif self.ball["x"] + self.ball_radius >= self.width:
            self.paddle_left["score"] += 1
            self.game_over = True
            self.winner = "left"

        self.ball["vx"] = max(-effective_max, min(effective_max, self.ball["vx"]))
        self.ball["vy"] = max(-effective_max, min(effective_max, self.ball["vy"]))

        return self.to_dict()


_current_state: GameState | None = None


@router.get("/ping")
async def pong_ping():
    return {"module": "pong", "message": "pong", "ok": True}


@router.post("/new-game")
async def new_game(data: Optional[NewGameRequest] = Body(None)):
    global _current_state
    factor = data.speedFactor if data else 1.0
    _current_state = GameState(speed_factor=factor)
    return _current_state.to_dict()


@router.post("/update")
async def update(data: UpdateRequest):
    global _current_state
    if _current_state is None:
        # auto-initialise au lieu d'échouer : évite de rester bloqué
        factor = data.speedFactor if hasattr(data, 'speedFactor') else 1.0
        _current_state = GameState(speed_factor=factor)
    state = _current_state
    state.update(data.paddleLeftY, data.speedFactor)
    return state.to_dict()
