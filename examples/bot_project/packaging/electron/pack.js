/**
 * Package the Electron app into a directory for Inno Setup to consume.
 *
 * Usage:  node pack.js --staging-dir ../staging
 * Output: <staging-dir>/electron/ModexBot-win32-x64/
 *
 * Supports two modes:
 *   1. LOCAL ZIP (offline):  Place electron-v<version>-win32-x64.zip next to
 *      this script.  pack.js extracts it and assembles the app directory
 *      manually — no network needed.
 *   2. REMOTE (online):  Falls back to @electron/packager which downloads
 *      the Electron binary.  Set ELECTRON_MIRROR for mirrors.
 *
 * Trim rules (applied after packaging):
 *   - Keep only en-US.pak + zh-CN.pak locales (delete ~90 others)
 *   - Delete default_app.asar (Electron's welcome page)
 *   - Delete vk_swiftshader.dll + vulkan-1.dll (no software rendering)
 *   - Delete d3dcompiler_47.dll (rarely needed for web apps)
 *
 * NOTE: ffmpeg.dll is REQUIRED — Electron loads it at startup even without
 * media playback. Do NOT delete it.
 */

const fs = require("fs").promises;
const fss = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const { createReadStream } = require("fs");
const { createWriteStream } = require("fs");
const { pipeline } = require("stream/promises");

// ── Helpers ─────────────────────────────────────────────────────────────────

const ELECTRON_VERSION = "33.4.11";
const RCEDIT_VERSION = "2.0.0";
const APP_NAME = "ModexBot";

async function exists(p) {
  try { await fs.access(p); return true; } catch { return false; }
}

function psQuote(s) {
  return "'" + String(s).replace(/'/g, "''") + "'";
}

async function copyDir(src, dst) {
  await fs.mkdir(dst, { recursive: true });
  const entries = await fs.readdir(src, { withFileTypes: true });
  for (const entry of entries) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      await copyDir(s, d);
    } else {
      await fs.copyFile(s, d);
    }
  }
}

async function rmrf(p) {
  await fs.rm(p, { recursive: true, force: true });
}

async function pruneLocales(dir) {
  const localesDir = path.join(dir, "locales");
  const keep = ["en-US.pak", "zh-CN.pak"];
  try {
    const files = await fs.readdir(localesDir);
    let removed = 0;
    for (const f of files) {
      if (!keep.includes(f)) {
        await fs.unlink(path.join(localesDir, f));
        removed++;
      }
    }
    console.log(`  [pack] Locales: kept ${keep.length}, removed ${removed}`);
  } catch {
    // locales dir may not exist
  }
}

async function deleteFile(filePath, label) {
  try {
    await fs.unlink(filePath);
    console.log(`  [pack] Deleted: ${label}`);
  } catch {
    // file may not exist — not an error
  }
}

async function trimElectron(appPath) {
  console.log("  [pack] Trimming unnecessary files...");
  await pruneLocales(appPath);
  await deleteFile(path.join(appPath, "resources", "default_app.asar"), "default_app.asar");
  await deleteFile(path.join(appPath, "vk_swiftshader.dll"), "vk_swiftshader.dll");
  await deleteFile(path.join(appPath, "vulkan-1.dll"), "vulkan-1.dll");
  await deleteFile(path.join(appPath, "d3dcompiler_47.dll"), "d3dcompiler_47.dll");
}

function ensureRcedit() {
  const cacheDir = path.join(__dirname, ".cache");
  const rceditPath = path.join(cacheDir, `rcedit-x64-v${RCEDIT_VERSION}.exe`);
  if (fss.existsSync(rceditPath)) {
    console.log(`  [pack] rcedit cached: ${rceditPath}`);
    return rceditPath;
  }

  const url = `https://github.com/electron/rcedit/releases/download/v${RCEDIT_VERSION}/rcedit-x64.exe`;
  console.log(`  [pack] Downloading rcedit v${RCEDIT_VERSION} ...`);
  fss.mkdirSync(cacheDir, { recursive: true });

  try {
    execSync(
      `powershell -NoProfile -Command "Invoke-WebRequest -Uri ${psQuote(url)} -OutFile ${psQuote(rceditPath)} -UseBasicParsing"`,
      { stdio: "inherit" },
    );
  } catch (e) {
    console.error(`  [pack] ERROR: Failed to download rcedit from ${url}`);
    console.error(`  [pack] Download manually and place at: ${rceditPath}`);
    throw e;
  }

  console.log(`  [pack] rcedit ready: ${rceditPath}`);
  return rceditPath;
}

async function applyIcon(appPath, iconPath) {
  if (!iconPath) {
    console.log("  [pack] No icon specified — skipping icon embedding.");
    return;
  }
  if (!await exists(iconPath)) {
    console.warn(`  [pack] WARNING: icon not found: ${iconPath} — skipping.`);
    return;
  }

  const exePath = path.join(appPath, `${APP_NAME}.exe`);
  const rceditPath = ensureRcedit();

  console.log(`  [pack] Setting icon on ${APP_NAME}.exe ...`);
  execSync(`"${rceditPath}" "${exePath}" --set-icon "${iconPath}"`, { stdio: "inherit" });

  const appDir = path.join(appPath, "resources", "app");
  const destIcon = path.join(appDir, "logo.ico");
  await fs.copyFile(iconPath, destIcon);
  console.log(`  [pack] Icon copied to resources/app/logo.ico`);
}

function reportSize(appPath) {
  try {
    const out = execSync(
      `powershell -Command "(Get-ChildItem ${psQuote(appPath)} -Recurse -File | Measure-Object -Property Length -Sum).Sum"`,
      { encoding: "utf-8" },
    );
    const sizeMB = Math.round(parseFloat(out.trim()) / 1e6);
    console.log(`  [pack] Final size: ${sizeMB} MB`);
  } catch {
    // informational
  }
}

// ── Mode 1: Local zip extraction (offline) ──────────────────────────────────

async function unpackLocalZip(zipPath, outDir) {
  const appPath = path.join(outDir, `${APP_NAME}-win32-x64`);

  console.log(`  [pack] Using local zip: ${zipPath}`);
  console.log(`  [pack] Extracting Electron runtime...`);

  // Clean output
  if (await exists(appPath)) await rmrf(appPath);
  await fs.mkdir(appPath, { recursive: true });

  // Extract zip using PowerShell's Expand-Archive (always available on Win10+)
  const tmpExtract = path.join(outDir, "_electron_extract_tmp");
  if (await exists(tmpExtract)) await rmrf(tmpExtract);

  execSync(
    `powershell -NoProfile -Command "Expand-Archive -LiteralPath ${psQuote(zipPath)} -DestinationPath ${psQuote(tmpExtract)} -Force"`,
    { stdio: "inherit" },
  );

  // The zip extracts to a flat structure: electron.exe, locales/, resources/, etc.
  // Move all contents into appPath
  const entries = await fs.readdir(tmpExtract);
  for (const entry of entries) {
    const src = path.join(tmpExtract, entry);
    const dst = path.join(appPath, entry);
    await fs.rename(src, dst);
  }
  await rmrf(tmpExtract);

  // Rename electron.exe → ModexBot.exe
  const electronExe = path.join(appPath, "electron.exe");
  const modexbotExe = path.join(appPath, `${APP_NAME}.exe`);
  if (await exists(electronExe)) {
    await fs.rename(electronExe, modexbotExe);
    console.log(`  [pack] Renamed electron.exe → ${APP_NAME}.exe`);
  }

  // Create resources/app/ with our code
  const appDir = path.join(appPath, "resources", "app");
  await fs.mkdir(appDir, { recursive: true });

  // Copy main.js and package.json (strip devDependencies from package.json)
  await fs.copyFile(path.join(__dirname, "main.js"), path.join(appDir, "main.js"));

  const pkg = JSON.parse(fss.readFileSync(path.join(__dirname, "package.json"), "utf-8"));
  delete pkg.devDependencies;
  delete pkg.scripts;
  fss.writeFileSync(path.join(appDir, "package.json"), JSON.stringify(pkg, null, 2));

  console.log(`  [pack] App code copied to resources/app/`);

  return appPath;
}

// ── Mode 2: @electron/packager (online fallback) ────────────────────────────

async function packWithPackager(outDir) {
  const packager = require("@electron/packager");

  console.log("  [pack] No local zip found — using @electron/packager (online)...");
  console.log("  [pack] Set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ for China mirror");

  const appPaths = await packager({
    dir: __dirname,
    out: outDir,
    platform: "win32",
    arch: "x64",
    electronVersion: ELECTRON_VERSION,
    prune: true,
    overwrite: true,
    asar: false,
    name: APP_NAME,
    ignore: [
      /\.gitignore$/,
      /pack\.js$/,
      /node_modules\/@electron/,
      /node_modules\/electron$/,
    ],
    afterCopy: [async (buildPath, electronVersion, platform, arch, callback) => {
      try {
        await pruneLocales(buildPath);
        callback();
      } catch (e) {
        callback(e);
      }
    }],
  });

  if (appPaths.length === 0) {
    throw new Error("packager produced no output");
  }
  return appPaths[0];
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);
  let stagingDir = path.resolve(__dirname, "..", "staging");
  let iconPath = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--staging-dir" && args[i + 1]) {
      stagingDir = path.resolve(args[i + 1]);
      i++;
    } else if (args[i] === "--icon" && args[i + 1]) {
      iconPath = path.resolve(args[i + 1]);
      i++;
    }
  }

  const outDir = path.join(stagingDir, "electron");
  if (await exists(outDir)) await rmrf(outDir);
  await fs.mkdir(outDir, { recursive: true });

  console.log(`  [pack] Output: ${outDir}`);

  // Mode 1: check for local zip
  const localZip = path.join(__dirname, `electron-v${ELECTRON_VERSION}-win32-x64.zip`);
  let appPath;

  if (await exists(localZip)) {
    appPath = await unpackLocalZip(localZip, outDir);
  } else {
    // Mode 2: online fallback
    try {
      appPath = await packWithPackager(outDir);
    } catch (err) {
      console.error("");
      console.error("  [pack] ERROR: Cannot download Electron binary.");
      console.error("");
      console.error(`  [pack] Download manually and place at:`);
      console.error(`    ${localZip}`);
      console.error("");
      console.error("  [pack] Download URLs:");
      console.error(`    https://github.com/electron/electron/releases/download/v${ELECTRON_VERSION}/electron-v${ELECTRON_VERSION}-win32-x64.zip`);
      console.error(`    https://npmmirror.com/mirrors/electron/${ELECTRON_VERSION}/electron-v${ELECTRON_VERSION}-win32-x64.zip`);
      console.error("");
      process.exit(1);
    }
  }

  console.log(`  [pack] Packaged to: ${appPath}`);

  // Post-packaging trim (reaches files at the electron binary level)
  await trimElectron(appPath);

  await applyIcon(appPath, iconPath);

  reportSize(appPath);
  console.log("  [pack] Done.");
}

main().catch((err) => {
  console.error("  [pack] ERROR:", err);
  process.exit(1);
});
