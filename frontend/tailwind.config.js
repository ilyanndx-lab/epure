/** @type {import('tailwindcss').Config} */

// Toutes les couleurs viennent de src/styles/tokens.css (variables CSS).
// Les triplets -rgb permettent les modificateurs d'opacité (ex: bg-accent/10).
const token = (name) => `rgb(var(--${name}-rgb) / <alpha-value>)`

export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        base:     token('bg-base'),
        surface:  token('bg-surface'),
        elevated: token('bg-elevated'),
        line:     token('border'),
        primary:   token('text-primary'),
        secondary: token('text-secondary'),
        muted:     token('text-muted'),
        accent: {
          DEFAULT: token('accent-primary'),
          hover:   token('accent-primary-hover'),
        },
        accent2: {
          DEFAULT: token('accent-secondary'),
          hover:   token('accent-secondary-hover'),
        },
        'on-accent': token('on-accent'),
        success: token('success'),
        warning: token('warning'),
        error:   token('error'),
        info:    token('info'),
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Cascadia Code', 'Consolas', 'monospace'],
      },
      fontSize: {
        xs:    ['12px', '1.5'],
        sm:    ['13px', '1.5'],
        base:  ['14px', '1.6'],
        lg:    ['16px', '1.5'],
        xl:    ['20px', '1.4'],
        '2xl': ['28px', '1.3'],
      },
      borderRadius: {
        sm:   'var(--radius-sm)',
        md:   'var(--radius-md)',
        lg:   'var(--radius-lg)',
        full: 'var(--radius-full)',
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
      },
      backgroundImage: {
        'gradient-primary': 'var(--gradient-primary)',
      },
      transitionDuration: {
        DEFAULT: '150ms',
      },
    },
  },
  plugins: [],
}
