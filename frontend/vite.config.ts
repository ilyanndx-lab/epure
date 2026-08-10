import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  define: {
    'import.meta.env.VITE_BUILD_TIME': JSON.stringify(new Date().toLocaleTimeString('fr-FR')),
  },
  build: {
    // `_assets` et non le défaut `assets` : dans le paquet distribué, FastAPI
    // sert ce dossier et son préfixe d'URL est exempté d'authentification (la
    // page doit se charger avant que le JS puisse s'appairer). Or un id de
    // module valide est `[a-z][a-z0-9_]{1,30}` (module_workshop._ID_RE) et un
    // module monté sur le préfixe vide écrit ses routes à la main : un module
    // nommé `assets` aurait vu `/assets/*` exempté d'auth, et aurait pu être
    // masqué par le mount. L'underscore initial rend la collision impossible
    // par construction, et reprend la convention déjà en place côté source
    // (registry.ts exclut `./generated/_*/**`).
    assetsDir: '_assets',
  },
  server: {
    host: true,
    port: 5173,
  },
})