import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { Car, Gamepad2, Settings, Coins, Target } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { usePersistentState } from '../../../usePersistentState'
import { Button, Card, Select, Tabs, Modal } from '../../../components/ui'
import { API, apiFetch } from '../../../api'

/* ------------------------------------------------------------------ */
/*  Interfaces                                                        */
/* ------------------------------------------------------------------ */
interface Car {
  id: string
  name: string
  speed: number          // influences max speed
  acceleration: number   // influences acceleration force
  handling: number       // not used directly in this simple physics
  base_price: number
}

interface Track {
  id: string
  name: string
  difficulty: number   // influences terrain amplitude
  length: number       // track length in meters
}

/* ------------------------------------------------------------------ */
/*  Constants                                                         */
/* ------------------------------------------------------------------ */
const CANVAS_W = 800
const CANVAS_H = 400
const PIXEL_SCALE = 5           // px per meter
const GRAVITY = 9.8             // m/s²
const FRICTION_COEFF = 0.3      // 1/s
const BRAKE_DECEL = 20          // m/s² additional decel when brake pressed
const ENGINE_FACTOR = 60        // multiplies car.acceleration to get engine force (N)

/* ------------------------------------------------------------------ */
/*  Terrain helpers                                                   */
/* ------------------------------------------------------------------ */
function hashStr(str: string): number {
  let h = 0
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) - h) + str.charCodeAt(i)
    h |= 0
  }
  return Math.abs(h)
}

function generateTerrain(trackId: string, difficulty: number, trackLength: number) {
  const seed = hashStr(trackId)
  const step = 2
  const points: { x: number; y: number }[] = []
  const amp = 20 + difficulty * 8
  const freq1 = 0.02 + (seed % 100) * 0.0005
  const freq2 = 0.05 + ((seed >> 4) % 100) * 0.001
  const phase1 = (seed % 360) * (Math.PI / 180)
  const phase2 = ((seed >> 8) % 360) * (Math.PI / 180)

  for (let x = 0; x <= trackLength; x += step) {
    const y =
      amp * Math.sin(freq1 * x + phase1) +
      amp * 0.5 * Math.sin(freq2 * x + phase2)
    points.push({ x, y })
  }
  return points
}

/* ------------------------------------------------------------------ */
/*  Main component                                                    */
/* ------------------------------------------------------------------ */
export default function VroomModule(_props: SharedModuleProps) {
  /* ---- persistent app state ---- */
  const [activeTab, setActiveTab] = usePersistentState<string>('vroom.activeTab', 'play')
  const [selectedCarId, setSelectedCarId] = usePersistentState<string>('vroom.selectedCar', 'sport')
  const [selectedTrackId, setSelectedTrackId] = usePersistentState<string>('vroom.selectedTrack', 'forest')
  const [coins, setCoins] = usePersistentState<number>('vroom.coins', 100)
  const [ownedCars, setOwnedCars] = usePersistentState<string[]>('vroom.ownedCars', ['sport'])
  const [bestTimes, setBestTimes] = usePersistentState<{ [trackId: string]: number }>('vroom.bestTimes', {})

  /* ---- network data ---- */
  const [cars, setCars] = useState<Car[]>([])
  const [tracks, setTracks] = useState<Track[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  /* ---- race UI state ---- */
  const [isRaceActive, setIsRaceActive] = useState(false)
  const [gasPressed, setGasPressed] = useState(false)
  const [brakePressed, setBrakePressed] = useState(false)
  const [currentSpeed, setCurrentSpeed] = useState(0)
  const [currentDistance, setCurrentDistance] = useState(0)
  const [showRaceResult, setShowRaceResult] = useState(false)
  const [raceResult, setRaceResult] = useState<{ distance: number; coinsEarned: number } | null>(null)
  const [showSettings, setShowSettings] = useState(false)

  const canvasRef = useRef<HTMLCanvasElement>(null)

  /* ---- mutable refs for the game loop ---- */
  const isRaceActiveRef = useRef(false)
  const gasPressedRef = useRef(false)
  const brakePressedRef = useRef(false)
  const carXRef = useRef(0)
  const carSpeedRef = useRef(0)
  const carDistanceRef = useRef(0)
  const lastTimeRef = useRef(0)
  const terrainRef = useRef<{ x: number; y: number }[]>([])
  const currentCarRef = useRef<Car | null>(null)
  const currentTrackRef = useRef<Track | null>(null)
  const animationIdRef = useRef<number | null>(null)

  /* ------------------------------------------------------------------
   *  Fetch static data (cars & tracks) on mount
   * ------------------------------------------------------------------ */
  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        const [carsRes, tracksRes] = await Promise.all([
          apiFetch(`${API}/cars`),
          apiFetch(`${API}/tracks`),
        ])
        if (!carsRes.ok || !tracksRes.ok) throw new Error('Erreur lors du chargement des données')
        const carsData: Car[] = (await carsRes.json()).cars
        const tracksData: Track[] = (await tracksRes.json()).tracks
        setCars(carsData)
        setTracks(tracksData)
        setError(null)
      } catch (e) {
        console.error(e)
        setError('Impossible de charger les données.')
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

  /* ---- derived values ---- */
  const currentCar = useMemo(() => cars.find(c => c.id === selectedCarId), [cars, selectedCarId])
  const currentTrack = useMemo(() => tracks.find(t => t.id === selectedTrackId), [tracks, selectedTrackId])

  // sync refs
  useEffect(() => { currentCarRef.current = currentCar ?? null }, [currentCar])
  useEffect(() => { currentTrackRef.current = currentTrack ?? null }, [currentTrack])

  // generate terrain when track changes
  useEffect(() => {
    if (!currentTrack) return
    terrainRef.current = generateTerrain(currentTrack.id, currentTrack.difficulty, currentTrack.length)
  }, [currentTrack])

  /* ------------------------------------------------------------------
   *  Keyboard controls
   * ------------------------------------------------------------------ */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'ArrowUp' || e.key === 'w' || e.key === 'W') {
        e.preventDefault()
        setGasPressed(true)
        gasPressedRef.current = true
      }
      if (e.key === 'ArrowDown' || e.key === 's' || e.key === 'S') {
        e.preventDefault()
        setBrakePressed(true)
        brakePressedRef.current = true
      }
    }
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'ArrowUp' || e.key === 'w' || e.key === 'W') {
        e.preventDefault()
        setGasPressed(false)
        gasPressedRef.current = false
      }
      if (e.key === 'ArrowDown' || e.key === 's' || e.key === 'S') {
        e.preventDefault()
        setBrakePressed(false)
        brakePressedRef.current = false
      }
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [])

  /* ------------------------------------------------------------------
   *  Terrain helpers
   * ------------------------------------------------------------------ */
  const getHeight = useCallback((x: number): number => {
    const pts = terrainRef.current
    if (pts.length === 0) return 0
    if (x <= pts[0].x) return pts[0].y
    if (x >= pts[pts.length - 1].x) return pts[pts.length - 1].y
    let lo = 0, hi = pts.length - 1
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1
      if (pts[mid].x <= x) lo = mid
      else hi = mid
    }
    const t = (x - pts[lo].x) / (pts[hi].x - pts[lo].x)
    return pts[lo].y + t * (pts[hi].y - pts[lo].y)
  }, [])

  const getSlope = useCallback((x: number): number => {
    const pts = terrainRef.current
    if (pts.length < 2) return 0
    const dx = Math.max(1, pts[1].x - pts[0].x)
    const y0 = getHeight(x - dx)
    const y1 = getHeight(x + dx)
    return (y1 - y0) / (2 * dx)
  }, [getHeight])

  /* ------------------------------------------------------------------
   *  Draw a frame (canvas)
   * ------------------------------------------------------------------ */
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const w = CANVAS_W, h = CANVAS_H
    ctx.clearRect(0, 0, w, h)

    const carX = carXRef.current
    const carY = getHeight(carX)
    const cameraX = carX - w / (2 * PIXEL_SCALE)
    const cameraY = carY - h / (2 * PIXEL_SCALE)

    // sky gradient
    const skyGrad = ctx.createLinearGradient(0, 0, 0, h)
    skyGrad.addColorStop(0, '#1E3A8A')
    skyGrad.addColorStop(0.5, '#60A5FA')
    skyGrad.addColorStop(1, '#93C5FD')
    ctx.fillStyle = skyGrad
    ctx.fillRect(0, 0, w, h)

    // terrain
    const pts = terrainRef.current
    if (pts.length > 0) {
      ctx.beginPath()
      let first = true
      for (const p of pts) {
        const sx = (p.x - cameraX) * PIXEL_SCALE
        const sy = h / 2 + (cameraY - p.y) * PIXEL_SCALE
        if (first) { ctx.moveTo(sx, sy); first = false }
        else ctx.lineTo(sx, sy)
      }
      // close to bottom
      const lastX = (pts[pts.length - 1].x - cameraX) * PIXEL_SCALE
      ctx.lineTo(lastX, h)
      const firstX = (pts[0].x - cameraX) * PIXEL_SCALE
      ctx.lineTo(firstX, h)
      ctx.closePath()
      const groundGrad = ctx.createLinearGradient(0, h / 2, 0, h)
      groundGrad.addColorStop(0, '#4ADE80')
      groundGrad.addColorStop(1, '#166534')
      ctx.fillStyle = groundGrad
      ctx.fill()
      ctx.strokeStyle = '#064E3B'
      ctx.lineWidth = 2
      ctx.stroke()
    }

    // draw car
    const carScreenX = (carX - cameraX) * PIXEL_SCALE
    const carScreenY = h / 2 + (cameraY - carY) * PIXEL_SCALE
    const slope = getSlope(carX)
    const angle = Math.atan(slope)

    ctx.save()
    ctx.translate(carScreenX, carScreenY)
    ctx.rotate(angle)
    ctx.fillStyle = '#EF4444'
    ctx.fillRect(-20, -10, 40, 20)
    ctx.fillStyle = '#B91C1C'
    ctx.fillRect(-12, -22, 24, 14)
    ctx.fillStyle = '#60A5FA'
    ctx.fillRect(-8, -20, 16, 10)
    ctx.fillStyle = '#1F2937'
    ctx.fillRect(-24, -12, 8, 24)
    ctx.fillRect(16, -12, 8, 24)
    ctx.restore()

    // HUD
    ctx.fillStyle = 'white'
    ctx.font = 'bold 14px monospace'
    ctx.fillText(`Vitesse : ${carSpeedRef.current.toFixed(1)} m/s`, 12, 24)
    ctx.fillText(`Distance : ${carDistanceRef.current.toFixed(0)} m`, 12, 44)
  }, [getHeight, getSlope])

  /* ------------------------------------------------------------------
   *  Game loop
   * ------------------------------------------------------------------ */
  const gameLoop = useCallback((timestamp: number) => {
    if (!isRaceActiveRef.current) return
    if (!lastTimeRef.current) lastTimeRef.current = timestamp
    const dt = Math.min((timestamp - lastTimeRef.current) / 1000, 0.05)
    lastTimeRef.current = timestamp

    const car = currentCarRef.current
    const track = currentTrackRef.current
    if (!car || !track) return

    const slope = getSlope(carXRef.current)
    const sinTheta = slope / Math.sqrt(1 + slope * slope)

    // Physics
    const engineForce = gasPressedRef.current ? car.acceleration * ENGINE_FACTOR : 0
    const maxSpeed = car.speed * 4          // m/s
    let netAcceleration = (engineForce - FRICTION_COEFF * carSpeedRef.current - GRAVITY * sinTheta) / 1000

    if (brakePressedRef.current) {
      if (carSpeedRef.current > 0) {
        netAcceleration -= BRAKE_DECEL
      } else {
        netAcceleration = -BRAKE_DECEL // allow reverse braking
      }
    }

    carSpeedRef.current += netAcceleration * dt
    // clamping
    if (carSpeedRef.current < 0) carSpeedRef.current = 0
    if (carSpeedRef.current > maxSpeed) carSpeedRef.current = maxSpeed

    carXRef.current += carSpeedRef.current * dt
    if (carXRef.current < 0) { carXRef.current = 0; carSpeedRef.current = 0 }
    if (carXRef.current > track.length) { carXRef.current = track.length; carSpeedRef.current = 0 }

    if (carSpeedRef.current > 0) carDistanceRef.current += carSpeedRef.current * dt

    setCurrentSpeed(carSpeedRef.current)
    setCurrentDistance(carDistanceRef.current)

    draw()
    animationIdRef.current = requestAnimationFrame(gameLoop)
  }, [draw, getSlope])

  /* ------------------------------------------------------------------
   *  Start / End race
   * ------------------------------------------------------------------ */
  const startRace = () => {
    const track = currentTrackRef.current
    if (!track) return
    carXRef.current = 0
    carSpeedRef.current = 0
    carDistanceRef.current = 0
    lastTimeRef.current = 0
    setGasPressed(false)
    setBrakePressed(false)
    gasPressedRef.current = false
    brakePressedRef.current = false
    setCurrentSpeed(0)
    setCurrentDistance(0)
    setIsRaceActive(true)
    isRaceActiveRef.current = true
    animationIdRef.current = requestAnimationFrame(gameLoop)
  }

  const endRace = useCallback(async () => {
    if (!isRaceActiveRef.current) return
    isRaceActiveRef.current = false
    setIsRaceActive(false)
    setGasPressed(false)
    setBrakePressed(false)
    gasPressedRef.current = false
    brakePressedRef.current = false
    if (animationIdRef.current) {
      cancelAnimationFrame(animationIdRef.current)
      animationIdRef.current = null
    }
    lastTimeRef.current = 0

    const distance = carDistanceRef.current
    const track = currentTrackRef.current
    if (!track) return

    const coinsEarned = Math.max(10, Math.round(distance / 50 * track.difficulty))
    setCoins(prev => prev + coinsEarned)
    setRaceResult({ distance, coinsEarned })
    setShowRaceResult(true)

    try {
      await apiFetch(`${API}/save-progress`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          progress: Math.round(distance),
          coins: coins + coinsEarned,
          selectedCar: selectedCarId,
          selectedTrack: selectedTrackId,
          bestTime: null,
        }),
      })
    } catch (e) {
      console.error('Erreur de sauvegarde:', e)
    }
  }, [coins, selectedCarId, selectedTrackId])

  /* ------------------------------------------------------------------
   *  Buy a car (local logic)
   * ------------------------------------------------------------------ */
  const handleBuyCar = useCallback((carId: string) => {
    const car = cars.find(c => c.id === carId)
    if (!car) return
    if (ownedCars.includes(carId)) return
    if (coins < car.base_price) {
      alert('Pas assez de pièces !')
      return
    }
    setCoins(prev => prev - car.base_price)
    setOwnedCars(prev => [...prev, carId])
  }, [cars, ownedCars, coins, setCoins, setOwnedCars])

  /* ------------------------------------------------------------------
   *  Loading / Error states
   * ------------------------------------------------------------------ */
  if (loading) {
    return (
      <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
        <div className="flex justify-center items-center h-full">
          <p className="text-secondary">Chargement en cours...</p>
        </div>
      </main>
    )
  }

  if (error) {
    return (
      <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
        <div className="flex justify-center items-center h-full flex-col gap-2">
          <p className="text-destructive">{error}</p>
          <Button onClick={() => window.location.reload()}>Réessayer</Button>
        </div>
      </main>
    )
  }

  /* ------------------------------------------------------------------
   *  Render
   * ------------------------------------------------------------------ */
  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      {/* Header */}
      <div className="flex justify-between items-center">
        <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
          <Car size={18} className="text-accent" /> Vroom – Hillclimber
        </h1>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => setShowSettings(true)}>
            <Settings size={16} />
          </Button>
          <div className="flex items-center gap-1 bg-elevated px-2 py-1 rounded-md">
            <Coins size={14} className="text-yellow-500" />
            <span className="text-sm font-bold">{coins}</span>
          </div>
        </div>
      </div>

      <Tabs
        value={activeTab}
        onChange={setActiveTab}
        tabs={[
          { key: 'play', label: 'Jouer', icon: <Gamepad2 size={16} /> },
          { key: 'garage', label: 'Garage', icon: <Car size={16} /> },
          { key: 'tracks', label: 'Pistes', icon: <Target size={16} /> },
          { key: 'shop', label: 'Boutique', icon: <Coins size={16} /> },
        ]}
      />

      {/* PLAY TAB */}
      {activeTab === 'play' && (
        <Card className="p-4">
          <div className="space-y-4">
            {!isRaceActive ? (
              <>
                <div>
                  <label className="block text-sm font-medium mb-1">Véhicule</label>
                  <Select
                    value={selectedCarId}
                    onValueChange={setSelectedCarId}
                    options={cars
                      .filter(c => ownedCars.includes(c.id))
                      .map(c => ({ value: c.id, label: `${c.name} (${c.base_price} 💰)` }))
                    }
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">Piste</label>
                  <Select
                    value={selectedTrackId}
                    onValueChange={setSelectedTrackId}
                    options={tracks.map(t => ({ value: t.id, label: `${t.name} (Difficulté ${t.difficulty})` }))}
                  />
                </div>
                <Button onClick={startRace} className="w-full" disabled={ownedCars.length === 0}>
                  Démarrer la course
                </Button>
              </>
            ) : (
              <div className="flex flex-col gap-2">
                <canvas
                  ref={canvasRef}
                  width={CANVAS_W}
                  height={CANVAS_H}
                  className="border border-line rounded-md"
                />
                <div className="flex justify-between items-center gap-2">
                  <Button
                    variant="secondary"
                    onMouseDown={() => { setGasPressed(true); gasPressedRef.current = true }}
                    onMouseUp={() => { setGasPressed(false); gasPressedRef.current = false }}
                    onTouchStart={() => { setGasPressed(true); gasPressedRef.current = true }}
                    onTouchEnd={() => { setGasPressed(false); gasPressedRef.current = false }}
                    className="flex-1"
                  >
                    Accélérer (⬆)
                  </Button>
                  <Button
                    variant="secondary"
                    onMouseDown={() => { setBrakePressed(true); brakePressedRef.current = true }}
                    onMouseUp={() => { setBrakePressed(false); brakePressedRef.current = false }}
                    onTouchStart={() => { setBrakePressed(true); brakePressedRef.current = true }}
                    onTouchEnd={() => { setBrakePressed(false); brakePressedRef.current = false }}
                    className="flex-1"
                  >
                    Freiner (⬇)
                  </Button>
                </div>
                <div className="flex justify-between text-sm">
                  <span>Vitesse : {currentSpeed.toFixed(1)} m/s</span>
                  <span>Distance : {currentDistance.toFixed(0)} m</span>
                </div>
                <Button onClick={endRace} variant="destructive" className="w-full">
                  Arrêter la course
                </Button>
              </div>
            )}
          </div>
        </Card>
      )}

      {/* GARAGE TAB */}
      {activeTab === 'garage' && (
        <Card className="p-4">
          <h2 className="text-lg font-medium mb-4 flex items-center gap-2">
            <Car size={18} className="text-accent" /> Garage
            <span className="ml-auto text-sm font-bold flex items-center gap-1">
              <Coins size={14} className="text-yellow-500" /> {coins}
            </span>
          </h2>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">Véhicule sélectionné</label>
              <Select
                value={selectedCarId}
                onValueChange={setSelectedCarId}
                options={cars
                  .filter(c => ownedCars.includes(c.id))
                  .map(c => ({ value: c.id, label: c.name }))
                }
              />
            </div>
            {currentCar && (
              <div className="grid grid-cols-3 gap-2">
                <div className="bg-elevated p-2 rounded-md text-center">
                  <p className="text-xs text-secondary">Vitesse</p>
                  <p className="font-medium">{currentCar.speed}</p>
                </div>
                <div className="bg-elevated p-2 rounded-md text-center">
                  <p className="text-xs text-secondary">Accélération</p>
                  <p className="font-medium">{currentCar.acceleration}</p>
                </div>
                <div className="bg-elevated p-2 rounded-md text-center">
                  <p className="text-xs text-secondary">Direction</p>
                  <p className="font-medium">{currentCar.handling}</p>
                </div>
              </div>
            )}
          </div>
        </Card>
      )}

      {/* TRACKS TAB */}
      {activeTab === 'tracks' && (
        <Card className="p-4">
          <h2 className="text-lg font-medium mb-4 flex items-center gap-2">
            <Target size={18} className="text-accent" /> Pistes
          </h2>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">Piste sélectionnée</label>
              <Select
                value={selectedTrackId}
                onValueChange={setSelectedTrackId}
                options={tracks.map(t => ({ value: t.id, label: t.name }))}
              />
            </div>
            {currentTrack && (
              <div className="grid grid-cols-2 gap-2">
                <div className="bg-elevated p-2 rounded-md text-center">
                  <p className="text-xs text-secondary">Difficulté</p>
                  <p className="font-medium">{currentTrack.difficulty}</p>
                </div>
                <div className="bg-elevated p-2 rounded-md text-center">
                  <p className="text-xs text-secondary">Longueur</p>
                  <p className="font-medium">{currentTrack.length} m</p>
                </div>
                {bestTimes[currentTrack.id] && (
                  <div className="col-span-2 bg-elevated p-2 rounded-md text-center">
                    <p className="text-xs text-secondary">Meilleur temps</p>
                    <p className="font-medium">{bestTimes[currentTrack.id].toFixed(2)} s</p>
                  </div>
                )}
              </div>
            )}
          </div>
        </Card>
      )}

      {/* SHOP TAB */}
      {activeTab === 'shop' && (
        <Card className="p-4">
          <h2 className="text-lg font-medium mb-4 flex items-center gap-2">
            <Coins size={18} className="text-accent" /> Boutique
            <span className="ml-auto text-sm font-bold flex items-center gap-1">
              <Coins size={14} className="text-yellow-500" /> {coins}
            </span>
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {cars.map(car => (
              <div key={car.id} className="bg-elevated p-3 rounded-md">
                <h3 className="font-medium">{car.name}</h3>
                <div className="grid grid-cols-3 gap-2 my-2">
                  <div className="text-center">
                    <p className="text-xs text-secondary">Vitesse</p>
                    <p className="font-medium">{car.speed}</p>
                  </div>
                  <div className="text-center">
                    <p className="text-xs text-secondary">Accél.</p>
                    <p className="font-medium">{car.acceleration}</p>
                  </div>
                  <div className="text-center">
                    <p className="text-xs text-secondary">Dir.</p>
                    <p className="font-medium">{car.handling}</p>
                  </div>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-sm font-bold flex items-center gap-1">
                    <Coins size={12} className="text-yellow-500" /> {car.base_price}
                  </span>
                  {ownedCars.includes(car.id) ? (
                    <Button size="sm" disabled>Possédé</Button>
                  ) : (
                    <Button
                      size="sm"
                      onClick={() => handleBuyCar(car.id)}
                      disabled={coins < car.base_price}
                    >
                      Acheter
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Race Result Modal */}
      <Modal open={showRaceResult} onClose={() => setShowRaceResult(false)}>
        <div className="p-6">
          <h3 className="text-lg font-medium mb-4">Résultat de la course</h3>
          {raceResult && (
            <div className="space-y-3">
              <div className="flex justify-between">
                <span>Distance parcourue :</span>
                <span className="font-medium">{raceResult.distance.toFixed(0)} m</span>
              </div>
              <div className="flex justify-between">
                <span>Pièces gagnées :</span>
                <span className="font-medium flex items-center gap-1">
                  <Coins size={14} className="text-yellow-500" /> {raceResult.coinsEarned}
                </span>
              </div>
            </div>
          )}
        </div>
      </Modal>

      {/* Settings Modal */}
      <Modal open={showSettings} onClose={() => setShowSettings(false)}>
        <div className="p-6">
          <h3 className="text-lg font-medium mb-4">Paramètres</h3>
          <p className="text-sm text-secondary">Paramètres du jeu à venir...</p>
        </div>
      </Modal>
    </main>
  )
}
