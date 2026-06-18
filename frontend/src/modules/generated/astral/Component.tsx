import React, { useState, useEffect } from 'react';
import { Star, Swords, Gem, Shield, Hammer, FlaskConical, Armchair, Cable, Cpu, Skull, Users, CloudRain, Network, HardDrive, Monitor, Gauge, Puzzle, Music, Eye, Scale, BookOpen, ChevronRight, ChevronDown, Loader2 } from 'lucide-react';

const API = 'http://localhost:8000';

/* ================================================================
   Module Astral — Encyclopedie du Game Design d'Astral Earth
   Le composant charge les donnees depuis /astral/* et les affiche
   dans un visualiseur a navigation laterale avec 18 sections.
   ================================================================ */

const SECTIONS = [
  { id: 'weapons',    label: 'Armes',             icon: Swords,       endpoint: '/astral/weapons' },
  { id: 'ores',       label: 'Minerais & Alliages',icon: Gem,          endpoint: '/astral/ores' },
  { id: 'armor',      label: 'Armures',           icon: Shield,       endpoint: '/astral/armor' },
  { id: 'crafting',   label: 'Crafting',          icon: Hammer,       endpoint: '/astral/crafting' },
  { id: 'potions',    label: 'Potions & Herbes',  icon: FlaskConical, endpoint: '/astral/potions' },
  { id: 'furniture',  label: 'Mobilier',          icon: Armchair,     endpoint: '/astral/furniture' },
  { id: 'accessories',label: 'Accessoires',       icon: Cable,        endpoint: '/astral/accessories' },
  { id: 'drone',      label: 'Drone',             icon: Cpu,          endpoint: '/astral/drone' },
  { id: 'bosses',     label: 'Boss',              icon: Skull,        endpoint: '/astral/bosses' },
  { id: 'npcs',       label: 'PNJ',               icon: Users,        endpoint: '/astral/npcs' },
  { id: 'events',     label: 'Evenements',        icon: CloudRain,    endpoint: '/astral/events' },
  { id: 'multiplayer',label: 'Multijoueur',       icon: Network,      endpoint: '/astral/multiplayer' },
  { id: 'save',       label: 'Sauvegarde',        icon: HardDrive,    endpoint: '/astral/save-system' },
  { id: 'ui',         label: 'Interface UI',      icon: Monitor,      endpoint: '/astral/ui' },
  { id: 'perf',       label: 'Performance',       icon: Gauge,        endpoint: '/astral/performance' },
  { id: 'mods',       label: 'API Mods',          icon: Puzzle,       endpoint: '/astral/mod-api' },
  { id: 'audio',      label: 'Audio',             icon: Music,        endpoint: '/astral/audio' },
  { id: 'access',     label: 'Accessibilite',     icon: Eye,          endpoint: '/astral/accessibility' },
  { id: 'balancing',  label: 'Equilibrage',       icon: Scale,        endpoint: '/astral/balancing' },
  { id: 'full',       label: 'Document complet',   icon: BookOpen,     endpoint: '/astral/meta' },
];

// Composants d'affichage reutilisables

function Badge({ children, color = 'accent' }: { children: React.ReactNode; color?: string }) {
  const colors: Record<string, string> = {
    accent: 'bg-accent/10 text-accent border-accent/20',
    green: 'bg-green-500/10 text-green-400 border-green-500/20',
    red: 'bg-red-500/10 text-red-400 border-red-500/20',
    blue: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    yellow: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    purple: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
    orange: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
  };
  return <span className={`px-2 py-0.5 rounded-full text-xs border ${colors[color] || colors.accent}`}>{children}</span>;
}

function DataTable({ headers, rows }: { headers: string[]; rows: (string | React.ReactNode)[][] }) {
  if (!rows.length) return <p className="text-sm text-secondary italic">Aucune donnee.</p>;
  return (
    <div className="overflow-x-auto border border-line rounded-lg">
      <table className="w-full text-sm text-left">
        <thead className="bg-elevated text-secondary text-xs uppercase">
          <tr>{headers.map((h, i) => <th key={i} className="px-3 py-2 whitespace-nowrap">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((row, i) => (
            <tr key={i} className="hover:bg-elevated/50 transition-colors">
              {row.map((cell, j) => <td key={j} className="px-3 py-2">{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <div className={`bg-elevated border border-line rounded-lg p-4 ${className}`}>{children}</div>;
}

// --- Rendu par section ---

function WeaponsView({ data }: { data: any }) {
  if (!data) return null;
  const categories = [
    { key: 'melee', label: 'Melee', color: 'red', items: data.melee || [] },
    { key: 'ranged', label: 'Distance', color: 'green', items: data.ranged || [] },
    { key: 'magic', label: 'Magie', color: 'blue', items: data.magic || [] },
    { key: 'summon', label: 'Invocation', color: 'purple', items: data.summon || [] },
    { key: 'gadget', label: 'Gadget', color: 'orange', items: data.gadget || [] },
  ];
  return (
    <div className="space-y-6">
      {categories.map(cat => (
        <div key={cat.key}>
          <h3 className="text-lg font-semibold text-primary mb-3 flex items-center gap-2">
            <span className={`w-3 h-3 rounded-full bg-${cat.color}-500`} />{cat.label} <Badge color={cat.color}>{cat.items.length} armes</Badge>
          </h3>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {cat.items.map((w: any) => (
              <Card key={w.id} className="hover:border-accent/30 transition-colors">
                <div className="flex items-start justify-between mb-2">
                  <h4 className="font-medium text-primary">{w.name}</h4>
                  <Badge>Tier {w.tier}</Badge>
                </div>
                <div className="grid grid-cols-3 gap-2 text-xs text-secondary mb-2">
                  <div><span className="font-medium text-primary">{w.damage}</span> degats</div>
                  <div><span className="font-medium text-primary">{w.speed}</span> coups/s</div>
                  <div><span className="font-medium text-primary">{w.knockback}</span> recul</div>
                  {w.mana_cost !== undefined && <div>Mana: <span className="font-medium text-primary">{w.mana_cost}</span></div>}
                  {w.minion_cap_bonus !== undefined && <div>Sbires: <span className="font-medium text-primary">+{w.minion_cap_bonus}</span></div>}
                  {w.velocity !== undefined && <div>Velocite: <span className="font-medium text-primary">{w.velocity}</span></div>}
                </div>
                <p className="text-xs text-secondary mb-2">{w.desc}</p>
                {w.combo && <p className="text-xs text-accent mb-1"><span className="font-medium">Combo:</span> {w.combo}</p>}
                {w.effects && w.effects.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {w.effects.map((e: string, i: number) => <Badge key={i} color="blue">{e}</Badge>)}
                  </div>
                )}
                <p className="text-xs text-secondary mt-2 italic">Craft: {w.material}</p>
              </Card>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function OresView({ data }: { data: any }) {
  if (!data) return null;
  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Minerais <Badge color="yellow">{data.ores?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Profondeur', 'Monde', 'Equivalent Reve', 'Rendement', 'Biome']}
          rows={(data.ores || []).map((o: any) => [
            <span className="font-medium text-primary">{o.name}</span>,
            `${o.depth_min}-${o.depth_max}`,
            o.world === 'reve' ? <Badge color="purple">Reve</Badge> : <Badge color="green">Normal</Badge>,
            o.alt,
            o.bar_yield,
            o.spawn_biome
          ])}
        />
      </div>
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Alliages <Badge color="orange">{data.alloys?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Ingredients', 'Station', 'Usage']}
          rows={(data.alloys || []).map((a: any) => [
            <span className="font-medium text-primary">{a.name}</span>,
            a.ingredients,
            a.station,
            a.usage
          ])}
        />
      </div>
    </div>
  );
}

function ArmorView({ data }: { data: any }) {
  if (!Array.isArray(data)) return null;
  return (
    <div>
      <h3 className="text-lg font-semibold text-primary mb-3">Sets d'armure <Badge>{data.length}</Badge></h3>
      <DataTable
        headers={['Nom', 'Tier', 'Casque', 'Plastron', 'Jambieres', 'Bottes', 'Def. Totale', 'Bonus de Set']}
        rows={data.map((a: any) => [
          <span className="font-medium text-primary">{a.name}</span>,
          <Badge>{a.tier}</Badge>,
          a.head, a.chest, a.legs, a.feet,
          <span className="font-semibold text-primary">{a.total_def}</span>,
          <span className="text-xs text-accent">{a.set_bonus}</span>
        ])}
      />
    </div>
  );
}

function CraftingView({ data }: { data: any }) {
  if (!Array.isArray(data)) return null;
  const cats = [...new Set(data.map((r: any) => r.cat))] as string[];
  return (
    <div className="space-y-6">
      {cats.map(cat => {
        const items = data.filter((r: any) => r.cat === cat);
        return (
          <div key={cat}>
            <h3 className="text-lg font-semibold text-primary mb-2">{cat} <Badge>{items.length}</Badge></h3>
            <DataTable
              headers={['Objet', 'Station', 'Ingredients', 'Quantite']}
              rows={items.map((r: any) => [
                <span className="font-medium text-primary">{r.output}</span>,
                r.station,
                Object.entries(r.input).map(([k, v]) => `${v}× ${k}`).join(', '),
                r.count
              ])}
            />
          </div>
        );
      })}
    </div>
  );
}

function PotionsView({ data }: { data: any }) {
  if (!data) return null;
  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Herbes <Badge color="green">{data.herbs?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Biome', 'Condition', 'Usage']}
          rows={(data.herbs || []).map((h: any) => [
            <span className="font-medium text-primary">{h.name}</span>,
            h.biome,
            h.spawn_condition,
            h.desc
          ])}
        />
      </div>
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Potions <Badge color="purple">{data.potions?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Ingredients', 'Effet', 'Duree', 'Cooldown', 'Categorie']}
          rows={(data.potions || []).map((p: any) => [
            <span className="font-medium text-primary">{p.name}</span>,
            Object.entries(p.ingredients).map(([k, v]: any) => `${v}× ${k}`).join(', '),
            <span className="text-xs">{p.effect}</span>,
            p.duration,
            p.cooldown ? `${p.cooldown}s` : '-',
            <Badge color="blue">{p.cat}</Badge>
          ])}
        />
      </div>
    </div>
  );
}

function FurnitureView({ data }: { data: any }) {
  if (!Array.isArray(data)) return null;
  const cats = [...new Set(data.map((f: any) => f.cat))] as string[];
  return (
    <div className="space-y-6">
      {cats.map(cat => {
        const items = data.filter((f: any) => f.cat === cat);
        return (
          <div key={cat}>
            <h3 className="text-lg font-semibold text-primary mb-2">{cat} <Badge>{items.length}</Badge></h3>
            <DataTable
              headers={['Nom', 'Ingredients', 'Station']}
              rows={items.map((f: any) => [
                <span className="font-medium text-primary">{f.name}</span>,
                Object.entries(f.input).map(([k, v]) => `${v}× ${k}`).join(', '),
                f.station
              ])}
            />
          </div>
        );
      })}
    </div>
  );
}

function AccessoriesView({ data }: { data: any }) {
  if (!Array.isArray(data)) return null;
  return (
    <div>
      <h3 className="text-lg font-semibold text-primary mb-3">Accessoires & Combinaisons <Badge color="purple">{data.length}</Badge></h3>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {data.map((a: any) => (
          <Card key={a.id}>
            <div className="flex items-start justify-between mb-2">
              <h4 className="font-medium text-primary">{a.name}</h4>
            </div>
            <p className="text-xs text-secondary mb-1">Source: {a.input}</p>
            <p className="text-xs text-accent mb-1">Effet: {a.effect}</p>
            {a.combo && (
              <div className="mt-2 p-2 bg-accent/5 rounded border border-accent/10">
                <p className="text-xs font-medium text-accent">Combo: {a.combo.name}</p>
                <p className="text-xs text-secondary">Avec: {a.combo.combine_with}</p>
                <p className="text-xs text-accent">Resultat: {a.combo.result_effect}</p>
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}

function DroneView({ data }: { data: any }) {
  if (!data) return null;
  return (
    <div className="space-y-6">
      <Card>
        <h3 className="font-semibold text-primary mb-2">Vue d'ensemble</h3>
        <p className="text-sm text-secondary">{data.overview}</p>
      </Card>
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Modeles de drone</h3>
        <DataTable
          headers={['Modele', 'Slots', 'Batterie', 'Bouclier', 'Craft']}
          rows={Object.entries(data.hardware || {}).map(([key, val]: any) => [
            <span className="font-medium text-primary">{key}</span>,
            val.match(/d+ slots/)?.[0] || '-',
            val.match(/batterie d+/)?.[0] || val.match(/batterie : d+/)?.[0] || '-',
            val.match(/bouclier d+ PV/)?.[0] || '-',
            <span className="text-xs text-secondary">{val}</span>
          ])}
        />
      </div>
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Noeuds disponibles <Badge color="blue">{data.node_types?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Parametres', 'Cout', 'Description']}
          rows={(data.node_types || []).map((n: any) => [
            <span className="font-medium text-primary">{n.name}</span>,
            <code className="text-xs">{n.params}</code>,
            <Badge color="yellow">{n.cost}</Badge>,
            n.desc
          ])}
        />
      </div>
      {data.example_script && (
        <Card>
          <h3 className="font-semibold text-primary mb-2">Exemple de script</h3>
          <pre className="bg-black/30 rounded p-3 text-xs text-green-400 overflow-x-auto font-mono">
            {data.example_script.join('\n')}
          </pre>
        </Card>
      )}
    </div>
  );
}

function BossesView({ data }: { data: any }) {
  if (!data) return null;
  const bosses = data.bosses || [];
  const bossesReve = data.bosses_reve || [];
  return (
    <div className="space-y-8">
      {bosses.map((boss: any) => (
        <Card key={boss.id} className="space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-lg font-semibold text-primary">{boss.name}</h3>
            <div className="flex gap-2">
              <Badge color="red">Tier {boss.tier}</Badge>
              <Badge color="orange">PV {boss.hp}</Badge>
              <Badge>Def {boss.defense}</Badge>
            </div>
          </div>
          <p className="text-sm text-secondary">{boss.summon}</p>
          <p className="text-sm text-secondary">Arene: {boss.arena}</p>
          <p className="text-sm text-accent">Equipement recommande: {boss.recommended_gear}</p>
          {boss.phases.map((phase: any) => (
            <div key={phase.phase} className="border border-line rounded p-3 space-y-2">
              <h4 className="font-medium text-primary">Phase {phase.phase} <Badge color="yellow">{phase.trigger}</Badge></h4>
              <DataTable
                headers={['Attaque', 'Type', 'Degats', 'Hitbox', 'Freq.', 'Notes']}
                rows={phase.attacks.map((atk: any) => [
                  <span className="font-medium text-primary">{atk.name}</span>,
                  <Badge>{atk.type}</Badge>,
                  <span className="font-semibold text-red-400">{atk.damage}</span>,
                  atk.hitbox,
                  atk.frequency,
                  <span className="text-xs text-secondary">{atk.notes}</span>
                ])}
              />
            </div>
          ))}
          <div>
            <h4 className="font-medium text-primary mb-2">Butin</h4>
            <DataTable
              headers={['Objet', 'Chance', 'Quantite', 'Description']}
              rows={boss.loot.map((l: any) => [
                <span className="font-medium text-accent">{l.item}</span>,
                <span className="font-mono text-xs">{(l.chance * 100).toFixed(0)}%</span>,
                l.qty,
                l.desc
              ])}
            />
          </div>
          <p className="text-xs text-yellow-400 italic">Conseil: {boss.tips}</p>
        </Card>
      ))}
      {bossesReve.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold text-primary mb-3">Boss du Reve Astral <Badge color="purple">{bossesReve.length}</Badge></h3>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {bossesReve.map((b: any) => (
              <Card key={b.id}>
                <h4 className="font-medium text-primary">{b.name}</h4>
                <p className="text-sm text-secondary">PV: {b.hp}</p>
                <p className="text-sm text-secondary">Lien monde normal: {b.normal_world_link}</p>
                <p className="text-sm text-secondary">{b.desc}</p>
              </Card>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function NPCsView({ data }: { data: any }) {
  if (!Array.isArray(data)) return null;
  return (
    <div className="space-y-4">
      {data.map((npc: any) => (
        <Card key={npc.id}>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-lg font-semibold text-primary">{npc.name}</h3>
            <Badge color="green">{npc.spawn_condition}</Badge>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div>
              <h4 className="text-sm font-medium text-primary mb-2">Boutique</h4>
              <DataTable
                headers={['Objet', 'Prix', 'Qty']}
                rows={npc.shop.map((s: any) => [
                  <span className="font-medium text-primary">{s.item}</span>,
                  <span className="font-mono text-xs">{s.price} pieces</span>,
                  <span className="text-xs text-secondary">{s.qty}</span>
                ])}
              />
            </div>
            <div className="space-y-3">
              {npc.quests.map((q: any, i: number) => (
                <div key={i} className="border border-line rounded p-2">
                  <h4 className="text-sm font-medium text-accent">Quete: {q.name}</h4>
                  <p className="text-xs text-secondary">{q.desc}</p>
                  <p className="text-xs">Objectif: {q.obj}</p>
                  <p className="text-xs text-green-400">Recompense: {q.reward}</p>
                </div>
              ))}
              <div>
                <h4 className="text-sm font-medium text-primary mb-1">Dialogues</h4>
                {npc.dialogues.map((d: string, i: number) => (
                  <p key={i} className="text-xs text-secondary italic">"{d}"</p>
                ))}
              </div>
              <p className="text-xs text-secondary">Humeurs: {npc.moods.join(', ')}</p>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function EventsView({ data }: { data: any }) {
  if (!data) return null;
  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Meteo <Badge color="blue">{data.weather?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Duree', 'Effets', 'Frequence']}
          rows={(data.weather || []).map((w: any) => [
            <span className="font-medium text-primary">{w.name}</span>,
            w.duration,
            <span className="text-xs text-secondary">{w.effects}</span>,
            w.frequency
          ])}
        />
      </div>
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Saisons <Badge color="green">{data.seasons?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Duree', 'Effets']}
          rows={(data.seasons || []).map((s: any) => [
            <span className="font-medium text-primary">{s.name}</span>,
            s.duration,
            s.effects
          ])}
        />
      </div>
      <div>
        <h3 className="text-lg font-semibold text-primary mb-3">Invasions <Badge color="red">{data.invasions?.length || 0}</Badge></h3>
        <DataTable
          headers={['Nom', 'Declencheur', 'Vagues', 'Ennemis/Vague', 'Boss', 'Recompenses']}
          rows={(data.invasions || []).map((inv: any) => [
            <span className="font-medium text-primary">{inv.name}</span>,
            <span className="text-xs text-secondary">{inv.trigger}</span>,
            inv.waves,
            inv.enemies_per_wave,
            inv.boss,
            inv.rewards
          ])}
        />
      </div>
    </div>
  );
}

function JsonCardView({ data, title }: { data: any; title: string }) {
  if (!data) return null;
  return (
    <Card>
      <h3 className="font-semibold text-primary mb-3">{title}</h3>
      <pre className="bg-black/30 rounded p-3 text-xs text-secondary overflow-x-auto max-h-96 font-mono whitespace-pre-wrap">
        {JSON.stringify(data, null, 2)}
      </pre>
    </Card>
  );
}

function MetaView({ data }: { data: any }) {
  if (!data) return null;
  return (
    <Card className="space-y-4">
      <div className="flex items-center gap-3">
        <Star className="text-accent" size={32} />
        <div>
          <h2 className="text-2xl font-bold text-primary">{data.title}</h2>
          <p className="text-sm text-secondary">v{data.version} — {data.date} — {data.sections} sections</p>
        </div>
      </div>
      <p className="text-sm text-secondary leading-relaxed">{data.summary}</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
        {[
          ['Armes', data.total_weapons, 'red'],
          ['Minerais', data.total_ores, 'yellow'],
          ['Alliages', data.total_alloys, 'orange'],
          ['Armures', data.total_armor_sets, 'green'],
          ['Recettes', data.total_recipes, 'blue'],
          ['Potions', data.total_potions, 'purple'],
          ['Boss', data.total_bosses, 'red'],
          ['PNJ', data.total_npcs, 'green'],
        ].map(([label, count, color]) => (
          <div key={label as string} className="text-center p-2 bg-elevated rounded border border-line">
            <div className="text-lg font-bold text-primary">{count as number}</div>
            <div className={`text-xs text-${color}-400`}>{label}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

// --- Composant principal ---

export default function Component() {
  const [activeSection, setActiveSection] = useState<string>('full');
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const section = SECTIONS.find(s => s.id === activeSection) || SECTIONS[SECTIONS.length - 1];

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(`${API}${section.endpoint}`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(d => { if (!cancelled) { setData(d); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(e.message); setLoading(false); } });
    return () => { cancelled = true; };
  }, [activeSection]);

  const renderContent = () => {
    if (loading) return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="animate-spin text-accent" size={32} />
        <span className="ml-3 text-secondary">Chargement...</span>
      </div>
    );
    if (error) return (
      <Card className="text-center py-8">
        <p className="text-red-400">Erreur de chargement: {error}</p>
        <p className="text-xs text-secondary mt-1">Verifiez que le backend est lance sur {API}</p>
      </Card>
    );

    switch (activeSection) {
      case 'weapons': return <WeaponsView data={data} />;
      case 'ores': return <OresView data={data} />;
      case 'armor': return <ArmorView data={data} />;
      case 'crafting': return <CraftingView data={data} />;
      case 'potions': return <PotionsView data={data} />;
      case 'furniture': return <FurnitureView data={data} />;
      case 'accessories': return <AccessoriesView data={data} />;
      case 'drone': return <DroneView data={data} />;
      case 'bosses': return <BossesView data={data} />;
      case 'npcs': return <NPCsView data={data} />;
      case 'events': return <EventsView data={data} />;
      case 'full': return <MetaView data={data} />;
      default: return <JsonCardView data={data} title={section.label} />;
    }
  };

  return (
    <main className="flex flex-1 h-full overflow-hidden">
      {/* Sidebar */}
      <aside className={`${sidebarOpen ? 'w-56' : 'w-12'} transition-all duration-200 bg-elevated border-r border-line overflow-y-auto flex-shrink-0`}>
        <div className="flex items-center justify-between p-3 border-b border-line">
          {sidebarOpen && <h2 className="text-sm font-semibold text-primary flex items-center gap-1"><Star size={14} className="text-accent" /> Astral Earth</h2>}
          <button onClick={() => setSidebarOpen(!sidebarOpen)} className="text-secondary hover:text-primary p-1">
            {sidebarOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        </div>
        <nav className="py-2">
          {SECTIONS.map(s => (
            <button
              key={s.id}
              onClick={() => setActiveSection(s.id)}
              className={`w-full flex items-center gap-2 px-3 py-2 text-sm transition-colors ${activeSection === s.id ? 'bg-accent/10 text-accent border-r-2 border-accent' : 'text-secondary hover:text-primary hover:bg-elevated/50'}`}
            >
              <s.icon size={14} />
              {sidebarOpen && <span className="truncate">{s.label}</span>}
            </button>
          ))}
        </nav>
      </aside>

      {/* Content */}
      <section className="flex-1 overflow-y-auto px-6 py-6">
        <div className="max-w-6xl mx-auto space-y-4">
          {sidebarOpen && (
            <div className="flex items-center gap-2 mb-2">
              <section.icon size={18} className="text-accent" />
              <h2 className="text-xl font-semibold text-primary">{section.label}</h2>
            </div>
          )}
          {renderContent()}
        </div>
      </section>
    </main>
  );
}
