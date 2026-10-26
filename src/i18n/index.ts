import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './en'
import id from './id'

export const LANGUAGE_STORAGE_KEY = 'clipforge_lang'

const saved = localStorage.getItem(LANGUAGE_STORAGE_KEY)
const detected = navigator.language.toLowerCase().startsWith('id') ? 'id' : 'en'
const initial = saved === 'id' || saved === 'en' ? saved : detected

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    id: { translation: id },
  },
  lng: initial,
  fallbackLng: 'en',
  supportedLngs: ['en', 'id'],
  interpolation: { escapeValue: false },
  returnNull: false,
})

i18n.on('languageChanged', (language) => {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, language)
  document.documentElement.lang = language
})

document.documentElement.lang = i18n.language

export default i18n
