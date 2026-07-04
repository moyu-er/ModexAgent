// tailwind.config.js - AI Chat UI 配色系统
//
// 颜色单一真相源在 src/index.css 的 :root / .dark CSS 变量里（见 --color-*）。
// 这里每个 token 仅映射到对应的 var()，组件用单 class（如 bg-page-bg）即可，
// 浅/深色由 :root/.dark 自动翻转——不要再在此处写死 hex。
const v = (token) => `var(--color-${token})`;

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 背景层
        "page-bg": v("page-bg"),
        "content-bg": v("content-bg"),
        "sidebar-bg": v("sidebar-bg"),
        "sidebar-hover": v("sidebar-hover"),
        overlay: v("overlay"),

        // 文字颜色
        "text-primary": v("text-primary"),
        "text-body": v("text-body"),
        "text-secondary": v("text-secondary"),
        "text-disabled": v("text-disabled"),
        "text-link": v("text-link"),
        "ai-brand": v("ai-brand"),

        // 消息气泡
        "user-bubble": v("user-bubble"),
        "user-bubble-text": v("user-bubble-text"),
        "ai-bubble": v("ai-bubble"),
        "ai-bubble-text": v("ai-bubble-text"),
        "system-bubble": v("system-bubble"),
        "error-bubble": v("error-bubble"),

        // 输入框区域
        "input-bg": v("input-bg"),
        "input-border": v("input-border"),
        "input-focus": v("input-focus"),
        "input-placeholder": v("input-placeholder"),
        "send-btn": v("send-btn"),
        "send-btn-text": v("send-btn-text"),
        "send-btn-hover": v("send-btn-hover"),

        // 组件交互
        "btn-primary": v("btn-primary"),
        "btn-primary-text": v("btn-primary-text"),
        "btn-secondary": v("btn-secondary"),
        "btn-secondary-text": v("btn-secondary-text"),
        "btn-secondary-border": v("btn-secondary-border"),
        "icon-hover": v("icon-hover"),
        "dropdown-bg": v("dropdown-bg"),
        "dropdown-hover": v("dropdown-hover"),
        "dropdown-divider": v("dropdown-divider"),

        // 代码块
        "code-bg": v("code-bg"),
        "code-border": v("code-border"),
        "code-text": v("code-text"),
        "code-lineno": v("code-lineno"),
        "inline-code-bg": v("inline-code-bg"),
        "inline-code-text": v("inline-code-text"),

        // 引用与表格
        "quote-border": v("quote-border"),
        "quote-bg": v("quote-bg"),
        "table-header": v("table-header"),
        "table-border": v("table-border"),
        "table-hover": v("table-hover"),
        "list-marker": v("list-marker"),

        // 状态反馈
        success: v("success"),
        warning: v("warning"),
        error: v("error"),
        info: v("info"),
        loading: v("loading"),
        typing: v("typing"),

        // 审批严重级别（取色刻意降饱和，避免 dangerous/hardline 刺眼）
        severity: {
          normal: v("severity-normal"),
          sensitive: v("severity-sensitive"),
          dangerous: v("severity-dangerous"),
          hardline: v("severity-hardline"),
        },
        approve: v("approve"),
        deny: v("deny"),

        // TodoPanel iOS 风格专属配色
        task: {
          accent: v("task-accent"),
          "accent-2": v("task-accent-2"),
          surface: v("task-surface"),
          "surface-hover": v("task-surface-hover"),
          line: v("task-line"),
          text: v("task-text"),
          "text-muted": v("task-text-muted"),
          "text-faint": v("task-text-faint"),
        },

        // 边框分割线
        divider: v("divider"),
        "divider-weak": v("divider-weak"),
        "card-border": v("card-border"),
        shadow: v("shadow"),

        // 滚动条
        "scrollbar-track": "transparent",
        "scrollbar-thumb": v("scrollbar-thumb"),
        "scrollbar-thumb-hover": v("scrollbar-thumb-hover"),
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
