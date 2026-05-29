import { useState } from 'react'
import Sidebar from './components/Sidebar'
import Chat from './components/Chat'
import Kholle from './components/Kholle'

type Module = 'chat' | 'kholle'

export default function App() {
  const [activeModule, setActiveModule] = useState<Module>('chat')

  return (
    <div className="flex h-screen w-full bg-[#0d0d0d] text-[#e0e0e0] overflow-hidden">
      <Sidebar activeModule={activeModule} onModuleChange={setActiveModule} />
      {activeModule === 'chat' ? <Chat /> : <Kholle />}
    </div>
  )
}
