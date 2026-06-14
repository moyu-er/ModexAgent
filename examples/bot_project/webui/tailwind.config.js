/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // Soft warm-tinted Material elevation scale. Lighter and airier than
        // the previous near-black; each step is a solid flat tone with clearly
        // visible gaps for strong panel distinction.
        ink: {
          950: "#151519",
          900: "#1C1C21",
          850: "#22222A",
          800: "#2A2A34",
          750: "#343440",
          700: "#40404C",
          600: "#4E4E5A",
        },
        // Soft periwinkle accent — less saturated than Tailwind indigo-500,
        // friendlier against warm dark surfaces.
        brand: {
          400: "#A0A7FF",
          500: "#8B8FF7",
          600: "#7378E6",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
