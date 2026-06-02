
import pygame
import time
import random

# Initialisation de Pygame
pygame.init()

# Définir les dimensions de la fenêtre du jeu
window_width = 800
window_height = 600
win = pygame.display.set_mode((window_width, window_height))

# Définir la vitesse du serpent
snake_speed = 15

# Couleurs
white = (255, 255, 255)
yellow = (255, 255, 102)
black = (0, 0, 0)
red = (213, 50, 80)
green = (0, 255, 0)
blue = (50, 153, 213)

# Définir la vitesse du serpent
snake_list = []
snake_length = 1

# Font
font_style = pygame.font.SysFont("bahnschrift", 25)
score_font = pygame.font.SysFont("comicsansms", 35)

def our_snake(snake_block, snake_list):
    for x in snake_list:
        pygame.draw.rect(win, black, [x[0], x[1], snake_block, snake_block])

def message(msg, color):
    mesg = font_style.render(msg, True, color)
    win.blit(mesg, [window_width / 6, window_height / 3])

def gameLoop():
    game_over = False
    game_close = False

    x1 = window_width / 2
    y1 = window_height / 2

    x1_change = 0
    y1_change = 0

    snake_List = []
    Length_of_snake = 1

    foodx = round(random.randrange(0, window_width - 10) / 10.0) * 10.0
    foody = round(random.randrange(0, window_height - 10) / 10.0) * 10.0

    while not game_over:

        while game_close == True:
            win.fill(blue)
            message("You Lost! Press Q-Quit or C-Play Again", red)
            pygame.display.update()

            for event in pygame.event.get():
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_q:
                        game_over = True
                        game_close = False
                    if event.key == pygame.K_c:
                        gameLoop()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                game_over = True
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_LEFT:
                    x1_change = -10
                    y1_change = 0
                elif event.key == pygame.K_RIGHT:
                    x1_change = 10
                    y1_change = 0
                elif event.key == pygame.K_UP:
                    y1_change = -10
                    x1_change = 0
                elif event.key == pygame.K_DOWN:
                    y1_change = 10
                    x1_change = 0

        if x1 >= window_width or x1 < 0 or y1 >= window_height or y1 < 0:
            game_close = True
        x1 += x1_change
        y1 += y1_change
        win.fill(white)
        pygame.draw.rect(win, green, [foodx, foody, 10, 10])
        snake_Head = []
        snake_Head.append(x1)
        snake_Head.append(y1)
        snake_List.append(snake_Head)
        if len(snake_List) > Length_of_snake:
            del snake_List[0]

        for x in snake_List[:-1]:
            if x == snake_Head:
                game_close = True

        our_snake(10, snake_List)

        pygame.display.update()

        if x1 == foodx and y1 == foody:
            foodx = round(random.randrange(0, window_width - 10) / 10.0) * 10.0
            foody = round(random.randrange(0, window_height - 10) / 10.0) * 10.0
            Length_of_snake += 1

        clock = pygame.time.Clock()
        clock.tick(snake_speed)

    pygame.quit()
    quit()

gameLoop()
