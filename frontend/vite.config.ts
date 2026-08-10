import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Chemin ABSOLU, exigé par rolldown : un remplacement relatif est accepté par la
// config puis échoue à la résolution (« rewrote … but was not an absolute path »),
// et le build s'arrête. Le dépôt est en ESM, donc pas de __dirname.
//
// Et en slashs AVANT, pas les antislashs que `fileURLToPath` rend sous Windows :
// le remplacement d'un alias `find` en RegExp passe par `String.replace`, où un
// antislash est un échappement. `C:\Users\…\src\components\…` y devient une
// séquence invalide et le build meurt sur « os error 123 » — un message qui ne
// nomme ni l'alias ni le fichier. Plateforme primaire oblige, ça se paie ici.
const ATELIER_ABSENT = fileURLToPath(
  new URL('./src/components/AtelierAbsent.tsx', import.meta.url),
).replace(/\\/g, '/')

export default defineConfig(({ mode }) => {
  // loadEnv et non process.env : c'est ce que verra le SOURCE via
  // import.meta.env (fichiers .env compris). Lire process.env ici et .env
  // là-bas ferait diverger le drapeau du code et le drapeau du bundler — et la
  // divergence serait silencieuse, l'Atelier restant dans le paquet.
  const env = loadEnv(mode, process.cwd(), '')
  const atelier = env.VITE_ATELIER !== '0'

  return {
    plugins: [react()],
    define: {
      'import.meta.env.VITE_BUILD_TIME': JSON.stringify(new Date().toLocaleTimeString('fr-FR')),
    },
    resolve: {
      // Paquet distribué : le composant de l'Atelier est remplacé par un module
      // vide. `src/atelier.ts` suffit à le rendre inatteignable, mais PAS à
      // l'exclure du paquet — rolldown émet un chunk pour tout `import()` qu'il
      // parse, y compris dans une branche morte. Mesuré : 26,1 ko de source de
      // l'Atelier écrits sur le disque, non référencés par l'index. Le
      // détournement vide le chunk au lieu de le supprimer, ce qui suffit :
      // ce qui ne doit pas partir, c'est le code.
      alias: atelier
        ? []
        // Le motif couvre le specifier ENTIER (`^.*`), et ce n'est pas
        // cosmétique : un alias `find` en RegExp ne remplace que la partie
        // appariée. Avec `/\/components\/Workshop$/`, le `..` de
        // `'../components/Workshop'` restait collé devant le chemin absolu de
        // remplacement, produisant `..C:/Users/…` — et rolldown échouait sur
        // « os error 123 », un message qui ne nomme ni l'alias ni le fichier.
        : [{ find: /^.*\/components\/Workshop$/, replacement: ATELIER_ABSENT }],
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
  }
})
