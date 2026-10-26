import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from '../components/Layout'
import AnalyzePage from '../pages/AnalyzePage'
import ProjectDetailPage from '../pages/ProjectDetailPage'
import StudioPage from '../pages/StudioPage'
import ProjectsPage from '../pages/ProjectsPage'
import SettingsPage from '../pages/SettingsPage'

export function AppRouter() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<AnalyzePage />} />
        <Route path="/studio" element={<StudioPage />} />
        <Route path="/studio/:projectId" element={<StudioPage />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:id" element={<ProjectDetailPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
