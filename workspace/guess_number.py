
import random

def jouer():
    nombre_secret = random.randint(1, 100)
    essais = 0
    print("Bienvenue dans le jeu du nombre mystère !")
    print("J'ai choisi un nombre entre 1 et 100. À toi de le deviner.")

    while True:
        try:
            proposition = int(input("Quel est ton proposition ? "))
        except ValueError:
            print("Veuillez entrer un nombre entier valide.")
            continue

        essais += 1

        if proposition < nombre_secret:
            print("Trop bas ! Essaie encore.")
        elif proposition > nombre_secret:
            print("Trop haut ! Essaie encore.")
        else:
            print(f"Félicitations ! Tu as trouvé le nombre {nombre_secret} en {essais} essai(s).")
            break

if __name__ == "__main__":
    jouer()
