"""
Routeur du module « minecraft » — moteur 2D type Terraria, refonte « grandiose ».

Nouveautés majeures (tout est résolu côté serveur, le serveur fait autorité) :
  - Physique temps réel : gravité, saut, dégâts de chute, montée de marche auto,
    nage dans l'eau avec jauge d'oxygène.
  - Blocs qui tombent : le sable et la neige chutent ; l'eau et la lave s'écoulent
    (vers le bas + diagonale, volume conservé) ; lave + eau => obsidienne / pierre.
  - Lumière : torches + exposition au ciel ; les grottes sont sombres et c'est
    l'obscurité qui pilote l'apparition des monstres.
  - IA des monstres avec gravité + saut d'obstacle, recul du joueur au contact.
  - Échange incrémental : /player/sim ne renvoie que les blocs modifiés
    (block_changes), pas le monde entier — léger même à ~9 requêtes/seconde.

Le crafting et les paramètres existants (monstres on/off, taux) sont conservés.

Contraintes respectées : seulement `from fastapi import APIRouter` + `router = APIRouter()`,
aucune exécution de processus, de réseau bas niveau ou de code dynamique, aucun accès aux clés API.
"""

import random
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# ============================================================
# Constantes monde (paramètres existants conservés)
# ============================================================
WORLD_W = 120
WORLD_H = 80

# ---------- types de blocs (24 types, inchangés) ----------
AIR          = 0
STONE        = 1
DIRT         = 2
GRASS        = 3
WOOD         = 4
LEAVES       = 5
SAND         = 6
COBBLESTONE  = 7
PLANKS       = 8
GLASS        = 9
TORCH        = 10
BEDROCK      = 11
WATER        = 12
SNOW         = 13
ICE          = 14
SNOW_GRASS   = 15
JUNGLE_GRASS = 16
SANDSTONE    = 17
CACTUS       = 18
OBSIDIAN     = 19
LAVA         = 20
IRON_ORE     = 21
GOLD_ORE     = 22
DIAMOND_ORE  = 23

BLOCK_NAMES: dict[int, str] = {
    0: "Air",          1: "Pierre",       2: "Terre",
    3: "Herbe",        4: "Bois",         5: "Feuilles",
    6: "Sable",        7: "Roche",        8: "Planches",
    9: "Verre",        10: "Torche",      11: "Bedrock",
    12: "Eau",         13: "Neige",       14: "Glace",
    15: "Herbe givrée",16: "Herbe jungle",17: "Grès",
    18: "Cactus",      19: "Obsidienne",  20: "Lave",
    21: "Fer",         22: "Or",          23: "Diamant",
}

# blocs soumis à la gravité (chute verticale)
FALLING_BLOCKS = (SAND, SNOW)
# blocs liquides (écoulement)
LIQUID_BLOCKS = (WATER, LAVA)

# ---------- drops de minage ----------
MINING_DROPS: dict[int, "str | None"] = {
    STONE: "cobblestone",   DIRT: "dirt",         GRASS: "dirt",
    WOOD: "wood",           LEAVES: None,          SAND: "sand",
    COBBLESTONE: "cobblestone", PLANKS: "planks", GLASS: "glass",
    TORCH: "torch",         BEDROCK: None,         WATER: None,
    SNOW: "snow",           ICE: "ice",            SNOW_GRASS: "dirt",
    JUNGLE_GRASS: "dirt",   SANDSTONE: "sandstone",CACTUS: "cactus",
    OBSIDIAN: "obsidian",   LAVA: None,            IRON_ORE: "iron_ore",
    GOLD_ORE: "gold_ore",   DIAMOND_ORE: "diamond",
}

# quel bloc poser depuis un item
ITEM_TO_BLOCK: dict[str, int] = {
    "dirt": DIRT, "cobblestone": COBBLESTONE, "wood": WOOD,
    "planks": PLANKS, "sand": SAND, "glass": GLASS,
    "torch": TORCH, "snow": SNOW, "ice": ICE,
    "sandstone": SANDSTONE, "cactus": CACTUS, "obsidian": OBSIDIAN,
}

# niveau de pioche requis (défaut = 1)
BLOCK_MINE_LEVEL: dict[int, int] = {
    STONE: 1, COBBLESTONE: 1, IRON_ORE: 2, GOLD_ORE: 2,
    DIAMOND_ORE: 3, OBSIDIAN: 3, ICE: 1, SANDSTONE: 1,
}

# puissance des pioches / poings
PICKAXE_POWER: dict[str, int] = {
    "": 1, "wooden_pickaxe": 1, "stone_pickaxe": 2, "iron_pickaxe": 3,
}

# dégâts des armes / poings
WEAPON_DAMAGE: dict[str, int] = {
    "": 1,
    "wooden_sword": 3, "stone_sword": 5, "iron_sword": 8,
    "wooden_pickaxe": 1, "stone_pickaxe": 2, "iron_pickaxe": 2,
}

# ---------- recettes de crafting (conservées, simples) ----------
RECIPES: dict[str, dict] = {
    "planks":          {"input": {"wood": 1},               "output": "planks",       "count": 4,  "cat": "Matériaux"},
    "stick":           {"input": {"planks": 2},             "output": "stick",        "count": 4,  "cat": "Matériaux"},
    "glass":           {"input": {"sand": 2},               "output": "glass",        "count": 1,  "cat": "Matériaux"},
    "torch":           {"input": {"stick": 1, "coal": 1},  "output": "torch",        "count": 4,  "cat": "Matériaux"},
    "sandstone":       {"input": {"sand": 4},               "output": "sandstone",    "count": 1,  "cat": "Matériaux"},
    "wooden_pickaxe":  {"input": {"wood": 3, "stick": 2},  "output": "wooden_pickaxe","count": 1, "cat": "Outils"},
    "stone_pickaxe":   {"input": {"cobblestone": 3, "stick": 2}, "output": "stone_pickaxe", "count": 1, "cat": "Outils"},
    "iron_pickaxe":    {"input": {"iron_ingot": 3, "stick": 2}, "output": "iron_pickaxe",  "count": 1, "cat": "Outils"},
    "wooden_sword":    {"input": {"wood": 2, "stick": 1},  "output": "wooden_sword",  "count": 1,  "cat": "Armes"},
    "stone_sword":     {"input": {"cobblestone": 2, "stick": 1}, "output": "stone_sword", "count": 1, "cat": "Armes"},
    "iron_sword":      {"input": {"iron_ingot": 2, "stick": 1}, "output": "iron_sword",  "count": 1, "cat": "Armes"},
    "smelt_iron":      {"input": {"iron_ore": 1, "coal": 1}, "output": "iron_ingot",  "count": 1,  "cat": "Fonderie"},
    "smelt_gold":      {"input": {"gold_ore": 1, "coal": 1}, "output": "gold_ingot",  "count": 1,  "cat": "Fonderie"},
}

# ---------- monstres ----------
MONSTER_DEFS: dict[str, dict] = {
    "zombie":     {"hp": 5,  "dmg": 2},
    "skeleton":   {"hp": 6,  "dmg": 2},
    "slime":      {"hp": 3,  "dmg": 1},
    "bat":        {"hp": 2,  "dmg": 1},
    "lava_slime": {"hp": 8,  "dmg": 3},
}

MONSTER_DROPS: dict[str, "list[tuple[str, float]]"] = {
    "zombie":     [("stick", 0.50), ("iron_ore", 0.10)],
    "skeleton":   [("coal", 0.60), ("gold_ore", 0.08)],
    "slime":      [("dirt", 0.70), ("coal", 0.10)],
    "bat":        [("iron_ore", 0.15), ("gold_ore", 0.05)],
    "lava_slime": [("obsidian", 0.30), ("diamond", 0.05)],
}

MAX_MONSTERS = 30

# ---------- réglages physiques ----------
JUMP_TILES = 4        # hauteur de saut (en blocs)
SAFE_FALL = 5         # chute sans dégât (en blocs)
MAX_BREATH = 12       # oxygène sous l'eau
REACH_MINE = 3        # portée minage/pose (distance de Tchebychev)
REACH_ATTACK = 2      # portée d'attaque
HEAVY_EVERY = 4       # 1 simulation "lourde" tous les N pas de /sim


# ============================================================
# Modèles Pydantic
# ============================================================
class WorldRequest(BaseModel):
    seed: int
    width: int = WORLD_W
    height: int = WORLD_H

class CoordRequest(BaseModel):
    x: int
    y: int

class PlaceRequest(BaseModel):
    x: int
    y: int
    item: str

class CraftRequest(BaseModel):
    recipe: str

class SelectRequest(BaseModel):
    item: str

class AttackRequest(BaseModel):
    monster_id: int

class SimRequest(BaseModel):
    left: bool = False
    right: bool = False
    jump: bool = False
    down: bool = False
    peaceful: bool = False       # depuis le réglage "monstres on/off"
    spawn_rate: int = 100        # depuis le réglage "taux de monstres"


# ============================================================
# État global du jeu
# ============================================================
game: dict = {
    "world": [],
    "seed": 0,
    "surface": [],               # plus haut bloc solide par colonne
    "torches": set(),            # positions (x, y) des torches
    "player_x": WORLD_W // 2,
    "player_y": WORLD_H // 2,
    "player_inventory": {},
    "player_selected": "",
    "player_hp": 20,
    "player_max_hp": 20,
    "player_damage_cooldown": 0,
    "player_score": 0,
    "player_facing": 1,          # 1 = droite, -1 = gauche
    "player_jump_left": 0,       # blocs de montée restants
    "player_fall": 0,            # blocs chutés d'affilée
    "player_breath": MAX_BREATH,
    "monsters": [],
    "game_time": 0,              # 0=aube, 6000=midi, 12000=crépuscule, 18000=minuit
    "next_monster_id": 1,
    "sim_count": 0,
    "peaceful": False,
    "spawn_rate": 100,
}


# ============================================================
# Utilitaires
# ============================================================
def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _sign(a: int) -> int:
    return 1 if a > 0 else (-1 if a < 0 else 0)


def _passable(block: int) -> bool:
    """Le joueur peut occuper cette case (air, torche, liquides)."""
    return block in (AIR, TORCH, WATER, LAVA)


def _solid(block: int) -> bool:
    """Bloc plein sur lequel on peut se tenir."""
    return block not in (AIR, TORCH, WATER, LAVA)


def _mob_passable(block: int, mtype: str) -> bool:
    if block in (AIR, TORCH, WATER):
        return True
    if block == LAVA:
        return mtype == "lava_slime"
    return False


def _init_inventory() -> dict:
    return {
        "dirt": 0, "cobblestone": 0, "wood": 0, "sand": 0,
        "coal": 0, "stick": 0, "planks": 0, "glass": 0, "torch": 0,
        "snow": 0, "ice": 0, "sandstone": 0, "cactus": 0, "obsidian": 0,
        "iron_ore": 0, "gold_ore": 0, "diamond": 0,
        "iron_ingot": 0, "gold_ingot": 0,
        "wooden_pickaxe": 0, "stone_pickaxe": 0, "iron_pickaxe": 0,
        "wooden_sword": 0, "stone_sword": 0, "iron_sword": 0,
    }


def _is_night() -> bool:
    return game["game_time"] >= 12000


def _occupied(x: int, y: int) -> bool:
    if x == game["player_x"] and y == game["player_y"]:
        return True
    for m in game["monsters"]:
        if m["x"] == x and m["y"] == y:
            return True
    return False


def _occupied_excl(x: int, y: int, self_m: dict) -> bool:
    if x == game["player_x"] and y == game["player_y"]:
        return True
    for m in game["monsters"]:
        if m is not self_m and m["x"] == x and m["y"] == y:
            return True
    return False


def _recompute_surface_col(x: int):
    w = game["world"]
    for y in range(WORLD_H):
        if _solid(w[y][x]):
            game["surface"][x] = y
            return
    game["surface"][x] = WORLD_H


def _recompute_surface():
    game["surface"] = [WORLD_H] * WORLD_W
    for x in range(WORLD_W):
        _recompute_surface_col(x)


def _is_dark(x: int, y: int) -> bool:
    """Une case est sombre (favorable au spawn) si souterraine ou de nuit,
    et hors de portée d'une torche."""
    surf = game["surface"][x] if x < len(game["surface"]) else WORLD_H
    underground = y > surf + 2
    base = underground or _is_night()
    if not base:
        return False
    for (tx, ty) in game["torches"]:
        if abs(tx - x) + abs(ty - y) <= 4:
            return False
    return True


# ============================================================
# Génération du monde (conservée, fiable)
# ============================================================
def _generate_world(seed: int, width: int, height: int) -> list:
    rng = random.Random(seed)
    w = [[AIR for _ in range(width)] for _ in range(height)]

    for x in range(width):
        w[height - 1][x] = BEDROCK
        w[height - 2][x] = BEDROCK

    stone_bottom = height - 2
    stone_top = height - 22
    for y in range(stone_top, stone_bottom):
        for x in range(width):
            w[y][x] = STONE

    dirt_top = stone_top - 3
    for y in range(dirt_top, stone_top):
        for x in range(width):
            w[y][x] = DIRT

    # filons de minerais
    ore_veins = (width * height) // 65
    for _ in range(ore_veins):
        ox = rng.randint(2, width - 3)
        oy = rng.randint(stone_top + 2, stone_bottom - 3)
        depth_factor = (oy - stone_top) / max(1, (stone_bottom - stone_top))
        if depth_factor < 0.25:
            ore_type = IRON_ORE
        elif depth_factor < 0.55:
            ore_type = rng.choices([IRON_ORE, GOLD_ORE], [65, 35])[0]
        else:
            ore_type = rng.choices([IRON_ORE, GOLD_ORE, DIAMOND_ORE], [45, 35, 20])[0]
        vein_size = rng.randint(1, 4)
        for dx in range(-vein_size, vein_size + 1):
            for dy in range(-vein_size, vein_size + 1):
                nx, ny = ox + dx, oy + dy
                if 0 <= nx < width and 0 <= ny < height:
                    if abs(dx) + abs(dy) <= vein_size and w[ny][nx] == STONE:
                        w[ny][nx] = ore_type

    # grottes
    num_caves = (width * height) // 140
    for _ in range(num_caves):
        cx = rng.randint(3, width - 4)
        cy = rng.randint(stone_top + 2, stone_bottom - 4)
        radius = rng.randint(2, 4)
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < width and 0 <= ny < height:
                    if dx * dx + dy * dy <= radius * radius:
                        if w[ny][nx] not in (AIR, BEDROCK):
                            w[ny][nx] = AIR

    # lacs d'eau souterrains
    num_lakes = (width * height) // 300
    for _ in range(num_lakes):
        lx = rng.randint(5, width - 6)
        ly = rng.randint(stone_top + 4, stone_bottom - 8)
        lr = rng.randint(2, 4)
        for dx in range(-lr, lr + 1):
            for dy in range(-lr, lr + 1):
                nx, ny = lx + dx, ly + dy
                if 0 <= nx < width and 0 <= ny < height:
                    if dx * dx + dy * dy <= lr * lr and w[ny][nx] == AIR:
                        w[ny][nx] = WATER

    # lacs de lave (profonds)
    num_lava = (width * height) // 500
    for _ in range(num_lava):
        lx = rng.randint(5, width - 6)
        ly = rng.randint(stone_bottom - 12, stone_bottom - 4)
        lr = rng.randint(2, 3)
        for dx in range(-lr, lr + 1):
            for dy in range(-lr, lr + 1):
                nx, ny = lx + dx, ly + dy
                if 0 <= nx < width and 0 <= ny < height:
                    if dx * dx + dy * dy <= lr * lr and w[ny][nx] not in (BEDROCK, LAVA):
                        w[ny][nx] = LAVA

    # biomes de surface
    surface_base = height - 25
    biome_zones = [
        (0,                    int(width * 0.10), "ocean"),
        (int(width * 0.10),    int(width * 0.24), "tundra"),
        (int(width * 0.24),    int(width * 0.62), "plains"),
        (int(width * 0.62),    int(width * 0.81), "desert"),
        (int(width * 0.81),    width,             "jungle"),
    ]

    def _biome_at(xx: int) -> str:
        for start, end, b in biome_zones:
            if start <= xx < end:
                return b
        return "plains"

    heights: list = []
    prev_h = surface_base
    for x in range(width):
        biome = _biome_at(x)
        delta = rng.choice([-1, 0, 0, 0, 1])
        prev_h = max(surface_base - 4, min(surface_base + 3, prev_h + delta))
        if biome == "ocean":
            prev_h = surface_base + rng.randint(1, 3)
        elif biome == "tundra":
            prev_h = max(surface_base - 3, prev_h - rng.randint(0, 1))
        elif biome == "jungle":
            prev_h = min(surface_base + 3, prev_h + rng.randint(0, 1))
        heights.append(prev_h)

    for x, h in enumerate(heights):
        biome = _biome_at(x)
        if biome == "ocean":
            w[h][x] = SAND
            for y in range(h + 1, dirt_top):
                if w[y][x] == AIR:
                    w[y][x] = SAND
            for wy in range(surface_base - 1, h):
                if 0 <= wy < height:
                    w[wy][x] = WATER
        elif biome == "tundra":
            w[h][x] = SNOW_GRASS
            for y in range(h + 1, dirt_top):
                if w[y][x] == AIR:
                    w[y][x] = SNOW if y == h + 1 else DIRT
            if h - 1 >= 0 and w[h - 1][x] == AIR:
                w[h - 1][x] = SNOW
        elif biome == "plains":
            w[h][x] = GRASS
            for y in range(h + 1, dirt_top):
                if w[y][x] == AIR:
                    w[y][x] = DIRT
        elif biome == "desert":
            w[h][x] = SAND
            for y in range(h + 1, dirt_top):
                if w[y][x] == AIR:
                    w[y][x] = SAND
            if rng.random() < 0.06 and h - 2 >= 0:
                cactus_h = rng.randint(1, 3)
                for ch in range(cactus_h):
                    cy = h - 1 - ch
                    if cy >= 0 and w[cy][x] == AIR:
                        w[cy][x] = CACTUS
        elif biome == "jungle":
            w[h][x] = JUNGLE_GRASS
            for y in range(h + 1, dirt_top):
                if w[y][x] == AIR:
                    w[y][x] = DIRT
        for y in range(h + 1, dirt_top):
            if w[y][x] == AIR:
                w[y][x] = DIRT

    # arbres
    for x in range(2, width - 2):
        biome = _biome_at(x)
        h = heights[x]
        tree_chance = {"plains": 0.14, "jungle": 0.35, "tundra": 0.04}.get(biome, 0)
        if biome in ("ocean", "desert") or h - 3 < 0:
            continue
        if rng.random() >= tree_chance:
            continue
        trunk_h = rng.randint(3, 6) if biome == "jungle" else rng.randint(2, 4)
        for ty in range(1, trunk_h + 1):
            cy = h - ty
            if cy >= 0 and w[cy][x] == AIR:
                w[cy][x] = WOOD
        leaf_top = h - trunk_h
        leaf_r = 2 if biome == "jungle" else 1
        for lx in range(x - leaf_r, x + leaf_r + 1):
            for ly in range(leaf_top - leaf_r, leaf_top + leaf_r + 1):
                if 0 <= lx < width and 0 <= ly < height:
                    if w[ly][lx] == AIR:
                        w[ly][lx] = LEAVES
        if leaf_top - leaf_r - 1 >= 0 and w[leaf_top - leaf_r - 1][x] == AIR:
            w[leaf_top - leaf_r - 1][x] = LEAVES

    return w


def _find_spawn(w: list, width: int) -> tuple:
    SURFACE_BLOCKS = {GRASS, SNOW_GRASS, JUNGLE_GRASS}
    mid = width // 2
    for dx in range(width // 2):
        for sign in (1, -1):
            x = mid + sign * dx
            if 0 <= x < width:
                for y in range(1, WORLD_H - 1):
                    if w[y][x] in SURFACE_BLOCKS and w[y - 1][x] == AIR:
                        return x, y - 1
    for y in range(WORLD_H):
        for x in range(width):
            if w[y][x] in SURFACE_BLOCKS and y > 0 and w[y - 1][x] == AIR:
                return x, y - 1
    return mid, WORLD_H // 2


# ============================================================
# Physique du joueur (un pas de simulation)
# ============================================================
def _grounded(px: int, py: int) -> bool:
    if py + 1 >= WORLD_H:
        return True
    return _solid(game["world"][py + 1][px])


def _player_step(left: bool, right: bool, jump: bool, down: bool):
    w = game["world"]
    px, py = game["player_x"], game["player_y"]
    in_water = (w[py][px] == WATER)

    # --- déplacement horizontal (avec montée de marche automatique) ---
    dirx = (1 if right else 0) - (1 if left else 0)
    if dirx != 0:
        game["player_facing"] = dirx
        nx = px + dirx
        if 0 <= nx < WORLD_W:
            if _passable(w[py][nx]):
                px = nx
            elif (py - 1 >= 0 and _grounded(px, py)
                  and _passable(w[py - 1][nx]) and _passable(w[py - 1][px])):
                px, py = nx, py - 1  # franchit une marche d'1 bloc

    # --- vertical : saut puis gravité ---
    grounded = _grounded(px, py)
    if jump and (grounded or in_water):
        game["player_jump_left"] = JUMP_TILES

    ascended = False
    if game["player_jump_left"] > 0:
        ny = py - 1
        if ny >= 0 and _passable(w[ny][px]):
            py = ny
            game["player_jump_left"] -= 1
            game["player_fall"] = 0
            ascended = True
        else:
            game["player_jump_left"] = 0  # plafond

    if not ascended:
        sink = True
        if in_water and not down:
            sink = (game["sim_count"] % 2 == 0)  # on coule lentement dans l'eau
        if sink:
            ny = py + 1
            if ny < WORLD_H and _passable(w[ny][px]):
                py = ny
                if in_water or w[ny][px] == WATER:
                    game["player_fall"] = 0
                else:
                    game["player_fall"] += 1
            else:
                # atterrissage : dégâts de chute
                if (game["player_fall"] > SAFE_FALL and not in_water
                        and game["player_damage_cooldown"] <= 0):
                    game["player_hp"] -= (game["player_fall"] - SAFE_FALL)
                    game["player_damage_cooldown"] = 2
                game["player_fall"] = 0

    game["player_x"], game["player_y"] = _clamp(px, 0, WORLD_W - 1), _clamp(py, 0, WORLD_H - 1)


# ============================================================
# Simulation du monde (blocs qui tombent + liquides)
# ============================================================
def _sim_world_blocks() -> list:
    """Fait tomber le sable/neige et écouler l'eau/lave. Renvoie les changements
    [x, y, block] à appliquer côté client. Mouvement conservatif (pas de duplication)."""
    w = game["world"]
    changes: list = []
    touched_cols: set = set()

    def _set(x: int, y: int, b: int):
        w[y][x] = b
        changes.append([x, y, b])
        touched_cols.add(x)

    for y in range(WORLD_H - 2, -1, -1):
        for x in range(WORLD_W):
            b = w[y][x]

            if b in FALLING_BLOCKS:
                if w[y + 1][x] == AIR and not _occupied(x, y + 1):
                    _set(x, y + 1, b)
                    _set(x, y, AIR)

            elif b in LIQUID_BLOCKS:
                # réaction lave <-> eau
                if b == LAVA:
                    reacted = False
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < WORLD_W and 0 <= ny < WORLD_H and w[ny][nx] == WATER:
                            _set(x, y, OBSIDIAN)
                            _set(nx, ny, STONE)
                            reacted = True
                            break
                    if reacted:
                        continue

                # écoulement : bas, puis diagonale bas (ordre alterné)
                order = ((x, y + 1), (x - 1, y + 1), (x + 1, y + 1))
                if (game["sim_count"] + x) % 2 == 1:
                    order = ((x, y + 1), (x + 1, y + 1), (x - 1, y + 1))
                for tx, ty in order:
                    if (0 <= tx < WORLD_W and ty < WORLD_H
                            and w[ty][tx] == AIR and not _occupied(tx, ty)):
                        _set(tx, ty, b)
                        _set(x, y, AIR)
                        break

    for x in touched_cols:
        _recompute_surface_col(x)
    return changes


# ============================================================
# IA des monstres
# ============================================================
def _spawn_monsters():
    if game["peaceful"]:
        return
    monsters = game["monsters"]
    cap = int(MAX_MONSTERS * game["spawn_rate"] / 100)
    if len(monsters) >= cap:
        return
    w = game["world"]
    px, py = game["player_x"], game["player_y"]
    for _ in range(14):
        sx = px + random.randint(-22, 22)
        sy = py + random.randint(-16, 16)
        if not (0 <= sx < WORLD_W and 0 <= sy < WORLD_H):
            continue
        dist = abs(sx - px) + abs(sy - py)
        if dist < 9 or dist > 32:
            continue
        if not _passable(w[sy][sx]):
            continue
        if _occupied(sx, sy):
            continue

        surf = game["surface"][sx]
        deep = sy >= WORLD_H - 14
        underground = sy > surf + 2
        dark = _is_dark(sx, sy)
        grounded = sy + 1 < WORLD_H and _solid(w[sy + 1][sx])

        eligible: list = []
        if deep and grounded:
            eligible.append("lava_slime")
        if underground:
            eligible.append("bat")  # vole, pas besoin de sol
            if dark and grounded:
                eligible.append("skeleton")
        if not underground and grounded:
            eligible.append("slime")
            if _is_night() and dark:
                eligible.append("zombie")
        if not eligible:
            continue

        mtype = random.choice(eligible)
        mdef = MONSTER_DEFS[mtype]
        monsters.append({
            "id": game["next_monster_id"],
            "type": mtype,
            "x": sx, "y": sy,
            "hp": mdef["hp"], "max_hp": mdef["hp"], "dmg": mdef["dmg"],
        })
        game["next_monster_id"] += 1
        return


def _tick_monsters():
    w = game["world"]
    px, py = game["player_x"], game["player_y"]
    for m in game["monsters"]:
        mt = m["type"]
        mx, my = m["x"], m["y"]

        if mt == "bat":
            # vol libre vers le joueur, avec errance
            dx = _sign(px - mx)
            dy = _sign(py - my)
            if random.random() < 0.3:
                dx = random.choice([-1, 0, 1])
            if random.random() < 0.3:
                dy = random.choice([-1, 0, 1])
            nx, ny = mx + dx, my + dy
            if (0 <= nx < WORLD_W and 0 <= ny < WORLD_H
                    and _mob_passable(w[ny][nx], mt) and not _occupied_excl(nx, ny, m)):
                mx, my = nx, ny
            else:
                nx = mx + dx
                if (0 <= nx < WORLD_W and _mob_passable(w[my][nx], mt)
                        and not _occupied_excl(nx, my, m)):
                    mx = nx
        else:
            below = w[my + 1][mx] if my + 1 < WORLD_H else STONE
            if (my + 1 < WORLD_H and _mob_passable(below, mt)
                    and not (mx == px and my + 1 == py)):
                my += 1  # gravité
            else:
                dirx = _sign(px - mx)
                if dirx != 0:
                    nx = mx + dirx
                    if (0 <= nx < WORLD_W and _mob_passable(w[my][nx], mt)
                            and not _occupied_excl(nx, my, m)):
                        mx = nx
                    elif (my - 1 >= 0 and _mob_passable(w[my - 1][mx], mt)
                          and 0 <= nx < WORLD_W and _mob_passable(w[my - 1][nx], mt)):
                        my -= 1  # saute par-dessus un obstacle d'1 bloc

        m["x"], m["y"] = mx, my

        # contact -> dégâts + recul du joueur
        if (not game["peaceful"] and abs(mx - px) <= 1 and abs(my - py) <= 1
                and game["player_damage_cooldown"] <= 0 and game["player_hp"] > 0):
            game["player_hp"] -= m["dmg"]
            game["player_damage_cooldown"] = 3
            kx = px + _sign(px - mx)
            if 0 <= kx < WORLD_W and _passable(w[py][kx]) and not _occupied(kx, py):
                game["player_x"] = kx


def _monster_drops(mtype: str) -> dict:
    drops: dict = {}
    for item, chance in MONSTER_DROPS.get(mtype, []):
        if random.random() < chance:
            drops[item] = drops.get(item, 0) + 1
    return drops


# ============================================================
# Environnement (oxygène, lave, régénération)
# ============================================================
def _apply_environment():
    w = game["world"]
    px, py = game["player_x"], game["player_y"]
    head = w[py][px]

    # oxygène
    if head == WATER:
        game["player_breath"] -= 1
        if game["player_breath"] < 0:
            game["player_breath"] = 0
            if game["player_damage_cooldown"] <= 0:
                game["player_hp"] -= 1
                game["player_damage_cooldown"] = 2
    else:
        game["player_breath"] = min(MAX_BREATH, game["player_breath"] + 3)

    # lave
    if head == LAVA and game["player_damage_cooldown"] <= 0:
        game["player_hp"] -= 6
        game["player_damage_cooldown"] = 3

    # régénération lente de jour, au calme
    if (not _is_night() and game["player_hp"] < game["player_max_hp"]
            and game["player_hp"] > 0 and game["sim_count"] % 16 == 0):
        nearby = any(abs(m["x"] - px) + abs(m["y"] - py) <= 6 for m in game["monsters"])
        if not nearby:
            game["player_hp"] += 1


# ============================================================
# Routes
# ============================================================
@router.post("/world")
async def create_world(req: WorldRequest):
    """Génère un nouveau monde et place le joueur."""
    w = _generate_world(req.seed, req.width, req.height)
    game["world"] = w
    game["seed"] = req.seed
    _recompute_surface()
    game["torches"] = set()
    for y in range(WORLD_H):
        for x in range(WORLD_W):
            if w[y][x] == TORCH:
                game["torches"].add((x, y))

    px, py = _find_spawn(w, req.width)
    game["player_x"] = px
    game["player_y"] = py
    game["player_inventory"] = _init_inventory()
    game["player_selected"] = ""
    game["player_hp"] = 20
    game["player_max_hp"] = 20
    game["player_damage_cooldown"] = 0
    game["player_score"] = 0
    game["player_facing"] = 1
    game["player_jump_left"] = 0
    game["player_fall"] = 0
    game["player_breath"] = MAX_BREATH
    game["monsters"] = []
    game["game_time"] = 0
    game["next_monster_id"] = 1
    game["sim_count"] = 0
    game["player_inventory"]["wooden_pickaxe"] = 1
    game["player_inventory"]["wooden_sword"] = 1
    return _game_state(include_world=True)


@router.get("/world")
async def get_world():
    """Renvoie l'état complet (monde inclus) — utile pour resynchroniser."""
    return _game_state(include_world=True)


@router.post("/player/sim")
async def sim(req: SimRequest):
    """Avance d'un pas de simulation : physique du joueur + (périodiquement)
    blocs/liquides, IA, spawns, temps. Renvoie l'état léger + block_changes."""
    if not game["world"]:
        return {**_game_state(include_world=False), "success": False, "block_changes": []}

    game["peaceful"] = req.peaceful
    game["spawn_rate"] = _clamp(req.spawn_rate, 0, 100)
    game["sim_count"] += 1

    if game["player_hp"] > 0:
        _player_step(req.left, req.right, req.jump, req.down)

    changes: list = []
    if game["sim_count"] % HEAVY_EVERY == 0:
        if game["player_damage_cooldown"] > 0:
            game["player_damage_cooldown"] -= 1
        changes = _sim_world_blocks()
        _tick_monsters()
        game["monsters"] = [m for m in game["monsters"] if m["hp"] > 0]
        _spawn_monsters()
        game["game_time"] = (game["game_time"] + 90) % 24000
        if game["player_hp"] > 0:
            _apply_environment()

    return {**_game_state(include_world=False), "success": True, "block_changes": changes}


@router.post("/player/mine")
async def mine_block(req: CoordRequest):
    """Mine un bloc à portée."""
    x, y = req.x, req.y
    if not game["world"]:
        return {**_game_state(False), "success": False, "reason": "no world"}
    if not (0 <= x < WORLD_W and 0 <= y < WORLD_H):
        return {**_game_state(False), "success": False, "reason": "out of bounds"}
    dist = max(abs(x - game["player_x"]), abs(y - game["player_y"]))
    if dist > REACH_MINE:
        return {**_game_state(False), "success": False, "reason": "Trop loin"}

    block = game["world"][y][x]
    if block in (AIR, BEDROCK, WATER, LAVA):
        return {**_game_state(False), "success": False, "reason": "Inminable"}

    required_level = BLOCK_MINE_LEVEL.get(block, 1)
    pick_level = PICKAXE_POWER.get(game["player_selected"], 1)
    if pick_level < required_level:
        return {**_game_state(False), "success": False,
                "reason": f"Pioche niv. {required_level} requise (vous: {pick_level})"}

    drop = MINING_DROPS.get(block)
    extra_drop = None
    if block == LEAVES and random.random() < 0.30:
        extra_drop = "stick"
    if block == STONE and random.random() < 0.12:
        extra_drop = "coal"
    if block == CACTUS and random.random() < 0.20:
        extra_drop = "stick"

    game["world"][y][x] = AIR
    game["torches"].discard((x, y))
    _recompute_surface_col(x)

    inv = game["player_inventory"]
    if drop:
        inv[drop] = inv.get(drop, 0) + 1
    if extra_drop:
        inv[extra_drop] = inv.get(extra_drop, 0) + 1
    game["player_score"] += 10

    return {
        **_game_state(False),
        "success": True,
        "block_mined": BLOCK_NAMES.get(block, "?"),
        "drop": drop,
        "extra_drop": extra_drop,
        "block_changes": [[x, y, AIR]],
    }


@router.post("/player/place")
async def place_block(req: PlaceRequest):
    """Pose un bloc à portée sur une case vide."""
    x, y, item = req.x, req.y, req.item
    if not game["world"]:
        return {**_game_state(False), "success": False, "reason": "no world"}
    if not (0 <= x < WORLD_W and 0 <= y < WORLD_H):
        return {**_game_state(False), "success": False, "reason": "out of bounds"}
    dist = max(abs(x - game["player_x"]), abs(y - game["player_y"]))
    if dist > REACH_MINE:
        return {**_game_state(False), "success": False, "reason": "Trop loin"}
    if game["world"][y][x] != AIR:
        return {**_game_state(False), "success": False, "reason": "Case occupée"}
    if _occupied(x, y):
        return {**_game_state(False), "success": False, "reason": "Quelqu'un est là"}
    if item not in ITEM_TO_BLOCK:
        return {**_game_state(False), "success": False, "reason": f"Impossible de poser « {item} »"}

    inv = game["player_inventory"]
    if inv.get(item, 0) <= 0:
        return {**_game_state(False), "success": False, "reason": f"Aucun {item}"}

    block_type = ITEM_TO_BLOCK[item]
    game["world"][y][x] = block_type
    if block_type == TORCH:
        game["torches"].add((x, y))
    _recompute_surface_col(x)
    inv[item] -= 1
    game["player_score"] += 5
    if game["player_selected"] == item and inv.get(item, 0) <= 0:
        game["player_selected"] = ""

    return {
        **_game_state(False),
        "success": True,
        "block_placed": BLOCK_NAMES.get(block_type, "?"),
        "block_changes": [[x, y, block_type]],
    }


@router.post("/player/craft")
async def craft_item(req: CraftRequest):
    """Crafte un objet."""
    recipe_name = req.recipe
    if recipe_name not in RECIPES:
        return {**_game_state(False), "success": False, "reason": "Recette inconnue"}
    recipe = RECIPES[recipe_name]
    inv = game["player_inventory"]
    for item, needed in recipe["input"].items():
        if inv.get(item, 0) < needed:
            return {**_game_state(False), "success": False,
                    "reason": f"Il faut {needed}× {item} (vous: {inv.get(item, 0)})"}
    for item, needed in recipe["input"].items():
        inv[item] -= needed
    out_item = recipe["output"]
    out_count = recipe["count"]
    inv[out_item] = inv.get(out_item, 0) + out_count
    game["player_score"] += 25
    return {**_game_state(False), "success": True, "crafted": out_item, "count": out_count}


@router.get("/recipes")
async def get_recipes():
    return {"recipes": RECIPES}


@router.post("/player/select")
async def select_item(req: SelectRequest):
    item = req.item
    inv = game["player_inventory"]
    game["player_selected"] = item if (item and inv.get(item, 0) > 0) else ""
    return {"selected": game["player_selected"]}


@router.post("/player/attack")
async def attack_monster(req: AttackRequest):
    """Attaque un monstre à portée, avec recul."""
    monsters = game["monsters"]
    px, py = game["player_x"], game["player_y"]
    w = game["world"]
    for m in monsters:
        if m["id"] == req.monster_id:
            dist = max(abs(m["x"] - px), abs(m["y"] - py))
            if dist > REACH_ATTACK:
                return {**_game_state(False), "success": False, "reason": "Trop loin"}
            dmg = WEAPON_DAMAGE.get(game["player_selected"], 1)
            m["hp"] -= dmg
            # recul du monstre
            kdx = _sign(m["x"] - px)
            kx = m["x"] + (kdx if kdx != 0 else 1)
            if 0 <= kx < WORLD_W and _mob_passable(w[m["y"]][kx], m["type"]) and not _occupied_excl(kx, m["y"], m):
                m["x"] = kx
            killed = m["hp"] <= 0
            drops = {}
            if killed:
                drops = _monster_drops(m["type"])
                for item, n in drops.items():
                    game["player_inventory"][item] = game["player_inventory"].get(item, 0) + n
                game["player_score"] += 50
            game["monsters"] = [mm for mm in monsters if mm["hp"] > 0]
            return {**_game_state(False), "success": True, "damage": dmg,
                    "killed": killed, "drops": drops}
    return {**_game_state(False), "success": False, "reason": "Monstre introuvable"}


# ============================================================
# Sérialisation de l'état
# ============================================================
def _game_state(include_world: bool = True) -> dict:
    state = {
        "seed": game["seed"],
        "width": WORLD_W,
        "height": WORLD_H,
        "player": {
            "x": game["player_x"],
            "y": game["player_y"],
            "inventory": game["player_inventory"],
            "selected": game["player_selected"],
            "hp": game["player_hp"],
            "max_hp": game["player_max_hp"],
            "score": game["player_score"],
            "facing": game["player_facing"],
            "breath": game["player_breath"],
            "max_breath": MAX_BREATH,
            "dead": game["player_hp"] <= 0,
        },
        "monsters": game["monsters"],
        "game_time": game["game_time"],
    }
    if include_world:
        state["world"] = game["world"]
    return state
