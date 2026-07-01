import { useState, useEffect, useRef } from 'react'
import { Timer, Coffee, Play, Pause, RotateCcw } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { API, apiFetch } from '../../../api'

interface PomodoroSettings {
  work_duration: number
  short_break: number
  long_break: number
  cycles_before_long_break: number
}

/**
 * Composant du module minuteur.
 * Découvert automatiquement par registry.ts via import.meta.glob — aucun import
 * à écrire dans App.tsx / Sidebar.tsx.
 */
export default function MinuteurModule(_props: SharedModuleProps) {
  const [time, setTime] = useState(0)
  const [isRunning, setIsRunning] = useState(false)
  const [isPomodoroMode, setIsPomodoroMode] = useState(false)
  const [pomodoroSettings, setPomodoroSettings] = useState<PomodoroSettings | null>(null)
  const [pomodoroCycle, setPomodoroCycle] = useState(0)
  const [pomodoroPhase, setPomodoroPhase] = useState<'work' | 'short_break' | 'long_break'>('work')
  const timerRef = useRef<NodeJS.Timeout | null>(null)

  useEffect(() => {
    if (isPomodoroMode && pomodoroSettings === null) {
      fetchPomodoroSettings()
    }
  }, [isPomodoroMode])

  useEffect(() => {
    if (isRunning) {
      timerRef.current = setInterval(() => {
        setTime(prevTime => {
          const newTime = prevTime + 10
          if (isPomodoroMode && pomodoroSettings) {
            const phaseDuration = pomodoroPhase === 'work'
              ? pomodoroSettings.work_duration
              : pomodoroPhase === 'short_break'
                ? pomodoroSettings.short_break
                : pomodoroSettings.long_break

            if (newTime >= phaseDuration) {
              handlePomodoroPhaseChange()
              return 0
            }
          }
          return newTime
        })
      }, 10)
    } else if (timerRef.current) {
      clearInterval(timerRef.current)
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [isRunning, isPomodoroMode, pomodoroPhase, pomodoroSettings])

  const fetchPomodoroSettings = async () => {
    try {
      const res = await apiFetch(`${API}/minuteur/pomodoro-settings`)
      const data = await res.json()
      setPomodoroSettings(data)
    } catch (error) {
      console.error('Erreur lors de la récupération des paramètres Pomodoro:', error)
    }
  }

  const handlePomodoroPhaseChange = () => {
    if (pomodoroPhase === 'work') {
      setPomodoroCycle(prev => prev + 1)
      if (pomodoroCycle + 1 >= (pomodoroSettings?.cycles_before_long_break || 4)) {
        setPomodoroPhase('long_break')
        setPomodoroCycle(0)
      } else {
        setPomodoroPhase('short_break')
      }
    } else {
      setPomodoroPhase('work')
    }
  }

  const formatTime = (ms: number) => {
    const minutes = Math.floor(ms / 60000)
    const seconds = Math.floor((ms % 60000) / 1000)
    const centiseconds = Math.floor((ms % 1000) / 10)
    return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}:${centiseconds.toString().padStart(2, '0')}`
  }

  const startTimer = () => setIsRunning(true)
  const pauseTimer = () => setIsRunning(false)
  const resetTimer = () => {
    setIsRunning(false)
    setTime(0)
    if (isPomodoroMode) {
      setPomodoroPhase('work')
      setPomodoroCycle(0)
    }
  }

  const togglePomodoroMode = () => {
    if (isPomodoroMode) {
      setIsPomodoroMode(false)
      resetTimer()
    } else {
      setIsPomodoroMode(true)
    }
  }

  const getPhaseLabel = () => {
    switch (pomodoroPhase) {
      case 'work': return 'Travail'
      case 'short_break': return 'Pause courte'
      case 'long_break': return 'Pause longue'
      default: return ''
    }
  }

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8 gap-4">
      <div className="flex justify-between items-center">
        <h1 className="text-xl font-semibold text-primary flex items-center gap-2">
          <Timer size={18} className="text-accent" /> Minuteur
        </h1>
        <button
          onClick={togglePomodoroMode}
          className={`px-3 py-1 rounded-md text-sm flex items-center gap-1 ${isPomodoroMode ? 'bg-accent text-accent-foreground' : 'bg-secondary hover:bg-accent/80'}`}
        >
          <Coffee size={14} />
          Pomodoro
        </button>
      </div>

      {isPomodoroMode && pomodoroSettings && (
        <div className="text-sm text-secondary flex items-center gap-2">
          <span className="font-medium">{getPhaseLabel()}</span>
          <span>Cycle: {pomodoroCycle + 1}/{pomodoroSettings.cycles_before_long_break}</span>
        </div>
      )}

      <div className="text-4xl font-mono font-bold text-center py-6">
        {formatTime(time)}
      </div>

      <div className="flex justify-center gap-4">
        <button
          onClick={startTimer}
          className="px-4 py-2 rounded-md bg-green-500 text-white hover:bg-green-600 disabled:bg-green-300 flex items-center gap-2"
          disabled={isRunning}
        >
          <Play size={16} /> Démarrer
        </button>
        <button
          onClick={pauseTimer}
          className="px-4 py-2 rounded-md bg-yellow-500 text-white hover:bg-yellow-600 disabled:bg-yellow-300 flex items-center gap-2"
          disabled={!isRunning}
        >
          <Pause size={16} /> Pause
        </button>
        <button
          onClick={resetTimer}
          className="px-4 py-2 rounded-md bg-red-500 text-white hover:bg-red-600 flex items-center gap-2"
        >
          <RotateCcw size={16} /> Réinitialiser
        </button>
      </div>
    </main>
  )
}
