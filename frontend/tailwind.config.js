/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        background: '#090A0C',
        surface: 'rgba(255, 255, 255, 0.035)',
        'surface-solid': '#121418',
        border: 'rgba(255, 255, 255, 0.07)',
        accent: '#E87722',
        gold: '#D4AF37',
        success: '#3DDC97',
        danger: '#FF4D4D',
        warning: '#F5A623',
        'text-primary': '#F4F5F7',
        'text-secondary': '#A7ABB4',
      },
      fontFamily: {
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      borderRadius: {
        '2xl': '1rem',
        '3xl': '1.25rem',
      },
      boxShadow: {
        card: '0 1px 0 rgba(255,255,255,0.03) inset, 0 12px 40px -24px rgba(0,0,0,0.8)',
        glow: '0 0 32px rgba(232,119,34,0.16)',
      },
      maxWidth: {
        content: '1440px',
      },
    },
  },
  plugins: [],
}
