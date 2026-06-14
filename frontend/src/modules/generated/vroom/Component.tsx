import { useState, useEffect } from 'react'
import { Car, Gamepad2, Settings, Coins } from 'lucide-react'
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
}

interface Track {
  id: string
  name: string
  difficulty: number
  length: number
}

export default function VroomModule(_props: SharedModuleProps) {
  const [activeTab, setActiveTab] = usePersistentState<string>('vroom.activeTab', 'play')
  const [selectedCar, setSelectedCar] = usePersistentState<string>('vroom.selectedCar', 'sport')
  const [selectedTrack, setSelectedTrack] = usePersistentState<string>('vroom.selectedTrack', 'forest')
  const [coins, setCoins] = usePersistentState<number>('vroom.coins', 100)
  const [progress, setProgress] = usePersistentState<number>('vroom.progress', 0)
  const [isGameRunning, setIsGameRunning] = useState(false)
  const [cars, setCars] = useState<Car[]>([])
  const [tracks, setTracks] = useState<Track[]>([])
  const [showSettings, setShowSettings] = useState(false)

  useEffect(() => {
    const fetchData = async () => {
      try {
        const carsRes = await fetch(`${API}/vroom/cars`)
        const tracksRes = await fetch(`${API}/vroom/tracks`)
        setCars(await carsRes.json().then(data => data.cars))
        setTracks(await tracksRes.json().then(data => data.tracks))
      } catch (error) {
        console.error('Erreur de chargement des données:', error)
      }
    }
    fetchData()
  }, [])

  const startGame = () => {
    setIsGameRunning(true)
    // Simulation de progression
    const interval = setInterval(() => {
      setProgress(prev => {
        if (prev >= 100) {
          clearInterval(interval)
          setIsGameRunning(false)
          setCoins(prevCoins => prevCoins + 50)
          return 100
        }
        return prev + 1
      })
    }, 100)
  }

  const saveProgress = async () => {
    try {
      await fetch(`${API}/vroom/save-progress`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ progress, coins, selectedCar, selectedTrack })
      })
    } catch (error) {
      console.error('Erreur de sauvegarde:', error)
    }
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
            <span className="text-sm">{coins}</span>
          </div>
        </div>
      </div>

      <Tabs
        value={activeTab}
        onChange={setActiveTab}
        tabs={[
          { key: 'play', label: 'Jouer', icon: <Gamepad2 size={16} /> },
          { key: 'garage', label: 'Garage', icon: <Car size={16} /> },
          { key: 'tracks', label: 'Pistes', icon: <Settings size={16} /> }
        ]}
      />

      {activeTab === 'play' && (
        <Card className="p-4">
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">Véhicule</label>
              <Select
                value={selectedCar}
                onValueChange={setSelectedCar}
                options={cars.map(car => ({ value: car.id, label: car.name }))}
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Piste</label>
              <Select
                value={selectedTrack}
                onValueChange={setSelectedTrack}
                options={tracks.map(track => ({ value: track.id, label: track.name }))}
              />
            </div>
            <Button
              onClick={startGame}
              disabled={isGameRunning}
              className="w-full"
            >
              {isGameRunning ? 'Course en cours...' : 'Démarrer la course'}
            </Button>
            {isGameRunning && (
              <div className="mt-4">
                <ProgressBar percent={progress} />
                <p className="text-sm text-secondary mt-2">
                  Progression: {progress}% - +50 pièces à la fin
                </p>
              </div>
            )}
          </div>
        </Card>
      )}

      {activeTab === 'garage' && (
        <Card className="p-4">
          <h2 className="text-lg font-medium mb-4">Garage</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {cars.map(car => (
              <div
                key={car.id}
                className={`p-3 rounded-md border ${selectedCar === car.id ? 'border-accent bg-elevated' : 'border-line'}`}
              >
                <h3 className="font-medium">{car.name}</h3>
                <div className="mt-2 space-y-1">
                  <div className="flex justify-between text-sm">
                    <span>Vitesse:</span>
                    <span>{car.speed}/10</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Accélération:</span>
                    <span>{car.acceleration}/10</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Maniabilité:</span>
                    <span>{car.handling}/10</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {activeTab === 'tracks' && (
        <Card className="p-4">
          <h2 className="text-lg font-medium mb-4">Pistes</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {tracks.map(track => (
              <div
                key={track.id}
                className={`p-3 rounded-md border ${selectedTrack === track.id ? 'border-accent bg-elevated' : 'border-line'}`}
              >
                <h3 className="font-medium">{track.name}</h3>
                <div className="mt-2 space-y-1">
                  <div className="flex justify-between text-sm">
                    <span>Difficulté:</span>
                    <span>{track.difficulty}/10</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span>Longueur:</span>
                    <span>{track.length}m</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Modal
        isOpen={showSettings}
        onClose={() => setShowSettings(false)}
      >
        <div className="space-y-4">
          <h2 className="text-lg font-medium mb-2">Paramètres</h2>
          <div className="flex items-center justify-between">
            <span>Contrôles tactiles</span>
            <input type="checkbox" className="toggle" />
          </div>
          <div className="flex items-center justify-between">
            <span>Effets sonores</span>
            <input type="checkbox" className="toggle" defaultChecked />
          </div>
          <div className="flex items-center justify-between">
            <span>Musique</span>
            <input type="checkbox" className="toggle" defaultChecked />
          </div>
          <Button onClick={saveProgress} className="w-full mt-4">
            Sauvegarder la progression
          </Button>
        </div>
      </Modal>
    </main>
  )
}
