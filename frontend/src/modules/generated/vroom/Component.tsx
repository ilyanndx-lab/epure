import { useState, useEffect, useRef, useCallback } from 'react'
import { Car, Gamepad2, Settings, Coins, Target } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { usePersistentState } from '../../../usePersistentState'
import { Button, Card, Select, Tabs, ProgressBar, Modal } from '../../../components/ui'

const API = 'http://localhost:8000'

interface Car {
  id: string
  name: string
  speed: number
  acceleration: number
  handling: number
  base_price: number
}

interface Track {
  id: string
  name: string
  difficulty: number
  length: number
}

interface PlayerProgress {
  coins: number
  selectedCar: string
  selectedTrack: string
  bestTimes: { [trackId: string]: number }
}

const CANVAS_WIDTH = 800
const CANVAS_HEIGHT = 400

export default function VroomModule(_props: SharedModuleProps) {
  const [activeTab, setActiveTab] = usePersistentState<string>('vroom.activeTab', 'play')
  const [selectedCarId, setSelectedCarId] = usePersistentState<string>('vroom.selectedCar', 'sport')
  const [selectedTrackId, setSelectedTrackId] = usePersistentState<string>('vroom.selectedTrack', 'forest')
  const [coins, setCoins] = usePersistentState<number>('vroom.coins', 100)
  const [bestTimes, setBestTimes] = usePersistentState<{ [trackId: string]: number }>('vroom.bestTimes', {})

  const [cars, setCars] = useState<Car[]>([])
  const [tracks, setTracks] = useState<Track[]>([])
  const [showSettings, setShowSettings] = useState(false)
  const [showRaceResult, setShowRaceResult] = useState(false)
  const [raceResult, setRaceResult] = useState<{ time: number, position: number, coinsEarned: number } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Game state
  const [isRaceActive, setIsRaceActive] = useState(false)
  const [raceStartTime, setRaceStartTime] = useState(0)
  const [raceElapsedTime, setRaceElapsedTime] = useState(0)

  const canvasRef = useRef<HTMLCanvasElement>(null)
  const animationFrameId = useRef<number | null>(null)

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        const carsRes = await fetch(`${API}/cars`)
        const tracksRes = await fetch(`${API}/tracks`)
        const progressRes = await fetch(`${API}/player-progress`)

        if (!carsRes.ok || !tracksRes.ok || !progressRes.ok) {
          throw new Error('Erreur de chargement des données')
        }

        const carsData = await carsRes.json().then(data => data.cars)
        const tracksData = await tracksRes.json().then(data => data.tracks)
        const progressData: PlayerProgress = await progressRes.json()

        setCars(carsData)
        setTracks(tracksData)
        setCoins(progressData.coins)
        setSelectedCarId(progressData.selectedCar)
        setSelectedTrackId(progressData.selectedTrack)
        setBestTimes(progressData.bestTimes)
        setError(null)
      } catch (error) {
        console.error('Erreur de chargement des données:', error)
        setError('Impossible de charger les données. Veuillez réessayer.')
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

  const currentCar = cars.find(c => c.id === selectedCarId)
  const currentTrack = tracks.find(t => t.id === selectedTrackId)

  const startRace = () => {
    if (!currentCar || !currentTrack) return
    setIsRaceActive(true)
    setRaceStartTime(Date.now())
    setRaceElapsedTime(0)
    animationFrameId.current = requestAnimationFrame(gameLoop)
  }

  const endRace = useCallback(async () => {
    if (!isRaceActive) return

    setIsRaceActive(false)
    if (animationFrameId.current) {
      cancelAnimationFrame(animationFrameId.current)
      animationFrameId.current = null
    }

    const finalTime = raceElapsedTime / 1000
    const trackId = selectedTrackId
    const currentBest = bestTimes[trackId] || Infinity
    let coinsEarned = 0
    let newBestTime = false

    if (finalTime < currentBest) {
      coinsEarned = Math.max(50, Math.round(currentTrack!.difficulty * 1000 / finalTime))
      setCoins(prev => prev + coinsEarned)
      setBestTimes(prev => ({ ...prev, [trackId]: finalTime }))
      newBestTime = true
    } else {
      coinsEarned = Math.round(currentTrack!.difficulty * 50)
      setCoins(prev => prev + coinsEarned)
    }

    setRaceResult({ time: finalTime, position: 1, coinsEarned })
    setShowRaceResult(true)

    try {
      await fetch(`${API}/save-progress`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          progress: 100,
          coins: coins + coinsEarned,
          selectedCar: selectedCarId,
          selectedTrack: trackId,
          bestTime: newBestTime ? finalTime : undefined
        })
      })
    } catch (error) {
      console.error('Erreur de sauvegarde:', error)
    }
  }, [isRaceActive, raceElapsedTime, selectedTrackId, currentTrack, coins, bestTimes, selectedCarId])

  const gameLoop = useCallback(() => {
    if (!canvasRef.current || !isRaceActive) return

    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')!

    // Simple game loop for demonstration
    setRaceElapsedTime(Date.now() - raceStartTime)

    // Draw a simple race track
    ctx.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
    ctx.fillStyle = '#4CAF50'
    ctx.fillRect(0, CANVAS_HEIGHT / 2 - 20, CANVAS_WIDTH, 40)

    // Draw car
    ctx.fillStyle = 'red'
    ctx.fillRect(CANVAS_WIDTH / 4, CANVAS_HEIGHT / 2 - 10, 40, 20)

    animationFrameId.current = requestAnimationFrame(gameLoop)
  }, [isRaceActive, raceStartTime])

  const handleBuyCar = async (carId: string) => {
    try {
      await fetch(`${API}/buy-car`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ car_id: carId })
      })
      // Refresh player progress
      const progressRes = await fetch(`${API}/player-progress`)
      const progressData: PlayerProgress = await progressRes.json()
      setCoins(progressData.coins)
      alert(`Vous avez acheté la voiture !`)
    } catch (error: any) {
      alert(`Erreur: ${error.message || 'Impossible d\'acheter la voiture.'}`)
    }
  }

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
        <div className="flex justify-center items-center h-full">
          <p className="text-destructive">{error}</p>
          <Button onClick={() => window.location.reload()}>Réessayer</Button>
        </div>
      </main>
    )
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <div className="flex justify-between items-center">
        <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
          <Car size={18} className="text-accent" /> Vroom - Course 2D
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
          { key: 'shop', label: 'Boutique', icon: <Coins size={16} /> }
        ]}
      />

      {activeTab === 'play' && (
        <Card className="p-4">
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">Véhicule</label>
              <Select
                value={selectedCarId}
                onValueChange={setSelectedCarId}
                options={cars.map(car => ({ value: car.id, label: `${car.name} (${car.base_price} 💰)` }))}
                disabled={isRaceActive}
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Piste</label>
              <Select
                value={selectedTrackId}
                onValueChange={setSelectedTrackId}
                options={tracks.map(track => ({ value: track.id, label: `${track.name} (Diff: ${track.difficulty})` }))}
                disabled={isRaceActive}
              />
            </div>
            {!isRaceActive ? (
              <Button onClick={startRace} className="w-full">
                Démarrer la course
              </Button>
            ) : (
              <div className="flex flex-col gap-2">
                <canvas
                  ref={canvasRef}
                  width={CANVAS_WIDTH}
                  height={CANVAS_HEIGHT}
                  className="border border-line rounded-md"
                />
                <ProgressBar percent={Math.min(100, (raceElapsedTime / 1000) / 60 * 100)} />
                <p className="text-sm text-secondary">Temps: {(raceElapsedTime / 1000).toFixed(2)}s</p>
                <Button onClick={endRace} variant="destructive" className="w-full">
                  Abandonner la course
                </Button>
              </div>
            )}
          </div>
        </Card>
      )}

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
                options={cars.map(car => ({ value: car.id, label: car.name }))}
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
                options={tracks.map(track => ({ value: track.id, label: track.name }))}
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
                  <p className="font-medium">{currentTrack.length}m</p>
                </div>
                {bestTimes[currentTrack.id] && (
                  <div className="col-span-2 bg-elevated p-2 rounded-md text-center">
                    <p className="text-xs text-secondary">Meilleur temps</p>
                    <p className="font-medium">{bestTimes[currentTrack.id].toFixed(2)}s</p>
                  </div>
                )}
              </div>
            )}
          </div>
        </Card>
      )}

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
                    <p className="text-xs text-secondary">Accélération</p>
                    <p className="font-medium">{car.acceleration}</p>
                  </div>
                  <div className="text-center">
                    <p className="text-xs text-secondary">Direction</p>
                    <p className="font-medium">{car.handling}</p>
                  </div>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-sm font-bold flex items-center gap-1">
                    <Coins size={12} className="text-yellow-500" /> {car.base_price}
                  </span>
                  <Button
                    size="sm"
                    onClick={() => handleBuyCar(car.id)}
                    disabled={coins < car.base_price || selectedCarId === car.id}
                  >
                    {selectedCarId === car.id ? 'Déjà possédé' : 'Acheter'}
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Modal open={showRaceResult} onClose={() => setShowRaceResult(false)}>
        <div className="p-6">
          <h3 className="text-lg font-medium mb-4">Résultat de la course</h3>
          {raceResult && (
            <div className="space-y-3">
              <div className="flex justify-between">
                <span>Temps:</span>
                <span className="font-medium">{raceResult.time.toFixed(2)}s</span>
              </div>
              <div className="flex justify-between">
                <span>Position:</span>
                <span className="font-medium">{raceResult.position}</span>
              </div>
              <div className="flex justify-between">
                <span>Pièces gagnées:</span>
                <span className="font-medium flex items-center gap-1">
                  <Coins size={14} className="text-yellow-500" /> {raceResult.coinsEarned}
                </span>
              </div>
            </div>
          )}
        </div>
      </Modal>

      <Modal open={showSettings} onClose={() => setShowSettings(false)}>
        <div className="p-6">
          <h3 className="text-lg font-medium mb-4">Paramètres</h3>
          <p className="text-sm text-secondary">Paramètres du jeu à venir...</p>
        </div>
      </Modal>
    </main>
  )
}
