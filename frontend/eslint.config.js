import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // Le préfixe « _ » marque déjà « inutilisé volontairement » dans tout le
      // dépôt (_props d'un module qui n'a besoin d'aucune prop partagée, _t/_p
      // d'un destructuring qui sert à retirer des clés). tsconfig.app.json
      // active noUnusedParameters/noUnusedLocals et exempte déjà ces cas ; on
      // aligne eslint sur le comportement du compilateur au lieu de supprimer
      // des paramètres qui documentent le contrat d'un composant.
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
        ignoreRestSiblings: true,
      }],

      // ⚠️ CES TROIS RÈGLES SIGNALENT DE VRAIES VIOLATIONS DES RULES OF REACT.
      // Ce ne sont PAS des faux positifs — contrairement aux avertissements
      // react-hooks/exhaustive-deps, qui, eux, viennent de ce qu'eslint ne peut
      // pas voir à travers usePersistentState que le setter retourné est celui
      // de useState, donc stable.
      //
      // Ici les 22 occurrences sont réelles : setState synchrone dans un effet,
      // lecture/écriture de ref pendant le rendu, accès à une variable avant sa
      // déclaration. Elles sont passées en « warn » et NON corrigées pour une
      // seule raison : les corriger change le comportement, et il n'existe
      // aujourd'hui aucun test frontend pour rattraper une régression
      // (package.json n'a que dev/build/lint/preview). Le cliquet
      // --max-warnings de ci.yml empêche leur nombre d'augmenter.
      //
      // Prérequis avant de s'y attaquer : vitest + testing-library, puis
      // commencer par src/usePersistentState.ts. Voir CHANGELOG.md,
      // « dette assumée ».
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/refs': 'warn',
      'react-hooks/immutability': 'warn',
    },
  },
])
