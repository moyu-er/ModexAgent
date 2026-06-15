// tailwind.config.js - AI Chat UI 配色系统
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 背景层
        "page-bg": {
          light: "#FAFAFA",
          dark: "#0D0D0D",
        },
        "content-bg": {
          light: "#FFFFFF",
          dark: "#141414",
        },
        "sidebar-bg": {
          light: "#F5F5F5",
          dark: "#111111",
        },
        "sidebar-hover": {
          light: "#EBEBEB",
          dark: "#1A1A1A",
        },
        overlay: {
          light: "rgba(0,0,0,0.4)",
          dark: "rgba(0,0,0,0.6)",
        },

        // 文字颜色
        "text-primary": {
          light: "#111827",
          dark: "#F9FAFB",
        },
        "text-body": {
          light: "#374151",
          dark: "#E5E7EB",
        },
        "text-secondary": {
          light: "#6B7280",
          dark: "#9CA3AF",
        },
        "text-disabled": {
          light: "#9CA3AF",
          dark: "#4B5563",
        },
        "text-link": {
          light: "#2563EB",
          dark: "#60A5FA",
        },
        "ai-brand": {
          light: "#2563EB",
          dark: "#60A5FA",
        },

        // 消息气泡
        "user-bubble": {
          light: "#EFF6FF",
          dark: "#1E3A5F",
        },
        "user-bubble-text": {
          light: "#1E40AF",
          dark: "#93C5FD",
        },
        "ai-bubble": {
          light: "#FFFFFF",
          dark: "#1A1A1A",
        },
        "ai-bubble-text": {
          light: "#374151",
          dark: "#E5E7EB",
        },
        "system-bubble": {
          light: "#FEF3C7",
          dark: "#451A03",
        },
        "error-bubble": {
          light: "#FEE2E2",
          dark: "#450A0A",
        },

        // 输入框区域
        "input-bg": {
          light: "#FFFFFF",
          dark: "#1A1A1A",
        },
        "input-border": {
          light: "#E5E7EB",
          dark: "#333333",
        },
        "input-focus": {
          light: "#2563EB",
          dark: "#3B82F6",
        },
        "input-placeholder": {
          light: "#9CA3AF",
          dark: "#6B7280",
        },
        "send-btn": {
          light: "#111827",
          dark: "#F9FAFB",
        },
        "send-btn-text": {
          light: "#FFFFFF",
          dark: "#111827",
        },
        "send-btn-hover": {
          light: "#374151",
          dark: "#E5E7EB",
        },

        // 组件交互
        "btn-primary": {
          light: "#111827",
          dark: "#F9FAFB",
        },
        "btn-primary-text": {
          light: "#FFFFFF",
          dark: "#111827",
        },
        "btn-secondary": {
          light: "#F3F4F6",
          dark: "#262626",
        },
        "btn-secondary-text": {
          light: "#374151",
          dark: "#D1D5DB",
        },
        "btn-secondary-border": {
          light: "#D1D5DB",
          dark: "#404040",
        },
        "icon-hover": {
          light: "#F3F4F6",
          dark: "#262626",
        },
        "dropdown-bg": {
          light: "#FFFFFF",
          dark: "#1A1A1A",
        },
        "dropdown-hover": {
          light: "#F3F4F6",
          dark: "#262626",
        },
        "dropdown-divider": {
          light: "#E5E7EB",
          dark: "#333333",
        },

        // 代码块
        "code-bg": {
          light: "#F3F4F6",
          dark: "#0D0D0D",
        },
        "code-border": {
          light: "#E5E7EB",
          dark: "#262626",
        },
        "code-text": {
          light: "#1F2937",
          dark: "#E5E7EB",
        },
        "code-lineno": {
          light: "#9CA3AF",
          dark: "#6B7280",
        },
        "inline-code-bg": {
          light: "#E5E7EB",
          dark: "#262626",
        },
        "inline-code-text": {
          light: "#BE123C",
          dark: "#F472B6",
        },

        // 引用与表格
        "quote-border": {
          light: "#D1D5DB",
          dark: "#4B5563",
        },
        "quote-bg": {
          light: "#F9FAFB",
          dark: "#1A1A1A",
        },
        "table-header": {
          light: "#F9FAFB",
          dark: "#1A1A1A",
        },
        "table-border": {
          light: "#E5E7EB",
          dark: "#333333",
        },
        "table-hover": {
          light: "#F9FAFB",
          dark: "#1A1A1A",
        },
        "list-marker": {
          light: "#6B7280",
          dark: "#9CA3AF",
        },

        // 状态反馈
        success: {
          light: "#10B981",
          dark: "#34D399",
        },
        warning: {
          light: "#F59E0B",
          dark: "#FBBF24",
        },
        error: {
          light: "#EF4444",
          dark: "#F87171",
        },
        info: {
          light: "#3B82F6",
          dark: "#60A5FA",
        },
        loading: {
          light: "#D1D5DB",
          dark: "#4B5563",
        },
        typing: {
          light: "#9CA3AF",
          dark: "#6B7280",
        },

        // 边框分割线
        divider: {
          light: "#E5E7EB",
          dark: "#262626",
        },
        "divider-weak": {
          light: "#F3F4F6",
          dark: "#1A1A1A",
        },
        "card-border": {
          light: "#E5E7EB",
          dark: "#262626",
        },
        shadow: {
          light: "rgba(0,0,0,0.05)",
          dark: "rgba(0,0,0,0.3)",
        },

        // 滚动条
        "scrollbar-track": "transparent",
        "scrollbar-thumb": {
          light: "#D1D5DB",
          dark: "#4B5563",
        },
        "scrollbar-thumb-hover": {
          light: "#9CA3AF",
          dark: "#6B7280",
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
