
import time

# Setup screen
screen = turtle.Screen()
screen.title("Pong Game")
screen.bgcolor("black")
screen.setup(width=800, height=600)
screen.tracer(0)  # Turn off screen updates

# Paddles
left_paddle = turtle.Turtle()
left_paddle.speed(0)
left_paddle.shape("square")
left_paddle.color("white")
left_paddle.shapesize(stretch_wid=1, stretch_len=5)
left_paddle.penup()
left_paddle.goto(x=-350, y=0)

right_paddle = turtle.Turtle()
right_paddle.speed(0)
right_paddle.shape("square")
right_paddle.color("white")
right_paddle.shapesize(stretch_wid=1, stretch_len=5)
right_paddle.penup()
right_paddle.goto(x=350, y=0)

# Ball
ball = turtle.Turtle()
ball.speed(0)
ball.shape("circle")
ball.color("white")
ball.penup()
ball.goto(x=0, y=0)
ball.dx = 2  # X direction movement
ball.dy = 2  # Y direction movement

# Score
score = turtle.Turtle()
score.speed(0)
score.color("white")
score.penup()
score.hideturtle()
score.goto(0, 260)
score.write("Score: 0", align="center", font=("Courier", 24, "normal"))

# Functions to move paddles
def left_paddle_up():
    y = left_paddle.ycor()
    y += 20
    left_paddle.sety(y)

def left_paddle_down():
    y = left_paddle.ycor()
    y -= 20
    left_paddle.sety(y)

def right_paddle_up():
    y = right_paddle.ycor()
    y += 20
    right_paddle.sety(y)

def right_paddle_down():
    y = right_paddle.ycor()
    y -= 20
    right_paddle.sety(y)

# Keyboard bindings
screen.listen()
screen.onkeypress(left_paddle_up, "w")
screen.onkeypress(left_paddle_down, "s")
screen.onkeypress(right_paddle_up, "Up")
screen.onkeypress(right_paddle_down, "Down")

# Main game loop
while True:
    screen.update()
    time.sleep(0.01)

    # Move ball
    ball.setx(ball.xcor() + ball.dx)
    ball.sety(ball.ycor() + ball.dy)

    # Border checking
    if ball.ycor() > 290:
        ball.sety(290)
        ball.dy *= -1  # Reverse direction

    if ball.ycor() < -290:
        ball.sety(-290)
        ball.dy *= -1

    if ball.xcor() > 390:
        ball.setx(390)
        ball.dx *= -1
        score.clear()
        score.write("Score: 0", align="center", font=("Courier", 24, "normal"))

    if ball.xcor() < -390:
        ball.setx(-390)
        ball.dx *= -1
        score.clear()
        score.write("Score: 0", align="center", font=("Courier", 24, "normal"))

    # Paddle collisions
    if (ball.xcor() > 340 and ball.xcor() < 350) and (ball.ycor() < right_paddle.ycor() + 40 and ball.ycor() > right_paddle.ycor() - 40):
        ball.setx(340)
        ball.dx *= -1
        score.clear()
        score.write("Score: 0", align="center", font=("Courier", 24, "normal"))

    if (ball.xcor() < -340 and ball.xcor() > -350) and (ball.ycor() < left_paddle.ycor() + 40 and ball.ycor() > left_paddle.ycor() - 40):
        ball.setx(-340)
        ball.dx *= -1
        score.clear()
        score.write("Score: 0", align="center", font=("Courier", 24, "normal"))
