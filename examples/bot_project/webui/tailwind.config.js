// tailwind.config.js - AI Chat UI 配色系统（Teal & Ember，见 DESIGN.md §2–§4）
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

        // 品牌（logo teal）+ ember 次级强调 + 语义色
        brand: v("brand"),
        "brand-deep": v("brand-deep"),
        "brand-bright": v("brand-bright"),
        "brand-soft": v("brand-soft"),
        ember: v("ember"),
        danger: v("danger"),

        // 状态反馈
        success: v("success"),
        warning: v("warning"),
        error: v("error"),

        // 任务面板独立强调色（indigo，区别于 brand teal）
        task: v("task"),
        "task-soft": v("task-soft"),

        // 审批严重级别（取色刻意降饱和，避免 dangerous/hardline 刺眼）
        severity: {
          normal: v("severity-normal"),
          sensitive: v("severity-sensitive"),
          dangerous: v("severity-dangerous"),
          hardline: v("severity-hardline"),
        },

        // 中性 token 阶梯（暖纸底 / 带 teal 底色的暖石墨底，mint-white 文字梯度）
        ink: v("ink"),
        bright: v("bright"),
        body: v("body"),
        mute: v("mute"),
        faint: v("faint"),
        hairline: v("hairline"),
        "border-strong": v("border-strong"),
        "hairline-strong": v("hairline-strong"),
        "hairline-soft": v("hairline-soft"),
        selection: v("selection"),
        canvas: v("canvas"),
        "canvas-sidebar": v("canvas-sidebar"),
        "canvas-elevated": v("canvas-elevated"),
        "canvas-popover": v("canvas-popover"),

        // 历史别名：link/primary/signal 在 index.css 里已指向 brand 系列，
        // 所有 *-link / *-primary / *-signal 类自动变为新 teal，无需改组件。
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
          prompts: v("cat-prompts"),
        },

        // Graph 可视化语义 token（graph PRD §7.3）— 全部映射 var()，
        // 不要加 /alpha 修饰用法（var() token 上不生成 CSS）。
        graph: {
          "node-fill": v("graph-node-fill"),
          "node-fill-done": v("graph-node-fill-done"),
          "dot-pending": v("graph-dot-pending"),
          "dot-canceled": v("graph-dot-canceled"),
          "node-border": v("graph-node-border"),
          "node-border-active": v("graph-node-border-active"),
          edge: v("graph-edge"),
          "edge-active": v("graph-edge-active"),
          arrow: v("graph-arrow"),
          "arrow-active": v("graph-arrow-active"),
          deliver: v("graph-deliver"),
          "deliver-glow": v("graph-deliver-glow"),
          "deliver-trail": v("graph-deliver-trail"),
          "active-ring": v("graph-active-ring"),
          "mini-node": v("graph-mini-node"),
          "mini-edge": v("graph-mini-edge"),
          "mini-start": v("graph-mini-start"),
          "mini-end": v("graph-mini-end"),
        },
      },
      fontFamily: {
        // Single-family system (DESIGN.md §3): Inter carries both display and
        // body roles — weight + tracking differentiate tiers, not family.
        // `display` is a semantic alias to Inter so existing `font-display`
        // usages keep working; components pair it with font-bold + tracking.
        display: [
          "Inter",
          "Noto Sans SC",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
        sans: [
          "Inter",
          "Noto Sans SC",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        // JetBrains Mono: code blocks, inline code, eyebrow labels, paths,
        // badges — the developer-tool monospace (DESIGN.md §3).
        mono: [
          "JetBrains Mono",
          "Noto Sans SC",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      // Type scale — 7 tiers, single source of truth in index.css :root
      // (DESIGN.md §3). Every text-[Npx] arbitrary value converges onto
      // these. Line-heights are paired per tier so `text-base` alone gives
      // the right leading without a separate leading-* class.
      fontSize: {
        xs: ["var(--text-xs)", { lineHeight: "var(--leading-tight)" }],
        sm: ["var(--text-sm)", { lineHeight: "var(--leading-snug)" }],
        base: ["var(--text-base)", { lineHeight: "var(--leading-snug)" }],
        md: ["var(--text-md)", { lineHeight: "var(--leading-relaxed)" }],
        lg: ["var(--text-lg)", { lineHeight: "var(--leading-tight)" }],
        xl: ["var(--text-xl)", { lineHeight: "var(--leading-tight)" }],
        "2xl": ["var(--text-2xl)", { lineHeight: "var(--leading-tight)" }],
      },
      lineHeight: {
        tight: "var(--leading-tight)",
        snug: "var(--leading-snug)",
        relaxed: "var(--leading-relaxed)",
        prose: "var(--leading-prose)",
      },
      letterSpacing: {
        tight: "var(--tracking-tight)",
        normal: "var(--tracking-normal)",
        wide: "var(--tracking-wide)",
        eyebrow: "var(--tracking-eyebrow)",
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
        // floating is a soft elevation shadow used by floating widgets
        // (TodoPanel pill). Kept as a literal rgba, not a var() token, because
        // it's a shadow value (not a color) and mirrors the rgba style of the
        // --shadow-* tokens in index.css; both themes use the same dark
        // shadow on a light/elevated surface. The card/popover shadows below
        // DO map to var() tokens because they differ per theme.
        floating:
          "0 2px 2px rgba(0,0,0,0.04), 0 8px 16px -4px rgba(0,0,0,0.08)",
        card: "var(--shadow-card)",
        "card-hover": "var(--shadow-card-hover)",
        popover: "var(--shadow-popover)",
      },
      transitionTimingFunction: {
        app: "var(--ease)",
        out: "var(--ease-out)",
        // Graph 动效（graph PRD §7.2）
        deliver: "var(--ease-deliver)",
        "ring-pulse": "var(--ease-ring-pulse)",
      },
      transitionDuration: {
        fast: "var(--dur-fast)",
        app: "var(--dur)",
        slow: "var(--dur-slow)",
        // Graph 动效（graph PRD §7.2）
        deliver: "var(--dur-deliver)",
        "ring-pulse": "var(--dur-ring-pulse)",
        layout: "var(--dur-layout)",
      },
    },
  },
  plugins: [],
};
