import { Outlet } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { ChaosBanner } from './ChaosBanner'

export function Layout() {
  return (
    <div className="min-h-screen bg-background">
      <Sidebar />
      <div className="lg:ml-64 flex flex-col min-h-screen">
        <Topbar />
        <ChaosBanner />
        <main className="flex-1 p-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
