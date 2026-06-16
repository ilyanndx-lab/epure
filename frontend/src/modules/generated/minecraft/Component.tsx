import React, { useState, useEffect, useCallback } from 'react';
import { usePersistentState } from '../../../usePersistentState';

interface Block {
  id: number;
  color: string;
  name: string;
}

const BLOCKS: Block[] = [
  { id: 0, color: '#87CEEB', name: 'Air' },
  { id: 1, color: '#808080', name: 'Pierre' },
  { id: 2, color: '#8B4513', name: 'Terre' },
  { id: 3, color: '#228B22', name: 'Herbe' },
  { id: 4, color: '#D2B48C', name: 'Bois' },
];

const BLOCK_SIZE = 20;

export default function Minecraft() {
  const [world, setWorld] = useState<number[][]>([]);
  const [seed, setSeed] = usePersistentState<number>('minecraft.seed', 0);
  const [selectedBlock, setSelectedBlock] = usePersistentState<number>('minecraft.selectedBlock', 1);
  const [craftQuestion, setCraftQuestion] = usePersistentState<string>('minecraft.craftQuestion', '');
  const [craftAnswer, setCraftAnswer] = usePersistentState<string>('minecraft.craftAnswer', '');
  const [loadingCraft, setLoadingCraft] = useState(false);

  const fetchWorld = useCallback(async (s?: number) => {
    const useSeed = s || Date.now();
    try {
      const res = await fetch('http://localhost:8000/minecraft/world', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seed: useSeed, width: 40, height: 30 }),
      });
      const data = await res.json();
      setWorld(data.world);
      setSeed(data.seed);
    } catch (err) {
      console.error(err);
    }
  }, []);

  useEffect(() => {
    fetchWorld(seed || undefined);
  }, []);

  const handleCellClick = (x: number, y: number, event: React.MouseEvent) => {
    event.preventDefault();
    if (event.button === 0) {
      if (world[y][x] !== 0) {
        const newWorld = world.map(row => [...row]);
        newWorld[y][x] = 0;
        setWorld(newWorld);
      }
    } else if (event.button === 2) {
      if (world[y][x] === 0 && selectedBlock !== 0) {
        const newWorld = world.map(row => [...row]);
        newWorld[y][x] = selectedBlock;
        setWorld(newWorld);
      }
    }
  };

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
  };

  const handleCraftSubmit = async () => {
    if (!craftQuestion.trim()) return;
    setLoadingCraft(true);
    try {
      const res = await fetch('http://localhost:8000/minecraft/craft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: craftQuestion }),
      });
      const data = await res.json();
      setCraftAnswer(data.answer);
    } catch (err) {
      console.error(err);
    } finally {
      setLoadingCraft(false);
    }
  };

  return (
    <div style={{ padding: '1rem', fontFamily: 'monospace' }}>
      <h2>⛏️ Minecraft (seed: {seed})</h2>

      <div style={{ display: 'flex', gap: '2rem', alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div
          onContextMenu={handleContextMenu}
          style={{ border: '2px solid #555', display: 'inline-block', lineHeight: 0 }}
        >
          {world.map((row, y) => (
            <div key={y} style={{ display: 'flex' }}>
              {row.map((block, x) => {
                const b = BLOCKS[block] || BLOCKS[0];
                return (
                  <div
                    key={`${x}-${y}`}
                    onMouseDown={(e) => handleCellClick(x, y, e)}
                    style={{
                      width: BLOCK_SIZE,
                      height: BLOCK_SIZE,
                      backgroundColor: b.color,
                      display: 'inline-block',
                      border: '1px solid rgba(0,0,0,0.2)',
                      boxSizing: 'border-box',
                    }}
                  />
                );
              })}
            </div>
          ))}
        </div>

        <div style={{ minWidth: 260 }}>
          <fieldset>
            <legend>🧰 Aide au crafting</legend>
            <input
              type="text"
              value={craftQuestion}
              onChange={(e) => setCraftQuestion(e.target.value)}
              placeholder="Comment crafter une épée ?"
              style={{ width: '100%', marginBottom: 4 }}
            />
            <button onClick={handleCraftSubmit} disabled={loadingCraft}>
              {loadingCraft ? '🤖 réflexion...' : '💡 Demander'}
            </button>
            {craftAnswer && (
              <pre style={{ background: '#f5f5f5', padding: 8, marginTop: 8, whiteSpace: 'pre-wrap' }}>
                {craftAnswer}
              </pre>
            )}
          </fieldset>

          <div style={{ marginTop: 12 }}>
            <strong>Bloc sélectionné :</strong> {BLOCKS[selectedBlock]?.name || 'Air'}
            <br />
            <select
              value={selectedBlock}
              onChange={(e) => setSelectedBlock(Number(e.target.value))}
            >
              {BLOCKS.slice(1).map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>

          <button onClick={() => fetchWorld(Date.now())} style={{ marginTop: 12 }}>
            🔄 Nouvelle seed aléatoire
          </button>
        </div>
      </div>
    </div>
  );
}
