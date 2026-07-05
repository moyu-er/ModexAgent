const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");

// ─── Icon Helpers ───────────────────────────────────────────────────────────

function renderIconSvg(IconComponent, color = "#000000", size = 256) {
  return ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(size) })
  );
}

async function iconToBase64Png(IconComponent, color, size = 256) {
  const svg = renderIconSvg(IconComponent, color, size);
  const pngBuffer = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + pngBuffer.toString("base64");
}

// ─── Color Palette ───────────────────────────────────────────────────────────

const C = {
  darkBg:      "1A365D",
  lightBg:     "F7FAFC",
  primary:     "028090",
  secondary:   "00A896",
  accent:      "2D3748",
  white:       "FFFFFF",
  lightTeal:   "E6FFFA",
  darkText:    "1A202C",
  mutedText:   "718096",
  cardBg:      "FFFFFF",
  subtitle:    "4FD1C5",
};

// ─── Fonts ───────────────────────────────────────────────────────────────────

const FONT_TITLE = "Arial Black";
const FONT_HEAD = "Arial";
const FONT_BODY  = "Calibri";
const FONT_SUB   = "Calibri";

// ─── Reusable Helpers ────────────────────────────────────────────────────────

function addSectionHeader(slide, en, zh, pres) {
  // Decorative line
  slide.addShape(pres.shapes.RECTANGLE, {
    x: 0.6, y: 0.25, w: 0.08, h: 0.6,
    fill: { color: C.primary },
  });
  slide.addText([
    { text: en, options: { fontSize: 28, fontFace: FONT_TITLE, color: C.darkText, breakLine: true } },
    { text: zh, options: { fontSize: 16, fontFace: FONT_BODY, color: C.mutedText } },
  ], {
    x: 0.85, y: 0.2, w: 8.5, h: 0.7,
    margin: 0,
  });
}

// ─── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = "Office Assistant";
  pres.title = "Office 助手 - 自我介绍";

  // Pre-render icons
  const { FaRobot, FaFileWord, FaFileExcel, FaFilePowerpoint, FaFilePdf,
          FaBolt, FaLanguage, FaGem, FaShieldAlt, FaStar } = require("react-icons/fa");

  const iconRobot   = await iconToBase64Png(FaRobot, "#FFFFFF", 256);
  const iconWord    = await iconToBase64Png(FaFileWord, "#FFFFFF", 256);
  const iconExcel   = await iconToBase64Png(FaFileExcel, "#FFFFFF", 256);
  const iconPpt     = await iconToBase64Png(FaFilePowerpoint, "#FFFFFF", 256);
  const iconPdf     = await iconToBase64Png(FaFilePdf, "#FFFFFF", 256);
  const iconBolt    = await iconToBase64Png(FaBolt, "#FFFFFF", 256);
  const iconLang    = await iconToBase64Png(FaLanguage, "#FFFFFF", 256);
  const iconGem     = await iconToBase64Png(FaGem, "#FFFFFF", 256);
  const iconShield  = await iconToBase64Png(FaShieldAlt, "#FFFFFF", 256);
  const iconStar    = await iconToBase64Png(FaStar, "#FFFFFF", 256);

  // Icons for capabilities (colored versions)
  const iconWordC    = await iconToBase64Png(FaFileWord, "#2B5797", 256);    // Word blue
  const iconExcelC   = await iconToBase64Png(FaFileExcel, "#217346", 256);   // Excel green
  const iconPptC     = await iconToBase64Png(FaFilePowerpoint, "#D04423", 256); // PPT red
  const iconPdfC     = await iconToBase64Png(FaFilePdf, "#EC1C24", 256);     // PDF red

  // Icons for advantages (colored)
  const iconBoltC    = await iconToBase64Png(FaBolt, "#028090", 256);
  const iconLangC    = await iconToBase64Png(FaLanguage, "#028090", 256);
  const iconGemC     = await iconToBase64Png(FaGem, "#028090", 256);
  const iconShieldC  = await iconToBase64Png(FaShieldAlt, "#028090", 256);
  const iconStarC    = await iconToBase64Png(FaStar, "#028090", 256);

  // ============================
  // SLIDE 1 — Title (Dark BG)
  // ============================
  {
    const s1 = pres.addSlide();
    s1.background = { color: C.darkBg };

    // Decorative large circle (top-right)
    s1.addShape(pres.shapes.OVAL, {
      x: 7.5, y: -1.2, w: 4.0, h: 4.0,
      fill: { color: C.primary, transparency: 30 },
    });

    // Decorative small circle (bottom-left)
    s1.addShape(pres.shapes.OVAL, {
      x: -0.8, y: 3.8, w: 2.5, h: 2.5,
      fill: { color: C.secondary, transparency: 40 },
    });

    // Robot icon in center area
    s1.addImage({ data: iconRobot, x: 1.0, y: 0.8, w: 0.9, h: 0.9 });

    // Main title - Chinese
    s1.addText("Office 助手", {
      x: 0.6, y: 1.5, w: 6.5, h: 1.0,
      fontSize: 48, fontFace: FONT_TITLE, color: C.white, bold: true,
      margin: 0,
    });

    // Main title - English
    s1.addText("Office Assistant", {
      x: 0.6, y: 2.5, w: 6.5, h: 0.7,
      fontSize: 28, fontFace: FONT_HEAD, color: C.subtitle,
      margin: 0,
    });

    // Decorative line
    s1.addShape(pres.shapes.LINE, {
      x: 0.6, y: 3.4, w: 2.5, h: 0,
      line: { color: C.primary, width: 3 },
    });

    // Tagline
    s1.addText("你的智能文档处理专家  ·  Your Intelligent Document Expert", {
      x: 0.6, y: 3.6, w: 8.5, h: 0.6,
      fontSize: 14, fontFace: FONT_BODY, color: C.mutedText,
      margin: 0,
    });
  }

  // ============================
  // SLIDE 2 — About Me
  // ============================
  {
    const s2 = pres.addSlide();
    s2.background = { color: C.lightBg };

    addSectionHeader(s2, "About Me", "关于我", pres);

    // Left decorative area - colored rounded shape
    s2.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: 0.6, y: 1.2, w: 3.8, h: 3.6,
      fill: { color: C.primary, transparency: 10 },
      rectRadius: 0.15,
    });

    // Large robot icon inside
    s2.addImage({ data: iconRobot, x: 1.3, y: 1.7, w: 2.4, h: 2.4 });

    // Bottom caption under icon
    s2.addText("AI · Office Expert", {
      x: 0.6, y: 4.2, w: 3.8, h: 0.4,
      fontSize: 12, fontFace: FONT_BODY, color: C.primary, align: "center",
      margin: 0,
    });

    // Right column - description text
    // Name badge
    s2.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: 5.0, y: 1.2, w: 4.4, h: 0.45,
      fill: { color: C.primary },
      rectRadius: 0.08,
    });
    s2.addText("🤖  Office 助手  /  Office Assistant", {
      x: 5.0, y: 1.2, w: 4.4, h: 0.45,
      fontSize: 13, fontFace: FONT_BODY, color: C.white, align: "center", valign: "middle",
      margin: 0,
    });

    // Description
    s2.addText([
      { text: "我是一个AI驱动的办公文档专家，专门处理 Word、Excel、PowerPoint 和 PDF 文件。我能够创建、编辑、转换和优化各类办公文档，让你从繁琐的文档处理工作中解放出来。", options: { breakLine: true, fontSize: 13, fontFace: FONT_BODY, color: C.darkText } },
      { text: "", options: { breakLine: true, fontSize: 8 } },
      { text: "I am an AI-powered Office document expert, specializing in Word, Excel, PowerPoint, and PDF files. I can create, edit, convert, and optimize all types of office documents — freeing you from tedious document processing tasks.", options: { fontSize: 13, fontFace: FONT_BODY, color: C.darkText } },
    ], {
      x: 5.0, y: 1.9, w: 4.4, h: 3.0,
      valign: "top",
      margin: 0,
      paraSpaceAfter: 4,
    });
  }

  // ============================
  // SLIDE 3 — Capabilities
  // ============================
  {
    const s3 = pres.addSlide();
    s3.background = { color: C.lightBg };

    addSectionHeader(s3, "Core Capabilities", "核心能力", pres);

    // 2x2 Grid of capability cards
    const cards = [
      { icon: iconWordC,  title: "Word",    zh: "文档处理", desc: "创建、编辑、排版专业文档，支持目录、页眉页脚、图片等" },
      { icon: iconExcelC, title: "Excel",   zh: "电子表格", desc: "数据分析、公式计算、图表制作、数据清洗与格式化" },
      { icon: iconPptC,   title: "PowerPoint", zh: "演示文稿", desc: "专业幻灯片制作，模板设计，图文排版与动画" },
      { icon: iconPdfC,   title: "PDF",     zh: "PDF处理",  desc: "合并拆分、提取文字表格、添加水印、加密解密、OCR识别" },
    ];

    const cardW = 4.15;
    const cardH = 1.7;
    const gapX = 0.4;
    const gapY = 0.3;
    const startX = 0.6;
    const startY = 1.15;

    cards.forEach((card, i) => {
      const col = i % 2;
      const row = Math.floor(i / 2);
      const cx = startX + col * (cardW + gapX);
      const cy = startY + row * (cardH + gapY);

      // Card background
      s3.addShape(pres.shapes.ROUNDED_RECTANGLE, {
        x: cx, y: cy, w: cardW, h: cardH,
        fill: { color: C.cardBg },
        rectRadius: 0.1,
        shadow: { type: "outer", color: "000000", blur: 6, offset: 2, angle: 135, opacity: 0.08 },
      });

      // Icon
      s3.addImage({ data: card.icon, x: cx + 0.25, y: cy + 0.25, w: 0.5, h: 0.5 });

      // Title (English)
      s3.addText(card.title, {
        x: cx + 0.9, y: cy + 0.2, w: 3.0, h: 0.35,
        fontSize: 16, fontFace: FONT_HEAD, color: C.darkText, bold: true,
        margin: 0,
      });

      // Subtitle (Chinese)
      s3.addText(card.zh, {
        x: cx + 0.9, y: cy + 0.55, w: 3.0, h: 0.3,
        fontSize: 12, fontFace: FONT_BODY, color: C.mutedText,
        margin: 0,
      });

      // Description
      s3.addText(card.desc, {
        x: cx + 0.25, y: cy + 0.95, w: 3.65, h: 0.6,
        fontSize: 11, fontFace: FONT_BODY, color: C.darkText,
        margin: 0,
      });
    });
  }

  // ============================
  // SLIDE 4 — Core Advantages
  // ============================
  {
    const s4 = pres.addSlide();
    s4.background = { color: C.lightBg };

    addSectionHeader(s4, "Key Advantages", "核心优势", pres);

    const advantages = [
      { icon: iconBoltC,   en: "Lightning Fast",     zh: "极速处理", desc: "秒级完成文档创建、编辑与格式转换" },
      { icon: iconGemC,    en: "Professional Quality", zh: "专业品质", desc: "精致排版，符合商业标准的文档输出" },
      { icon: iconLangC,   en: "Bilingual Support",   zh: "双语支持", desc: "中英文自由切换，满足国际化需求" },
      { icon: iconShieldC, en: "Format Integrity",    zh: "格式无损", desc: "精准保留原始格式，跨平台兼容无忧" },
      { icon: iconStarC,   en: "All-in-One Solution", zh: "全能整合", desc: "Word/Excel/PPT/PDF一站式处理" },
    ];

    const itemH = 0.74;
    const startY = 1.15;
    const leftX = 0.6;
    const iconSize = 0.45;

    advantages.forEach((item, i) => {
      const iy = startY + i * (itemH + 0.12);

      // Background strip
      s4.addShape(pres.shapes.ROUNDED_RECTANGLE, {
        x: leftX, y: iy, w: 8.8, h: itemH,
        fill: { color: C.cardBg },
        rectRadius: 0.08,
        shadow: { type: "outer", color: "000000", blur: 3, offset: 1, angle: 135, opacity: 0.06 },
      });

      // Icon
      s4.addImage({ data: item.icon, x: leftX + 0.2, y: iy + (itemH - iconSize) / 2, w: iconSize, h: iconSize });

      // Title EN
      s4.addText(item.en, {
        x: leftX + 0.85, y: iy + 0.05, w: 3.5, h: 0.3,
        fontSize: 14, fontFace: FONT_HEAD, color: C.darkText, bold: true,
        margin: 0,
      });

      // Subtitle ZH
      s4.addText(item.zh, {
        x: leftX + 0.85, y: iy + 0.35, w: 1.5, h: 0.28,
        fontSize: 11, fontFace: FONT_BODY, color: C.mutedText,
        margin: 0,
      });

      // Description
      s4.addText(item.desc, {
        x: leftX + 2.5, y: iy + 0.35, w: 6.0, h: 0.28,
        fontSize: 11, fontFace: FONT_BODY, color: C.darkText,
        margin: 0,
      });
    });
  }

  // ============================
  // SLIDE 5 — Closing (Dark BG)
  // ============================
  {
    const s5 = pres.addSlide();
    s5.background = { color: C.darkBg };

    // Decorative elements
    s5.addShape(pres.shapes.OVAL, {
      x: -1.0, y: -1.0, w: 3.5, h: 3.5,
      fill: { color: C.primary, transparency: 25 },
    });
    s5.addShape(pres.shapes.OVAL, {
      x: 8.0, y: 3.5, w: 3.0, h: 3.0,
      fill: { color: C.secondary, transparency: 35 },
    });

    // Center content
    s5.addText("Thank You", {
      x: 0.5, y: 1.2, w: 9.0, h: 1.0,
      fontSize: 44, fontFace: FONT_TITLE, color: C.white, bold: true, align: "center",
      margin: 0,
    });

    s5.addText("谢谢", {
      x: 0.5, y: 2.1, w: 9.0, h: 0.8,
      fontSize: 36, fontFace: FONT_HEAD, color: C.subtitle, align: "center",
      margin: 0,
    });

    // Decorative line
    s5.addShape(pres.shapes.LINE, {
      x: 3.5, y: 3.1, w: 3.0, h: 0,
      line: { color: C.primary, width: 3 },
    });

    s5.addText("Office 助手  ·  让文档处理更简单", {
      x: 0.5, y: 3.4, w: 9.0, h: 0.5,
      fontSize: 14, fontFace: FONT_BODY, color: C.mutedText, align: "center",
      margin: 0,
    });

    s5.addText("Office Assistant  ·  Making Document Processing Simple", {
      x: 0.5, y: 3.85, w: 9.0, h: 0.5,
      fontSize: 12, fontFace: FONT_BODY, color: C.mutedText, align: "center",
      margin: 0,
    });

    // Bottom small note
    s5.addText("Powered by AI  |  2026", {
      x: 0.5, y: 4.8, w: 9.0, h: 0.4,
      fontSize: 11, fontFace: FONT_BODY, color: C.mutedText, align: "center",
      margin: 0,
    });
  }

  // ─── Write file ──────────────────────────────────────────────────────────
  const outputPath = "F:\\tool\\pythonProject\\ModexAgent\\examples\\bot_project\\skills\\main\\office-expert\\pptx\\Office_Assistant_Introduction.pptx";
  await pres.writeFile({ fileName: outputPath });
  console.log("Presentation saved to:", outputPath);
}

main().catch(err => { console.error(err); process.exit(1); });
