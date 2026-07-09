// tailwind.config.js - AI Chat UI 配色系统
//
// 颜色单一真相源在 src/index.css 的 :root / .dark CSS 变量里（见 --color-*）。
// 这里每个 token 仅映射到对应的 var()，组件用单 class（如 bg-canvas）即可，
// 浅/深色由 :root/.dark 自动翻转——不要再在此处写死 hex。
const v = (token) => `var(--color-${token})`;

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 背景层
        overlay: v("overlay"),

        // 消息气泡
        "user-bubble": v("user-bubble"),
        "user-bubble-text": v("user-bubble-text"),

        // 代码块
        "inline-code-bg": v("inline-code-bg"),
        "inline-code-text": v("inline-code-text"),

        // 状态反馈
        success: v("success"),
        warning: v("warning"),
        error: v("error"),

        // 审批严重级别（取色刻意降饱和，避免 dangerous/hardline 刺眼）
        severity: {
          normal: v("severity-normal"),
          sensitive: v("severity-sensitive"),
          dangerous: v("severity-dangerous"),
          hardline: v("severity-hardline"),
        },

        // 中性 token 阶梯（loom 风格：暖白底 / 近黑底，zinc 文字梯度）
        ink: v("ink"),
        bright: v("bright"),
        body: v("body"),
        mute: v("mute"),
        faint: v("faint"),
        hairline: v("hairline"),
        "hairline-strong": v("hairline-strong"),
        "hairline-soft": v("hairline-soft"),
        canvas: v("canvas"),
        "canvas-sidebar": v("canvas-sidebar"),
        "canvas-elevated": v("canvas-elevated"),

        // 强调色（emerald 体系；link 复用为 primary/signal，使所有 *-link 类自动转翡翠绿）
        link: v("link"),
        "link-deep": v("link-deep"),
        "link-soft": v("link-soft"),
        primary: v("primary"),
        signal: v("signal"),
        accent: v("accent"),

        // 每个配置大类的独立强调色（解决「mcp 等缺少图案」）
        cat: {
          mcp: v("cat-mcp"),
          pools: v("cat-pools"),
          skills: v("cat-skills"),
          models: v("cat-models"),
          im: v("cat-im"),
        },
      },
      fontFamily: {
        sans: [
          "Geist",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "Geist Mono",
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      borderRadius: {
        xs: "var(--radius-xs)",
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
        pill: "var(--radius-pill)",
        full: "var(--radius-full)",
      },
      boxShadow: {
        floating:
          "0 2px 2px rgba(0,0,0,0.04), 0 8px 16px -4px rgba(0,0,0,0.08)",
        card: "var(--shadow-card)",
        "card-hover": "var(--shadow-card-hover)",
        popover: "var(--shadow-popover)",
      },
      transitionTimingFunction: {
        app: "var(--ease)",
      },
      transitionDuration: {
        app: "var(--dur)",
      },
    },
  },
  plugins: [],
};
