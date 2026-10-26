import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { AiConfig } from '../types'

export const DEFAULT_AI_CONFIG: AiConfig = {
  baseUrl: import.meta.env.VITE_AI_BASE_URL ?? 'http://localhost:20128/v1',
  apiKey: '',
  model: import.meta.env.VITE_AI_MODEL ?? '',
}

interface UiState {
  ai: AiConfig
  setAi: (patch: Partial<AiConfig>) => void
  resetAi: () => void
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      ai: DEFAULT_AI_CONFIG,
      setAi: (patch) => set((state) => ({ ai: { ...state.ai, ...patch } })),
      resetAi: () => set({ ai: DEFAULT_AI_CONFIG }),
    }),
    { name: 'clipforge_ai', version: 1 },
  ),
)
