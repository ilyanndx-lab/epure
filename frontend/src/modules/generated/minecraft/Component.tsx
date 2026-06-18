import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';

/* ============================================================
   Module Minecraft — Composant React (moteur 2D type Terraria)
   Refonte « grandiose » : physique temps réel (gravité, saut, chute,
   nage), lumière dynamique (torches + ciel), fluides & sable qui
   tombent, IA des monstres, ciel jour/nuit en parallaxe.
   Le serveur fait autorité ; le client envoie ses intentions à /player/sim
   et n'applique que les blocs modifiés (block_changes).
   ============================================================ */

// ---------- constantes ----------
const API = 'http://localhost:8000';
const BLOCK_SIZE = 22;
const VIEW_W = 26;
const VIEW_H = 17;
const WORLD_W = 120;
const WORLD_H = 80;
const SIM_MS = 110;            // cadence de la boucle physique

// ids de blocs (alignés avec le backend)
const AIR = 0, TORCH = 10, WATER = 12, LAVA = 20;
const SOLID_SET = new Set([1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23]);
const isSolid = (b: number) => SOLID_SET.has(b);

const BLOCK_DEFS: { color: string; name: string; icon?: string }[] = [
  { color: 'transparent',            name: 'Air' },              // 0
  { color: '#7f7f7f',                name: 'Pierre' },           // 1
  { color: '#8B4513',                name: 'Terre' },            // 2
  { color: '#4a8c3f',                name: 'Herbe', icon: '🌿' },// 3
  { color: '#9a6a3a',                name: 'Bois' },             // 4
  { color: '#3d7a2e',                name: 'Feuilles' },         // 5
  { color: '#e8d896',                name: 'Sable' },            // 6
  { color: '#6b6b6b',                name: 'Roche' },            // 7
  { color: '#c49a6c',                name: 'Planches' },         // 8
  { color: 'rgba(160,200,220,0.55)', name: 'Verre' },           // 9
  { color: '#ffa040',                name: 'Torche', icon: '🔥' },// 10
  { color: '#181818',                name: 'Bedrock' },          // 11
  { color: '#3a70c0',                name: 'Eau' },              // 12
  { color: '#e8f0f8',                name: 'Neige' },            // 13
  { color: '#c8e0f0',                name: 'Glace' },            // 14
  { color: '#d8e8d0',                name: 'Herbe givrée' },     // 15
  { color: '#3ca83c',                name: 'Herbe jungle' },     // 16
  { color: '#d4b878',                name: 'Grès' },             // 17
  { color: '#3a7030',                name: 'Cactus', icon: '🌵' },// 18
  { color: '#2c2040',                name: 'Obsidienne' },       // 19
  { color: '#e84020',                name: 'Lave' },             // 20
  { color: '#9e8e7e',                name: 'Fer' },              // 21
  { color: '#c8b040',                name: 'Or' },               // 22
  { color: '#60d0d0',                name: 'Diamant' },          // 23
];

const ITEM_NAMES: Record<string, string> = {
  dirt: 'Terre', cobblestone: 'Roche', wood: 'Bois', sand: 'Sable',
  coal: 'Charbon', stick: 'Bâton', planks: 'Planches', glass: 'Verre',
  torch: 'Torche', snow: 'Neige', ice: 'Glace', sandstone: 'Grès',
  cactus: 'Cactus', obsidian: 'Obsidienne',
  iron_ore: 'Fer brut', gold_ore: 'Or brut', diamond: 'Diamant',
  iron_ingot: 'Lingot fer', gold_ingot: 'Lingot or',
  wooden_pickaxe: 'Pioche bois', stone_pickaxe: 'Pioche pierre',
  iron_pickaxe: 'Pioche fer',
  wooden_sword: 'Épée bois', stone_sword: 'Épée pierre', iron_sword: 'Épée fer',
};

const ITEM_ORDER = [
  'dirt', 'cobblestone', 'wood', 'sand', 'coal', 'stick', 'planks', 'glass', 'torch',
  'snow', 'ice', 'sandstone', 'cactus', 'obsidian',
  'iron_ore', 'gold_ore', 'diamond', 'iron_ingot', 'gold_ingot',
  'wooden_pickaxe', 'stone_pickaxe', 'iron_pickaxe',
  'wooden_sword', 'stone_sword', 'iron_sword',
];

const MONSTER_ICONS: Record<string, string> = {
  zombie: '🧟', skeleton: '💀', slime: '🟢', bat: '🦇', lava_slime: '🔥',
};

// ---------- types ----------
interface PlayerState {
  x: number; y: number;
  inventory: Record<string, number>;
  selected: string;
  hp: number; max_hp: number;
  score: number;
  facing: number;
  breath: number; max_breath: number;
  dead: boolean;
}
interface Monster { id: number; type: string; x: number; y: number; hp: number; max_hp: number; dmg: number; }
interface RecipeDef { input: Record<string, number>; output: string; count: number; cat: string; }
interface Settings { monstersEnabled: boolean; monsterRate: number; showParticles: boolean; soundEnabled: boolean; }

// ---------- helpers ----------
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

const DEFAULT_SETTINGS: Settings = {
  monstersEnabled: true,
  monsterRate: 100,
  showParticles: true,
  soundEnabled: true,
};

// étoiles déterministes (pas de Math.random au rendu)
const STARS = Array.from({ length: 40 }, (_, i) => ({
  left: ((i * 53) % 100),
  top: ((i * 29) % 55),
  size: 1 + (i % 3) * 0.6,
  twinkle: (i % 5) * 0.4,
}));

// ---------- composant ----------
export default function Component() {
  const [world, setWorld] = useState<number[][]>([]);
  const [seed, setSeed] = useState(0);
  const [player, setPlayer] = useState<PlayerState>({
    x: 60, y: 50, inventory: {}, selected: '', hp: 20, max_hp: 20, score: 0,
    facing: 1, breath: 12, max_breath: 12, dead: false,
  });
  const [monsters, setMonsters] = useState<Monster[]>([]);
  const [gameTime, setGameTime] = useState(0);
  const [recipes, setRecipes] = useState<Record<string, RecipeDef>>({});
  const [toast, setToast] = useState('');
  const [toastOk, setToastOk] = useState(true);
  const [anim, setAnim] = useState(0);              // horloge d'animation (fluides, torches)

  const [settings, setSettings] = useState<Settings>(() => {
    try {
      const saved = localStorage.getItem('mc2d_settings');
      if (saved) return { ...DEFAULT_SETTINGS, ...JSON.parse(saved) };
    } catch { /* ignore */ }
    return DEFAULT_SETTINGS;
  });
  const [showSettings, setShowSettings] = useState(false);

  // refs
  const playerRef = useRef(player);
  useEffect(() => { playerRef.current = player; }, [player]);
  const worldRef = useRef(world);
  useEffect(() => { worldRef.current = world; }, [world]);
  const settingsRef = useRef(settings);
  useEffect(() => { settingsRef.current = settings; }, [settings]);
  const keysRef = useRef({ left: false, right: false, jump: false, down: false });
  const simInFlight = useRef(false);
  const lastClickPos = useRef<{ x: number; y: number } | null>(null);

  // ---------- réseau ----------
  const post = useCallback(async (path: string, body: object = {}) => {
    try {
      const res = await fetch(`${API}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return await res.json();
    } catch { return { success: false, reason: 'Erreur réseau' }; }
  }, []);

  const getJson = useCallback(async (path: string) => {
    try { return await (await fetch(`${API}${path}`)).json(); } catch { return null; }
  }, []);

  // ---------- application de l'état serveur ----------
  const applyState = useCallback((data: any) => {
    if (!data) return;
    if (data.world) {
      setWorld(data.world);
      worldRef.current = data.world;
    } else if (Array.isArray(data.block_changes) && data.block_changes.length) {
      setWorld(prev => {
        if (!prev.length) return prev;
        const next = prev.slice();
        const touched = new Set<number>();
        for (const [bx, by, b] of data.block_changes) {
          if (by < 0 || by >= next.length) continue;
          if (!touched.has(by)) { next[by] = prev[by].slice(); touched.add(by); }
          next[by][bx] = b;
        }
        worldRef.current = next;
        return next;
      });
    }
    if (data.seed !== undefined) setSeed(data.seed);
    if (data.player) setPlayer(prev => ({ ...prev, ...data.player, inventory: data.player.inventory ?? prev.inventory }));
    if (data.monsters) setMonsters(data.monsters);
    if (data.game_time !== undefined) setGameTime(data.game_time);
  }, []);

  // ---------- particules ----------
  const spawnParticles = useCallback((clientX: number, clientY: number, color: string) => {
    const container = document.getElementById('mc-particles');
    if (!container) return;
    const count = 5 + Math.floor(Math.random() * 4);
    for (let i = 0; i < count; i++) {
      const p = document.createElement('div');
      p.className = 'mc-particle';
      p.style.left = `${clientX}px`;
      p.style.top = `${clientY}px`;
      p.style.background = color;
      p.style.setProperty('--dx', `${(Math.random() - 0.5) * 38}px`);
      p.style.setProperty('--dy', `${(Math.random() - 0.5) * 38 - 6}px`);
      container.appendChild(p);
      setTimeout(() => p.remove(), 450);
    }
  }, []);

  // ---------- actions ----------
  const loadWorld = useCallback(async (s?: number) => {
    const useSeed = s ?? Date.now();
    const data = await post('/world', { seed: useSeed, width: WORLD_W, height: WORLD_H });
    applyState(data);
    if (data?.world) { setToast(`Monde généré (seed ${data.seed})`); setToastOk(true); }
  }, [post, applyState]);

  const mineBlock = useCallback(async (x: number, y: number) => {
    const data = await post('/player/mine', { x, y });
    applyState(data);
    if (data?.success) {
      const dropName = ITEM_NAMES[data.drop] ?? data.drop ?? '?';
      const extra = data.extra_drop ? ` (+ ${ITEM_NAMES[data.extra_drop] ?? data.extra_drop})` : '';
      setToast(`⛏ ${data.block_mined} → ${dropName}${extra}`); setToastOk(true);
      if (settingsRef.current.showParticles && lastClickPos.current) {
        spawnParticles(lastClickPos.current.x, lastClickPos.current.y, '#cbb');
      }
    } else if (data?.reason) { setToast(data.reason); setToastOk(false); }
  }, [post, applyState, spawnParticles]);

  const placeBlock = useCallback(async (x: number, y: number, item: string) => {
    if (!item) { setToast('Aucun bloc sélectionné'); setToastOk(false); return; }
    const data = await post('/player/place', { x, y, item });
    applyState(data);
    if (data?.success) { setToast(`🏗 ${data.block_placed}`); setToastOk(true); }
    else if (data?.reason) { setToast(data.reason); setToastOk(false); }
  }, [post, applyState]);

  const craftItem = useCallback(async (recipeName: string) => {
    const data = await post('/player/craft', { recipe: recipeName });
    applyState(data);
    if (data?.success) { setToast(`🔨 ${data.count}× ${ITEM_NAMES[data.crafted] ?? data.crafted}`); setToastOk(true); }
    else if (data?.reason) { setToast(data.reason); setToastOk(false); }
  }, [post, applyState]);

  const selectItem = useCallback(async (item: string) => {
    const data = await post('/player/select', { item });
    if (data) setPlayer(prev => ({ ...prev, selected: data.selected ?? '' }));
  }, [post]);

  const attackMonster = useCallback(async (monsterId: number) => {
    const data = await post('/player/attack', { monster_id: monsterId });
    applyState(data);
    if (data?.success) {
      if (data.killed) {
        const dropText = data.drops && Object.keys(data.drops).length
          ? ' — Butin: ' + Object.entries(data.drops as Record<string, number>).map(([k, v]) => `${v}× ${ITEM_NAMES[k] ?? k}`).join(', ')
          : '';
        setToast(`⚔ Tué !${dropText}`); setToastOk(true);
      } else { setToast(`⚔ -${data.damage} PV`); setToastOk(true); }
      if (settingsRef.current.showParticles && lastClickPos.current) {
        spawnParticles(lastClickPos.current.x, lastClickPos.current.y, '#e55');
      }
    } else if (data?.reason) { setToast(data.reason); setToastOk(false); }
  }, [post, applyState, spawnParticles]);

  // ---------- boucle de simulation (physique) ----------
  const simStep = useCallback(async () => {
    if (simInFlight.current) return;
    if (!worldRef.current.length) return;
    if (playerRef.current.dead) return;
    simInFlight.current = true;
    const k = keysRef.current;
    const s = settingsRef.current;
    const data = await post('/player/sim', {
      left: k.left, right: k.right, jump: k.jump, down: k.down,
      peaceful: !s.monstersEnabled, spawn_rate: s.monsterRate,
    });
    applyState(data);
    simInFlight.current = false;
  }, [post, applyState]);

  // ---------- cycle de vie ----------
  useEffect(() => {
    loadWorld(Date.now());
    getJson('/recipes').then(d => { if (d?.recipes) setRecipes(d.recipes); });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const iv = setInterval(simStep, SIM_MS);
    return () => clearInterval(iv);
  }, [simStep]);

  useEffect(() => {
    const iv = setInterval(() => setAnim(a => (a + 1) % 100000), 160);
    return () => clearInterval(iv);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(''), 2400);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    localStorage.setItem('mc2d_settings', JSON.stringify(settings));
  }, [settings]);

  // ---------- clavier (touches maintenues) ----------
  useEffect(() => {
    const setKey = (e: KeyboardEvent, down: boolean) => {
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
      const k = e.key.toLowerCase();
      const K = keysRef.current;
      switch (k) {
        case 'a': case 'q': case 'arrowleft': K.left = down; e.preventDefault(); break;
        case 'd': case 'arrowright': K.right = down; e.preventDefault(); break;
        case 'w': case 'z': case 'arrowup': case ' ': K.jump = down; e.preventDefault(); break;
        case 's': case 'arrowdown': K.down = down; e.preventDefault(); break;
        default:
          if (down && k >= '1' && k <= '9') {
            e.preventDefault();
            const item = ITEM_ORDER[parseInt(k) - 1];
            if (item && (playerRef.current.inventory[item] ?? 0) > 0) {
              selectItem(playerRef.current.selected === item ? '' : item);
            }
          }
      }
    };
    const dn = (e: KeyboardEvent) => setKey(e, true);
    const up = (e: KeyboardEvent) => setKey(e, false);
    window.addEventListener('keydown', dn);
    window.addEventListener('keyup', up);
    return () => { window.removeEventListener('keydown', dn); window.removeEventListener('keyup', up); };
  }, [selectItem]);

  // ---------- viewport ----------
  const offsetX = clamp(player.x - Math.floor(VIEW_W / 2), 0, Math.max(0, WORLD_W - VIEW_W));
  const offsetY = clamp(player.y - Math.floor(VIEW_H / 2), 0, Math.max(0, WORLD_H - VIEW_H));

  // ---------- lumière : surface des colonnes + torches (memo) ----------
  const { columnTop, torches } = useMemo(() => {
    const top = new Array<number>(WORLD_W).fill(WORLD_H);
    const tor: { x: number; y: number }[] = [];
    for (let x = 0; x < WORLD_W; x++) {
      const colDone = { v: false };
      for (let y = 0; y < WORLD_H; y++) {
        const b = world[y]?.[x] ?? AIR;
        if (!colDone.v && isSolid(b)) { top[x] = y; colDone.v = true; }
        if (b === TORCH) tor.push({ x, y });
      }
    }
    return { columnTop: top, torches: tor };
  }, [world]);

  // torches visibles (filtrées au viewport élargi)
  const torchesNear = useMemo(
    () => torches.filter(t => t.x >= offsetX - 6 && t.x < offsetX + VIEW_W + 6 && t.y >= offsetY - 6 && t.y < offsetY + VIEW_H + 6),
    [torches, offsetX, offsetY],
  );

  // facteur de lumière du jour (0..1)
  const dayFactor = useMemo(() => {
    const t = gameTime;
    if (t < 2000) return 0.32 + (t / 2000) * 0.68;
    if (t < 11000) return 1.0;
    if (t < 13000) return 1.0 - ((t - 11000) / 2000) * 0.86;
    if (t < 23000) return 0.14;
    return 0.14 + ((t - 23000) / 1000) * 0.18;
  }, [gameTime]);

  const isNight = dayFactor < 0.4;

  // luminosité d'une case du monde (0 = noir, 1 = plein jour)
  const brightnessAt = useCallback((wx: number, wy: number): number => {
    const top = columnTop[wx] ?? WORLD_H;
    const exposed = wy < top;
    let light = exposed ? dayFactor : Math.max(0.05, dayFactor - (wy - top) * 0.16);
    for (const t of torchesNear) {
      const d = Math.max(Math.abs(t.x - wx), Math.abs(t.y - wy));
      if (d <= 5) light = Math.max(light, 0.96 * (1 - d / 6));
    }
    const dp = Math.max(Math.abs(player.x - wx), Math.abs(player.y - wy));
    if (dp <= 4) light = Math.max(light, 0.5 * (1 - dp / 4.5));
    return clamp(light, 0, 1);
  }, [columnTop, torchesNear, dayFactor, player.x, player.y]);

  // ---------- helpers rendu ----------
  const reach = (cx: number, cy: number) => Math.max(Math.abs(cx - player.x), Math.abs(cy - player.y));

  const canCraft = (name: string): boolean => {
    const r = recipes[name];
    if (!r) return false;
    for (const [item, n] of Object.entries(r.input)) if ((player.inventory[item] ?? 0) < n) return false;
    return true;
  };
  const formatInput = (input: Record<string, number>) =>
    Object.entries(input).map(([item, n]) => `${n}× ${ITEM_NAMES[item] ?? item}`).join(' + ');

  const visibleMonsters = useMemo(() => {
    if (!settings.monstersEnabled) return [];
    if (settings.monsterRate >= 100) return monsters;
    return monsters.filter(m => (m.id * 37) % 100 < settings.monsterRate);
  }, [monsters, settings.monstersEnabled, settings.monsterRate]);

  const timeLabel = gameTime < 2000 ? '🌅 Aube'
    : gameTime < 11000 ? '☀️ Jour'
    : gameTime < 13000 ? '🌇 Crépuscule'
    : '🌙 Nuit';

  // ciel : dégradé interpolé jour/nuit
  const skyTop = `rgb(${Math.round(lerp(11, 135, dayFactor))},${Math.round(lerp(16, 206, dayFactor))},${Math.round(lerp(38, 235, dayFactor))})`;
  const skyBot = `rgb(${Math.round(lerp(30, 175, dayFactor))},${Math.round(lerp(34, 216, dayFactor))},${Math.round(lerp(60, 240, dayFactor))})`;
  const sunProgress = clamp(gameTime / 12000, 0, 1);   // course du soleil 0→1 (jour)
  const moonProgress = clamp((gameTime - 12000) / 12000, 0, 1);

  // ---------- clics sur la grille ----------
  const handleCellMouseDown = (wx: number, wy: number, e: React.MouseEvent) => {
    e.preventDefault();
    if (player.dead) return;
    lastClickPos.current = { x: e.clientX, y: e.clientY };
    const b = world[wy]?.[wx] ?? AIR;
    if (e.button === 0) {
      const mob = visibleMonsters.find(m => m.x === wx && m.y === wy);
      if (mob && reach(wx, wy) <= 2) { attackMonster(mob.id); return; }
      if (reach(wx, wy) <= 3 && isSolid(b)) mineBlock(wx, wy);
    } else if (e.button === 2) {
      if (reach(wx, wy) <= 3 && b === AIR && player.selected) placeBlock(wx, wy, player.selected);
    }
  };

  // ---------- HUD : coeurs ----------
  const hearts = (() => {
    const els: React.ReactNode[] = [];
    const full = Math.floor(player.hp / 2);
    const half = player.hp % 2;
    const empty = Math.max(0, Math.floor(player.max_hp / 2) - full - half);
    for (let i = 0; i < full; i++) els.push(<span key={`f${i}`}>❤️</span>);
    if (half) els.push(<span key="h" style={{ opacity: 0.6 }}>❤️</span>);
    for (let i = 0; i < empty; i++) els.push(<span key={`e${i}`} style={{ opacity: 0.25 }}>🤍</span>);
    return els;
  })();

  const headBlock = world[player.y]?.[player.x] ?? AIR;
  const submerged = headBlock === WATER;
  const playerEmoji = submerged ? '🤿' : '🧑';

  const craftCats = ['Matériaux', 'Outils', 'Armes', 'Fonderie'];
  const hotbarItems = ITEM_ORDER.slice(0, 9);

  // ============================================================
  // RENDU
  // ============================================================
  return (
    <div style={{
      padding: '0.5rem', paddingBottom: '3.2rem',
      fontFamily: "'Segoe UI', system-ui, sans-serif",
      color: '#e0e0e0', background: '#0d0d10', minHeight: '100vh', userSelect: 'none',
    }}>
      {/* en-tête */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 6, flexWrap: 'wrap', padding: '0 4px' }}>
        <h2 style={{ margin: 0, fontSize: '1.15rem', display: 'flex', alignItems: 'center', gap: 6 }}>
          <span>⛏️</span> Terralike
        </h2>
        <span style={{ color: '#999', fontSize: '0.78rem' }}>Seed <b style={{ color: '#ccc' }}>{seed}</b></span>
        <span style={{ color: '#999', fontSize: '0.78rem' }}>{timeLabel}</span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 1, fontSize: '0.8rem' }}>{hearts}</span>
        {/* jauge d'oxygène (sous l'eau) */}
        {(submerged || player.breath < player.max_breath) && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.75rem', color: '#8cf' }}>
            💧
            <span style={{ width: 56, height: 7, background: '#123', borderRadius: 4, overflow: 'hidden', display: 'inline-block' }}>
              <span style={{ display: 'block', height: '100%', width: `${(player.breath / player.max_breath) * 100}%`, background: 'linear-gradient(90deg,#39f,#6cf)', transition: 'width 0.2s' }} />
            </span>
          </span>
        )}
        <span style={{ color: '#ffd700', fontSize: '0.8rem', fontWeight: 600 }}>⭐ {player.score}</span>
        <button onClick={() => setShowSettings(s => !s)} title="Réglages" style={{
          marginLeft: 'auto', background: showSettings ? '#3a3a5c' : '#222228',
          border: `1px solid ${showSettings ? '#6a6aff' : '#555'}`, borderRadius: 6, color: '#ccc',
          cursor: 'pointer', fontSize: '1.1rem', width: 32, height: 32,
          display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0, lineHeight: 1,
        }}>⚙️</button>
      </div>

      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        {/* ---- viewport ---- */}
        <div style={{ position: 'relative', lineHeight: 0 }}>
          <div
            onContextMenu={e => e.preventDefault()}
            style={{
              border: '2px solid #444', borderRadius: 6, display: 'inline-block', lineHeight: 0,
              background: `linear-gradient(180deg, ${skyTop} 0%, ${skyBot} 100%)`,
              position: 'relative', overflow: 'hidden',
              width: VIEW_W * BLOCK_SIZE, height: VIEW_H * BLOCK_SIZE,
            }}
          >
            {/* étoiles (nuit) */}
            {isNight && STARS.map((s, i) => (
              <div key={i} style={{
                position: 'absolute', left: `${s.left}%`, top: `${s.top}%`,
                width: s.size, height: s.size, borderRadius: '50%', background: '#fff',
                opacity: clamp((0.4 - dayFactor) * 2, 0, 1) * (0.5 + 0.5 * Math.sin((anim + i * 7) * 0.3 + s.twinkle)),
                pointerEvents: 'none',
              }} />
            ))}
            {/* soleil / lune */}
            {dayFactor > 0.2 && (
              <div style={{
                position: 'absolute', fontSize: 26, pointerEvents: 'none',
                left: `${lerp(6, 88, sunProgress)}%`,
                top: `${22 - Math.sin(sunProgress * Math.PI) * 16 + 8}%`,
                opacity: clamp((dayFactor - 0.2) * 1.6, 0, 1),
                filter: 'drop-shadow(0 0 10px rgba(255,220,120,0.8))',
              }}>☀️</div>
            )}
            {isNight && (
              <div style={{
                position: 'absolute', fontSize: 22, pointerEvents: 'none',
                left: `${lerp(8, 86, moonProgress)}%`,
                top: `${22 - Math.sin(moonProgress * Math.PI) * 14 + 8}%`,
                filter: 'drop-shadow(0 0 8px rgba(200,220,255,0.7))',
              }}>🌙</div>
            )}

            {world.length === 0 && (
              <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#bbb', fontSize: '1rem' }}>
                Génération du monde…
              </div>
            )}

            {world.slice(offsetY, offsetY + VIEW_H).map((row, vy) => {
              const wy = offsetY + vy;
              return (
                <div key={wy} style={{ display: 'flex' }}>
                  {row.slice(offsetX, offsetX + VIEW_W).map((block, vx) => {
                    const wx = offsetX + vx;
                    const here = wx === player.x && wy === player.y;
                    const adj = reach(wx, wy) <= 3;
                    const def = BLOCK_DEFS[block] ?? BLOCK_DEFS[0];
                    const mob = visibleMonsters.find(m => m.x === wx && m.y === wy);
                    const light = brightnessAt(wx, wy);
                    const dark = (1 - light) * 0.93;

                    let bg: string = def.color;
                    let shadow: string | undefined;
                    if (block === WATER) {
                      const s = Math.round(Math.sin((wx + wy + anim) * 0.7) * 18);
                      bg = `linear-gradient(180deg, rgb(40,${120 + s},${190 + s}) 0%, rgb(22,70,135) 100%)`;
                    } else if (block === LAVA) {
                      const s = Math.round(Math.sin((wx + wy + anim) * 1.1) * 26);
                      bg = `linear-gradient(180deg, rgb(${235 + s},${75 + Math.round(s * 0.6)},20) 0%, rgb(170,32,8) 100%)`;
                    } else if (block === AIR) {
                      bg = 'transparent';
                    } else {
                      shadow = 'inset 1px 1px 0 rgba(255,255,255,0.10), inset -1px -1px 0 rgba(0,0,0,0.22)';
                    }

                    return (
                      <div
                        key={`${wx}-${wy}`}
                        onMouseDown={e => handleCellMouseDown(wx, wy, e)}
                        title={mob ? `${mob.type} (${mob.hp}/${mob.max_hp} PV)` : here ? `Vous — ${player.hp}/${player.max_hp} PV` : adj ? `${def.name} — G: miner | D: poser` : def.name}
                        style={{
                          width: BLOCK_SIZE, height: BLOCK_SIZE, background: bg,
                          border: here ? '1px solid rgba(255,255,255,0.5)' : adj ? '1px solid rgba(255,255,255,0.08)' : '1px solid rgba(0,0,0,0.10)',
                          boxSizing: 'border-box', cursor: adj ? 'pointer' : 'default',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          fontSize: 11, lineHeight: 1, position: 'relative', boxShadow: shadow,
                        }}
                        onMouseEnter={e => { if (adj && !here) (e.currentTarget as HTMLDivElement).style.filter = 'brightness(1.3)'; }}
                        onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.filter = ''; }}
                      >
                        {/* icônes de blocs */}
                        {!here && block !== AIR && (def.icon
                          ? <span style={{ fontSize: 12, opacity: 0.92 }}>{def.icon}</span>
                          : block === 21 ? <span style={{ fontSize: 8, color: '#e0a060' }}>●</span>
                          : block === 22 ? <span style={{ fontSize: 8, color: '#ffe040' }}>●</span>
                          : block === 23 ? <span style={{ fontSize: 8, color: '#60ffff' }}>◆</span>
                          : null)}

                        {/* joueur */}
                        {here && (
                          <span style={{ fontSize: 15, zIndex: 4, transform: `scaleX(${player.facing})`, filter: 'drop-shadow(0 1px 1px rgba(0,0,0,0.7))' }}>
                            {playerEmoji}
                          </span>
                        )}

                        {/* monstre */}
                        {mob && !here && (
                          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 3 }}>
                            <span style={{ fontSize: 15, filter: 'drop-shadow(0 1px 1px rgba(0,0,0,0.6))' }}>{MONSTER_ICONS[mob.type] ?? '👾'}</span>
                            <div style={{ position: 'absolute', bottom: 0, left: 2, right: 2, height: 3, background: '#300', borderRadius: 1, overflow: 'hidden' }}>
                              <div style={{ width: `${(mob.hp / mob.max_hp) * 100}%`, height: '100%', background: mob.hp / mob.max_hp > 0.5 ? '#4c4' : '#c44' }} />
                            </div>
                          </div>
                        )}

                        {/* voile d'obscurité (lumière dynamique) */}
                        {dark > 0.02 && (
                          <div style={{ position: 'absolute', inset: 0, background: `rgba(4,6,16,${dark})`, pointerEvents: 'none', zIndex: 5 }} />
                        )}
                      </div>
                    );
                  })}
                </div>
              );
            })}

            {/* overlay mort */}
            {player.dead && (
              <div style={{ position: 'absolute', inset: 0, background: 'rgba(40,0,0,0.55)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', zIndex: 20 }}>
                <div style={{ fontSize: '2rem' }}>💀</div>
                <div style={{ color: '#fff', fontWeight: 700, marginTop: 6 }}>Vous êtes mort</div>
                <button onClick={() => loadWorld(Date.now())} style={{ marginTop: 12, padding: '8px 18px', borderRadius: 6, border: '1px solid #a55', background: '#3a1e1e', color: '#fcc', cursor: 'pointer', fontWeight: 600 }}>
                  🔄 Renaître (nouveau monde)
                </button>
              </div>
            )}
          </div>

          <div style={{ fontSize: '0.65rem', color: '#555', marginTop: 4, textAlign: 'center' }}>
            📍 {player.x},{player.y} · A/D ou ←/→ : courir · W/Espace : sauter · S : descendre/nager
          </div>
        </div>

        {/* ---- panneau latéral ---- */}
        <div style={{ minWidth: 230, maxWidth: 290, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {/* inventaire */}
          <fieldset style={{ border: '1px solid #444', borderRadius: 6, padding: '6px 10px', background: '#1e1e22', margin: 0 }}>
            <legend style={{ color: '#bbb', fontWeight: 600, fontSize: '0.85rem' }}>
              🎒 Inventaire {player.selected && <span style={{ color: '#ffd700', fontSize: '0.7rem' }}>— {ITEM_NAMES[player.selected] ?? player.selected}</span>}
            </legend>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxHeight: 300, overflowY: 'auto' }}>
              {ITEM_ORDER.map(item => {
                const count = player.inventory[item] ?? 0;
                const isSel = player.selected === item;
                const isTool = item.includes('pickaxe');
                const isWeapon = item.includes('sword');
                return (
                  <div key={item} onClick={() => count > 0 && selectItem(isSel ? '' : item)}
                    style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 6px',
                      borderRadius: 4, cursor: count > 0 ? 'pointer' : 'default',
                      background: isSel ? '#3a3a5c' : 'transparent', border: isSel ? '1px solid #6a6aff' : '1px solid transparent',
                      opacity: count > 0 ? 1 : 0.3, fontSize: '0.76rem',
                    }}>
                    <span style={{ color: isTool ? '#87ceeb' : isWeapon ? '#f0a0a0' : '#ddd' }}>{ITEM_NAMES[item] ?? item}</span>
                    <span style={{ fontWeight: 600, fontSize: '0.78rem', color: count > 0 ? '#fff' : '#555' }}>{count}</span>
                  </div>
                );
              })}
            </div>
          </fieldset>

          {/* crafting */}
          <fieldset style={{ border: '1px solid #444', borderRadius: 6, padding: '6px 10px', background: '#1e1e22', margin: 0 }}>
            <legend style={{ color: '#bbb', fontWeight: 600, fontSize: '0.85rem' }}>🔨 Crafting</legend>
            {craftCats.map(cat => {
              const catRecipes = Object.entries(recipes).filter(([, r]) => r.cat === cat);
              if (!catRecipes.length) return null;
              return (
                <div key={cat} style={{ marginBottom: 6 }}>
                  <div style={{ fontSize: '0.7rem', color: '#888', fontWeight: 600, marginBottom: 3, paddingBottom: 2, borderBottom: '1px solid #333' }}>{cat}</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                    {catRecipes.map(([name, r]) => {
                      const ok = canCraft(name);
                      return (
                        <button key={name} onClick={() => craftItem(name)} disabled={!ok}
                          title={ok ? `Fabriquer ${ITEM_NAMES[r.output] ?? r.output} ×${r.count}` : `Manque: ${formatInput(r.input)}`}
                          style={{
                            padding: '4px 7px', borderRadius: 4, border: ok ? '1px solid #5a5' : '1px solid #3a3a3a',
                            background: ok ? '#1e2e1e' : '#1a1a1d', color: ok ? '#cfc' : '#555',
                            cursor: ok ? 'pointer' : 'not-allowed', fontSize: '0.72rem', textAlign: 'left',
                          }}>
                          <span style={{ fontWeight: 600 }}>{ITEM_NAMES[r.output] ?? r.output} ×{r.count}</span>
                          <span style={{ fontSize: '0.66rem', opacity: 0.65, marginLeft: 6 }}>({formatInput(r.input)})</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
            {Object.keys(recipes).length === 0 && <span style={{ color: '#555', fontSize: '0.75rem' }}>Chargement…</span>}
          </fieldset>

          <button onClick={() => loadWorld(Date.now())} style={{ padding: '7px 12px', borderRadius: 6, border: '1px solid #555', background: '#222228', color: '#ccc', cursor: 'pointer', fontSize: '0.82rem', fontWeight: 500 }}>
            🔄 Nouveau monde
          </button>

          <div style={{ fontSize: '0.68rem', color: '#666', lineHeight: 1.6, background: '#18181c', padding: '7px 9px', borderRadius: 6, border: '1px solid #2a2a2a' }}>
            <b style={{ color: '#888' }}>⌨️ Contrôles</b><br />
            <span style={{ color: '#aaa' }}>A/D · ←/→</span> : courir (gravité !)<br />
            <span style={{ color: '#aaa' }}>W · Espace</span> : sauter / nager vers le haut<br />
            <span style={{ color: '#aaa' }}>S · ↓</span> : descendre / plonger<br />
            <span style={{ color: '#aaa' }}>Clic G</span> : miner / attaquer · <span style={{ color: '#aaa' }}>Clic D</span> : poser<br />
            <span style={{ color: '#888', marginTop: 4, display: 'inline-block' }}>
              💡 Pose des torches pour éclairer les grottes<br />
              💡 Lave + eau = obsidienne · attention aux chutes !
            </span>
          </div>

          <div style={{ fontSize: '0.65rem', color: '#555', lineHeight: 1.5, background: '#18181c', padding: '5px 8px', borderRadius: 6, border: '1px solid #2a2a2a' }}>
            <b style={{ color: '#777' }}>👾 Créatures</b><br />
            🟢 Slime · 🧟 Zombie (nuit) · 💀 Squelette (grottes)<br />
            🦇 Chauve-souris (vole) · 🔥 Lave-slime (profondeurs)
          </div>
        </div>
      </div>

      {/* hotbar */}
      <div style={{
        position: 'fixed', bottom: 0, left: '50%', transform: 'translateX(-50%)', display: 'flex', gap: 3,
        padding: '6px 10px', background: '#1a1a1f', border: '1px solid #444', borderBottom: 'none',
        borderRadius: '8px 8px 0 0', zIndex: 500, boxShadow: '0 -2px 12px rgba(0,0,0,0.5)',
      }}>
        {hotbarItems.map((item, idx) => {
          const count = player.inventory[item] ?? 0;
          const isSel = player.selected === item;
          const name = ITEM_NAMES[item] ?? item;
          return (
            <div key={item} onClick={() => count > 0 && selectItem(isSel ? '' : item)} title={`${idx + 1}: ${name} (×${count})`}
              style={{
                width: 44, height: 40, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                background: isSel ? '#3a3a5c' : '#222228', border: isSel ? '2px solid #ffd700' : '1px solid #444',
                borderRadius: 4, cursor: count > 0 ? 'pointer' : 'default', opacity: count > 0 ? 1 : 0.3, position: 'relative',
              }}>
              <span style={{ fontSize: '0.56rem', fontWeight: 600, color: isSel ? '#ffd700' : '#aaa', lineHeight: 1 }}>{name.length > 5 ? name.slice(0, 5) : name}</span>
              <span style={{ fontSize: '0.58rem', color: count > 0 ? '#ddd' : '#555', lineHeight: 1, marginTop: 1 }}>{count}</span>
              <span style={{ fontSize: '0.5rem', color: '#555', position: 'absolute', top: 1, right: 3 }}>{idx + 1}</span>
            </div>
          );
        })}
      </div>

      {/* modale réglages (paramètres existants conservés) */}
      {showSettings && (
        <>
          <div onClick={() => setShowSettings(false)} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', zIndex: 900 }} />
          <div style={{
            position: 'fixed', top: '50%', left: '50%', transform: 'translate(-50%, -50%)', zIndex: 901,
            background: '#1e1e24', border: '1px solid #555', borderRadius: 10, padding: '20px 24px', minWidth: 300, maxWidth: 380,
            boxShadow: '0 8px 32px rgba(0,0,0,0.7)', color: '#ddd', fontSize: '0.85rem',
          }}>
            <h3 style={{ margin: '0 0 16px 0', fontSize: '1rem', color: '#fff' }}>⚙️ Réglages</h3>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, cursor: 'pointer' }}>
              <input type="checkbox" checked={settings.monstersEnabled} onChange={e => setSettings(s => ({ ...s, monstersEnabled: e.target.checked }))} style={{ width: 18, height: 18, cursor: 'pointer', accentColor: '#6a6aff' }} />
              <span>Monstres (mode paisible si décoché)</span>
            </label>
            {settings.monstersEnabled && (
              <div style={{ marginBottom: 14, marginLeft: 28 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                  <span style={{ fontSize: '0.78rem', color: '#aaa' }}>Taux de monstres</span>
                  <span style={{ fontSize: '0.78rem', fontWeight: 600, color: settings.monsterRate < 100 ? '#ffd700' : '#6f6' }}>{settings.monsterRate}%</span>
                </div>
                <input type="range" min={0} max={100} step={5} value={settings.monsterRate}
                  onChange={e => setSettings(s => ({ ...s, monsterRate: parseInt(e.target.value) }))}
                  style={{ width: '100%', cursor: 'pointer', accentColor: '#6a6aff' }} />
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.6rem', color: '#555', marginTop: 2 }}>
                  <span>0% (aucun)</span><span>100% (tous)</span>
                </div>
              </div>
            )}
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, cursor: 'pointer' }}>
              <input type="checkbox" checked={settings.showParticles} onChange={e => setSettings(s => ({ ...s, showParticles: e.target.checked }))} style={{ width: 18, height: 18, cursor: 'pointer', accentColor: '#6a6aff' }} />
              <span>Particules</span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18, cursor: 'not-allowed', opacity: 0.5 }}>
              <input type="checkbox" checked={settings.soundEnabled} disabled style={{ width: 18, height: 18 }} />
              <span>Son (bientôt disponible)</span>
            </label>
            <button onClick={() => setShowSettings(false)} style={{ display: 'block', width: '100%', padding: '8px 0', borderRadius: 6, border: '1px solid #555', background: '#222228', color: '#ccc', cursor: 'pointer', fontSize: '0.85rem', fontWeight: 500 }}>Fermer</button>
          </div>
        </>
      )}

      <div id="mc-particles" style={{ position: 'fixed', inset: 0, pointerEvents: 'none', zIndex: 999 }} />

      {toast && (
        <div style={{
          position: 'fixed', bottom: 56, left: '50%', transform: 'translateX(-50%)', padding: '7px 18px', borderRadius: 8,
          background: toastOk ? '#1e3a1e' : '#3a1e1e', border: `1px solid ${toastOk ? '#5a5' : '#a55'}`,
          color: toastOk ? '#cfc' : '#fcc', fontSize: '0.82rem', fontWeight: 500, zIndex: 1000,
          boxShadow: '0 4px 18px rgba(0,0,0,0.6)', pointerEvents: 'none', animation: 'mcFadeUp 0.2s ease-out',
        }}>{toast}</div>
      )}

      <style>{`
        @keyframes mcFadeUp { from { opacity: 0; transform: translateX(-50%) translateY(10px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }
        .mc-particle { position: absolute; width: 4px; height: 4px; border-radius: 50%; pointer-events: none; animation: mcBurst 0.45s ease-out forwards; }
        @keyframes mcBurst { 0% { opacity: 1; transform: translate(0,0); } 100% { opacity: 0; transform: translate(var(--dx), var(--dy)); } }
      `}</style>
    </div>
  );
}
