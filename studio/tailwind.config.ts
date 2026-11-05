import type { Config } from "tailwindcss";

/**
 * Studio Tailwind config. Tokens mirror the public website + @bird/dashboard-ui
 * so studio-rendered dashboards look identical to published ones. Colors are
 * driven by CSS variables (HSL channels) defined in globals.css.
 */
const config: Config = {
  darkMode: "class",
  content: [
    "./src/**/*.{ts,tsx}",
    // Pull in the workspace component library so its utility classes survive purge.
    "../packages/dashboard-ui/src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        border: "hsl(var(--border))",
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--foreground))",
        },
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        // The indeterminate progress sweep (EvidenceDock's run banner while a
        // step is still booting): a 1/3-width segment gliding left to right.
        "dock-sweep": {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(300%)" },
        },
      },
      animation: {
        "dock-sweep": "dock-sweep 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
