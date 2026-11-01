import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { Overview } from './pages/Overview'
import { ModelHealth } from './pages/ModelHealth'
import { RoutingExplorer } from './pages/RoutingExplorer'
import { Analytics } from './pages/Analytics'
import { ApiKeys } from './pages/ApiKeys'
import { ModelCatalog } from './pages/ModelCatalog'
import { ProviderKeys } from './pages/ProviderKeys'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Overview />} />
          <Route path="/catalog" element={<ModelCatalog />} />
          <Route path="/models" element={<ModelHealth />} />
          <Route path="/explorer" element={<RoutingExplorer />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route path="/keys" element={<ApiKeys />} />
          <Route path="/providers" element={<ProviderKeys />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
