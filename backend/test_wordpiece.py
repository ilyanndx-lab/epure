#!/usr/bin/env python3
"""Le tokeniseur Python pur reproduit `tokenizers`, identifiant par identifiant.

**POURQUOI CE FICHIER EXISTE.** `core/wordpiece.py` réimplémente en Python la
chaîne de tokenisation de `all-MiniLM-L6-v2`, que la bibliothèque `tokenizers`
(Rust, HuggingFace) fait en trois lignes. Réécrire un tokeniseur n'est pas
anodin : une divergence d'un seul identifiant ne lève rien, elle produit un
vecteur légèrement faux — donc un index légèrement incohérent, et une recherche
documentaire qui se dégrade sans que rien ne le signale. C'est exactement le
genre de panne que ce dépôt paie deux fois : une fois en la créant, une fois en
la cherchant.

**LE CHOIX DE `tokenizers` COMME RÉFÉRENCE, ET SON ABSENCE ICI.** La parité a été
mesurée contre le vrai `tokenizers` sur **200 échantillons** — les 180 chunks
réels des trois collections vectorielles de l'instance, plus 20 cas limites —
avec **zéro divergence**. Mais `tokenizers` n'est pas une dépendance d'Épure, et
ne doit pas le devenir : son `.pyd` n'est pas signé, et c'est précisément la
catégorie de binaire que Smart App Control bloque sur la machine ARM64 du
destinataire (cf. `core/wordpiece.py`). L'installer pour tester reviendrait à
faire dépendre la CI de ce qu'on a retiré.

D'où la **table figée** ci-dessous : 25 cas et leurs identifiants attendus,
produits par le vrai `tokenizers` au moment de la migration. Ce n'est pas un
substitut au hasard — c'est le seul moyen de garder la référence sans garder la
dépendance. Les 20 cas limites sont là parce que chacun casse une implémentation
naïve :

    accent combinant (e + U+0301) vs précomposé  -> strip_accents après NFD
    CJK                                          -> isolé entre deux espaces
    pleine largeur, katakana demi-largeur        -> pas de normalisation NFKC
    U+0000, U+FFFD                               -> supprimés par clean_text
    tabulation / saut de ligne                   -> deviennent des espaces
    mot de 150 caractères                        -> [UNK] entier, pas découpé
    f(x)=y+2 $3 <a|b> ~z ^w `q                   -> symboles ASCII = ponctuation
    espace de largeur nulle                      -> caractère de format, gardé

Les cinq derniers cas sont des chunks réels, dont **deux tronqués à 256 jetons**
— la troncature fait partie du contrat (`max_seq_length` du modèle) et 37 des
40 chunks de la mesure de parité l'exerçaient.

**CE QUE CE FICHIER NE PEUT PAS FAIRE** : détecter que la table elle-même est
fausse. Elle a été produite par `tokenizers` et vérifiée à ce moment-là ; si le
modèle changeait un jour, il faudrait la régénérer contre le nouveau, pas
l'ajuster à la main pour faire passer les tests.

Usage :
    python test_wordpiece.py
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — avant tout import core.*

from core.wordpiece import (  # noqa: E402
    LONGUEUR_MAX,
    MAX_CARACTERES_PAR_MOT,
    TokeniseurWordPiece,
    lire_vocabulaire,
)

#: Table de parité : [[texte, [identifiants]], ...], produite par le vrai
#: `tokenizers` (0.22.2) sur la révision épinglée du modèle. Encodée en JSON
#: échappé ASCII pour que ce fichier reste lisible sous n'importe quelle page de
#: code — la leçon de `tools/dev-epure.ps1`.
TABLE_PARITE_JSON = r"""[["", [101, 102]], ["   ", [101, 102]], ["e\u0301", [101, 1041, 102]], ["\u00e9", [101, 1041, 102]], ["\u00c9cole POLYTECHNIQUE", [101, 12431, 26572, 15007, 3490, 4226, 102]], ["na\u00efve fa\u00e7ade \u2014 d\u00e9riv\u00e9e \u2202f/\u2202x", [101, 15743, 8508, 1517, 18547, 2063, 1592, 2546, 1013, 1592, 2595, 102]], ["\u4e2d\u6587\u5b57\u7b26 test", [101, 1746, 1861, 100, 100, 3231, 102]], ["emoji \ud83d\ude42 ok", [101, 7861, 29147, 2072, 100, 7929, 102]], ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", [101, 100, 102]], ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", [101, 22038, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 20348, 2595, 102]], ["C:\\Users\\Ilyan\\epure", [101, 1039, 1024, 1032, 5198, 1032, 6335, 7054, 1032, 4958, 5397, 102]], ["hello\tworld\nnew", [101, 7592, 2088, 2047, 102]], ["l'\u00e9l\u00e8ve d'ENS, 3.14 %", [101, 1048, 1005, 3449, 18697, 1040, 1005, 4372, 2015, 1010, 1017, 1012, 2403, 1003, 102]], ["\u0000zero", [101, 5717, 102]], ["\ufffdremplacement", [101, 2128, 8737, 19217, 3672, 102]], ["\uff26\uff35\uff2c\uff2c", [101, 100, 102]], ["\u00df Stra\u00dfe", [101, 1096, 2358, 27807, 102]], ["\uff71\uff72\uff73", [101, 100, 102]], ["\u200bzerowidth", [101, 5717, 9148, 11927, 2232, 102]], ["f(x)=y+2 $3 <a|b> ~z ^w `q", [101, 1042, 1006, 1060, 1007, 1027, 1061, 1009, 1016, 1002, 1017, 1026, 1037, 1064, 1038, 1028, 1066, 1062, 1034, 1059, 1036, 1053, 102]], ["PTSI2 2025-2026 - LNX \n1/3 \n \nEXERCICE n\u00b01 :  CANON MAGNETIQUE [  ] \nOn consid\u00e8re le circuit \u00e9lectrique plan ci -dessous, dans lequel une barre conductrice \ud835\udc40\ud835\udc41 de masse \ud835\udc5a = \n100 g peut rouler sans glisser (sans perte \u00e9nerg\u00e9tique), et sans que le contact \u00e9lectrique soit rompu, sur \nles conducteurs \ud835\udc44\ud835\udc44\u2032 et \ud835\udc43\ud835\udc43\u2032 parall\u00e8les entre eux, tout en leur restant perpendiculaire. \n \nL\u2019ensemble est plac\u00e9 dans un champ magn\u00e9tique uniforme \ud835\udc35\u20d7  normal au plan du circuit avec \u2016\ud835\udc35\u20d7 \u2016 = 0,50 T. \nLa distance entre les rails (\ud835\udc44\ud835\udc44\u2032) et (\ud835\udc43\ud835\udc43\u2032) est \ud835\udc59 = 10 cm et la barre est initialement immobile. \nLe circuit est aliment\u00e9 par une source de courant continu pouvant produire une intensit\u00e9 \ud835\udc3c = 1,0.103 A.  \nOn pourra utiliser la base cart\u00e9sienne orthonorm\u00e9e dessin\u00e9e sur le sch\u00e9ma de droite (vue de dessus). \n1. D\u2019apr\u00e8s le dessin de l\u2019aimant en \ud835\udc48, exprimer le vecteur \ud835\udc35\u20d7  dans la base cart\u00e9sienne. \n2. Quelle sera la norme de la vitesse du rail, not\u00e9e \ud835\udc63\ud835\udc53, apr\u00e8s avoir parcouru une distance \ud835\udc51 = 30 cm ? \nEXERCICE n\u00b02 :  AIMANT EN EQUILIBRE [     ] \nUn aimant tr\u00e8s fin, de moment magn\u00e9tique \u2133\u20d7\u20d7\u20d7  et de masse \ud835\udc5a est en contact avec une pointe verticale en \nun point \ud835\udc42 diff\u00e9rent de son centre de gravit\u00e9 \ud835\udc3a. \nIl est soumis \u00e0 l\u2019action d\u2019un champ magn\u00e9tique uniforme \ud835\udc35\u20d7  et \u00e0 son poids. \n \nQuelle doit \u00eatre la distance \ud835\udc51 = \ud835\udc42\ud835\udc3a pour que l\u2019aimant soit en \u00e9quilibre en position horizontale ? \nEXERCICE n\u00b03 :  PETITES OSCILLATIONS DE L\u2019AIGUILLE D\u2019UNE BOUSSOLE [     ] \nUne aiguille de boussole, de moment magn\u00e9tique  \u2133\u20d7\u20d7\u20d7 , peut tourner a", [101, 19637, 2072, 2475, 16798, 2629, 1011, 16798, 2575, 1011, 1048, 26807, 1015, 1013, 1017, 4654, 2121, 19053, 2063, 1050, 7737, 2487, 1024, 9330, 16853, 7413, 1031, 1033, 2006, 5136, 2063, 3393, 4984, 11322, 3089, 4226, 2933, 25022, 1011, 4078, 6499, 2271, 1010, 18033, 3393, 22197, 16655, 23189, 6204, 17599, 100, 2139, 3742, 2063, 100, 1027, 2531, 1043, 21877, 4904, 20996, 9307, 2099, 20344, 1043, 6856, 8043, 1006, 20344, 2566, 2618, 4372, 2121, 18150, 7413, 1007, 1010, 3802, 20344, 10861, 3393, 3967, 11322, 3089, 4226, 2061, 4183, 17083, 14289, 1010, 7505, 4649, 6204, 26744, 100, 1531, 3802, 100, 1531, 5903, 2229, 4372, 7913, 7327, 2595, 1010, 2000, 4904, 4372, 3393, 3126, 2717, 4630, 2566, 11837, 14808, 7068, 7442, 1012, 1048, 1521, 7241, 9765, 2173, 18033, 4895, 24782, 16853, 7413, 6375, 2063, 100, 3671, 8740, 2933, 4241, 4984, 13642, 2278, 1519, 100, 1519, 1027, 1014, 1010, 2753, 1056, 1012, 2474, 3292, 4372, 7913, 4649, 15168, 1006, 100, 1531, 1007, 3802, 1006, 100, 1531, 1007, 9765, 100, 1027, 2184, 4642, 3802, 2474, 23189, 9765, 3988, 13665, 10047, 17751, 1012, 3393, 4984, 9765, 4862, 3672, 2063, 11968, 16655, 3120, 2139, 2522, 4648, 3372, 9530, 7629, 2226, 13433, 2226, 18941, 4013, 8566, 7442, 16655, 20014, 6132, 4221, 100, 1027, 1015, 1010, 1014, 1012, 9800, 1037, 1012, 2006, 10364, 2527, 21183, 24411, 2121, 2474, 2918, 11122, 2229, 9013, 2638, 2030, 2705, 17175, 10867, 4402, 4078, 11493, 4402, 7505, 3393, 8040, 28433, 2139, 2852, 28100, 2063, 1006, 24728, 2063, 2139, 4078, 13203, 1007, 1012, 1015, 1012, 1040, 1521, 19804, 2229, 3393, 102]], ["(\ud835\udc35\u20d7 ,\u2133\u20d7\u20d7\u20d7 \u0302 ) compt\u00e9 positivement dans le \nsens direct d\u00e9fini par l\u2019axe orient\u00e9 (\ud835\udc3a\ud835\udc67). \n1. D\u00e9terminer la p\u00e9riode  propre \ud835\udc470 des petites oscillations de l \u2019aiguille autour de sa position \nd\u2019\u00e9quilibre. \n\ud835\udc44 \n\ud835\udc43\u2032 \n\ud835\udc43 \n\ud835\udc44\u2032 \n\ud835\udc40 \n\ud835\udc41 \n\ud835\udc3c \n\ud835\udc62\u20d7 \ud835\udc65 \n\ud835\udc62\u20d7 \ud835\udc66 \n\ud835\udc62\u20d7 \ud835\udc67  . \n\ud835\udc41 \n\ud835\udc46 \n\u00d7 \n\ud835\udc3a \n\ud835\udc42 \n\u00d7 \n\ud835\udc46 \n \ud835\udc41 \n\ud835\udc54  \n \ud835\udc35\u20d7  \n\ud835\udc62\u20d7 \ud835\udc65 \n\ud835\udc62\u20d7 \ud835\udc66 \n\ud835\udc62\u20d7 \ud835\udc67  . \nOndes et signaux TD12 : Action d\u2019un champ magn\u00e9tique \nPTSI2 2025-2026 - LNX \n2/3 \n2. On peut utiliser cette relation pour mesurer la composante horizontale \ud835\udc35\u20d7 \ud835\udc3b du champ magn\u00e9tique \nterrestre, sans avoir \u00e0 conna\u00eetre ni la valeur de la norme \u2133 du moment magn\u00e9tique, ni la valeur \nde \ud835\udc3d. On r\u00e9alise pour cela deux exp\u00e9riences. \n- Dans la premi\u00e8re exp\u00e9rience on  place l\u2019aiguille \u00e0 l\u2019int\u00e9rieur d\u2019une bobine dans laquelle passe un \ncourant qui cr\u00e9e un champ \ud835\udc35\u20d7 \ud835\udc4f\ud835\udc5c\ud835\udc4f de m\u00eame direction et sens que \ud835\udc35\u20d7 \ud835\udc3b et de norme \ud835\udc35\ud835\udc4f\ud835\udc5c\ud835\udc4f > \ud835\udc35\ud835\udc3b. On \nmesure la p\u00e9riode \ud835\udc471 des petites oscillations. \n- Dans la deuxi\u00e8me exp\u00e9rience, on inverse le sens du courant  passant dans la bobine et on mesure \nla p\u00e9riode \ud835\udc472 des petites oscillations. \nExprimer \ud835\udc35\ud835\udc3b en fonction de \ud835\udc35\ud835\udc4f\ud835\udc5c\ud835\udc4f et du rapport \n\ud835\udc471\n\ud835\udc472\n. \nEXERCICE n\u00b04 :  VERIFICATION DE L\u2019EXPRESSION DU COUPLE DE LAPLACE [     ] \nSoit une spire rectangulaire \ud835\udc40\ud835\udc41\ud835\udc43\ud835\udc44 susceptible de tourner autour de l\u2019axe vertical (\ud835\udc42\ud835\udc66) = (\ud835\udc3b\ud835\udc3f) avec \ud835\udc3b \net \ud835\udc3f milieux respectifs de [\ud835\udc40\ud835\udc44] et [\ud835\udc41\ud835\udc43]. On note \ud835\udc3d et \ud835\udc3e les milieux respectifs de [\ud835\udc40\ud835\udc41] et [\ud835\udc43\ud835\udc44]. \nLa spire est parcourue par un courant continu d\u2019intensit\u00e9 \ud835\udc3c > 0 dans le sens \ud835\udc40\ud835\udc41\ud835\udc43\ud835\udc44. \nOn d\u00e9finit un rep\u00e8re cart\u00e9sien orthonorm\u00e9 (\ud835\udc42,\ud835\udc62\u20d7 \ud835\udc65,\ud835\udc62\u20d7 \ud835\udc66,\ud835\udc62\u20d7 \ud835\udc67) et on suppose q", [101, 1006, 100, 1010, 100, 1007, 4012, 13876, 2063, 3893, 3672, 18033, 3393, 12411, 2015, 3622, 13366, 5498, 11968, 1048, 1521, 12946, 16865, 2063, 1006, 100, 1007, 1012, 1015, 1012, 5646, 2099, 2474, 2558, 2063, 17678, 2890, 100, 4078, 20146, 2015, 9808, 6895, 20382, 2015, 2139, 1048, 1521, 9932, 25698, 6216, 8285, 3126, 2139, 7842, 2597, 1040, 1521, 1041, 26147, 12322, 2890, 1012, 100, 100, 1531, 100, 100, 1531, 100, 100, 100, 100, 100, 100, 100, 100, 100, 1012, 100, 100, 1095, 100, 100, 1095, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 1012, 2006, 6155, 3802, 3696, 13754, 14595, 12521, 1024, 2895, 1040, 1521, 4895, 24782, 16853, 7413, 19637, 2072, 2475, 16798, 2629, 1011, 16798, 2575, 1011, 1048, 26807, 1016, 1013, 1017, 1016, 1012, 2006, 21877, 4904, 21183, 24411, 2121, 8292, 4674, 7189, 10364, 2033, 28632, 2099, 2474, 4012, 6873, 22341, 2063, 9876, 2063, 100, 100, 4241, 24782, 16853, 7413, 25170, 3367, 2890, 1010, 20344, 20704, 21165, 1037, 9530, 26416, 7913, 9152, 2474, 10380, 3126, 2139, 2474, 13373, 2063, 100, 4241, 2617, 16853, 7413, 1010, 9152, 2474, 10380, 3126, 2139, 100, 1012, 2006, 19148, 10364, 8292, 2721, 24756, 6322, 1012, 1011, 18033, 2474, 6765, 3325, 2006, 2173, 1048, 1521, 9932, 25698, 6216, 1037, 1048, 1521, 6970, 17301, 2099, 1040, 1521, 16655, 3960, 3170, 18033, 2474, 22197, 2571, 3413, 2063, 4895, 2522, 4648, 3372, 21864, 27831, 4895, 24782, 100, 100, 2139, 2033, 4168, 3257, 3802, 12411, 2015, 10861, 100, 100, 3802, 2139, 13373, 2063, 100, 1028, 100, 1012, 2006, 2033, 28632, 2474, 2558, 102]], ["par la spire. \n \n \n1. On suppose que la force de Laplace qui s\u2019applique sur chaque c\u00f4t\u00e9 de la spire s\u2019applique en son \nmilieu. Exprimer chacune de ces forces et les repr\u00e9senter sur les sch\u00e9mas ci-dessus (en choisissant \n", [101, 11968, 2474, 19823, 1012, 1015, 1012, 2006, 6814, 10861, 2474, 2486, 2139, 5001, 19217, 21864, 1055, 1521, 10439, 3669, 4226, 7505, 15775, 4226, 17155, 2139, 2474, 19823, 1055, 1521, 10439, 3669, 4226, 4372, 2365, 23689, 17301, 1012, 4654, 18098, 14428, 2099, 15775, 10841, 2638, 2139, 8292, 2015, 2749, 3802, 4649, 5050, 2121, 7505, 4649, 8040, 28433, 2015, 25022, 1011, 4078, 13203, 1006, 4372, 18151, 6190, 22341, 102]], ["\u2019\u00e9quilibrer la balance. \nEn l\u2019absence de champ magn\u00e9tique et de masse \ud835\udc5a, la position du plateau est ajust\u00e9e afin que la balance \nsoit \u00e0 l\u2019\u00e9quilibre avec le bras de droite parfaitement horizontal. \nOn travaillera dans une", [101, 1521, 1041, 26147, 12322, 14544, 2474, 5703, 1012, 4372, 1048, 1521, 6438, 2139, 24782, 16853, 7413, 3802, 2139, 3742, 2063, 100, 1010, 2474, 2597, 4241, 9814, 9765, 19128, 19966, 4402, 28697, 2078, 10861, 2474, 5703, 2061, 4183, 1037, 1048, 1521, 1041, 26147, 12322, 2890, 13642, 2278, 3393, 11655, 2015, 2139, 2852, 28100, 2063, 11968, 7011, 4221, 3672, 9876, 1012, 2006, 19817, 12462, 10484, 2527, 18033, 16655, 102]], ["TD14 \u2013 Applications lin\u00e9aires PTSI2\nE.Pagnoud (2025-26)\nLes exercices not\u00e9s \"en autonomie\" doivent \u00eatre trait\u00e9s seuls avec le corrig\u00e9. Les exercices \u00e9toil\u00e9s\nsont un peu moins basiques.\nExercice 1 (Morphisme en g\u00e9om\u00e9trie)", [101, 14595, 16932, 1516, 5097, 2240, 14737, 2015, 19637, 2072, 2475, 1041, 1012, 6643, 26745, 6784, 1006, 16798, 2629, 1011, 2656, 1007, 4649, 4654, 2121, 19053, 2229, 3964, 1000, 4372, 8285, 3630, 9856, 1000, 9193, 15338, 3802, 2890, 18275, 2229, 7367, 28426, 13642, 2278, 3393, 2522, 18752, 3351, 1012, 4649, 4654, 2121, 19053, 2229, 3802, 10448, 4244, 2365, 2102, 4895, 21877, 2226, 25175, 3619, 19021, 19516, 1012, 4654, 2121, 19053, 2063, 1015, 1006, 22822, 21850, 6491, 2063, 4372, 20248, 11368, 7373, 1007, 102]]]"""

CAS: list[tuple[str, list[int]]] = [
    (texte, identifiants) for texte, identifiants in json.loads(TABLE_PARITE_JSON)
]

#: Le vocabulaire du modèle, s'il est déjà sur le disque de ce poste. Absent en
#: CI (le modèle ne se télécharge pas pendant la suite, cf. `_test_env`) : les
#: tests qui en ont besoin se sautent alors explicitement, plutôt que de
#: fabriquer un faux vocabulaire qui ne prouverait rien.
def _vocabulaire() -> dict[str, int] | None:
    from core.embedding_install import chemin_fichier_modele
    chemin = chemin_fichier_modele("vocab.txt")
    if not chemin.is_file():
        # Repli : le cache HuggingFace de l'ancienne pile, encore présent sur le
        # poste de dev. Utile pour que ce fichier serve pendant la migration.
        cache = (Path.home() / ".cache" / "huggingface" / "hub"
                 / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots")
        if cache.is_dir():
            for instantane in cache.iterdir():
                candidat = instantane / "vocab.txt"
                if candidat.is_file():
                    chemin = candidat
                    break
    if not chemin.is_file():
        return None
    return lire_vocabulaire(chemin)


VOCABULAIRE = _vocabulaire()
_SANS_VOCAB = "vocab.txt absent (le modèle n'est pas téléchargé sur ce poste)"


class ParitéTest(unittest.TestCase):
    """La table figée, cas par cas."""

    @classmethod
    def setUpClass(cls):
        if VOCABULAIRE is None:
            raise unittest.SkipTest(_SANS_VOCAB)
        cls.tok = TokeniseurWordPiece(VOCABULAIRE)

    def test_la_table_est_bien_remplie(self):
        """Garde-fou du garde-fou : une table vide ferait passer tout ce fichier."""
        self.assertGreaterEqual(len(CAS), 25)
        self.assertTrue(all(ids for _, ids in CAS if _ != ""))

    def test_chaque_cas_donne_exactement_les_memes_identifiants(self):
        for index, (texte, attendus) in enumerate(CAS):
            with self.subTest(cas=index, extrait=texte[:40]):
                self.assertEqual(attendus, self.tok.encoder(texte))

    def test_la_table_contient_des_cas_tronques(self):
        """Sinon la troncature ne serait pas éprouvée — et c'est le chemin de la
        grande majorité des chunks réels.
        """
        tronques = [t for t, ids in CAS if len(ids) == LONGUEUR_MAX]
        self.assertGreaterEqual(len(tronques), 1)

    def test_aucune_sequence_ne_depasse_la_longueur_max(self):
        for texte, _ in CAS:
            self.assertLessEqual(len(self.tok.encoder(texte)), LONGUEUR_MAX,
                                 texte[:40])


#: Vocabulaire SYNTHÉTIQUE pour les tests de règles. Il ne prétend pas imiter le
#: modèle : il est construit pour que chaque règle soit éprouvée SANS ÊTRE VACUE.
#: Le piège d'un vocabulaire jouet est là — si ni « élève » ni « eleve » n'y sont,
#: les deux donnent `[UNK]`, l'égalité est vraie, et le test ne prouve rien. On y
#: met donc la forme SANS accent et pas la forme accentuée : le résultat n'est
#: égal que si les accents ont réellement été retirés.
#:
#: Il rend ces tests exécutables PARTOUT, CI comprise, où `vocab.txt` n'est pas
#: téléchargé. C'est le complément de la table figée, pas son remplaçant : la
#: table dit « identique au vrai tokeniseur », ceux-ci disent « la règle est bien
#: celle-là ».
IDEOGRAMME = chr(0x4E2D)          # 中, dans une plage de _est_chinois
VOCAB_SYNTHETIQUE = {
    "[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3,
    "eleve": 4, "f": 5, "(": 6, "x": 7, ")": 8, "=": 9, "y": 10,
    "mot": 11, "a": 12, "b": 13, "##s": 14, IDEOGRAMME: 15, "##a": 16,
}


class ReglesTest(unittest.TestCase):
    """Les règles qui se déduisent mal, sur un vocabulaire synthétique.

    La table figée dit « ça correspond au vrai tokeniseur » ; ces tests disent
    POURQUOI, donc ce qu'un futur correctif casserait. Un échec ici nomme la
    règle ; un échec dans la table dit seulement qu'un identifiant a changé.

    Ils tournent PARTOUT, y compris là où le modèle n'est pas téléchargé — c'est
    tout l'intérêt du vocabulaire synthétique.
    """

    def setUp(self):
        self.tok = TokeniseurWordPiece(VOCAB_SYNTHETIQUE)
        self.unk = VOCAB_SYNTHETIQUE["[UNK]"]

    def _nu(self, texte: str) -> list[int]:
        """Les identifiants sans les jetons spéciaux, plus lisibles à comparer."""
        return self.tok.encoder(texte)[1:-1]

    def test_les_accents_sont_retires_car_strip_accents_suit_lowercase(self):
        """`tokenizer.json` dit `strip_accents: null`, ce qui NE veut PAS dire
        « ne pas retirer ». Dans `BertNormalizer`, `None` signifie « suivre
        `lowercase` » — donc ici les accents SONT retirés. Le lire comme un
        `False` donnerait un tokeniseur juste sur l'anglais et faux sur chaque mot
        accentué, c'est-à-dire sur tout le corpus de cette instance.

        Le vocabulaire ne contient QUE la forme sans accent : l'égalité ne peut
        donc pas être obtenue par deux `[UNK]`.
        """
        eleve_accentue = "élève"
        self.assertEqual([VOCAB_SYNTHETIQUE["eleve"]], self._nu(eleve_accentue))
        self.assertEqual([VOCAB_SYNTHETIQUE["eleve"]], self._nu(eleve_accentue.upper()))

    def test_accent_combinant_et_precompose_donnent_le_meme_resultat(self):
        """« é » précomposé et « e » + U+0301 doivent tomber sur le même jeton :
        c'est la normalisation NFD avant suppression des marques.
        """
        precompose = "élève"
        combinant = "e" + chr(0x301) + "le" + chr(0x300) + "ve"
        self.assertEqual([VOCAB_SYNTHETIQUE["eleve"]], self._nu(precompose))
        self.assertEqual(self._nu(precompose), self._nu(combinant))

    def test_les_symboles_ascii_comptent_comme_ponctuation(self):
        """`$ + < = ^ ` | ~` sont des SYMBOLES pour Unicode (Sc, Sm, Sk), pas de
        la ponctuation, et BERT les traite pourtant comme telle. S'en tenir à
        `category().startswith("P")` collerait `f(x)=y` en un seul mot — et le
        vocabulaire ne contient PAS `f(x)=y`, donc ce serait `[UNK]`.
        """
        attendu = [VOCAB_SYNTHETIQUE[j] for j in ("f", "(", "x", ")", "=", "y")]
        self.assertEqual(attendu, self._nu("f(x)=y"))
        self.assertNotIn(self.unk, self._nu("f(x)=y"))

    def test_un_mot_inconnu_devient_un_seul_inconnu(self):
        """`[UNK]` pour le mot ENTIER, pas pour chacun de ses morceaux."""
        self.assertEqual([self.unk], self._nu("zorglub"))

    def test_un_mot_trop_long_devient_un_seul_inconnu(self):
        """`max_input_chars_per_word` = 100 : au-delà on abandonne le mot sans
        même essayer de le découper. WordPiece étant quadratique sur la longueur
        du mot, ce n'est pas qu'une règle de fidélité — un « mot » de 50 000
        caractères existe dès qu'un PDF mal extrait recolle une page entière.
        """
        self.assertEqual([self.unk], self._nu("a" * (MAX_CARACTERES_PAR_MOT + 1)))
        # Juste en dessous de la borne, le mot est bien découpé : « a » puis
        # « ##a » répété. Le préfixe de continuation est indispensable ici — sans
        # « ##a » dans le vocabulaire, le mot entier retomberait sur `[UNK]`, et
        # ce test passerait pour la mauvaise raison.
        attendu = ([VOCAB_SYNTHETIQUE["a"]]
                   + [VOCAB_SYNTHETIQUE["##a"]] * (MAX_CARACTERES_PAR_MOT - 1))
        self.assertEqual(attendu, self._nu("a" * MAX_CARACTERES_PAR_MOT))

    def test_le_prefixe_de_continuation_est_bien_utilise(self):
        """« mots » = « mot » + « ##s » : sans le préfixe `##` sur les morceaux
        suivants, la recherche échouerait et le mot deviendrait `[UNK]`.
        """
        self.assertEqual([VOCAB_SYNTHETIQUE["mot"], VOCAB_SYNTHETIQUE["##s"]],
                         self._nu("mots"))

    def test_les_jetons_speciaux_encadrent_toujours(self):
        ids = self.tok.encoder("mot")
        self.assertEqual(VOCAB_SYNTHETIQUE["[CLS]"], ids[0])
        self.assertEqual(VOCAB_SYNTHETIQUE["[SEP]"], ids[-1])

    def test_les_jetons_speciaux_survivent_a_la_troncature(self):
        """La troncature retire du CONTENU, jamais `[CLS]`/`[SEP]` : une séquence
        qui perdrait son `[SEP]` produirait un vecteur subtilement différent — le
        genre d'écart qui ne se voit pas sur un test à trois mots et décale toute
        une réindexation.
        """
        ids = self.tok.encoder("mot " * 2000)
        self.assertEqual(LONGUEUR_MAX, len(ids))
        self.assertEqual(VOCAB_SYNTHETIQUE["[CLS]"], ids[0])
        self.assertEqual(VOCAB_SYNTHETIQUE["[SEP]"], ids[-1])

    def test_un_texte_vide_rend_les_deux_jetons_speciaux(self):
        self.assertEqual([VOCAB_SYNTHETIQUE["[CLS]"], VOCAB_SYNTHETIQUE["[SEP]"]],
                         self.tok.encoder(""))
        self.assertEqual(self.tok.encoder(""), self.tok.encoder("   "))

    def test_les_caracteres_de_controle_sont_supprimes(self):
        """`clean_text` : U+0000 et U+FFFD disparaissent, mais tabulation et saut
        de ligne deviennent des ESPACES — ils séparent deux mots au lieu de les
        coller.
        """
        self.assertEqual(self._nu("mot"), self._nu(chr(0) + "mot"))
        self.assertEqual(self._nu("mot"), self._nu(chr(0xFFFD) + "mot"))
        self.assertEqual([VOCAB_SYNTHETIQUE["a"], VOCAB_SYNTHETIQUE["b"]],
                         self._nu("a" + chr(9) + "b"))
        self.assertEqual(self._nu("a b"), self._nu("a" + chr(10) + "b"))

    def test_un_ideogramme_est_isole_entre_deux_espaces(self):
        """`handle_chinese_chars` : sans l'isolement, « 中mot » serait un seul mot,
        donc `[UNK]`. Avec, ce sont deux jetons connus.
        """
        self.assertEqual([VOCAB_SYNTHETIQUE[IDEOGRAMME], VOCAB_SYNTHETIQUE["mot"]],
                         self._nu(IDEOGRAMME + "mot"))

    def test_le_lot_rend_la_longueur_du_plus_long(self):
        lots, longueur = self.tok.encoder_lot(["mot", "mot mot mot mots"])
        self.assertEqual(longueur, max(len(x) for x in lots))
        self.assertEqual(2, len(lots))


class VocabulaireTest(unittest.TestCase):
    """La lecture de `vocab.txt` — l'identifiant EST le numéro de ligne."""

    def test_les_jetons_speciaux_sont_exiges(self):
        """Un vocabulaire amputé de `[CLS]` ne lèverait qu'au premier encodage,
        loin de sa cause. On refuse à la lecture.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            chemin = Path(tmp) / "vocab.txt"
            chemin.write_text("bonjour\nmonde\n", encoding="utf-8")
            with self.assertRaises(ValueError) as capture:
                lire_vocabulaire(chemin)
            self.assertIn("[PAD]", str(capture.exception))

    def test_l_identifiant_est_le_numero_de_ligne(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            chemin = Path(tmp) / "vocab.txt"
            chemin.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]\nzero\n", encoding="utf-8")
            vocabulaire = lire_vocabulaire(chemin)
            self.assertEqual(0, vocabulaire["[PAD]"])
            self.assertEqual(4, vocabulaire["zero"])

    def test_les_espaces_des_jetons_ne_sont_pas_rognes(self):
        """Un `.strip()` détruirait les jetons qui SONT de la ponctuation ou des
        espaces, et le résultat ne se verrait qu'en divergence de tokenisation.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            chemin = Path(tmp) / "vocab.txt"
            chemin.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]\n \n", encoding="utf-8")
            self.assertIn(" ", lire_vocabulaire(chemin))


class SansDependanceCompileeTest(unittest.TestCase):
    """Le point du chantier : aucun binaire non signé sur ce chemin."""

    def test_le_module_n_importe_que_la_bibliotheque_standard(self):
        """`core/wordpiece.py` ne doit importer ni `tokenizers`, ni
        `transformers`, ni `torch`. C'est tout l'intérêt de l'avoir réécrit : le
        réintroduire par commodité ramènerait le `.pyd` non signé que Smart App
        Control bloque.
        """
        source = (Path(__file__).resolve().parent / "core" / "wordpiece.py").read_text(
            encoding="utf-8")
        lignes_import = [l.strip() for l in source.splitlines()
                         if l.startswith(("import ", "from "))]
        for interdit in ("tokenizers", "transformers", "torch", "sklearn", "numpy"):
            for ligne in lignes_import:
                self.assertNotIn(interdit, ligne,
                                 f"{interdit} importé par core/wordpiece.py")
        self.assertIn("import unicodedata", lignes_import)


if __name__ == "__main__":
    unittest.main(verbosity=2)
