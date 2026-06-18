"""
Routeur du module astral — Conception complete d'Astral Earth.

Sert l'integralite du game design document (GDD) via des endpoints REST :
armes, minerais, crafting, potions, mobilier, accessoires, drone, boss,
PNJ, evenements, multijoueur, sauvegarde, UI, optimisation, mods, audio,
accessibilite, equilibrage.

Contraintes : from fastapi import APIRouter + router = APIRouter(),
aucun subprocess/socket/importlib/os.system/eval/exec, aucun acces API.
Toutes les routes sont prefixees par /astral (backend.prefix = "").
"""

from fastapi import APIRouter

router = APIRouter()

# ============================================================================
# 1. ARMES — 5 categories (melee, distance, magie, invocation, gadget)
#    Chaque arme a : nom, degats, vitesse (coups/s), recul, effets speciaux,
#    tier (1-5), materiau de craft, description.
# ============================================================================

MELEE_WEAPONS = [
    # Tier 1 (Bois / Pierre)
    {"id":"wooden_sword","tier":1,"type":"melee","name":"Epee en bois","damage":6,"speed":1.8,"knockback":2.0,"effects":[],"material":"Bois (8 planches)","combo":"3 coups : horizontal (100%) -> diagonal (110%) -> estoc (130% + percage 1 ennemi)","desc":"Arme de depart fiable. Combo basique a 3 coups."},
    {"id":"wooden_spear","tier":1,"type":"melee","name":"Lance en bois","damage":7,"speed":1.2,"knockback":3.5,"effects":["portee +1 tuile"],"material":"Bois (10 planches)","combo":"2 coups : estoc (100%) -> estoc large (120% + percage 2 ennemis)","desc":"Portee etendue, ideale pour tenir les ennemis a distance."},
    {"id":"stone_axe","tier":1,"type":"melee","name":"Hache en pierre","damage":8,"speed":1.4,"knockback":3.0,"effects":["+50% degats aux structures","coupe les arbres en 1 coup"],"material":"Pierre (8 roche + 2 batons)","combo":"2 coups : taillade (100%) -> fendage (150% + brise armure -2 defense)","desc":"Lente mais puissante. Bonus contre les ennemis blindes."},
    {"id":"copper_dagger","tier":1,"type":"melee","name":"Dague en cuivre","damage":4,"speed":3.0,"knockback":1.0,"effects":["+30% degats dans le dos","coup critique x3 au lieu de x2"],"material":"Cuivre (6 barres)","combo":"5 coups rapides : enchainement fluide sans recul croissant","desc":"Tres rapide, faible portee. Specialiste des attaques sournoises."},
    {"id":"bone_club","tier":1,"type":"melee","name":"Massue en os","damage":10,"speed":1.0,"knockback":5.0,"effects":["10% chance etourdissement 1s"],"material":"Os (12 os)","combo":"2 coups : coup horizontal (100%) -> smash vertical (140% + zone 3x2)","desc":"Tres lent, degats eleves. L'etourdissement change le cours du combat."},
    # Tier 2 (Fer)
    {"id":"iron_sword","tier":2,"type":"melee","name":"Epee en fer","damage":12,"speed":1.7,"knockback":3.0,"effects":[],"material":"Fer (10 barres)","combo":"3 coups : horizontal (100%) -> diagonal (115%) -> estoc (135% + percage 2 ennemis)","desc":"L'epee standard du milieu de partie. Fiable et polyvalente."},
    {"id":"iron_greatsword","tier":2,"type":"melee","name":"Espadon en fer","damage":18,"speed":0.9,"knockback":6.0,"effects":["zone d'impact 3x2"],"material":"Fer (14 barres)","combo":"2 coups : arc large (100%, zone 3x2) -> estoc charge (160%, ligne 4 tuiles)","desc":"Lent mais devastateur. Balaye les groupes d'ennemis."},
    {"id":"flameblade","tier":2,"type":"melee","name":"Lameflamme","damage":10,"speed":1.6,"knockback":2.5,"effects":["brulure 4 degats/s pendant 3s","lumiere rayon 3 tuiles"],"material":"Fer (8 barres) + Essence de feu (3)","combo":"3 coups : chaque coup applique 1 cumul de brulure (max 3)","desc":"Enflamme les ennemis. Utile comme source de lumiere portable."},
    {"id":"iron_spear","tier":2,"type":"melee","name":"Lance en fer","damage":13,"speed":1.2,"knockback":4.0,"effects":["portee +1.5 tuile"],"material":"Fer (11 barres)","combo":"2 coups : estoc (100%) -> moulinet (130%, zone cercle rayon 2)","desc":"Excellente portee. Le moulinet repousse les ennemis proches."},
    {"id":"ripper_claws","tier":2,"type":"melee","name":"Griffes eventreuses","damage":8,"speed":3.5,"knockback":1.5,"effects":["saignement 3 degats/s pendant 5s","+20% vitesse deplacement pendant 2s apres kill"],"material":"Fer (7 barres) + Cuir (4)","combo":"5 coups : chaque coup consecutif gagne +8% degats (max +40%)","desc":"Style de combat bestial. Recompense l'agressivite constante."},
    # Tier 3 (Or spectral / Acier)
    {"id":"spectral_blade","tier":3,"type":"melee","name":"Lame spectrale","damage":18,"speed":1.8,"knockback":3.5,"effects":["20% chance de traverser l'armure","lueur spectrale (revele ennemis invisibles)"],"material":"Or spectral (12 barres) + Ame errante (5)","combo":"3 coups : tranche (100%) -> tranche spectrale (120% + ignore 50% armure) -> estoc fantome (150% + teleportation 3 tuiles vers l'ennemi)","desc":"Mi-materielle, mi-ethere. L'estoc fantome est un gap-closer."},
    {"id":"steel_halberd","tier":3,"type":"melee","name":"Hallebarde en acier","damage":22,"speed":0.8,"knockback":7.0,"effects":["portee +2 tuiles","coup en fente (clic droit) : charge 3 tuiles, 200% degats"],"material":"Acier (16 barres)","combo":"2 coups : fauchage large (100%, arc 180deg) -> estoc puissant (150%, ligne 5 tuiles)","desc":"La reine de la portee. La charge change le positionnement."},
    {"id":"crystal_rapier","tier":3,"type":"melee","name":"Rapiere de cristal","damage":14,"speed":2.5,"knockback":2.0,"effects":["contre parfait : si attaque pendant la fenetre ennemie -> riposte critique x4","+15% esquive pendant 1s apres coup"],"material":"Cristal pur (10 barres) + Acier (4)","combo":"4 coups : serie d'estocs precis, le 4e coup est toujours critique","desc":"Arme de finesse. Le contre parfait change le skill ceiling."},
    {"id":"venom_fang","tier":3,"type":"melee","name":"Croc de venin","damage":12,"speed":2.0,"knockback":2.5,"effects":["poison 8 degats/s pendant 4s","si ennemi <30% PV -> poison double (16/s)"],"material":"Acier (8 barres) + Glande de venin (6)","combo":"3 coups : morsure (100% + poison) -> laceration (120% + 2 cumuls poison) -> croc final (140% + explosion poison zone 3x3)","desc":"Specialiste du poison. Acheve les ennemis affaiblis."},
    {"id":"titan_hammer","tier":3,"type":"melee","name":"Marteau titan","damage":28,"speed":0.6,"knockback":9.0,"effects":["onde de choc (zone 5x3 devant)","brise les blocs fragiles au contact"],"material":"Acier (20 barres) + Noyau de titan (1)","combo":"1 coup : smash au sol (100% + onde choc secondaire a 60% degats)","desc":"Le plus lent, le plus puissant. Chaque coup est un mini-seisme."},
    # Tier 4 (Draconique)
    {"id":"dragon_blade","tier":4,"type":"melee","name":"Lame draconique","damage":26,"speed":1.6,"knockback":5.0,"effects":["souffle draconique (clic droit) : cone feu 30 degats, cout 15 mana, CD 8s"],"material":"Ecaille de dragon (15) + Barre draconique (8)","combo":"3 coups : tranche (100%) -> double tranche (110% x2 hits) -> souffle integre (140% + effet souffle gratuit)","desc":"Polyvalente avec son souffle integre. Le combo 3 declenche un souffle sans cout."},
    {"id":"void_scythe","tier":4,"type":"melee","name":"Faux du neant","damage":24,"speed":1.2,"knockback":6.0,"effects":["aspiration (attire ennemis dans zone 5x5)","vol de vie 5% des degats"],"material":"Metal du neant (14 barres) + Fragment de neant (10)","combo":"2 coups : fauchage circulaire (100%, 360deg) -> moisson (150% + vol de vie double + aspiration renforcee)","desc":"Controle de foule ultime. Aspire puis moissonne."},
    {"id":"storm_blade","tier":4,"type":"melee","name":"Lame-tempete","damage":20,"speed":2.5,"knockback":4.0,"effects":["apres 5 coups -> eclair en chaine (3 ennemis, 15 degats chacun)","+10% vitesse par cumul de tempete (max 30%)"],"material":"Ecaille de dragon celeste (12) + Noyau d'orage (1)","combo":"5 coups rapides -> activation tempete automatique","desc":"Plus on frappe, plus on va vite. La tempete est un bonus passif."},
    {"id":"earthbreaker","tier":4,"type":"melee","name":"Brise-terre","damage":32,"speed":0.7,"knockback":10.0,"effects":["fissure sismique (ligne 8 tuiles, 50% degats)","+20 defense pendant le coup"],"material":"Metal draconique (18 barres) + Noyau terrestre (1)","combo":"1 coup : impact sismique (100% + fissure), hyper-armure pendant l'animation","desc":"Imparable pendant l'attaque. La fissure traverse les murs."},
    {"id":"starlight_rapier","tier":4,"type":"melee","name":"Rapiere stellaire","damage":18,"speed":3.2,"knockback":2.0,"effects":["apres esquive -> teleportation derriere l'ennemi","coup critique garanti apres teleportation"],"material":"Poussiere d'etoile (20) + Alliage stellaire (8)","combo":"Pas de combo fixe : chaque coup apres teleportation est critique + traverse armure","desc":"Mobilite pure. Recompense le jeu d'esquive et de repositionnement."},
    # Tier 5 (Legendaire)
    {"id":"excalibur_astral","tier":5,"type":"melee","name":"Excalibur Astrale","damage":38,"speed":2.0,"knockback":6.0,"effects":["lumiere sacree (zone 7x7, degats continus 15/s aux morts-vivants)","benediction : +15% degats de tout le groupe pendant 10s apres kill"],"material":"Lingot astral (25) + Fragment d'etoile filante (5) + Volonte du roi (1)","combo":"3 coups : tranche sacree (100%) -> croix lumineuse (130%, zone croix 5x5) -> jugement astral (200%, zone 7x7, soigne allies de 10% max PV)","desc":"L'arme ultime. Soigne les allies au 3e coup du combo."},
    {"id":"cosmic_cleaver","tier":5,"type":"melee","name":"Fendeur cosmique","damage":42,"speed":1.4,"knockback":8.0,"effects":["trou noir (clic droit) : aspire + degats 50/s zone 5x5 pendant 3s, CD 20s","brise-armure : -15 defense ennemie par coup (max -45)"],"material":"Masse noire (30) + Noyau cosmique (1) + Fragment d'etoile (10)","combo":"2 coups : entaille dimensionnelle (100%, ligne 6) -> effondrement cosmique (180%, explosion 5x5)","desc":"Controle absolu du champ de bataille. Le trou noir aspire meme les boss legers."},
    {"id":"phoenix_edge","tier":5,"type":"melee","name":"Lame Phenix","damage":32,"speed":2.2,"knockback":5.0,"effects":["a la mort -> resurrection (1 fois/combat, 50% PV, explosion feu 100 degats zone 8x8)","brulure sacree 12/s pendant 5s"],"material":"Plume de phenix (1) + Lingot astral (20) + Cendre eternelle (30)","combo":"3 coups : chaque coup laisse une trainee de feu sacre au sol (dure 4s, 8 degats/s)","desc":"La securite ultime. Une resurrection garantie par combat."},
    {"id":"eternity_blade","tier":5,"type":"melee","name":"Lame de l'Eternite","damage":36,"speed":1.8,"knockback":5.5,"effects":["arret du temps (clic droit) : fige ennemis dans zone 6x6 pendant 2s, CD 45s","+25% degats contre ennemis figes"],"material":"Sable du temps (50) + Lingot astral (22) + Noyau temporel (1)","combo":"4 coups : serie intemporelle, le 4e coup est toujours a 200% + ralentit de 50% pendant 3s","desc":"Manipulation du temps. L'arret du temps change les phases de boss."},
    {"id":"abyssal_blade","tier":5,"type":"melee","name":"Lame abyssale","damage":44,"speed":1.0,"knockback":7.0,"effects":["corruption : 5% PV max ennemis en degats supplementaires","tentacules abyssaux : attaque aussi les ennemis dans un rayon 3 autour de la cible"],"material":"Lingot abyssal (28) + Coeur du Kraken (1) + Oeil de Cthulhu (1)","combo":"2 coups : balayage abyssal (100%, 360deg) -> appel des profondeurs (160% + invocation tentacules 5s)","desc":"Degats proportionnels aux PV. Devore les boss a haut PV."},
]

RANGED_WEAPONS = [
    # Tier 1
    {"id":"wooden_bow","tier":1,"type":"ranged","name":"Arc en bois","damage":5,"speed":1.5,"knockback":1.5,"velocity":8,"ammo":"Fleche en bois","effects":[],"material":"Bois (8 planches + 2 ficelles)","desc":"Arc de base. Fleches droites, portee 20 tuiles."},
    {"id":"slingshot","tier":1,"type":"ranged","name":"Lance-pierre","damage":4,"speed":2.5,"knockback":1.0,"velocity":6,"ammo":"Pierre / Gravier","effects":["10% chance ricochet (1 rebond)"],"material":"Bois (6 planches) + Cuir (2)","desc":"Tres rapide mais faible. Le ricochet peut toucher 2 cibles."},
    {"id":"bone_bow","tier":1,"type":"ranged","name":"Arc en os","damage":7,"speed":1.3,"knockback":2.0,"velocity":9,"ammo":"Fleche en os","effects":[],"material":"Os (14) + Ficelle (3)","desc":"Plus puissant que l'arc en bois. Fleches plus rapides."},
    # Tier 2
    {"id":"iron_bow","tier":2,"type":"ranged","name":"Arc en fer","damage":10,"speed":1.4,"knockback":2.5,"velocity":10,"ammo":"Fleche en fer","effects":[],"material":"Fer (8 barres) + Ficelle (4)","desc":"Arc standard du milieu de partie. Fiable."},
    {"id":"crossbow","tier":2,"type":"ranged","name":"Arbalete","damage":16,"speed":0.7,"knockback":4.0,"velocity":14,"ammo":"Carreau","effects":["percage (traverse 2 ennemis)","rechargement (1s apres tir)"],"material":"Fer (10 barres) + Mecanisme (2) + Bois (6)","desc":"Lent mais percant. Chaque carreau traverse."},
    {"id":"frost_shortbow","tier":2,"type":"ranged","name":"Arc court de givre","damage":8,"speed":2.2,"knockback":1.5,"velocity":11,"ammo":"Fleche de glace","effects":["ralentissement 30% pendant 2s"],"material":"Fer (6 barres) + Noyau de glace (4)","desc":"Tir rapide avec controle. Kite facilite."},
    {"id":"firestarter","tier":2,"type":"ranged","name":"Boutefeu","damage":5,"speed":1.8,"knockback":1.0,"velocity":7,"ammo":"Fiole de feu","effects":["explosion zone 2x2","brulure 5/s pendant 3s","peut bruler herbe/bois"],"material":"Fer (8 barres) + Poudre a canon (10)","desc":"Degats de zone. Attention aux incendies accidentels."},
    # Tier 3
    {"id":"spectral_bow","tier":3,"type":"ranged","name":"Arc spectral","damage":16,"speed":1.5,"knockback":3.0,"velocity":12,"ammo":"Fleche spectrale","effects":["fleche traverse les murs (1 bloc)","lueur (revele zone impact)"],"material":"Or spectral (10 barres) + Poussiere d'ame (8)","desc":"Tir a travers les blocs. Revele les zones cachees."},
    {"id":"repeater_crossbow","tier":3,"type":"ranged","name":"Arbalete a repetition","damage":12,"speed":1.8,"knockback":3.0,"velocity":13,"ammo":"Carreau","effects":["chargeur 5 carreaux (pas de rechargement entre chaque)","rechargement 2s apres 5 tirs"],"material":"Acier (12 barres) + Mecanisme avance (4)","desc":"5 tirs rapides puis pause. DPS en rafale."},
    {"id":"thunder_rifle","tier":3,"type":"ranged","name":"Fusil-tonnerre","damage":20,"speed":0.5,"knockback":6.0,"velocity":18,"ammo":"Balle de foudre","effects":["hit-scan (pas de projectile)","chaine d'eclairs (2 cibles secondaires a 40% degats)"],"material":"Acier (15 barres) + Noyau d'orage (2) + Mecanisme avance (5)","desc":"Hit-scan ultra-rapide. Excellent contre les ennemis rapides."},
    {"id":"crystal_longbow","tier":3,"type":"ranged","name":"Arc long de cristal","damage":22,"speed":1.0,"knockback":4.0,"velocity":15,"ammo":"Fleche de cristal","effects":["tir charge (maintien 1.5s -> 250% degats + traversee illimitee)"],"material":"Cristal pur (12 barres) + Acier (6)","desc":"Le tir charge traverse tout. Sniper des couloirs."},
    # Tier 4
    {"id":"dragon_bow","tier":4,"type":"ranged","name":"Arc draconique","damage":26,"speed":1.3,"knockback":5.0,"velocity":14,"ammo":"Fleche draconique","effects":["fleche explosive zone 3x3 (30% degats explosion)","brulure 10/s pendant 3s"],"material":"Ecaille de dragon (14) + Barre draconique (8)","desc":"Explosions et brulures. Degats de zone excellents."},
    {"id":"needle_gun","tier":4,"type":"ranged","name":"Fusil a aiguilles","damage":6,"speed":4.0,"knockback":0.5,"velocity":16,"ammo":"Aiguille","effects":["tir automatique (maintien clic)","saignement 2/s par aiguille (max 10 cumuls, 20/s)"],"material":"Metal du neant (10 barres) + Mecanisme de precision (8)","desc":"Cadence de tir extreme. Empile les saignements."},
    {"id":"stardust_blaster","tier":4,"type":"ranged","name":"Fulgurateur stellaire","damage":18,"speed":2.0,"knockback":3.0,"velocity":12,"ammo":"Poussiere d'etoile","effects":["projectile guide (poursuit ennemis proches)","eclats (3 projectiles secondaires a 25% degats chacun)"],"material":"Poussiere d'etoile (20) + Alliage stellaire (10)","desc":"Projectiles autoguides. Ideal contre les ennemis mobiles."},
    {"id":"void_cannon","tier":4,"type":"ranged","name":"Canon du neant","damage":34,"speed":0.4,"knockback":8.0,"velocity":10,"ammo":"Orbe du neant","effects":["trou de ver : aspire ennemis vers point d'impact","degats de zone 4x4 (100% au centre, 50% bord)"],"material":"Metal du neant (16 barres) + Fragment de neant (12)","desc":"Trou noir portatif. Aspire puis explose."},
    # Tier 5
    {"id":"astral_arbalest","tier":5,"type":"ranged","name":"Arbalest astrale","damage":45,"speed":0.6,"knockback":7.0,"velocity":20,"ammo":"Carreau astral","effects":["hit-scan","traverse tous les ennemis sur la ligne","explosion retardee (1s apres impact, zone 5x5, 50% degats)"],"material":"Lingot astral (25) + Fragment d'etoile (15) + Mecanisme legendaire (1)","desc":"Sniper ultime. Chaque tir est un railgun a explosion retardee."},
    {"id":"infinity_quiver","tier":5,"type":"ranged","name":"Carquois infini","damage":30,"speed":3.0,"knockback":4.0,"velocity":14,"ammo":"Fleche infinie (pas de munition necessaire)","effects":["tire 3 fleches en eventail","10% chance fleche celeste (500% degats, zone 6x6)"],"material":"Lingot astral (30) + Plume de Phenix (1) + Essence infinie (1)","desc":"Pluie de fleches infinie. La fleche celeste est un mini-jugement."},
    {"id":"nova_cannon","tier":5,"type":"ranged","name":"Canon Nova","damage":55,"speed":0.3,"knockback":10.0,"velocity":8,"ammo":"Noyau de nova","effects":["explosion zone 8x8","brulure 20/s pendant 5s","aveuglement 2s (ecran blanc)"],"material":"Noyau de nova (1, drop boss final) + Lingot astral (20) + Alliage draconique (15)","desc":"Une explosion par combat peut suffire. L'arme la plus lente et la plus puissante."},
]

MAGIC_WEAPONS = [
    # Tier 1
    {"id":"apprentice_wand","tier":1,"type":"magic","name":"Baguette d'apprenti","damage":8,"speed":1.5,"knockback":2.0,"mana_cost":5,"mana_max_bonus":10,"effects":["projectile d'energie simple"],"material":"Bois (6 planches) + Poussiere de mana (5)","desc":"Premiere baguette. Projectile basique."},
    {"id":"spark_staff","tier":1,"type":"magic","name":"Baton d'etincelles","damage":5,"speed":2.5,"knockback":1.0,"mana_cost":3,"mana_max_bonus":5,"effects":["tire 3 etincelles en cone","5% chance brulure 2/s pendant 3s"],"material":"Bois (8 planches) + Poudre a canon (3)","desc":"Tir en cone rapproche. Efficace contre les groupes proches."},
    {"id":"water_orb","tier":1,"type":"magic","name":"Orbe d'eau","damage":6,"speed":1.8,"knockback":3.0,"mana_cost":4,"mana_max_bonus":15,"effects":["repousse les ennemis","eteint les flammes"],"material":"Perle d'eau (3) + Mana concentre (8)","desc":"Controle defensif. Repousse les ennemis au contact."},
    # Tier 2
    {"id":"ice_shard","tier":2,"type":"magic","name":"Eclat de givre","damage":12,"speed":1.4,"knockback":2.5,"mana_cost":7,"mana_max_bonus":20,"effects":["ralentissement 40% pendant 3s","10% chance gel 1s"],"material":"Fer (6 barres) + Noyau de glace (6) + Poussiere de mana (10)","desc":"Controle par le froid. Le gel est un mini-stun."},
    {"id":"fireball_tome","tier":2,"type":"magic","name":"Tome de boule de feu","damage":18,"speed":0.8,"knockback":5.0,"mana_cost":12,"mana_max_bonus":25,"effects":["explosion zone 3x3","brulure 6/s pendant 4s"],"material":"Fer (4 barres) + Essence de feu (8) + Livre (1)","desc":"Grosse explosion lente. Zone et degats eleves."},
    {"id":"thunder_staff","tier":2,"type":"magic","name":"Baton de foudre","damage":14,"speed":1.2,"knockback":3.0,"mana_cost":9,"mana_max_bonus":20,"effects":["eclair hit-scan (pas de projectile)","chaine 1 ennemi secondaire (50% degats)"],"material":"Fer (8 barres) + Noyau d'orage (3)","desc":"Hit-scan electrique. Touche instantanement."},
    # Tier 3
    {"id":"shadow_bolt","tier":3,"type":"magic","name":"Trait d'ombre","damage":20,"speed":1.3,"knockback":4.0,"mana_cost":10,"mana_max_bonus":30,"effects":["traverse les ennemis","vol de mana (rend 3 mana par ennemi touche)"],"material":"Or spectral (8 barres) + Poussiere d'ame (10)","desc":"Soutient la reserve de mana. Efficace en ligne."},
    {"id":"crystal_storm","tier":3,"type":"magic","name":"Tempete de cristaux","damage":9,"speed":3.0,"knockback":2.0,"mana_cost":4,"mana_max_bonus":35,"effects":["rafale automatique (maintien clic)","tire 2 projectiles a la fois","ricochet sur murs (1 rebond)"],"material":"Cristal pur (12 barres) + Mana concentre (15)","desc":"DPS continu en rafale. Les ricochets nettoient les couloirs."},
    {"id":"gravity_well","tier":3,"type":"magic","name":"Puits gravitationnel","damage":5,"speed":0.5,"knockback":"attire","mana_cost":25,"mana_max_bonus":30,"effects":["orbe persistante (dure 6s)","aspire ennemis vers le centre","degats 5/s dans zone 4x4"],"material":"Acier (10 barres) + Orbe de gravite (1) + Poussiere de mana (20)","desc":"Zone de controle persistante. Un seul puits actif a la fois."},
    {"id":"arcane_beam","tier":3,"type":"magic","name":"Rayon arcanique","damage":16,"speed":"continu","knockback":3.0,"mana_cost":8,"mana_max_bonus":30,"effects":["laser continu (maintien clic)","portee 12 tuiles","traverse tout"],"material":"Cristal pur (10 barres) + Prisme arcanique (1)","desc":"Rayon continu traversant. Cout en mana par seconde."},
    # Tier 4
    {"id":"dragon_breath","tier":4,"type":"magic","name":"Souffle de dragon","damage":24,"speed":1.0,"knockback":5.0,"mana_cost":14,"mana_max_bonus":40,"effects":["cone de feu (zone 7x3)","brulure 12/s pendant 4s","traverse l'armure de 30%"],"material":"Ecaille de dragon (12) + Noyau draconique (1)","desc":"Souffle en cone large. Penetre partiellement l'armure."},
    {"id":"void_siphon","tier":4,"type":"magic","name":"Siphon du neant","damage":18,"speed":1.5,"knockback":2.0,"mana_cost":11,"mana_max_bonus":45,"effects":["vol de vie 8% des degats","si ennemi tue -> explosion neant zone 3x3 (30 degats)"],"material":"Metal du neant (12 barres) + Fragment de neant (10)","desc":"Vol de vie et explosions en chaine. Sustain en combat."},
    {"id":"meteor_rain","tier":4,"type":"magic","name":"Pluie de meteores","damage":22,"speed":0.6,"knockback":6.0,"mana_cost":35,"mana_max_bonus":40,"effects":["invoque 8 meteores sur zone 8x8 sur 3s","chaque meteore zone 3x3"],"material":"Poussiere d'etoile (18) + Noyau de meteore (2)","desc":"Bombardement de zone. Couteux mais devastateur."},
    {"id":"chrono_stop","tier":4,"type":"magic","name":"Arret chrono","damage":0,"speed":0.3,"knockback":0,"mana_cost":50,"mana_max_bonus":40,"effects":["fige TOUS les ennemis dans zone 10x10 pendant 3s","les ennemis figes prennent +40% degats de toutes sources"],"material":"Sable du temps (40) + Noyau temporel (1)","desc":"Controle ultime. Pas de degats mais ouvre une fenetre de burst."},
    # Tier 5
    {"id":"astral_singularity","tier":5,"type":"magic","name":"Singularite astrale","damage":28,"speed":"canalisation","knockback":"attire fort","mana_cost":20,"mana_max_bonus":60,"effects":["orbe massive zone 6x6 persistante","aspire tous les ennemis","degats continus 28/s","dure 8s, CD 30s"],"material":"Lingot astral (25) + Noyau cosmique (1) + Poussiere d'etoile (30)","desc":"Trou noir magique. Controle total de zone pendant 8 secondes."},
    {"id":"omniscience","tier":5,"type":"magic","name":"Omniscience","damage":20,"speed":"auto","knockback":4.0,"mana_cost":5,"mana_max_bonus":100,"effects":["tire automatiquement sur tous les ennemis visibles","projectiles autoguides","+50% regeneration mana"],"material":"Lingot astral (30) + Oeil de Cthulhu (1) + Fragment d'etoile (20)","desc":"Tir automatique omnidirectionnel. Change le gameplay mage."},
    {"id":"creation_spell","tier":5,"type":"magic","name":"Sort de Creation","damage":35,"speed":0.5,"knockback":8.0,"mana_cost":60,"mana_max_bonus":80,"effects":["canalisation 3s -> explosion zone 15x15","degats 35 au centre, degressif","soigne allies de 30% max PV","repousse tous les ennemis hors zone"],"material":"Lingot astral (35) + Plume de Phenix (1) + Noyau de creation (1, drop boss final)","desc":"Sort ultime. Change l'issue d'un combat une fois par canalisation."},
]

SUMMON_WEAPONS = [
    # Tier 1
    {"id":"slime_staff","tier":1,"type":"summon","name":"Baton slime","damage":4,"speed":1.5,"knockback":1.0,"minion_cap_bonus":1,"minion_type":"Slime domestique","effects":["saute sur les ennemis proches"],"material":"Bois (10 planches) + Gel (15)","desc":"Premier sbire. Attaque au corps a corps."},
    {"id":"bird_totem","tier":1,"type":"summon","name":"Totem oiseau","damage":3,"speed":2.0,"knockback":0.5,"minion_cap_bonus":1,"minion_type":"Oiseau chanteur","effects":["attaque en pique","repere ennemis hors-ecran"],"material":"Bois (8 planches) + Plume (10)","desc":"Attaque aerienne rapide. Revele les ennemis proches."},
    {"id":"thorn_whip","tier":1,"type":"summon","name":"Fouet d'epines","damage":6,"speed":1.2,"knockback":2.5,"minion_cap_bonus":0,"minion_type":"n/a (fouet d'invocateur)","effects":["marque les ennemis (+15% degats des sbires sur cible marquee)"],"material":"Bois (6 planches) + Epines (12)","desc":"Fouet d'invocateur. Amplifie les degats des sbires."},
    # Tier 2
    {"id":"skeleton_staff","tier":2,"type":"summon","name":"Baton squelette","damage":7,"speed":1.3,"knockback":2.0,"minion_cap_bonus":1,"minion_type":"Archer squelette","effects":["attaque a distance","reste derriere l'invocateur"],"material":"Fer (8 barres) + Os (20)","desc":"Sbire a distance. Securise derriere vous."},
    {"id":"imp_fire_staff","tier":2,"type":"summon","name":"Baton diablotin","damage":8,"speed":1.5,"knockback":1.5,"minion_cap_bonus":1,"minion_type":"Diablotin de feu","effects":["tire des boules de feu","brulure 3/s pendant 2s"],"material":"Fer (6 barres) + Essence de feu (10)","desc":"Sbire a distance avec brulure."},
    {"id":"vine_whip","tier":2,"type":"summon","name":"Fouet de lianes","damage":10,"speed":1.0,"knockback":4.0,"minion_cap_bonus":0,"minion_type":"n/a (fouet d'invocateur)","effects":["marque ennemis (+25% degats sbires)","attire les sbires vers la cible marquee"],"material":"Fer (4 barres) + Lianes de jungle (15)","desc":"Fouet ameliore. Attire vos sbires sur la cible."},
    # Tier 3
    {"id":"spirit_staff","tier":3,"type":"summon","name":"Baton des esprits","damage":12,"speed":1.4,"knockback":2.5,"minion_cap_bonus":2,"minion_type":"Esprit vengeur","effects":["traverse les murs","ignorance 30% armure ennemie"],"material":"Or spectral (10 barres) + Poussiere d'ame (15)","desc":"+2 sbires traversants. Ignore partiellement l'armure."},
    {"id":"engineer_turret","tier":3,"type":"summon","name":"Tourelle d'ingenieur","damage":10,"speed":2.0,"knockback":1.0,"minion_cap_bonus":1,"minion_type":"Tourelle automatique","effects":["reste en place","degats constants","dure 30s, CD pose 5s"],"material":"Acier (12 barres) + Mecanisme avance (6)","desc":"Tourelle fixe. Zone de controle."},
    {"id":"leaf_crystal","tier":3,"type":"summon","name":"Cristal de feuilles","damage":0,"speed":"n/a","knockback":0,"minion_cap_bonus":0,"minion_type":"Cristal de soin","effects":["soigne l'invocateur de 3 PV/s","rayon 5 tuiles","dure 20s"],"material":"Cristal pur (8 barres) + Herbes sacrees (20)","desc":"Soin passif. Un seul actif a la fois."},
    # Tier 4
    {"id":"dragon_whelp","tier":4,"type":"summon","name":"Dragonnet","damage":18,"speed":1.2,"knockback":4.0,"minion_cap_bonus":1,"minion_type":"Dragonnet","effects":["souffle en cone (toutes les 5s)","vole (ignore terrain)"],"material":"Ecaille de dragon (15) + Noyau draconique (1)","desc":"Mini-dragon avec souffle periodique. Attaque aerienne de zone."},
    {"id":"void_tendril","tier":4,"type":"summon","name":"Tentacule du neant","damage":15,"speed":1.0,"knockback":2.0,"minion_cap_bonus":1,"minion_type":"Tentacule","effects":["attaque tous les ennemis a portee (3 tuiles)","frappe a travers les blocs"],"material":"Metal du neant (12 barres) + Fragment de neant (10)","desc":"Attaque de zone traversante. Nettoie les grottes."},
    {"id":"stardust_dragon","tier":4,"type":"summon","name":"Dragon de poussiere stellaire","damage":22,"speed":1.5,"knockback":5.0,"minion_cap_bonus":1,"minion_type":"Dragon stellaire","effects":["grandit avec chaque sbire supplementaire","degats +5 par segment"],"material":"Poussiere d'etoile (25) + Alliage stellaire (10)","desc":"Plus vous invoquez, plus il devient long et puissant. Un sbire qui s'etend."},
    # Tier 5
    {"id":"terra_prism","tier":5,"type":"summon","name":"Prismaterra","damage":25,"speed":3.0,"knockback":3.0,"minion_cap_bonus":2,"minion_type":"Prisme de lumiere","effects":["laser continu","traverse tout","detection ennemis (rayon 15 tuiles)"],"material":"Lingot astral (24) + Fragment d'etoile (15) + Prisme solaire (1)","desc":"+2 sbires laser. Detecte et attaque automatiquement."},
    {"id":"abyssal_leviathan","tier":5,"type":"summon","name":"Leviathan abyssal","damage":30,"speed":0.6,"knockback":8.0,"minion_cap_bonus":1,"minion_type":"Leviathan","effects":["enorme (occupe 3x3)","avale petits ennemis (<30% PV, 10% chance)","degats continus 8/s aux ennemis proches"],"material":"Lingot abyssal (30) + Coeur du Kraken (1)","desc":"Un seul sbire massif. Devore les petits ennemis."},
    {"id":"astral_commander_whip","tier":5,"type":"summon","name":"Fouet du Commandeur astral","damage":20,"speed":1.5,"knockback":5.0,"minion_cap_bonus":3,"minion_type":"n/a (fouet d'invocateur ultime)","effects":["marque (+50% degats sbires)","sbires gagnent +30% vitesse d'attaque sur cible marquee","chaque coup de fouet reduit CD competence de 1s"],"material":"Lingot astral (20) + Plume de Phenix (1) + Essence infinie (1)","desc":"Fouet ultime. +3 cap de sbires et boost massif."},
]

GADGET_WEAPONS = [
    {"id":"wooden_trap","tier":1,"type":"gadget","name":"Piege en bois","damage":8,"trigger":"contact","duration":10,"cooldown":5,"effects":["immobilise 2s"],"material":"Bois (8 planches) + Ficelle (2)","desc":"Immobilisation basique. Utile pour fuir."},
    {"id":"fire_mine","tier":2,"type":"gadget","name":"Mine incendiaire","damage":16,"trigger":"contact","duration":20,"cooldown":8,"effects":["explosion zone 3x3","brulure 4/s pendant 4s"],"material":"Fer (8 barres) + Poudre a canon (8)","desc":"Explosion de zone. Controle de passage."},
    {"id":"tesla_coil","tier":2,"type":"gadget","name":"Bobine Tesla","damage":12,"trigger":"proximite (3 tuiles)","duration":30,"cooldown":10,"effects":["eclair hit-scan continu","chaine 2 ennemis","ralentissement 10%"],"material":"Fer (10 barres) + Noyau d'orage (4)","desc":"Zone de deni electrique. Ralentit et blesse."},
    {"id":"poison_gas_grenade","tier":3,"type":"gadget","name":"Grenade a gaz toxique","damage":8,"trigger":"lance (explosion impact)","duration":12,"cooldown":12,"effects":["nuage 5x5 persistant","poison 6/s","traverse armure"],"material":"Acier (6 barres) + Glande de venin (8) + Poudre a canon (4)","desc":"Nuage persistant. Ignore l'armure, excellent contre ennemis lourds."},
    {"id":"decoy_drone","tier":3,"type":"gadget","name":"Drone leurre","damage":0,"trigger":"pose","duration":15,"cooldown":20,"effects":["attire l'attention des ennemis (rayon 8 tuiles)","explose a la destruction (20 degats zone 3x3)"],"material":"Acier (10 barres) + Mecanisme avance (4)","desc":"Leurre. Les ennemis l'attaquent en priorite."},
    {"id":"gravity_mine","tier":4,"type":"gadget","name":"Mine gravitationnelle","damage":20,"trigger":"contact","duration":8,"cooldown":15,"effects":["aspire ennemis dans zone 6x6","explose apres 3s (zone 6x6)","degats inversement proportionnels a la distance"],"material":"Metal du neant (12 barres) + Orbe de gravite (2)","desc":"Aspire puis explose. Combo devastateur."},
    {"id":"time_bomb","tier":4,"type":"gadget","name":"Bombe temporelle","damage":40,"trigger":"minuterie 5s","duration":5,"cooldown":30,"effects":["zone 8x8","ralentit tous les ennemis dans zone de 50% pendant compte a rebours"],"material":"Sable du temps (30) + Mecanisme avance (6) + Poudre a canon (15)","desc":"Grosse explosion a retardement. Le ralentissement piege les ennemis."},
    {"id":"black_hole_trap","tier":5,"type":"gadget","name":"Piege trou noir","damage":30,"trigger":"contact + activation","duration":6,"cooldown":60,"effects":["aspiration extreme zone 10x10","degats continus 30/s","aucun ennemi ne peut en sortir"],"material":"Lingot astral (22) + Noyau cosmique (1) + Fragment de neant (15)","desc":"Piege ultime. Emprisonne et dechire."},
    {"id":"nanite_swarm","tier":5,"type":"gadget","name":"Essaim de nanites","damage":15,"trigger":"lance","duration":15,"cooldown":25,"effects":["nuee 4x4","nanites poursuivent ennemis proches","degats cumulatifs (+2/s par nanite sur cible)","ignore armure 100%"],"material":"Lingot astral (25) + Mecanisme legendaire (2) + Poussiere d'etoile (20)","desc":"Nanites autoguidees. Degats croissants, perce tout."},
]

# ============================================================================
# 2. MINERAIS & ALLIAGES — Tableau complet
# ============================================================================

ORES = [
    # (id, nom, profondeur_min, profondeur_max, monde, equivalent_alternatif, barre_rendement, couleur)
    {"id":"copper_ore","name":"Minerai de cuivre","depth_min":0,"depth_max":80,"world":"normal","alt":"Cuivre du Reve (monde Reve)","bar_yield":"1 barre / minerai","color":"#d4784c","spawn_biome":"Tous biomes"},
    {"id":"tin_ore","name":"Minerai d'etain","depth_min":10,"depth_max":120,"world":"normal","alt":"Etain spectral (monde Reve)","bar_yield":"1 barre / minerai","color":"#c8c8c8","spawn_biome":"Plaines, Foret"},
    {"id":"iron_ore","name":"Minerai de fer","depth_min":20,"depth_max":200,"world":"normal","alt":"Fer hante (monde Reve)","bar_yield":"1 barre / minerai","color":"#b87858","spawn_biome":"Tous biomes"},
    {"id":"silver_ore","name":"Minerai d'argent","depth_min":40,"depth_max":180,"world":"normal","alt":"Argent lunaire (monde Reve)","bar_yield":"1 barre / 2 minerais","color":"#e0e8f0","spawn_biome":"Montagne, Caverne"},
    {"id":"gold_ore","name":"Minerai d'or","depth_min":60,"depth_max":250,"world":"normal","alt":"Or spectral (monde Reve)","bar_yield":"1 barre / 2 minerais","color":"#f0c840","spawn_biome":"Desert, Caverne profonde"},
    {"id":"cobalt_ore","name":"Minerai de cobalt","depth_min":100,"depth_max":350,"world":"normal","alt":"Cobalt onirique (monde Reve)","bar_yield":"1 barre / 2 minerais","color":"#4060c0","spawn_biome":"Caverne profonde, Volcan"},
    {"id":"mythril_ore","name":"Minerai de mythril","depth_min":150,"depth_max":400,"world":"normal","alt":"Mythril astral (monde Reve)","bar_yield":"1 barre / 3 minerais","color":"#60e0c0","spawn_biome":"Caverne profonde, Foret de cristal"},
    {"id":"titanium_ore","name":"Minerai de titane","depth_min":200,"depth_max":500,"world":"normal","alt":"Titane stellaire (monde Reve)","bar_yield":"1 barre / 3 minerais","color":"#c0c0c8","spawn_biome":"Caverne profonde, Abysses"},
    {"id":"obsidian_crystal","name":"Cristal d'obsidienne","depth_min":250,"depth_max":600,"world":"normal","alt":"Obsidienne du Reve","bar_yield":"1 barre / 4 cristaux","color":"#2c1040","spawn_biome":"Volcan, Lave"},
    {"id":"dragon_scale_ore","name":"Minerai d'ecaille draconique","depth_min":300,"depth_max":700,"world":"normal","alt":"Ecaille onirique (monde Reve)","bar_yield":"1 barre / 4 minerais","color":"#f04020","spawn_biome":"Volcan, Antre du Dragon"},
    {"id":"void_ore","name":"Minerai du neant","depth_min":350,"depth_max":800,"world":"normal","alt":"Fragment de neant (monde Reve)","bar_yield":"1 barre / 5 minerais","color":"#180818","spawn_biome":"Abysses, Corridor du Neant"},
    {"id":"stardust_ore","name":"Minerai de poussiere stellaire","depth_min":400,"depth_max":999,"world":"reve","alt":"N/A (exclusif Reve)","bar_yield":"1 barre / 5 minerais","color":"#ffe8a0","spawn_biome":"Plaine stellaire (Reve)"},
    {"id":"astral_ore","name":"Minerai astral","depth_min":600,"depth_max":999,"world":"reve","alt":"N/A (exclusif Reve)","bar_yield":"1 lingot / 6 minerais","color":"#ffe0ff","spawn_biome":"Nexus astral (Reve, zone boss final)"},
]

ALLOYS = [
    # (id, nom, ingredients, station, usage_principal)
    {"id":"bronze_bar","name":"Barre de bronze","ingredients":"3 Cuivre + 1 Etain","station":"Fournaise","usage":"Outils/armes Tier 1.5, armure bronze"},
    {"id":"steel_bar","name":"Barre d'acier","ingredients":"3 Fer + 1 Charbon","station":"Haut fourneau","usage":"Outils/armes Tier 3, mecanismes, armure acier"},
    {"id":"spectral_gold","name":"Or spectral","ingredients":"2 Or + 1 Poussiere d'ame","station":"Fournaise spectrale","usage":"Armes magiques Tier 3, armure d'evocateur"},
    {"id":"dragon_bar","name":"Barre draconique","ingredients":"3 Ecaille draconique + 1 Noyau de lave","station":"Forge draconique","usage":"Armes/armure Tier 4 draconiques"},
    {"id":"void_metal","name":"Metal du neant","ingredients":"3 Minerai du neant + 1 Fragment de neant","station":"Forge du neant","usage":"Armes/armure Tier 4 neant"},
    {"id":"stellar_alloy","name":"Alliage stellaire","ingredients":"3 Poussiere stellaire + 1 Fragment d'etoile","station":"Forge stellaire","usage":"Armes/armure Tier 4 stellaires"},
    {"id":"astral_ingot","name":"Lingot astral","ingredients":"3 Minerai astral + 1 Noyau cosmique + 1 Fragment d'etoile","station":"Forge astrale","usage":"Armes/armure Tier 5 ultimes"},
    {"id":"abyssal_ingot","name":"Lingot abyssal","ingredients":"4 Metal du neant + 1 Coeur du Kraken","station":"Autel abyssal","usage":"Armes abyssales Tier 5"},
]

ARMOR_SETS = [
    # (id, nom, tier, materiau, pieces: casque/plastron/jambieres/bottes, defense_totale, bonus_set)
    {"id":"wood_armor","tier":1,"material":"Bois","head":1,"chest":2,"legs":1,"feet":1,"total_def":5,"set_bonus":"+1 defense supplementaire","desc":"Armure de base."},
    {"id":"copper_armor","tier":1,"material":"Cuivre","head":2,"chest":3,"legs":2,"feet":1,"total_def":8,"set_bonus":"+10% vitesse de minage","desc":"Legere, bon pour miner."},
    {"id":"bronze_armor","tier":2,"material":"Bronze","head":3,"chest":4,"legs":3,"feet":2,"total_def":12,"set_bonus":"+15% degats de melee","desc":"Premier alliage. Bon rapport defense/attaque."},
    {"id":"iron_armor","tier":2,"material":"Fer","head":4,"chest":6,"legs":4,"feet":2,"total_def":16,"set_bonus":"+2 defense apres avoir ete touche (cumul 3 fois, dure 5s)","desc":"Armure standard. Set bonus defensif."},
    {"id":"silver_armor","tier":3,"material":"Argent","head":5,"chest":7,"legs":5,"feet":3,"total_def":20,"set_bonus":"+25% vitesse de deplacement","desc":"Rapide. Pour les builds mobiles."},
    {"id":"gold_armor","tier":3,"material":"Or","head":4,"chest":6,"legs":4,"feet":2,"total_def":16,"set_bonus":"+30% pieces d'or des ennemis, +20% chances butin rare","desc":"Faible defense mais bonus de butin."},
    {"id":"steel_armor","tier":3,"material":"Acier","head":7,"chest":10,"legs":7,"feet":4,"total_def":28,"set_bonus":"-15% degats subis des projectiles","desc":"Lourde et resistante. Anti-projectiles."},
    {"id":"spectral_armor","tier":3,"material":"Or spectral","head":5,"chest":8,"legs":5,"feet":3,"total_def":21,"set_bonus":"+40 mana max, +2 regeneration mana/s","desc":"Armure de mage. Boost de mana."},
    {"id":"titanium_armor","tier":4,"material":"Titane","head":9,"chest":13,"legs":9,"feet":5,"total_def":36,"set_bonus":"Immunite au recul","desc":"Resistance extreme. Plus de recul."},
    {"id":"dragon_armor","tier":4,"material":"Ecaille draconique","head":10,"chest":15,"legs":10,"feet":6,"total_def":41,"set_bonus":"+20% degats de feu, immunite brulure","desc":"Armure de feu ultime. Immunite lave."},
    {"id":"void_armor","tier":4,"material":"Metal du neant","head":8,"chest":12,"legs":8,"feet":5,"total_def":33,"set_bonus":"+15% degats ignores, +10% vol de vie","desc":"Armure de l'ombre. Vol de vie integre."},
    {"id":"stellar_armor","tier":4,"material":"Alliage stellaire","head":10,"chest":14,"legs":10,"feet":6,"total_def":40,"set_bonus":"+15% chance critique, +50% degats critiques","desc":"Armure de critique. Explose les degats."},
    {"id":"astral_armor","tier":5,"material":"Lingot astral","head":14,"chest":20,"legs":14,"feet":8,"total_def":56,"set_bonus":"+20% degats, +8% vol de vie, +50 mana, +2 regen mana, +15% vitesse","desc":"Armure ultime. Tous les bonus combines."},
    {"id":"abyssal_armor","tier":5,"material":"Lingot abyssal","head":12,"chest":18,"legs":12,"feet":7,"total_def":49,"set_bonus":"+25% degats si ennemi >50% PV, degats ignores 20% armure","desc":"Burst damage. Excellent pour debut de combat."},
]
# ============================================================================
# 3. CRAFTING — Recettes exhaustives
# ============================================================================

RECIPES = [
    # --- Stations d'artisanat ---
    {"id":"workbench","output":"Etabli","count":1,"input":{"wood":10},"station":"Inventaire","cat":"Stations"},
    {"id":"furnace","output":"Fournaise","count":1,"input":{"cobblestone":20},"station":"Etabli","cat":"Stations"},
    {"id":"anvil","output":"Enclume","count":1,"input":{"iron_bar":8},"station":"Etabli","cat":"Stations"},
    {"id":"alchemy_table","output":"Table d'alchimie","count":1,"input":{"wood":8,"glass":4,"mana_dust":5},"station":"Etabli","cat":"Stations"},
    {"id":"tinkerer_workbench","output":"Atelier du Bricoleur","count":1,"input":{"steel_bar":10,"mechanism":5,"glass":4},"station":"Enclume","cat":"Stations"},
    {"id":"enchanting_altar","output":"Autel d'enchantement","count":1,"input":{"obsidian":12,"diamond":2,"mana_dust":20},"station":"Enclume","cat":"Stations"},
    {"id":"dragon_forge","output":"Forge draconique","count":1,"input":{"dragon_scale":10,"obsidian":20,"lava_core":1},"station":"Enclume","cat":"Stations"},
    {"id":"void_forge","output":"Forge du neant","count":1,"input":{"void_metal":8,"void_fragment":12,"black_mass":5},"station":"Enclume","cat":"Stations"},
    {"id":"stellar_forge","output":"Forge stellaire","count":1,"input":{"stellar_alloy":8,"stardust":20},"station":"Enclume","cat":"Stations"},
    {"id":"astral_forge","output":"Forge astrale","count":1,"input":{"astral_ingot":5,"cosmic_core":1,"stardust":30},"station":"Enclume","cat":"Stations"},
    # --- Materiaux courants ---
    {"id":"planks","output":"Planches","count":4,"input":{"wood":1},"station":"Inventaire","cat":"Materiaux"},
    {"id":"sticks","output":"Baton","count":4,"input":{"planks":2},"station":"Inventaire","cat":"Materiaux"},
    {"id":"torch","output":"Torche","count":4,"input":{"sticks":1,"coal":1},"station":"Inventaire","cat":"Materiaux"},
    {"id":"glass_block","output":"Bloc de verre","count":1,"input":{"sand":2},"station":"Fournaise","cat":"Materiaux"},
    {"id":"stone_bricks","output":"Briques de pierre","count":4,"input":{"cobblestone":4},"station":"Etabli","cat":"Materiaux"},
    {"id":"rope","output":"Corde","count":2,"input":{"vine":3},"station":"Inventaire","cat":"Materiaux"},
    {"id":"paper","output":"Papier","count":3,"input":{"wood":1,"water_essence":1},"station":"Etabli","cat":"Materiaux"},
    {"id":"mechanism","output":"Mecanisme","count":1,"input":{"iron_bar":2,"copper_bar":1},"station":"Enclume","cat":"Materiaux"},
    {"id":"advanced_mechanism","output":"Mecanisme avance","count":1,"input":{"steel_bar":2,"mechanism":2,"mana_dust":3},"station":"Enclume","cat":"Materiaux"},
    {"id":"legendary_mechanism","output":"Mecanisme legendaire","count":1,"input":{"astral_ingot":2,"advanced_mechanism":2,"stardust":10},"station":"Forge astrale","cat":"Materiaux"},
    # --- Fonderie ---
    {"id":"smelt_copper","output":"Barre de cuivre","count":1,"input":{"copper_ore":1,"coal":1},"station":"Fournaise","cat":"Fonderie"},
    {"id":"smelt_tin","output":"Barre d'etain","count":1,"input":{"tin_ore":1,"coal":1},"station":"Fournaise","cat":"Fonderie"},
    {"id":"smelt_iron","output":"Barre de fer","count":1,"input":{"iron_ore":1,"coal":1},"station":"Fournaise","cat":"Fonderie"},
    {"id":"smelt_silver","output":"Barre d'argent","count":1,"input":{"silver_ore":2,"coal":1},"station":"Fournaise","cat":"Fonderie"},
    {"id":"smelt_gold","output":"Barre d'or","count":1,"input":{"gold_ore":2,"coal":1},"station":"Fournaise","cat":"Fonderie"},
    {"id":"smelt_cobalt","output":"Barre de cobalt","count":1,"input":{"cobalt_ore":2,"coal":2},"station":"Haut fourneau","cat":"Fonderie"},
    {"id":"smelt_mythril","output":"Barre de mythril","count":1,"input":{"mythril_ore":3,"coal":2},"station":"Haut fourneau","cat":"Fonderie"},
    {"id":"smelt_titanium","output":"Barre de titane","count":1,"input":{"titanium_ore":3,"coal":2},"station":"Haut fourneau","cat":"Fonderie"},
    {"id":"smelt_obsidian","output":"Barre d'obsidienne","count":1,"input":{"obsidian_crystal":4,"lava_core":1},"station":"Haut fourneau","cat":"Fonderie"},
    {"id":"smelt_bronze","output":"Barre de bronze","count":2,"input":{"copper_bar":3,"tin_bar":1},"station":"Fournaise","cat":"Fonderie"},
    {"id":"smelt_steel","output":"Barre d'acier","count":1,"input":{"iron_bar":3,"coal":1},"station":"Haut fourneau","cat":"Fonderie"},
]

# ============================================================================
# 4. POTIONS — Ingredients, herbes, effets, duree
# ============================================================================

HERBS = [
    {"id":"moonleaf","name":"Feuillelune","biome":"Foret","spawn_condition":"Nuit","color":"#a0a0ff","desc":"Base des potions de mana."},
    {"id":"sunbloom","name":"Fleursoleil","biome":"Plaines","spawn_condition":"Jour","color":"#ffe040","desc":"Base des potions de soin."},
    {"id":"fire_thistle","name":"Chardon de feu","biome":"Volcan / Desert","spawn_condition":"Toujours","color":"#ff4020","desc":"Base des potions offensives."},
    {"id":"frost_moss","name":"Mousse de givre","biome":"Toundra / Caverne glacee","spawn_condition":"Toujours","color":"#c0f0ff","desc":"Base des potions defensives."},
    {"id":"shadow_cap","name":"Chapeau d'ombre","biome":"Caverne profonde / Neant","spawn_condition":"Nuit / Obscurite","color":"#401840","desc":"Base des potions d'utilite."},
    {"id":"dream_veil","name":"Voile de reve","biome":"Monde du Reve","spawn_condition":"Toujours (Reve)","color":"#ffe8ff","desc":"Base des potions avancees."},
    {"id":"astral_petal","name":"Petale astral","biome":"Nexus astral (Reve)","spawn_condition":"Toujours","color":"#ffd0ff","desc":"Base des potions ultimes."},
    {"id":"golden_root","name":"Racine doree","biome":"Jungle","spawn_condition":"Toujours (rare)","color":"#f0c040","desc":"Amplifie les effets."},
    {"id":"phoenix_feather_herb","name":"Herbe Phenix","biome":"Volcan (pres lave)","spawn_condition":"Toujours (tres rare)","color":"#ff6000","desc":"Base des potions de resurrection."},
]

POTIONS = [
    {"id":"health_potion","name":"Potion de vie","ingredients":{"sunbloom":2,"water_essence":1},"effect":"Soigne 50 PV instantanement","duration":"Instantane","cooldown":60,"cat":"Soin"},
    {"id":"greater_health","name":"Potion de vie superieure","ingredients":{"sunbloom":4,"golden_root":1,"water_essence":2},"effect":"Soigne 150 PV","duration":"Instantane","cooldown":45,"cat":"Soin"},
    {"id":"mana_potion","name":"Potion de mana","ingredients":{"moonleaf":2,"water_essence":1},"effect":"Restaure 50 mana","duration":"Instantane","cooldown":45,"cat":"Ressource"},
    {"id":"greater_mana","name":"Potion de mana superieure","ingredients":{"moonleaf":4,"golden_root":1,"water_essence":2},"effect":"Restaure 150 mana","duration":"Instantane","cooldown":30,"cat":"Ressource"},
    {"id":"ironskin_potion","name":"Potion peau de fer","ingredients":{"frost_moss":2,"iron_ore":1,"water_essence":1},"effect":"+8 defense","duration":"5 minutes","cooldown":0,"cat":"Defense"},
    {"id":"swiftness_potion","name":"Potion de rapidite","ingredients":{"sunbloom":1,"fire_thistle":1,"water_essence":1},"effect":"+25% vitesse deplacement","duration":"4 minutes","cooldown":0,"cat":"Mobilite"},
    {"id":"strength_potion","name":"Potion de force","ingredients":{"fire_thistle":3,"golden_root":1,"water_essence":1},"effect":"+20% degats toutes armes","duration":"4 minutes","cooldown":0,"cat":"Offensif"},
    {"id":"invisibility_potion","name":"Potion d'invisibilite","ingredients":{"shadow_cap":3,"moonleaf":1,"water_essence":1},"effect":"Invisibilite (reduit portee detection a 2 tuiles)","duration":"2 minutes","cooldown":0,"cat":"Utilite"},
    {"id":"night_vision","name":"Potion vision nocturne","ingredients":{"moonleaf":2,"shadow_cap":1,"water_essence":1},"effect":"Vision parfaite dans l'obscurite","duration":"8 minutes","cooldown":0,"cat":"Utilite"},
    {"id":"water_breathing","name":"Potion respiration aquatique","ingredients":{"water_essence":3,"frost_moss":1},"effect":"Respiration sous l'eau","duration":"6 minutes","cooldown":0,"cat":"Utilite"},
    {"id":"fire_resistance","name":"Potion resistance au feu","ingredients":{"frost_moss":3,"water_essence":2,"obsidian_crystal":1},"effect":"Immunite feu/lave","duration":"4 minutes","cooldown":0,"cat":"Defense"},
    {"id":"thorns_potion","name":"Potion d'epines","ingredients":{"fire_thistle":2,"cactus":2,"water_essence":1},"effect":"Renvoie 25% des degats subis","duration":"3 minutes","cooldown":0,"cat":"Defense"},
    {"id":"mana_regen","name":"Potion regeneration de mana","ingredients":{"moonleaf":3,"dream_veil":1,"water_essence":2},"effect":"+5 mana/s","duration":"5 minutes","cooldown":0,"cat":"Ressource"},
    {"id":"lifeforce","name":"Potion force vitale","ingredients":{"sunbloom":3,"golden_root":2,"dream_veil":1},"effect":"+25% PV max","duration":"6 minutes","cooldown":0,"cat":"Defense"},
    {"id":"rage_potion","name":"Potion de rage","ingredients":{"fire_thistle":4,"shadow_cap":2,"dream_veil":1},"effect":"+30% degats, -10% defense","duration":"3 minutes","cooldown":0,"cat":"Offensif"},
    {"id":"luck_potion","name":"Potion de chance","ingredients":{"golden_root":3,"dream_veil":2,"water_essence":2},"effect":"+15% chance butin rare, +10% critique","duration":"10 minutes","cooldown":0,"cat":"Utilite"},
    {"id":"resurrection_potion","name":"Potion de resurrection","ingredients":{"phoenix_feather_herb":2,"astral_petal":3,"golden_root":1},"effect":"Resurrection automatique (50% PV, 1 fois)","duration":"30 minutes ou declenchement","cooldown":600,"cat":"Ultime"},
    {"id":"omni_potion","name":"Potion ultime","ingredients":{"astral_petal":5,"dream_veil":5,"golden_root":3,"phoenix_feather_herb":1},"effect":"Tous les buffs ci-dessus combines a 50% efficacite","duration":"5 minutes","cooldown":0,"cat":"Ultime"},
]

# ============================================================================
# 5. MOBILIER & DECORATION
# ============================================================================

FURNITURE = [
    {"id":"wooden_chair","name":"Chaise en bois","input":{"planks":3},"station":"Etabli","cat":"Sieges"},
    {"id":"wooden_table","name":"Table en bois","input":{"planks":6},"station":"Etabli","cat":"Tables"},
    {"id":"torch_stand","name":"Porte-torche","input":{"sticks":3,"torch":1},"station":"Etabli","cat":"Eclairage"},
    {"id":"lantern","name":"Lanterne","input":{"iron_bar":2,"torch":1,"glass":1},"station":"Enclume","cat":"Eclairage"},
    {"id":"chandelier","name":"Lustre","input":{"gold_bar":3,"torch":5,"glass":2},"station":"Enclume","cat":"Eclairage"},
    {"id":"bookshelf","name":"Bibliotheque","input":{"planks":10,"paper":15},"station":"Etabli","cat":"Rangement"},
    {"id":"wooden_chest","name":"Coffre en bois","input":{"planks":8,"iron_bar":2},"station":"Etabli","cat":"Rangement"},
    {"id":"iron_chest","name":"Coffre en fer","input":{"iron_bar":6,"wood":4},"station":"Enclume","cat":"Rangement"},
    {"id":"golden_chest","name":"Coffre en or","input":{"gold_bar":4,"iron_bar":2,"wood":4},"station":"Enclume","cat":"Rangement"},
    {"id":"void_chest","name":"Coffre du neant","input":{"void_metal":4,"ender_pearl":2},"station":"Forge du neant","cat":"Rangement"},
    {"id":"bed","name":"Lit","input":{"planks":8,"wool":5},"station":"Etabli","cat":"Mobilier"},
    {"id":"paintings","name":"Tableau","input":{"sticks":4,"paper":6},"station":"Etabli","cat":"Decoration"},
    {"id":"vase","name":"Vase decoratif","input":{"clay":5},"station":"Fournaise","cat":"Decoration"},
    {"id":"statue","name":"Statue","input":{"stone_bricks":20},"station":"Etabli","cat":"Decoration"},
    {"id":"trophy_stand","name":"Porte-trophee","input":{"iron_bar":4,"wood":6},"station":"Enclume","cat":"Decoration"},
    {"id":"alchemy_station","name":"Station d'alchimie (meuble)","input":{"obsidian":4,"glass":6,"mana_dust":10},"station":"Enclume","cat":"Stations"},
    {"id":"cooking_pot","name":"Marmite","input":{"iron_bar":4,"clay":8},"station":"Enclume","cat":"Cuisine"},
    {"id":"oven","name":"Four","input":{"iron_bar":6,"stone_bricks":10},"station":"Enclume","cat":"Cuisine"},
    {"id":"loom","name":"Metier a tisser","input":{"planks":10,"sticks":4},"station":"Etabli","cat":"Stations"},
    {"id":"terrarium","name":"Terrarium","input":{"glass":8,"sand":4},"station":"Etabli","cat":"Decoration"},
    {"id":"aquarium","name":"Aquarium","input":{"glass":12,"water_essence":5,"sand":2},"station":"Etabli","cat":"Decoration"},
    {"id":"music_box","name":"Boite a musique","input":{"iron_bar":2,"mechanism":1,"wood":4},"station":"Enclume","cat":"Audio"},
    {"id":"teleporter_pad","name":"Plateforme de teleportation","input":{"steel_bar":8,"mechanism":4,"mana_dust":15},"station":"Atelier du Bricoleur","cat":"Transport"},
    {"id":"pylon_forest","name":"Pylone de foret","input":{"wood":20,"mana_dust":10,"vine":5},"station":"Atelier du Bricoleur","cat":"Transport"},
    {"id":"pylon_desert","name":"Pylone de desert","input":{"sandstone":20,"mana_dust":10,"fire_thistle":3},"station":"Atelier du Bricoleur","cat":"Transport"},
]
# ============================================================================
# 6. ACCESSOIRES — Combinaisons au Tinkerer's Workbench
# ============================================================================

ACCESSORIES = [
    {"id":"copper_watch","name":"Montre en cuivre","input":"3 Barres cuivre + 1 Mecanisme","effect":"Affiche l'heure en jeu","combo":{"name":"Chronometre dore","combine_with":"Montre en argent","result_effect":"Affiche heure + vitesse ennemis"}},
    {"id":"silver_watch","name":"Montre en argent","input":"3 Barres argent + 1 Mecanisme","effect":"Affiche l'heure + profondeur","combo":{"name":"GPS astral","combine_with":"Boussole + Montre en or","result_effect":"Affiche heure + profondeur + position + biomes"}},
    {"id":"hermes_boots","name":"Bottes d'Hermes","input":"8 Cuir + 4 Plumes + 2 Barres argent","effect":"+20% vitesse deplacement","combo":{"name":"Bottes du voyageur","combine_with":"Bottes de glace + Ballon","result_effect":"+30% vitesse + double saut + marche sur eau/glace"}},
    {"id":"cloud_in_a_bottle","name":"Nuage en bouteille","input":"10 Nuages (iles flottantes) + 1 Bouteille","effect":"Double saut","combo":{"name":"Tornade en bouteille","combine_with":"Nuage en bouteille x3","result_effect":"Triple saut + 10% vitesse ascension"}},
    {"id":"lucky_horseshoe","name":"Fer a cheval chanceux","input":"Trouve (iles flottantes)","effect":"Immunite degats de chute","combo":{"name":"Foulard du ciel","combine_with":"Foulard rouge","result_effect":"Immunite chute + +15% degats en l'air"}},
    {"id":"feral_claws","name":"Griffes feroces","input":"Trouve (jungle)","effect":"+15% vitesse d'attaque melee","combo":{"name":"Griffes du tigre astral","combine_with":"Gants de pouvoir + Griffes feroces","result_effect":"+20% vitesse attaque + +10% degats melee + recul augmente"}},
    {"id":"magic_quiver","name":"Carquois magique","input":"8 Fleches en os + 5 Barres de mana","effect":"+15% degats a distance, 20% de ne pas consommer munition","combo":{"name":"Carquois infini","combine_with":"Carquois magique + Pierre philosophale","result_effect":"+25% degats distance, 40% munition gratuite, fleches traversantes"}},
    {"id":"mana_flower","name":"Fleur de mana","input":"8 Mana concentre + Herbe de mana","effect":"Utilise automatiquement potion mana quand vide","combo":{"name":"Couronne de mana astrale","combine_with":"Fleur de mana + Embleme celestial","result_effect":"Auto-potion mana, -20% cout mana, +5 regen mana/s"}},
    {"id":"warrior_emblem","name":"Embleme du guerrier","input":"Drop: Mur de Chair (25%)","effect":"+15% degats melee","combo":{"name":"Sceau du roi","combine_with":"Embleme guerrier + Oeil du Golem","result_effect":"+20% degats melee, critique +10%, +15% vitesse"}},
    {"id":"sorcerer_emblem","name":"Embleme du sorcier","input":"Drop: Mur de Chair (25%)","effect":"+15% degats magiques","combo":{"name":"Sceau arcanique","combine_with":"Embleme sorcier + Noyau de mana","result_effect":"+20% degats magiques, -10% cout mana, +50 mana max"}},
    {"id":"summoner_emblem","name":"Embleme de l'invocateur","input":"Drop: Mur de Chair (25%)","effect":"+15% degats invocation, +1 sbire max","combo":{"name":"Sceau du bestiaire","combine_with":"Embleme invocateur + Corne de licorne","result_effect":"+20% degats sbires, +2 sbires max, sbires +10% vitesse"}},
    {"id":"ranger_emblem","name":"Embleme du ranger","input":"Drop: Mur de Chair (25%)","effect":"+15% degats a distance","combo":{"name":"Sceau du chasseur","combine_with":"Embleme ranger + Oeil de faucon","result_effect":"+20% degats distance, +10% critique distance, projectiles +20% vitesse"}},
    {"id":"gadgeteer_emblem","name":"Embleme du technicien","input":"Drop: Boss Mecha (25%)","effect":"+15% degats gadgets, -2s CD gadgets","combo":{"name":"Sceau d'ingenieur","combine_with":"Embleme technicien + Noyau Tesla","result_effect":"+20% degats gadgets, -4s CD, gadgets durent +50%"}},
    {"id":"ankh_shield","name":"Bouclier Ankh","input":"Bouclier obsidienne + Medaillon Ankh","effect":"Immunite: feu, poison, saignement, confusion, lent, silence","combo":{"name":"Aegis astrale","combine_with":"Bouclier Ankh + Bouclier de Cthulhu","result_effect":"Immunite totale + dash protecteur (2s invulnerabilite)"}},
    {"id":"celestial_shell","name":"Coquille celeste","input":"Coquille lunaire + Coquille solaire","effect":"Transformation loup-garou (nuit) / sirene (eau), bonus stats","combo":{"name":"Noyau celestial","combine_with":"Coquille celeste + Pierre philosophale","result_effect":"Transfo +15% stats, +10% vol de vie, +5 regen vie"}},
    {"id":"star_veil","name":"Voile d'etoile","input":"Voile + Etoile filante","effect":"+1.5s invulnerabilite apres degat, chute d'etoiles autour du joueur","combo":{"name":"Manteau stellaire","combine_with":"Voile d'etoile + Cape de mage","result_effect":"+2s invulnerabilite, etoiles +30% degats, +20 mana"}},
    {"id":"master_ninja_gear","name":"Equipement ninja supreme","input":"Ceinture noire + Tabi","effect":"10% chance dodge, dash ameliore","combo":{"name":"Ombre du vide","combine_with":"Equipement ninja + Eclat du neant","result_effect":"15% dodge, dash teleporte 5 tuiles, +5% critique apres dodge"}},
]

# ============================================================================
# 7. DRONE PROGRAMMABLE — Langage de script et nœuds
# ============================================================================

DRONE = {
    "overview": "Le drone programmable est un compagnon mecanique obtenu apres avoir vaincu le Boss Mecha Ingenieur. Il se programme via un langage de script visuel a base de noeuds connectes. L'interface de programmation est accessible via le Drone Workbench.",
    "hardware": {
        "base_model": "Drone Mk.I (8 slots de noeuds, batterie 100 unites)",
        "mk2": "Drone Mk.II (14 slots, batterie 200, bouclier 20 PV) — craft: 1 Mk.I + 10 Acier + 5 Mecanismes avances",
        "mk3": "Drone Mk.III (22 slots, batterie 400, bouclier 50 PV, module de combat) — craft: 1 Mk.II + 10 Titane + 5 Noyaux Tesla",
        "astral_drone": "Drone astral (32 slots, batterie 1000, bouclier 150 PV, tous modules) — craft: 1 Mk.III + 10 Lingots astraux + 1 Noyau cosmique"
    },
    "node_types": [
        {"id":"move_to","name":"Aller a","params":"x, y (relatif ou absolu)","cost":1,"desc":"Deplace le drone aux coordonnees indiquees."},
        {"id":"mine_block","name":"Miner bloc","params":"x, y","cost":5,"desc":"Mine le bloc cible (necessite module minage)."},
        {"id":"place_block","name":"Poser bloc","params":"x, y, item_id","cost":3,"desc":"Pose un bloc de l'inventaire du drone sur la case."},
        {"id":"attack_nearest","name":"Attaquer ennemi proche","params":"range (tuiles)","cost":8,"desc":"Attaque l'ennemi le plus proche dans la portee (module combat requis)."},
        {"id":"follow_player","name":"Suivre joueur","params":"distance (tuiles)","cost":0,"desc":"Suit le joueur a distance specifiee."},
        {"id":"patrol","name":"Patrouille","params":"[x1,y1, x2,y2, ...]","cost":1,"desc":"Patrouille entre les points definis."},
        {"id":"scan_area","name":"Scanner zone","params":"rayon","cost":3,"desc":"Revele minerais et ennemis dans le rayon (module scanner requis)."},
        {"id":"collect_items","name":"Ramasser objets","params":"rayon","cost":2,"desc":"Ramasser tous les objets au sol dans le rayon."},
        {"id":"store_items","name":"Stocker objets","params":"coffre_x, coffre_y","cost":5,"desc":"Depose l'inventaire dans un coffre."},
        {"id":"wait","name":"Attendre","params":"secondes","cost":0,"desc":"Pause l'execution."},
        {"id":"if_enemy","name":"Si ennemi detecte","params":"rayon","cost":0,"desc":"Branchement conditionnel. Deux sorties: oui/non."},
        {"id":"if_inventory_full","name":"Si inventaire plein","params":"-","cost":0,"desc":"Condition. Deux sorties: oui/non."},
        {"id":"if_health_low","name":"Si PV < seuil","params":"seuil_pv","cost":0,"desc":"Condition de survie. Retour au joueur si PV bas."},
        {"id":"loop","name":"Boucle","params":"nombre (0 = infini)","cost":0,"desc":"Repete la sequence N fois."},
        {"id":"return_to_player","name":"Retour au joueur","params":"-","cost":3,"desc":"Teleporte le drone au joueur (cout eleve)."},
        {"id":"self_repair","name":"Auto-reparation","params":"-","cost":15,"desc":"Repare 20 PV du drone (module reparation requis)."},
        {"id":"activate_shield","name":"Activer bouclier","params":"duree_s","cost":10,"desc":"Bouclier temporaire (+50 PV, dure max 10s)."},
        {"id":"throw_grenade","name":"Lancer grenade","params":"x, y, type","cost":8,"desc":"Lance une grenade de l'inventaire (module combat requis)."},
        {"id":"build_structure","name":"Batir structure","params":"schema_id, x, y","cost":20,"desc":"Construit une structure predefinie (module construction requis)."},
        {"id":"log_message","name":"Log message","params":"texte","cost":0,"desc":"Affiche un message dans le HUD."},
    ],
    "example_script": [
        "// Programme: Mineur automatique",
        "LOOP 0 {",
        "  SCAN_AREA 10",
        "  IF_ENEMY 8 {",
        "    RETURN_TO_PLAYER",
        "    WAIT 10",
        "  }",
        "  MOVE_TO nearest_ore.x nearest_ore.y",
        "  MINE_BLOCK nearest_ore.x nearest_ore.y",
        "  IF_INVENTORY_FULL {",
        "    RETURN_TO_PLAYER",
        "    STORE_ITEMS player_chest.x player_chest.y",
        "  }",
        "}"
    ]
}
# ============================================================================
# 8. BOSS — Comportements, phases, patterns, loot tables
# ============================================================================

BOSSES = [
    {
        "id":"awakened_golem","name":"Golem Eveille","tier":1,"hp":1200,"defense":10,"recommended_gear":"Tier 1-2 (bronze/fer)","summon":"Briser un Noyau de golem dans le Temple de pierre (foret)","arena":"Temple de pierre (arene 20x15, 4 piliers)",
        "phases":[
            {"phase":1,"trigger":"100%-60% PV","attacks":[
                {"name":"Coup de poing","type":"melee","damage":18,"hitbox":"3x3 devant","frequency":"1.5s","notes":"Recul moyen"},
                {"name":"Lancer de rocher","type":"distance","damage":22,"hitbox":"Projectile + zone 2x2 a l'impact","frequency":"4s","notes":"Peut etre detruit par les attaques"},
                {"name":"Onde de choc","type":"zone","damage":15,"hitbox":"Ligne 8 tuiles au sol","frequency":"6s","notes":"Sauter pour eviter"}
            ]},
            {"phase":2,"trigger":"60%-30% PV","attacks":[
                {"name":"Double coup","type":"melee","damage":"18+22","hitbox":"3x3 x2","frequency":"2s","notes":"Second coup a portee etendue"},
                {"name":"Pluie de rochers","type":"zone","damage":"15/rocher","hitbox":"5 rochers aleatoires zone 3x3","frequency":"8s","notes":"Indiques par des ombres au sol"},
                {"name":"Charge","type":"melee","damage":30,"hitbox":"Ligne 10 tuiles","frequency":"10s","notes":"Charge en ligne droite, detruit piliers"}
            ]},
            {"phase":3,"trigger":"<30% PV","attacks":[
                {"name":"Enrage","type":"buff","damage":0,"hitbox":"self","frequency":"Une fois","notes":"+30% degats, +20% vitesse"},
                {"name":"Rotation devastatrice","type":"zone","damage":25,"hitbox":"Cercle rayon 6 autour du boss","frequency":"5s","notes":"Tourne en cercle, reste au centre"},
                {"name":"Explosion des piliers","type":"zone","damage":35,"hitbox":"Explosion de chaque pilier restant zone 5x5","frequency":"Une fois par pilier","notes":"Detruit les piliers, force le joueur au centre"}
            ]}
        ],
        "loot":[
            {"item":"Noyau de golem","chance":1.0,"qty":"1","desc":"Materiau de craft"},
            {"item":"Minerai de fer","chance":1.0,"qty":"10-20","desc":"Minerai"},
            {"item":"Fragment de pierre","chance":0.5,"qty":"5-10","desc":"Materiau"},
            {"item":"Casque de golem","chance":0.25,"qty":"1","desc":"Armure tete def 5, +10% degats melee"},
            {"item":"Expert: Noyau eveille","chance":0.10,"qty":"1","desc":"Accessoire: +5% degats, +2 defense"}
        ],
        "tips":"Utiliser l'arene : les piliers bloquent les projectiles. Rester mobile en phase 2. En phase 3, coller le boss pour eviter la rotation."
    },
    {
        "id":"void_serpent","name":"Serpent du Neant","tier":2,"hp":2800,"defense":14,"recommended_gear":"Tier 3 (acier, or spectral)","summon":"Jeter une Perle du neant dans le Puits abyssal (corridor du Neant)","arena":"Corridor du Neant (arene 30x10, plateformes flottantes)",
        "phases":[
            {"phase":1,"trigger":"100%-70% PV","attacks":[
                {"name":"Morsure","type":"melee","damage":25,"hitbox":"Tete (3x3)","frequency":"1.2s","notes":"Tete du serpent, suit le joueur horizontalement"},
                {"name":"Orbe du neant","type":"distance","damage":20,"hitbox":"Projectile lent","frequency":"3s","notes":"Traverse les plateformes, explose au contact"},
                {"name":"Fouet de queue","type":"melee","damage":18,"hitbox":"Queue (ligne 5 tuiles)","frequency":"4s","notes":"Queue bouge independamment"}
            ]},
            {"phase":2,"trigger":"70%-40% PV","attacks":[
                {"name":"Orbes en cercle","type":"zone","damage":20,"hitbox":"8 orbes en cercle autour du boss","frequency":"7s","notes":"Orbes s'eloignent puis reviennent"},
                {"name":"Plongeon","type":"melee","damage":35,"hitbox":"Traversee de l'arene","frequency":"8s","notes":"Traverse l'arene en diagonale"},
                {"name":"Appel du neant","type":"zone","damage":10,"hitbox":"Aspiration zone 8x8","frequency":"12s","notes":"Aspire le joueur vers la tete"}
            ]},
            {"phase":3,"trigger":"<40% PV","attacks":[
                {"name":"Corps entier actif","type":"melee","damage":25,"hitbox":"Tout le corps (contact)","frequency":"Continu","notes":"Tout le corps inflige des degats"},
                {"name":"Pluie d'orbes","type":"zone","damage":20,"hitbox":"15 orbes aleatoires sur l'arene","frequency":"6s","notes":"Couverture de zone intense"},
                {"name":"Morsure enragee","type":"melee","damage":40,"hitbox":"3x3","frequency":"2s","notes":"Degats et vitesse augmentes"}
            ]}
        ],
        "loot":[
            {"item":"Fragment de neant","chance":1.0,"qty":"6-12","desc":"Materiau de craft"},
            {"item":"Ecaille du neant","chance":0.5,"qty":"3-6","desc":"Armure neant"},
            {"item":"Croc du serpent","chance":0.25,"qty":"1","desc":"Arme melee tier 3: Croc de venin"},
            {"item":"Expert: Coeur du serpent","chance":0.10,"qty":"1","desc":"Accessoire: +10% degats ignores"}
        ],
        "tips":"Restez sur les plateformes hautes. Evitez le centre en phase 3. Les ailes/grappin sont recommandes."
    },
    {
        "id":"astral_wyvern","name":"Wyverne Astrale","tier":3,"hp":4500,"defense":18,"recommended_gear":"Tier 3-4 (titane, draconique)","summon":"Offrande astrale a l'Autel celeste (iles flottantes)","arena":"Ciel astral (arene ouverte 40x30, nuages comme plateformes)",
        "phases":[
            {"phase":1,"trigger":"100%-60% PV","attacks":[
                {"name":"Pique","type":"melee","damage":30,"hitbox":"Tete + corps (ligne)","frequency":"1.5s","notes":"Fonce en ligne droite"},
                {"name":"Souffle stellaire","type":"magic","damage":22,"hitbox":"Cone 8x3","frequency":"5s","notes":"Dure 2s, suit le joueur lentement"},
                {"name":"Etoiles filantes","type":"distance","damage":18,"hitbox":"Projectile guide","frequency":"3s","notes":"3 etoiles autoguidees"}
            ]},
            {"phase":2,"trigger":"60%-30% PV","attacks":[
                {"name":"Danse celeste","type":"melee","damage":25,"hitbox":"Traversee multiple","frequency":"8s","notes":"3 passages rapides en zigzag"},
                {"name":"Pluie de meteores","type":"zone","damage":30,"hitbox":"10 meteores sur zone 15x15","frequency":"12s","notes":"Ombres au sol, delai 1s"},
                {"name":"Glyphe astral","type":"zone","damage":20,"hitbox":"Glyphe au sol 5x5","frequency":"10s","notes":"Explose apres 2s, zone bleue brillante"}
            ]},
            {"phase":3,"trigger":"<30% PV","attacks":[
                {"name":"Nova","type":"zone","damage":50,"hitbox":"Ecran entier","frequency":"20s","notes":"Charge 3s (aile brillante), s'abriter derriere nuages"},
                {"name":"Appel de wyvernes","type":"summon","damage":15,"hitbox":"2 mini-wyvernes","frequency":"Une fois","notes":"Les mini-wyvernes distraient et blessent"},
                {"name":"Supernova","type":"zone","damage":35,"hitbox":"Cercle concentrique depuis le boss","frequency":"15s","notes":"Vague d'energie qui s'etend"}
            ]}
        ],
        "loot":[
            {"item":"Ecaille de dragon celeste","chance":1.0,"qty":"10-16","desc":"Materiau tier 4"},
            {"item":"Fragment d'etoile","chance":1.0,"qty":"8-14","desc":"Materiau"},
            {"item":"Aile de wyverne","chance":0.25,"qty":"1","desc":"Accessoire: vol 3s, +15% vitesse vol"},
            {"item":"Expert: Coeur de Nova","chance":0.10,"qty":"1","desc":"Materiau pour Canon Nova"}
        ],
        "tips":"Gardez un nuage entre vous et le boss en phase 3. Detruisez les mini-wyvernes vite. La Nova est telegraphiee par les ailes brillantes."
    },
    {
        "id":"dream_weaver","name":"Tisserand du Reve","tier":4,"hp":6800,"defense":22,"recommended_gear":"Tier 4 (draconique, neant, stellaire)","summon":"Fiole de reve a l'Autel du sommeil (monde du Reve)","arena":"Nexus onirique (arene 25x25, 4 miroirs aux coins)",
        "phases":[
            {"phase":1,"trigger":"100%-70% PV","attacks":[
                {"name":"Fils du reve","type":"magic","damage":20,"hitbox":"Ligne entre 2 miroirs","frequency":"3s","notes":"Les fils persistent 5s (degats si touches)"},
                {"name":"Orbe onirique","type":"distance","damage":25,"hitbox":"Projectile lent","frequency":"2s","notes":"Traverse tout, 3 orbes a la fois"},
                {"name":"Teleportation","type":"mobilite","damage":0,"hitbox":"N/A","frequency":"8s","notes":"Se teleporte a un miroir aleatoire"}
            ]},
            {"phase":2,"trigger":"70%-40% PV","attacks":[
                {"name":"Clone onirique","type":"summon","damage":15,"hitbox":"2 clones (50% PV boss, 100% degats)","frequency":"12s","notes":"Les clones doivent etre tues"},
                {"name":"Explosion de miroir","type":"zone","damage":30,"hitbox":"Explosion 6x6 autour d'un miroir","frequency":"10s","notes":"Le miroir devient inactif 5s"},
                {"name":"Piege de reve","type":"debuff","damage":0,"hitbox":"Zone 3x3 au sol","frequency":"8s","notes":"Endort 2s si marche dessus"}
            ]},
            {"phase":3,"trigger":"<40% PV","attacks":[
                {"name":"Cauchemar","type":"debuff","damage":10,"hitbox":"Toute l'arene","frequency":"15s","notes":"Ecran noir 1s + ennemi invisible 3s (le boss est aleatoirement invisible)"},
                {"name":"Tempete de fils","type":"zone","damage":25,"hitbox":"Fils entre tous les miroirs simultanement","frequency":"8s","notes":"Seul le centre de l'arene est sur"},
                {"name":"Miroir brise","type":"zone","damage":40,"hitbox":"Eclats dans toute l'arene","frequency":"20s","notes":"Tous les miroirs eclatent, degats massifs sauf si colle au boss"}
            ]}
        ],
        "loot":[
            {"item":"Soie du reve","chance":1.0,"qty":"12-20","desc":"Materiau tier 4"},
            {"item":"Eclat de miroir","chance":0.5,"qty":"4-8","desc":"Craft accessoires"},
            {"item":"Baguette onirique","chance":0.25,"qty":"1","desc":"Arme magique tier 4"},
            {"item":"Expert: Masque du reve","chance":0.10,"qty":"1","desc":"Accessoire: +15% degats magiques, +50 mana"}
        ],
        "tips":"Detruisez les clones vite. Restez au centre en phase 2-3. Les miroirs sont vos amis en phase 1, ennemis en phase 3."
    },
    {
        "id":"astral_god","name":"Dieu Astral (Boss Final)","tier":5,"hp":12000,"defense":30,"recommended_gear":"Tier 5 (astral, abyssal)","summon":"Cle astrale (craft: 5 Lingots astraux + 1 Noyau cosmique + 1 Coeur du Reve) a l'Autel supreme (Nexus astral)","arena":"Nexus astral (arene 35x35, 8 cristaux astraux, fond etoile)",
        "phases":[
            {"phase":1,"trigger":"100%-80% PV","attacks":[
                {"name":"Lame astrale","type":"melee","damage":40,"hitbox":"Tranche large (5 tuiles)","frequency":"2s","notes":"Animation lente, telegraphiee"},
                {"name":"Rayon cosmique","type":"magic","damage":35,"hitbox":"Laser continu (largeur 2, longueur 20)","frequency":"6s","notes":"Balaye l'arene en 2s"},
                {"name":"Etoiles guides","type":"distance","damage":25,"hitbox":"6 etoiles autoguidees","frequency":"4s","notes":"Poursuivent 5s avant d'exploser"}
            ]},
            {"phase":2,"trigger":"80%-50% PV","attacks":[
                {"name":"Nebuleuse","type":"zone","damage":30,"hitbox":"Nuage persistant 8x8","frequency":"10s","notes":"Dure 8s, degats continus"},
                {"name":"Trou de ver","type":"zone","damage":20,"hitbox":"Aspiration + explosion 5x5","frequency":"8s","notes":"Aspire 2s puis explose"},
                {"name":"Metamorphose","type":"buff","damage":0,"hitbox":"self","frequency":"Une fois a 65%","notes":"+15% degats, +10% vitesse, ailes deployees"},
                {"name":"Pluie de cometes","type":"zone","damage":30,"hitbox":"20 cometes aleatoires","frequency":"12s","notes":"Ombres au sol, 0.5s delai"}
            ]},
            {"phase":3,"trigger":"50%-25% PV","attacks":[
                {"name":"Supernova","type":"zone","damage":50,"hitbox":"Toute l'arene sauf cristaux","frequency":"25s","notes":"3s charge (boss brille), s'abriter derriere cristal"},
                {"name":"Appel astral","type":"summon","damage":20,"hitbox":"4 gardiens astraux","frequency":"15s","notes":"Gardiens 200 PV, attaquent en melee"},
                {"name":"Rayon double","type":"magic","damage":40,"hitbox":"2 lasers croises","frequency":"5s","notes":"Les lasers tournent en sens inverse"}
            ]},
            {"phase":4,"trigger":"<25% PV","attacks":[
                {"name":"Jugement dernier","type":"zone","damage":80,"hitbox":"Toute l'arene","frequency":"35s","notes":"5s charge, TOUS les cristaux s'activent = zones sures 3x3 autour de chaque cristal"},
                {"name":"Distorsion","type":"debuff","damage":0,"hitbox":"Toute l'arene","frequency":"20s","notes":"Inverse les controles 5s"},
                {"name":"Desespoir cosmique","type":"all","damage":45,"hitbox":"Patterns combines: lames + lasers + cometes","frequency":"8s","notes":"Chaos total"}
            ]}
        ],
        "loot":[
            {"item":"Lingot astral","chance":1.0,"qty":"20-30","desc":"Materiau ultime"},
            {"item":"Fragment d'etoile","chance":1.0,"qty":"20-30","desc":"Materiau"},
            {"item":"Noyau de creation","chance":0.20,"qty":"1","desc":"Sort de Creation"},
            {"item":"Noyau de nova","chance":0.20,"qty":"1","desc":"Canon Nova"},
            {"item":"Volonte du roi","chance":0.20,"qty":"1","desc":"Excalibur Astrale"},
            {"item":"Expert: Relique supreme","chance":0.05,"qty":"1","desc":"Accessoire: +25% tous degats, +10% tous crit, +100 mana, +5 regen vie"}
        ],
        "tips":"La preparation est cle: potions ultimes, arene eclairee, tous les cristaux actives. Phase 4: memoriser la sequence Jugement dernier. Invoquer des sbires pour gerer les gardiens."
    }
]

BOSSES_REVE = [
    {"id":"dream_eater","name":"Devoreur de Reves","normal_world_link":"Apparait quand le joueur meurt en mode Hardcore dans le Reve — libere l'ame","hp":5000,"desc":"Boss du Reve Astral, lie au monde normal. Chaque mort dans le Reve nourrit le Devoreur. Vaincre ce boss libere toutes les ames piegees et ouvre un portail permanent Reve <-> Normal."},
    {"id":"mirror_lord","name":"Seigneur des Miroirs","normal_world_link":"Double malefique du joueur — copie l'equipement et l'apparence du joueur au moment du combat","hp":4000,"desc":"Boss exclusif au Reve. Le joueur doit vaincre son double avec ses propres armes. Les degats infliges au boss sont aussi infliges au joueur (lien de miroir)."},
    {"id":"astral_colossus","name":"Colosse Astral","normal_world_link":"Garde le Nexus astral. Ne peut etre vaincu qu'avec les 4 artefacts des 4 boss elementaires du monde normal","hp":10000,"desc":"Test ultime. Necessite d'avoir vaincu tous les boss majeurs du monde normal pour l'affronter."},
]

# ============================================================================
# 9. PNJ — Liste integrale
# ============================================================================

NPCS = [
    {
        "id":"merchant","name":"Marchand","spawn_condition":"50 pieces d'argent en inventaire + maison valide","shop":[
            {"item":"Torche","price":2,"qty":"Illimite"},
            {"item":"Fleche en bois","price":1,"qty":"Illimite"},
            {"item":"Potion de vie","price":50,"qty":"Illimite"},
            {"item":"Corde","price":5,"qty":"Illimite"},
            {"item":"Filet a insectes","price":100,"qty":"1"},
            {"item":"Pelle","price":150,"qty":"1"}
        ],
        "quests":[{"name":"Livraison express","desc":"Livrer 5 potions a 3 PNJ differents","obj":"Livrer potions","reward":"100 pieces, Reduction boutique 10%"}],
        "dialogues":["Bienvenue, aventurier !","J'ai ce qu'il vous faut.","Revenez me voir souvent."],
        "moods":["content","neutre"]
    },
    {
        "id":"nurse","name":"Infirmiere","spawn_condition":"Joueur a plus de 120 PV max + maison valide","shop":[
            {"item":"Soin complet","price":10,"qty":"Service"},
            {"item":"Bandage","price":25,"qty":"Illimite (soigne 30 PV)"},
            {"item":"Antidote","price":35,"qty":"Illimite (guerit poison)"}
        ],
        "quests":[{"name":"Herbes medicinales","desc":"Rapporter 10 Herbes soleil","obj":"10 Herbes soleil","reward":"50 Bandages gratuits"}],
        "dialogues":["Vous avez une mine affreuse !","Laissez-moi vous soigner.","Aie, ca doit faire mal."],
        "moods":["inquiete","compatissante"]
    },
    {
        "id":"blacksmith","name":"Forgeron","spawn_condition":"Enclume placee + maison valide","shop":[
            {"item":"Epee en fer","price":250,"qty":"1"},
            {"item":"Pioche en fer","price":200,"qty":"1"},
            {"item":"Hache en fer","price":200,"qty":"1"},
            {"item":"Armure fer (piece)","price":300,"qty":"1 par piece"}
        ],
        "quests":[{"name":"L'armee de l'ombre","desc":"Forger 10 armes en acier","obj":"Crafter 10 armes acier","reward":"Marteau du forgeron (arme melee tier 3)"}],
        "dialogues":["Le metal, ca me connait.","Vous avez de l'acier ?","Rien de tel qu'une bonne lame."],
        "moods":["bourru","fier"]
    },
    {
        "id":"alchemist","name":"Alchimiste","spawn_condition":"Table d'alchimie placee + 5 herbes differentes recoltees","shop":[
            {"item":"Potion de vie","price":40,"qty":"Illimite"},
            {"item":"Potion de mana","price":40,"qty":"Illimite"},
            {"item":"Herbe lune","price":15,"qty":"5/jour"},
            {"item":"Herbe soleil","price":15,"qty":"5/jour"},
            {"item":"Potion peau de fer","price":80,"qty":"Illimite"}
        ],
        "quests":[{"name":"Decouverte","desc":"Apporter une herbe de chaque biome","obj":"6 herbes differentes","reward":"Acces a potions superieures"}],
        "dialogues":["Hmm, cette concoction...","Les herbes sont fraiches aujourd'hui.","Attention au dosage !"],
        "moods":["excentrique","passionne"]
    },
    {
        "id":"tinkerer","name":"Bricoleur","spawn_condition":"Atelier du Bricoleur place + avoir combine 2 accessoires","shop":[
            {"item":"Mecanisme","price":100,"qty":"5/jour"},
            {"item":"Mecanisme avance","price":500,"qty":"2/jour"},
            {"item":"Gants de pouvoir","price":750,"qty":"1"},
            {"item":"Ceinture noire","price":500,"qty":"1"}
        ],
        "quests":[{"name":"Innovation","desc":"Combiner 5 paires d'accessoires differents","obj":"5 combos d'accessoires","reward":"Embleme du technicien gratuit"}],
        "dialogues":["Bricoler, c'est ma vie.","Ne jetez rien, tout se recycle.","Et si on assemblait ca avec ca ?"],
        "moods":["curieux","maniaque"]
    },
    {
        "id":"druid","name":"Druide","spawn_condition":"Tuer le Golem Eveille + arbre geant (chene sacre) present sur la map","shop":[
            {"item":"Gland sacre","price":200,"qty":"1/semaine en jeu"},
            {"item":"Herbes sacrees","price":50,"qty":"10/jour"},
            {"item":"Baton de soin","price":400,"qty":"1"}
        ],
        "quests":[{"name":"Restauration","desc":"Planter 20 arbres dans des biomes differents","obj":"20 arbres plantes","reward":"Cristal de feuilles (arme d'invocation)"}],
        "dialogues":["La nature vous guide.","Ecoutez le vent...","L'equilibre est precieux."],
        "moods":["sage","serein"]
    },
    {
        "id":"astronomer","name":"Astronome","spawn_condition":"Atteindre les iles flottantes + telescope place","shop":[
            {"item":"Fragment d'etoile","price":1000,"qty":"3/jour"},
            {"item":"Carte du ciel","price":500,"qty":"1 (revele iles flottantes)"},
            {"item":"Poussiere d'etoile","price":2000,"qty":"1/jour"}
        ],
        "quests":[{"name":"Cartographie celeste","desc":"Decouvrir 5 iles flottantes","obj":"5 iles flottantes","reward":"Poussiere d'etoile x10"}],
        "dialogues":["Les etoiles murmurent...","Regardez la-haut !","L'univers est vaste."],
        "moods":["reveur","mystique"]
    },
]
# ============================================================================
# 10. EVENEMENTS ALEATOIRES — Meteo, saisons, invasions
# ============================================================================

EVENTS = {
    "weather": [
        {"id":"clear","name":"Degage","duration":"2-6h jeu","effects":"Aucun","frequency":"50%"},
        {"id":"rain","name":"Pluie","duration":"1-3h jeu","effects":"Eau temporaire en surface, -20% visibilite, +10% degats electriques","frequency":"20%"},
        {"id":"storm","name":"Tempete","duration":"30min-1h jeu","effects":"Eclairs aleatoires (degats 25), vent pousse joueur, -40% visibilite","frequency":"10%"},
        {"id":"snowfall","name":"Neige","duration":"2-4h jeu","effects":"Surface enneigee (ralentit 10%), lac geles","frequency":"10% (toundra: 40%)"},
        {"id":"sandstorm","name":"Tempete de sable","duration":"1-2h jeu","effects":"-30% visibilite, degats 1/s sans casque, ralentit ennemis 15%","frequency":"10% (desert: 50%)"},
        {"id":"blood_moon","name":"Lune de sang","duration":"Nuit entiere","effects":"Monstres x3, monstres ouvrent portes, boss mineur possible","frequency":"8% toute nuit"},
        {"id":"solar_eclipse","name":"Eclipse solaire","duration":"Jour entier","effects":"Ennemis speciaux (oeil de Cthulhu, mothron), taux butin x2","frequency":"5% apres un boss vaincu"},
        {"id":"meteor_shower","name":"Pluie de meteores","duration":"30min jeu","effects":"Meteores tombent aleatoirement, nouveau minerai meteorique","frequency":"5% apres Golem Eveille"},
        {"id":"astral_convergence","name":"Convergence astrale","duration":"1h jeu","effects":"Tous les effets de butin x3, portails vers le Reve partout","frequency":"2% (fin de partie)"},
    ],
    "seasons": [
        {"id":"spring","name":"Printemps","duration":"20 jours jeu","effects":"+20% pousse plantes, +10% taux butin herbes, pluie frequente"},
        {"id":"summer","name":"Ete","duration":"20 jours jeu","effects":"+20% degats feu, -10% degats glace, tempetes frequentes"},
        {"id":"autumn","name":"Automne","duration":"20 jours jeu","effects":"+20% recolte, +15% taux butin arbres, recolte champignons"},
        {"id":"winter","name":"Hiver","duration":"20 jours jeu","effects":"Neige frequente, lac geles permanents, +10% degats glace, -10% degats feu"},
    ],
    "invasions": [
        {"id":"goblin_army","name":"Armee gobeline","trigger":"Conditions: 200+ PV, 1 ombre brisee","waves":5,"enemies_per_wave":"8-12 gobelins + 1 champion","boss":"Roi gobelin (vague 5)","duration":"Jusqu'a defaite","rewards":"Butin gobelin (mecanismes, or, equipement gobelin)"},
        {"id":"pirate_invasion","name":"Invasion pirate","trigger":"Conditions: Boss mer vaincu, 300+ PV","waves":6,"enemies_per_wave":"8-15 pirates","boss":"Capitaine pirate (vague 6)","duration":"Jusqu'a defaite","rewards":"Or, objets de pirate, canon"},
        {"id":"mecha_uprising","name":"Insurrection mecha","trigger":"Conditions: Boss Mecha vaincu","waves":8,"enemies_per_wave":"10-20 mechas","boss":"Mecha-Seigneur (vague 8)","duration":"Jusqu'a defaite","rewards":"Mecanismes legendaires, armes gadgets"},
        {"id":"astral_incursion","name":"Incursion astrale","trigger":"Conditions: Boss final vaincu","waves":12,"enemies_per_wave":"12-25 ennemis astraux","boss":"Echo du Dieu Astral (vague 12)","duration":"Jusqu'a defaite","rewards":"Butin ultime, objets cosmiques exclusifs"},
    ]
}

# ============================================================================
# 11. MULTIJOUEUR AVANCE — Architecture reseau
# ============================================================================

MULTIPLAYER = {
    "architecture": {
        "model": "Client-serveur avec autorite serveur totale",
        "transport": "WebSocket + TCP fallback",
        "tick_rate": "30 ticks/s (simulation), 60 ticks/s (rendu client avec interpolation)",
        "chunk_system": "Monde divise en chunks 32x32. Le serveur envoie les chunks dans un rayon de 4 autour du joueur. Mise a jour incrementale: seuls les chunks modifies sont renvoyes.",
        "entity_interpolation": "Les entites (joueurs, monstres) recoivent position + velocite. Le client interpole lineairement entre 2 etats. Prediction cote client pour le joueur local (corrigee par le serveur).",
        "latency_compensation": "Rollback netcode: le serveur rembobine l'etat au tick du client, applique l'action, rejoue jusqu'au tick courant. Lag jusqu'a 150ms compense.",
        "bandwidth": "~5-15 KB/s par joueur (compression delta + dictionnaire). Pic a 50 KB/s lors du chargement initial."
    },
    "permissions": {
        "owner": "Tous les droits (kick, ban, modifier monde, donner objets)",
        "admin": "Kick, donner objets, teleporter",
        "builder": "Construire, miner, poser blocs",
        "player": "Jouer normalement",
        "spectator": "Observer uniquement"
    },
    "anti_cheat": {
        "server_validation": "Toute action est validee cote serveur (deplacement, minage, combat).",
        "speed_hack_detection": "Si vitesse > 150% normale → flag + rollback.",
        "inventory_check": "Hash de l'inventaire verifie a chaque tick. Objet non valide → supprime.",
        "wall_hack": "Le serveur ne revele que les entites visibles (ligne de vue + portee).",
        "damage_validation": "Le serveur calcule les degats, pas le client. DPS anormal → kick."
    },
    "chat_system": {
        "channels": ["Global", "Equipe", "Proximite (20 tuiles)", "Commerce", "Prive (/msg)"],
        "formatting": "Markdown simplifie (gras, italique, couleur). Emojis supportes.",
        "moderation": "Filtre anti-spam (3 msg/5s). /mute <joueur> <duree>. /report <joueur> <raison>."
    },
    "trading": {
        "system": "Fenetre de troc: 8 slots offre + 8 slots demande. Double validation obligatoire.",
        "fees": "0% taxe (sauf evenements speciaux).",
        "auction_house": "Marche global accessible depuis les pylones. Frais: 5% du prix de vente."
    }
}

# ============================================================================
# 12. SAUVEGARDE & CHARGEMENT — Format de fichier
# ============================================================================

SAVE_SYSTEM = {
    "format": {
        "file_extension": ".astral (binaire compresse) / .astral.json (debug)",
        "compression": "zstd (niveau 3), puis chiffrement AES-256 optionnel",
        "structure": {
            "header": {"version": "1.0.0", "save_date": "timestamp", "play_time": "secondes", "seed": "int", "world_size": "w,h"},
            "world": {"modified_chunks": "[{chunk_x, chunk_y, block_data_base64}]", "tile_entities": "[{x,y,type,data}]"},
            "player": {"inventory": "{item_id: qty}", "equipment": "{slot: item_id}", "position": "x,y", "stats": "hp,max_hp,mana,max_mana,exp,level", "buffs": "[{buff_id, remaining_ticks}]"},
            "npcs": "[{npc_id, home_x, home_y, alive, relationship}]",
            "quests": "[{quest_id, status, progress}]",
            "bosses_defeated": "[{boss_id, kills, expert_kills}]",
            "events": {"current_day": "int", "season": "id", "active_events": "[{event_id, remaining_time}]"},
            "world_discovery": {"map_revealed": "bitmask 120x80", "pylons_activated": "[{biome, x, y}]"},
            "mods": {"active_mods": "[mod_id]", "mod_data": "{mod_id: binary_blob}"}
        }
    },
    "backward_compatibility": {
        "version_field": "Toute sauvegarde a un champ version semver. Le chargeur lit la version et applique les migrations.",
        "migrations": [
            "0.9.x → 1.0.0: Conversion inventaire (anciens IDs → nouveaux IDs), ajout champ stats",
            "1.0.x → 1.1.x: Ajout champ mods, conversion chunks au nouveau format"
        ],
        "graceful_degradation": "Si un champ est absent dans une vieille sauvegarde, la valeur par defaut est utilisee. Les objets supprimes deviennent 'Objet inconnu' (peut etre recycle)."
    }
}

# ============================================================================
# 13. INTERFACE UTILISATEUR — Chaque ecran
# ============================================================================

UI = {
    "main_menu": {
        "layout": "Fond anime (etoiles + paysage astral). Logo centre. Menu: Nouvelle partie, Charger, Multijoueur, Options, Mods, Quitter.",
        "interactions": "Clic ou fleches + Entree. Fond reactive a la souris (parallaxe).",
        "shortcuts": "N=Nouveau, C=Charger, M=Multijoueur, O=Options"
    },
    "character_creation": {
        "layout": "Selection sexe/apparence (8 coiffures, 5 yeux, 4 bouches, 3 tailles). Nom (20 char max). 4 slots de tenue de depart (customisable).",
        "interactions": "Fleches gauche/droite pour changer style. Entree pour valider.",
        "classes_depart": ["Aventurier (polyvalent, epee bois + pioche)", "Archer (arc bois + 50 fleches)", "Apprenti mage (baguette + 5 potions mana)", "Explorateur (pioche amelioree + torches + corde)"]
    },
    "hud": {
        "layout": "Barre de vie + mana (haut gauche). Hotbar 10 slots (bas). Minimap (haut droit). Buffs actifs (sous vie/mana). Objectif quete (droite). Chat (bas gauche).",
        "interactions": "Molette pour changer slot actif. 1-0 pour slots. Tab pour carte. E pour inventaire. C pour crafting. J pour quetes. B pour bestiaire. Esc pour menu.",
        "context_menu": "Clic droit sur entite ouvre menu contextuel (parler, echanger, attaquer, inspecter)."
    },
    "inventory": {
        "layout": "Grille 10x4 (40 slots). Equipement (droite): 5 slots armure + accessoire x5 + arme + bouclier + munition + drone. Stats joueur (bas droite).",
        "interactions": "Clic gauche: prendre/poser. Clic droit: split stack. Shift+clic: transfert rapide. Ctrl+clic: equiper/desequiper."
    },
    "crafting": {
        "layout": "Liste recettes (gauche, filtrable par categorie). Details recette (centre). Inventaire (droite).",
        "interactions": "Clic sur recette → craft 1. Clic droit → craft max. Barre de recherche en haut."
    },
    "map": {
        "layout": "Vue agrandie du monde. Biomes colores. Points d'interet (icones PNJ, boss, pylones). Legende (droite).",
        "interactions": "Zoom molette. Clic pour waypoint. Les zones non explorees sont en noir."
    },
    "quests": {
        "layout": "Liste des quetes (gauche). Detail quete + progression (centre). Recompenses (bas).",
        "interactions": "Clic pour selectionner/suivre quete. Bouton Abandonner."
    },
    "bestiary": {
        "layout": "Grille ennemis vaincus. Fiche detaillee (clic): PV, attaques, drops, faiblesses, resistances.",
        "interactions": "Filtre par biome/type. Barre de recherche. Completion: 120/120 ennemis."
    },
    "skills": {
        "layout": "Arbre de competences (5 branches: Combat, Survie, Craft, Magie, Social). Points gagnes par niveau.",
        "branches": {
            "Combat": ["+5% degats melee", "+10% critique", "Combo etendu (+1 coup)", "Brise-armure", "Execution (<15% PV, x1.5 degats)"],
            "Survie": ["+10 PV max", "+20% soin potions", "Immunite poison", "Resurrection automatique 1x/jour", "+30% vitesse nage"],
            "Craft": ["-10% cout craft", "+1 output craft", "Qualite superieure (10%)", "Craft rare (5% drop double)", "Recettes secretes devoilees"],
            "Magie": ["+20 mana max", "+2 regen mana/s", "-10% cout mana", "Sorts durent +30%", "Nouveau sort signature"],
            "Social": ["-10% prix PNJ", "+1 quete/jour", "PNJ offrent cadeaux", "Commerce inter-plan", "Allie PNJ combat"]
        }
    },
    "options": {
        "sections": ["Video (resolution, plein ecran, VSync, qualite, particules, ombres, lumiere)", "Audio (master, musique, effets, ambiance, voix)", "Controles (clavier/souris redefinissables, manette)", "Accessibilite", "Langue (FR, EN, ES, DE, JA, ZH, RU)"],
        "default_keybindings": {"Avancer":"Z/W","Reculer":"S","Gauche":"Q/A","Droite":"D","Sauter":"Espace","Inventaire":"E","Carte":"Tab","Quetes":"J","Bestiaire":"B","Craft":"C","Utiliser/Interagir":"Clic droit","Attaquer":"Clic gauche","Hotbar 1-0":"1-0","Menu":"Echap"}
    }
}

# ============================================================================
# 14. PERFORMANCE — Optimisations
# ============================================================================

PERFORMANCE = {
    "object_pooling": "Toutes les entites (projectiles, particules, objets au sol, monstres) sont gerees par un pool d'objets pre-alloues. Pas d'allocation memoire pendant le jeu.",
    "chunk_lod": {
        "level_0": "Chunk courant et adjacents: rendu complet (toutes les tuiles, lumiere, entites)",
        "level_1": "Chunks a 2-3 de distance: rendu simplifie (tuiles sans animation, lumiere approximee)",
        "level_2": "Chunks a 4-6 de distance: rendu basse resolution (1 tuile = 2x2 pixels, pas de lumiere dynamique)",
        "level_3": "Chunks au-dela: non rendus, simules uniquement"
    },
    "tile_batching": "Les tuiles identiques contigues sont combinees en un seul mesh (geometry batching). Recuperation de texture via atlas (1 texture pour tous les blocs). Draw calls reduits de ~90%.",
    "light_occlusion": "Lumiere calculee par propagation (flood fill) depuis les sources. Occlusion via raycasting simplifie (4 directions). Lumière statique pre-calculee pour les chunks non modifies.",
    "multithreading": {
        "main_thread": "Rendu, input, UI",
        "simulation_thread": "Physique, IA, liquides, entites",
        "generation_thread": "Generation terrain asynchrone (chunks hors ecran)",
        "network_thread": "Envoi/reception packets, compression, serialisation",
        "audio_thread": "Mixage et lecture audio"
    },
    "memory": "Monde 120x80 (~9600 tuiles) = ~1 MB non compresse. Avec 100 chunks modifies en RAM = ~30 MB. Object pool = ~15 MB. Total RAM typique = 60-100 MB.",
    "gpu_optimizations": "Frustum culling, occlusion culling (blocs opaques cachent blocs derriere), render distance adaptative selon FPS."
}

# ============================================================================
# 15. SUPPORT DES MODS — API publique
# ============================================================================

MOD_API = {
    "overview": "Chaque mod est un dossier dans /mods/. Il contient un manifest.json et des scripts Lua (ou Python compile). L'API expose des hooks pour alterer le jeu.",
    "manifest": {"name":"Nom du mod","version":"1.0.0","author":"Auteur","description":"Description","dependencies":["mod_id_optionnel"]},
    "api_hooks": [
        {"hook":"on_world_generate(world, seed)","desc":"Appele apres generation du monde. Permet d'ajouter/retirer des blocs, structures."},
        {"hook":"on_block_mined(x, y, block, player)","desc":"Appele quand un bloc est mine. Peut changer le drop, empecher le minage."},
        {"hook":"on_block_placed(x, y, block, player)","desc":"Appele quand un bloc est pose."},
        {"hook":"on_enemy_spawn(enemy_type, x, y)","desc":"Appele avant l'apparition d'un ennemi. Peut modifier stats, empecher."},
        {"hook":"on_enemy_death(enemy, killer)","desc":"Appele a la mort d'un ennemi. Peut modifier les drops."},
        {"hook":"on_item_crafted(recipe, player)","desc":"Appele apres un craft. Peut donner un item bonus."},
        {"hook":"on_player_damage(player, damage, source)","desc":"Appele quand le joueur subit des degats. Peut modifier/annuler."},
        {"hook":"on_boss_phase_change(boss, old_phase, new_phase)","desc":"Appele quand un boss change de phase."},
        {"hook":"register_custom_tile(tile_def)","desc":"Enregistre un nouveau type de tuile (nom, texture, solidite, lumiere, etc.)."},
        {"hook":"register_custom_item(item_def)","desc":"Enregistre un nouvel objet (nom, icone, stack_max, rarete, effet)."},
        {"hook":"register_custom_enemy(enemy_def)","desc":"Enregistre un nouvel ennemi (nom, PV, attacks, drops, biome)."},
        {"hook":"register_custom_biome(biome_def)","desc":"Enregistre un nouveau biome (nom, couleur, blocs, vegetation, mobilier)."},
        {"hook":"register_custom_quest(quest_def)","desc":"Enregistre une nouvelle quete (nom, description, objectif, recompense, PNJ)."},
    ],
    "example_mod": {
        "name": "Biome Champignon",
        "description": "Ajoute un biome champignon avec nouveaux blocs, ennemis, et une quete.",
        "code_example": '-- manifest.json: {"name":"mushroom_biome","version":"1.0.0"}\n\nfunction on_world_generate(world, seed)\n  -- Ajouter biome champignon\n  for x = seed % 10, world.width, 15 do\n    for y = world.surface[x] + 2, world.surface[x] + 6 do\n      world:set_block(x, y, "mycelium")\n    end\n  end\nend\n\nregister_custom_tile({name="mycelium", texture="mods/mushroom/mycelium.png", solid=true})\nregister_custom_enemy({name="champignon", hp=20, dmg=8, biome="mushroom", drops={"spore": 0.8}})\nregister_custom_quest({name="Chasseur de champignons", desc="Tuer 10 champignons", objective="kill:champignon:10", reward={"item":"casque_champi","qty":1}})'
    }
}

# ============================================================================
# 16. AUDIO — Effets sonores et musique
# ============================================================================

AUDIO = {
    "music": {
        "style": "Orchestral electronique (melange synthwave + orchestre classique). Chaque biome a un theme distinct.",
        "biome_themes": {
            "foret": "Theme 'Racines anciennes' — harpe, violons, vents legers, tempo 90 BPM, tonalite Do majeur",
            "plaines": "Theme 'Horizons' — piano, cordes douces, tempo 100 BPM, Re majeur",
            "desert": "Theme 'Sable etoile' — oud, percussions douces, nappes, tempo 80 BPM, La mineur",
            "jungle": "Theme 'Verdures' — marimba, percussions tribales, chants d'oiseaux, tempo 110 BPM, Mi mineur",
            "toundra": "Theme 'Silence blanc' — piano minimaliste, vents synthetiques, tempo 60 BPM, Fa mineur",
            "cavern": "Theme 'Profondeurs' — basses profondes, echoes, nappes sombres, tempo 70 BPM, Re mineur",
            "volcan": "Theme 'Fournaise' — percussions lourdes, cuivres, distortion, tempo 120 BPM, Do mineur",
            "neant": "Theme 'Abysses' — drones, textures granuleuses, silence ponctue, tempo libre, atonal",
            "reve": "Theme 'Onirique' — voix eth erees, carillons, nappes flottantes, tempo 75 BPM, Sol majeur",
            "nexus_astral": "Theme 'Eternite' — orchestre complet + synthe, crescendo epique, tempo 100 BPM, Mi bemol majeur",
            "boss": "Theme de boss dynamique: s'intensifie avec la phase (tempo +5 BPM, volume +10%, couche +1 par phase)"
        }
    },
    "sfx": {
        "combat": "Cliquetis metallique (epee), sifflement (fleche), explosion (boule de feu), bourdonnement (laser)",
        "environnement": "Vent (exterieur), gouttes d'eau (grotte), craquement (glace), ebullition (lave)",
        "ui": "Clic (bouton), glissement (inventaire), ding (quetes), buzz (erreur), fanfare (niveau up)",
        "player": "Pas (6 surfaces: herbe, pierre, sable, bois, eau, neige), cri de degat, souffle (nage), chute"
    },
    "ambient": "Chaque biome a une nappe ambiante: vent dans les arbres (foret), stridulations (desert), vagues (plage), craquements (caverne), murmures (neant). Volume mixable separement."
}

# ============================================================================
# 17. ACCESSIBILITE — Options inclusives
# ============================================================================

ACCESSIBILITY = {
    "colorblind_modes": ["Protanopie (rouge/vert filtre)", "Deuteranopie (vert/rouge filtre)", "Tritanopie (bleu/jaune filtre)", "Monochromatique (contraste eleve)"],
    "screen_reader": "Tous les menus et tooltips sont etiquetes ARIA. Narrateur optionnel pour le HUD (PV, mana, ennemis proches, alertes boss). Vitesse de lecture reglable.",
    "strobe_reduction": "Les explosions et flashs sont attenues (50%-100% reduction). Option 'mode sans flash' qui supprime tous les clignotements >3 Hz.",
    "subtitle_options": "Sous-titres pour tous les dialogues PNJ et boss. Taille reglable (petit/moyen/grand). Fond opaque optionnel.",
    "input_remapping": "Toutes les touches redefinissables. Support manette complete (vibration optionnelle). Mode 'un seul doigt' pour joueurs a mobilite reduite.",
    "difficulty_modifiers": "Curseurs separes: degats subis (50%-200%), degats infliges (50%-200%), vitesse ennemis (50%-150%), taux de drop (50%-300%). Modifiable a tout moment sans penalite.",
    "pause_anytime": "Le jeu peut etre mis en pause a tout moment, meme en multijoueur (sur serveur prive).",
    "high_contrast_mode": "Bordure blanche sur toutes les entites interactives. Arriere-plan des menus opaque a 90%."
}

# ============================================================================
# 18. EQUILIBRAGE GLOBAL — Courbes de progression
# ============================================================================

BALANCING = {
    "enemy_hp_curve": {
        "description": "PV = PV_base * (1 + 0.15 * tier) * (1 + 0.05 * distance_au_spawn_en_chunks)",
        "tier_1": "PV 5-30 (slime, zombie, squelette)",
        "tier_2": "PV 30-100 (slime de feu, araignee, squelette lourd)",
        "tier_3": "PV 100-300 (golem de fer, spectre, loup des ombres)",
        "tier_4": "PV 300-800 (garde draconique, tentacule du neant, archange)",
        "tier_5": "PV 800-2000+ (gardiens astraux, sentinelles cosmiques)"
    },
    "weapon_damage_curve": {
        "description": "Degats = degats_base * (1 + 0.2 * tier) * (1 + bonus_competence) * (1 + bonus_equipement)",
        "tier_1": "Degats 4-10, DPS 6-15",
        "tier_2": "Degats 8-18, DPS 12-28",
        "tier_3": "Degats 14-28, DPS 20-45",
        "tier_4": "Degats 20-34, DPS 30-60",
        "tier_5": "Degats 28-55, DPS 40-90"
    },
    "craft_cost_progression": {
        "tier_1": "Materiaux communs (bois, pierre). ~5-15 unites par craft.",
        "tier_2": "Minerai fer + charbon. ~10-20 unites par craft.",
        "tier_3": "Minerais rares (or, cobalt, mythril) + intermediare. ~15-30 unites.",
        "tier_4": "Minerais tres rares (titane, ecaille) + composants speciaux. ~20-50 unites.",
        "tier_5": "Minerais legendaires (astraux, abyssaux) + boss drops. ~30-60 unites."
    },
    "experience_curve": "Niveau N necessite 100 * N^1.5 XP. Niveau max = 100. XP des ennemis = PV * 0.1. XP des boss = HP * 0.5. XP craft = 10 par recette.",
    "gold_economy": "Ennemi commun: 1-10 pieces. Boss: 100-500 pieces. Prix PNJ equilibres pour que le joueur ait toujours un objectif d'achat. Inflation controlee par taxes (Auction House 5%).",
    "difficulty_scaling": "En multijoueur: +15% PV ennemis par joueur supplementaire. +10% degats ennemis par joueur. Loots partages equitablement (instanced ou round-robin)."
}

# ============================================================================
# API ROUTES — Chaque endpoint prefixe par /astral
# ============================================================================

@router.get("/astral/weapons")
async def get_weapons():
    """Retourne toutes les armes (melee, distance, magie, invocation, gadget)."""
    return {
        "melee": MELEE_WEAPONS,
        "ranged": RANGED_WEAPONS,
        "magic": MAGIC_WEAPONS,
        "summon": SUMMON_WEAPONS,
        "gadget": GADGET_WEAPONS
    }

@router.get("/astral/weapons/melee")
async def get_melee_weapons():
    return MELEE_WEAPONS

@router.get("/astral/weapons/ranged")
async def get_ranged_weapons():
    return RANGED_WEAPONS

@router.get("/astral/weapons/magic")
async def get_magic_weapons():
    return MAGIC_WEAPONS

@router.get("/astral/weapons/summon")
async def get_summon_weapons():
    return SUMMON_WEAPONS

@router.get("/astral/weapons/gadget")
async def get_gadget_weapons():
    return GADGET_WEAPONS

@router.get("/astral/ores")
async def get_ores():
    """Retourne tous les minerais et alliages."""
    return {"ores": ORES, "alloys": ALLOYS}

@router.get("/astral/armor")
async def get_armor():
    """Retourne tous les sets d'armure avec statistiques."""
    return ARMOR_SETS

@router.get("/astral/crafting")
async def get_crafting():
    """Retourne toutes les recettes de crafting."""
    return RECIPES

@router.get("/astral/potions")
async def get_potions():
    """Retourne les herbes et les recettes de potions."""
    return {"herbs": HERBS, "potions": POTIONS}

@router.get("/astral/furniture")
async def get_furniture():
    """Retourne le mobilier et les objets de decoration."""
    return FURNITURE

@router.get("/astral/accessories")
async def get_accessories():
    """Retourne les accessoires et leurs combinaisons."""
    return ACCESSORIES

@router.get("/astral/drone")
async def get_drone():
    """Retourne le systeme de drone programmable."""
    return DRONE

@router.get("/astral/bosses")
async def get_bosses():
    """Retourne les comportements des boss (phases, patterns, butin)."""
    return {"bosses": BOSSES, "bosses_reve": BOSSES_REVE}

@router.get("/astral/npcs")
async def get_npcs():
    """Retourne la liste integrale des PNJ."""
    return NPCS

@router.get("/astral/events")
async def get_events():
    """Retourne les evenements aleatoires, meteo, saisons, invasions."""
    return EVENTS

@router.get("/astral/multiplayer")
async def get_multiplayer():
    """Retourne l'architecture multijoueur."""
    return MULTIPLAYER

@router.get("/astral/save-system")
async def get_save_system():
    """Retourne le format de sauvegarde et la compatibilite ascendante."""
    return SAVE_SYSTEM

@router.get("/astral/ui")
async def get_ui():
    """Retourne la description detaillee de chaque ecran UI."""
    return UI

@router.get("/astral/performance")
async def get_performance():
    """Retourne les techniques d'optimisation des performances."""
    return PERFORMANCE

@router.get("/astral/mod-api")
async def get_mod_api():
    """Retourne l'API publique pour les mods."""
    return MOD_API

@router.get("/astral/audio")
async def get_audio():
    """Retourne le design audio (musique, SFX, ambiance)."""
    return AUDIO

@router.get("/astral/accessibility")
async def get_accessibility():
    """Retourne les options d'accessibilite."""
    return ACCESSIBILITY

@router.get("/astral/balancing")
async def get_balancing():
    """Retourne les courbes d'equilibrage global."""
    return BALANCING

@router.get("/astral/full")
async def get_full_design():
    """Retourne l'integralite du game design document."""
    return {
        "weapons": {
            "melee": MELEE_WEAPONS,
            "ranged": RANGED_WEAPONS,
            "magic": MAGIC_WEAPONS,
            "summon": SUMMON_WEAPONS,
            "gadget": GADGET_WEAPONS
        },
        "ores": ORES,
        "alloys": ALLOYS,
        "armor_sets": ARMOR_SETS,
        "recipes": RECIPES,
        "herbs": HERBS,
        "potions": POTIONS,
        "furniture": FURNITURE,
        "accessories": ACCESSORIES,
        "drone": DRONE,
        "bosses": BOSSES,
        "bosses_reve": BOSSES_REVE,
        "npcs": NPCS,
        "events": EVENTS,
        "multiplayer": MULTIPLAYER,
        "save_system": SAVE_SYSTEM,
        "ui": UI,
        "performance": PERFORMANCE,
        "mod_api": MOD_API,
        "audio": AUDIO,
        "accessibility": ACCESSIBILITY,
        "balancing": BALANCING,
        "meta": {
            "title": "Astral Earth — Game Design Document Complet",
            "version": "1.0.0",
            "author": "Equipe Epure",
            "date": "2026-06-18",
            "sections": 18,
            "total_weapons": len(MELEE_WEAPONS) + len(RANGED_WEAPONS) + len(MAGIC_WEAPONS) + len(SUMMON_WEAPONS) + len(GADGET_WEAPONS),
            "total_ores": len(ORES),
            "total_alloys": len(ALLOYS),
            "total_armor_sets": len(ARMOR_SETS),
            "total_recipes": len(RECIPES),
            "total_potions": len(POTIONS),
            "total_bosses": len(BOSSES) + len(BOSSES_REVE),
            "total_npcs": len(NPCS)
        }
    }

@router.get("/astral/meta")
async def get_meta():
    """Retourne les metadonnees du GDD."""
    return {
        "title": "Astral Earth — Game Design Document Complet",
        "version": "1.0.0",
        "date": "2026-06-18",
        "sections": 18,
        "summary": "Conception complete du jeu Astral Earth couvrant : systeme de combat (5 categories d'armes, 10+ armes par tier, mecaniques de combo), minerais et alliages (tableau complet avec profondeurs, equivalents Reve, barres, armures), crafting (recettes exhaustives), potions (ingredients, herbes, effets), mobilier et decoration, accessoires (combinaisons au Tinkerer's Workbench), drone programmable (langage de script avec 20+ noeuds), boss (phases, patterns, hitboxes, loots, conseils), PNJ (conditions, boutiques, quetes, dialogues), evenements aleatoires (meteo, saisons, invasions), multijoueur avance (architecture reseau, anti-triche, chat, commerce), sauvegarde (format de fichier, compatibilite ascendante), UI (chaque ecran detaille), optimisation (pooling, LOD, batching, multithreading), mod API (hooks et exemples), audio (musique, SFX, ambiance par biome), accessibilite (daltonisme, lecteur ecran), et equilibrage global (courbes PV, degats, couts, economie).",
        "total_weapons": len(MELEE_WEAPONS) + len(RANGED_WEAPONS) + len(MAGIC_WEAPONS) + len(SUMMON_WEAPONS) + len(GADGET_WEAPONS),
        "total_ores": len(ORES),
        "total_alloys": len(ALLOYS),
        "total_armor_sets": len(ARMOR_SETS),
        "total_recipes": len(RECIPES),
        "total_potions": len(POTIONS),
        "total_furniture": len(FURNITURE),
        "total_accessories": len(ACCESSORIES),
        "total_bosses": len(BOSSES) + len(BOSSES_REVE),
        "total_npcs": len(NPCS)
    }
