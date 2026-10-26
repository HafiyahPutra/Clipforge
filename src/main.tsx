import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Fonts are bundled, not fetched from a CDN (DESIGN.md §4) — offline work and a
// reproducible Docker image both depend on it.
import '@fontsource-variable/bricolage-grotesque'
import '@fontsource-variable/instrument-sans'
import { Providers } from './app/providers'
import { AppRouter } from './app/router'
import './index.css'

const container = document.getElementById('root')

if (!container) {
  throw new Error('Root element #root not found')
}

createRoot(container).render(
  <StrictMode>
    <Providers>
      <AppRouter />
    </Providers>
  </StrictMode>,
)
