import { Suspense, lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { Skeleton } from './components/Skeleton'
import { Overview } from './pages/Overview'

/**
 * Every page except the landing view is a separate lazy chunk.
 *
 * Why: the single-bundle build shipped ~950 kB of JS (the bulk of it recharts,
 * only used by /analytics) up front, so first paint waited on a library most
 * visits never render. With React.lazy each route loads on first navigation —
 * the build warning about the >500 kB chunk disappears and the initial payload
 * drops to the app shell + Overview.
 *
 * The Suspense fallback is a skeleton block, consistent with the app's loading
 * language elsewhere (ModelCatalog etc.), and aria-hidden per Skeleton's
 * contract.
 */
const ModelHealth = lazy(() =>
  import('./pages/ModelHealth').then((m) => ({ default: m.ModelHealth })),
)
const RoutingExplorer = lazy(() =>
  import('./pages/RoutingExplorer').then((m) => ({ default: m.RoutingExplorer })),
)
const Analytics = lazy(() =>
  import('./pages/Analytics').then((m) => ({ default: m.Analytics })),
)
const ApiKeys = lazy(() =>
  import('./pages/ApiKeys').then((m) => ({ default: m.ApiKeys })),
)
const ModelCatalog = lazy(() =>
  import('./pages/ModelCatalog').then((m) => ({ default: m.ModelCatalog })),
)
const ProviderKeys = lazy(() =>
  import('./pages/ProviderKeys').then((m) => ({ default: m.ProviderKeys })),
)

function PageFallback() {
  return (
    <div className="p-6" aria-busy="true">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="mt-4 h-48 w-full" />
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Overview />} />
          <Route
            path="/catalog"
            element={
              <Suspense fallback={<PageFallback />}>
                <ModelCatalog />
              </Suspense>
            }
          />
          <Route
            path="/models"
            element={
              <Suspense fallback={<PageFallback />}>
                <ModelHealth />
              </Suspense>
            }
          />
          <Route
            path="/explorer"
            element={
              <Suspense fallback={<PageFallback />}>
                <RoutingExplorer />
              </Suspense>
            }
          />
          <Route
            path="/analytics"
            element={
              <Suspense fallback={<PageFallback />}>
                <Analytics />
              </Suspense>
            }
          />
          <Route
            path="/keys"
            element={
              <Suspense fallback={<PageFallback />}>
                <ApiKeys />
              </Suspense>
            }
          />
          <Route
            path="/providers"
            element={
              <Suspense fallback={<PageFallback />}>
                <ProviderKeys />
              </Suspense>
            }
          />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
