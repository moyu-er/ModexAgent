const pptxgen = require("pptxgenjs");

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";
pres.author = "Office助手";
pres.title = "Office助手自我介绍";

// ===== COLOR PALETTE =====
const C = {
  darkBg:    "0B2B4A",   // Deep navy for cover/end
  darkBg2:   "0E3A5C",   // Slightly lighter navy
  primary:   "1565C0",   // Blue primary
  accent:    "00BFA5",   // Teal accent
  accent2:   "26A69A",   // Darker teal
  lightBg:   "F5F7FA",   // Light background
  cardBg:    "FFFFFF",   // Card white
  textDark:  "1A1A2E",   // Dark text
  textMuted: "64748B",   // Gray text
  textWhite: "FFFFFF",   // White text
  highlight: "E3F2FD",   // Light blue highlight
  tealLight: "E0F7FA",   // Light teal
};

// Helper: create a unique shadow object each time
const makeShadow = () => ({
  type: "outer", color: "000000", blur: 8, offset: 2, angle: 135, opacity: 0.12
});

// =========================================================
// SLIDE 1: Cover
// =========================================================
let slide1 = pres.addSlide();
slide1.background = { color: C.darkBg };

// Decorative top-right shape
slide1.addShape(pres.shapes.OVAL, {
  x: 7.5, y: -1.5, w: 4.5, h: 4.5,
  fill: { color: C.primary, transparency: 70 }
});
// Decorative bottom-left shape
slide1.addShape(pres.shapes.OVAL, {
  x: -1.5, y: 3.5, w: 4, h: 4,
  fill: { color: C.accent, transparency: 75 }
});

// Accent line
slide1.addShape(pres.shapes.RECTANGLE, {
  x: 0.8, y: 1.5, w: 1.2, h: 0.06,
  fill: { color: C.accent }
});

// Main Title
slide1.addText("Office助手自我介绍", {
  x: 0.8, y: 1.8, w: 8.4, h: 1.2,
  fontSize: 42, fontFace: "Arial Black",
  color: C.textWhite, bold: true,
  margin: 0, align: "left", valign: "top"
});

// Subtitle
slide1.addText("高效处理文档的智能伙伴", {
  x: 0.8, y: 3.2, w: 8, h: 0.7,
  fontSize: 20, fontFace: "Georgia",
  color: C.accent, italic: true,
  margin: 0, align: "left", valign: "top"
});

// Bottom accent bar
slide1.addShape(pres.shapes.RECTANGLE, {
  x: 0, y: 5.3, w: 10, h: 0.325,
  fill: { color: C.accent, transparency: 40 }
});

// =========================================================
// SLIDE 2: 我是谁
// =========================================================
let slide2 = pres.addSlide();
slide2.background = { color: C.lightBg };

// Title bar background
slide2.addShape(pres.shapes.RECTANGLE, {
  x: 0, y: 0, w: 10, h: 1.0,
  fill: { color: C.primary }
});

// Title text
slide2.addText("我是谁", {
  x: 0.8, y: 0.15, w: 5, h: 0.7,
  fontSize: 30, fontFace: "Arial Black",
  color: C.textWhite, bold: true,
  margin: 0, align: "left", valign: "middle"
});

// Subtitle under title bar
slide2.addText("您的全能文档处理专家", {
  x: 0.8, y: 1.2, w: 8, h: 0.5,
  fontSize: 16, fontFace: "Calibri",
  color: C.textMuted, italic: true,
  margin: 0, align: "left", valign: "top"
});

// --- Card 1: 功能定位 (left) ---
slide2.addShape(pres.shapes.RECTANGLE, {
  x: 0.8, y: 1.9, w: 4.0, h: 1.5,
  fill: { color: C.cardBg },
  shadow: makeShadow()
});
// Accent strip on card
slide2.addShape(pres.shapes.RECTANGLE, {
  x: 0.8, y: 1.9, w: 4.0, h: 0.06,
  fill: { color: C.accent }
});
slide2.addText([
  { text: "功能定位", options: { bold: true, fontSize: 18, color: C.textDark, breakLine: true } },
  { text: "专注办公文档处理的智能助手，", options: { fontSize: 13, color: C.textMuted, breakLine: true } },
  { text: "高效处理各类文档需求", options: { fontSize: 13, color: C.textMuted } }
], {
  x: 1.1, y: 2.15, w: 3.5, h: 1.1,
  fontFace: "Calibri", valign: "top", align: "left",
  margin: 0, lineSpacingMultiple: 1.15
});

// --- Card 2: 覆盖范围 (right) ---
slide2.addShape(pres.shapes.RECTANGLE, {
  x: 5.2, y: 1.9, w: 4.0, h: 1.5,
  fill: { color: C.cardBg },
  shadow: makeShadow()
});
slide2.addShape(pres.shapes.RECTANGLE, {
  x: 5.2, y: 1.9, w: 4.0, h: 0.06,
  fill: { color: C.primary }
});
slide2.addText([
  { text: "覆盖范围", options: { bold: true, fontSize: 18, color: C.textDark, breakLine: true } },
  { text: "Word · Excel · PowerPoint · PDF", options: { fontSize: 13, color: C.textMuted, breakLine: true } },
  { text: "全类型文档格式支持", options: { fontSize: 13, color: C.textMuted } }
], {
  x: 5.5, y: 2.15, w: 3.5, h: 1.1,
  fontFace: "Calibri", valign: "top", align: "left",
  margin: 0, lineSpacingMultiple: 1.15
});

// --- Bottom description area ---
slide2.addShape(pres.shapes.RECTANGLE, {
  x: 0.8, y: 3.7, w: 8.4, h: 1.4,
  fill: { color: C.cardBg },
  shadow: makeShadow()
});
slide2.addText([
  { text: "核心使命", options: { bold: true, fontSize: 18, color: C.textDark, breakLine: true } },
  { text: "以智能化的方式协助用户完成文档创建、编辑、格式转换与内容提取等任务，", options: { fontSize: 13, color: C.textMuted, breakLine: true } },
  { text: "让办公文档处理变得轻松、高效、专业。", options: { fontSize: 13, color: C.textMuted } }
], {
  x: 1.1, y: 3.9, w: 7.8, h: 1.0,
  fontFace: "Calibri", valign: "top", align: "left",
  margin: 0, lineSpacingMultiple: 1.15
});

// =========================================================
// SLIDE 3: 我能做什么
// =========================================================
let slide3 = pres.addSlide();
slide3.background = { color: C.lightBg };

// Title bar
slide3.addShape(pres.shapes.RECTANGLE, {
  x: 0, y: 0, w: 10, h: 1.0,
  fill: { color: C.primary }
});
slide3.addText("我能做什么", {
  x: 0.8, y: 0.15, w: 5, h: 0.7,
  fontSize: 30, fontFace: "Arial Black",
  color: C.textWhite, bold: true,
  margin: 0, align: "left", valign: "middle"
});
slide3.addText("核心能力一览", {
  x: 0.8, y: 1.2, w: 8, h: 0.5,
  fontSize: 16, fontFace: "Calibri",
  color: C.textMuted, italic: true,
  margin: 0, align: "left", valign: "top"
});

// --- 4 Capability Cards in 2x2 grid ---
const cardW = 3.8;
const cardH = 1.65;
const col1X = 0.8;
const col2X = 5.4;
const row1Y = 1.9;
const row2Y = 3.8;

const cards = [
  { x: col1X, y: row1Y, title: "格式转换", desc: "支持文档格式互转，如 PDF 转 Word、图片转文字、批量格式转换等，轻松应对不同场景需求。", accent: C.accent },
  { x: col2X, y: row1Y, title: "内容提取", desc: "精准提取文档中的文本、表格、图片等元素，支持结构化数据解析和关键信息快速定位。", accent: C.primary },
  { x: col1X, y: row2Y, title: "编辑批注", desc: "智能编辑文档内容，添加批注与修订，支持模板化批量处理和文档对比功能。", accent: C.accent2 },
  { x: col2X, y: row2Y, title: "文档生成", desc: "根据需求自动生成报告、合同、邮件等标准文档，支持自定义模板和智能排版。", accent: C.primary },
];

cards.forEach(c => {
  // Card background
  slide3.addShape(pres.shapes.RECTANGLE, {
    x: c.x, y: c.y, w: cardW, h: cardH,
    fill: { color: C.cardBg },
    shadow: makeShadow()
  });
  // Accent strip
  slide3.addShape(pres.shapes.RECTANGLE, {
    x: c.x, y: c.y, w: cardW, h: 0.06,
    fill: { color: c.accent }
  });
  // Number circle
  slide3.addShape(pres.shapes.OVAL, {
    x: c.x + 0.2, y: c.y + 0.25, w: 0.45, h: 0.45,
    fill: { color: c.accent }
  });
  const numIdx = cards.indexOf(c) + 1;
  slide3.addText(String(numIdx), {
    x: c.x + 0.2, y: c.y + 0.25, w: 0.45, h: 0.45,
    fontSize: 16, fontFace: "Arial Black",
    color: C.textWhite, bold: true,
    align: "center", valign: "middle", margin: 0
  });
  // Title
  slide3.addText(c.title, {
    x: c.x + 0.8, y: c.y + 0.2, w: 2.8, h: 0.4,
    fontSize: 16, fontFace: "Calibri",
    color: C.textDark, bold: true,
    align: "left", valign: "middle", margin: 0
  });
  // Description
  slide3.addText(c.desc, {
    x: c.x + 0.3, y: c.y + 0.75, w: 3.3, h: 0.8,
    fontSize: 11.5, fontFace: "Calibri",
    color: C.textMuted,
    align: "left", valign: "top", margin: 0,
    lineSpacingMultiple: 1.2
  });
});

// =========================================================
// SLIDE 4: 我的优势
// =========================================================
let slide4 = pres.addSlide();
slide4.background = { color: C.lightBg };

// Title bar
slide4.addShape(pres.shapes.RECTANGLE, {
  x: 0, y: 0, w: 10, h: 1.0,
  fill: { color: C.primary }
});
slide4.addText("我的优势", {
  x: 0.8, y: 0.15, w: 5, h: 0.7,
  fontSize: 30, fontFace: "Arial Black",
  color: C.textWhite, bold: true,
  margin: 0, align: "left", valign: "middle"
});
slide4.addText("为什么选择我？", {
  x: 0.8, y: 1.2, w: 8, h: 0.5,
  fontSize: 16, fontFace: "Calibri",
  color: C.textMuted, italic: true,
  margin: 0, align: "left", valign: "top"
});

// --- Three advantage cards in a row ---
const advW = 2.7;
const advH = 2.8;
const advGap = 0.3;
const startX = 0.65;
const advY = 1.9;

const advantages = [
  { title: "快速", iconText: "⚡", desc: "秒级响应，即时处理\n文档转换与内容提取\n大幅提升工作效率", accent: C.accent },
  { title: "准确", iconText: "✓", desc: "精准识别与处理\n保持格式与内容完整性\n减少人工校验成本", accent: C.primary },
  { title: "自动化", iconText: "⟳", desc: "批量处理，一键操作\n支持流程自动化编排\n降低重复劳动负担", accent: C.accent2 },
];

advantages.forEach((adv, i) => {
  const ax = startX + i * (advW + advGap);
  
  // Card bg
  slide4.addShape(pres.shapes.RECTANGLE, {
    x: ax, y: advY, w: advW, h: advH,
    fill: { color: C.cardBg },
    shadow: makeShadow()
  });
  
  // Top accent bar
  slide4.addShape(pres.shapes.RECTANGLE, {
    x: ax, y: advY, w: advW, h: 0.06,
    fill: { color: adv.accent }
  });
  
  // Icon circle
  slide4.addShape(pres.shapes.OVAL, {
    x: ax + advW/2 - 0.4, y: advY + 0.3, w: 0.8, h: 0.8,
    fill: { color: adv.accent }
  });
  slide4.addText(adv.iconText, {
    x: ax + advW/2 - 0.4, y: advY + 0.3, w: 0.8, h: 0.8,
    fontSize: 22, fontFace: "Arial",
    color: C.textWhite, bold: true,
    align: "center", valign: "middle", margin: 0
  });
  
  // Title
  slide4.addText(adv.title, {
    x: ax, y: advY + 1.2, w: advW, h: 0.5,
    fontSize: 20, fontFace: "Arial Black",
    color: C.textDark, bold: true,
    align: "center", valign: "middle", margin: 0
  });
  
  // Description
  slide4.addText(adv.desc, {
    x: ax + 0.2, y: advY + 1.7, w: advW - 0.4, h: 1.0,
    fontSize: 12, fontFace: "Calibri",
    color: C.textMuted,
    align: "center", valign: "top", margin: 0,
    lineSpacingMultiple: 1.3
  });
});

// =========================================================
// SLIDE 5: End Page
// =========================================================
let slide5 = pres.addSlide();
slide5.background = { color: C.darkBg };

// Decorative circles
slide5.addShape(pres.shapes.OVAL, {
  x: -1, y: -1, w: 3.5, h: 3.5,
  fill: { color: C.primary, transparency: 70 }
});
slide5.addShape(pres.shapes.OVAL, {
  x: 7.5, y: 3.5, w: 4, h: 4,
  fill: { color: C.accent, transparency: 75 }
});

// Center content area - accent line
slide5.addShape(pres.shapes.RECTANGLE, {
  x: 4.2, y: 1.6, w: 1.6, h: 0.06,
  fill: { color: C.accent }
});

// Main thank you text
slide5.addText("感谢观看", {
  x: 1, y: 1.9, w: 8, h: 1.2,
  fontSize: 44, fontFace: "Arial Black",
  color: C.textWhite, bold: true,
  align: "center", valign: "middle", margin: 0
});

// Subtitle
slide5.addText("随时为您服务", {
  x: 1, y: 3.1, w: 8, h: 0.7,
  fontSize: 22, fontFace: "Georgia",
  color: C.accent, italic: true,
  align: "center", valign: "middle", margin: 0
});

// Bottom accent bar
slide5.addShape(pres.shapes.RECTANGLE, {
  x: 0, y: 5.3, w: 10, h: 0.325,
  fill: { color: C.accent, transparency: 40 }
});

// =========================================================
// SAVE
// =========================================================
const outputPath = "F:\\tool\\pythonProject\\ModexAgent\\examples\\bot_project\\skills\\main\\office-expert\\pptx\\output\\Office助手自我介绍.pptx";
pres.writeFile({ fileName: outputPath }).then(() => {
  console.log("PPT created successfully: " + outputPath);
}).catch(err => {
  console.error("Error:", err);
});
