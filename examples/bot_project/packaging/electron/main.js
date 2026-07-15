/**
 * ModexBot Electron main process.
 *
 * Lifecycle:
 *   1. Check if bot is already running (quick port probe)
 *   2. If not, spawn bundled Python: python.exe -m modexbot start
 *   3. Create BrowserWindow (hidden until ready-to-show)
 *   4. Poll http://localhost:21800/webui/ until ready
 *   5. Load the WebUI URL in the window
 *   6. On window close → taskkill /T /F the Python process tree → quit
 *
 * Mirrors the logic of launcher.pyw but in Electron:
 *   - No console window (windowsHide: true)
 *   - Single instance lock (second launch focuses existing window)
 *   - Graceful shutdown kills the entire Python process tree
 */

const { app, BrowserWindow } = require("electron");
const { spawn, execSync } = require("child_process");
const path = require("path");
const http = require("http");
const fs = require("fs");

// ── Paths ───────────────────────────────────────────────────────────────────

const IS_PACKAGED = app.isPackaged;

// In production: electron/ is a subdirectory of {app} (install root)
// {app}/electron/ModexBot.exe → parent is {app}
// {app}/python/python.exe, {app}/app/examples/bot_project/
const ELECTRON_DIR = IS_PACKAGED
  ? path.dirname(app.getPath("exe"))
  : __dirname;

const APP_ROOT = IS_PACKAGED
  ? path.resolve(ELECTRON_DIR, "..")
  : path.resolve(__dirname, "..", "..", "..", "..");

const BUNDLED_PYTHON = IS_PACKAGED
  ? path.join(APP_ROOT, "python", "python.exe")
  : process.execPath; // dev: use system/venv Python

const BOT_PROJECT = IS_PACKAGED
  ? path.join(APP_ROOT, "app", "examples", "bot_project")
  : path.resolve(__dirname, "..", ".."); // dev: examples/bot_project

const LOG_DIR = path.join(BOT_PROJECT, "logs");
const LOG_FILE = path.join(LOG_DIR, "electron-launcher.log");

const WEBUI_URL = "http://localhost:21800/webui/";
const POLL_INTERVAL_MS = 1000;
const MAX_WAIT_MS = 90000;

const LOADING_HTML = `data:text/html,${encodeURIComponent(
  `<!DOCTYPE html><html><head><meta charset="utf-8"><title>ModexBot</title><style>` +
    `html,body{margin:0;padding:0;height:100%;}` +
    `body{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;` +
    `background:#fafaf9;color:#18181b;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;}` +
    `.spinner{width:34px;height:34px;border-radius:50%;` +
    `border:3px solid rgba(5,150,105,0.18);border-top-color:#059669;` +
    `animation:spin 0.9s linear infinite;}` +
    `.title{font-size:14px;font-weight:500;letter-spacing:0.01em;}` +
    `.sub{font-size:12px;color:#71717a;}` +
    `@keyframes spin{to{transform:rotate(360deg);}}` +
    `</style></head><body>` +
    `<div class="spinner"></div>` +
    `<div class="title">Starting ModexBot\u2026</div>` +
    `<div class="sub">Waiting for backend to start\u2026</div>` +
    `</body></html>`,
)}`;

// ── State ───────────────────────────────────────────────────────────────────

let mainWindow = null;
let pythonProcess = null;
let isQuitting = false;

// ── Logging ─────────────────────────────────────────────────────────────────

function log(message) {
  const timestamp = new Date().toISOString();
  const line = `[${timestamp}] ${message}\n`;
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(LOG_FILE, line, "utf-8");
  } catch (e) {
    console.error("Failed to write log:", e);
  }
  console.log(line.trim());
}

// ── HTTP server probe ───────────────────────────────────────────────────────

function isServerUp() {
  return new Promise((resolve) => {
    const req = http.get(WEBUI_URL, (res) => {
      res.resume();
      resolve(true);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(2000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForServer(maxWaitMs, intervalMs) {
  const startTime = Date.now();
  while (Date.now() - startTime < maxWaitMs) {
    if (await isServerUp()) return true;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return false;
}

// ── Python bot subprocess ───────────────────────────────────────────────────

function startPythonBot() {
  log("Starting Python bot subprocess...");
  log(`  Python: ${BUNDLED_PYTHON}`);
  log(`  CWD:    ${BOT_PROJECT}`);

  pythonProcess = spawn(
    BUNDLED_PYTHON,
    ["-m", "modexbot", "start"],
    {
      cwd: BOT_PROJECT,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );

  const logStream = fs.createWriteStream(LOG_FILE, { flags: "a" });

  pythonProcess.stdout.on("data", (data) => {
    logStream.write(`[bot stdout] ${data}`);
  });

  pythonProcess.stderr.on("data", (data) => {
    logStream.write(`[bot stderr] ${data}`);
  });

  pythonProcess.on("error", (err) => {
    log(`Python process error: ${err.message}`);
  });

  pythonProcess.on("exit", (code, signal) => {
    log(`Python process exited: code=${code}, signal=${signal}`);
    pythonProcess = null;
    // `modexbot start` is a daemon launcher: it spawns the real bot in the
    // background and exits with code 0 almost immediately. A clean exit is
    // expected — the bot keeps running detached. Only surface an error page
    // when the launcher itself failed (non-zero exit), since that means the
    // bot never started.
    if (code !== 0 && mainWindow && !mainWindow.isDestroyed() && !isQuitting) {
      mainWindow.loadURL(
        `data:text/html,${encodeURIComponent(
          `<html><body style="font-family:system-ui;padding:40px;color:#333">` +
            `<h2>Bot failed to start</h2>` +
            `<p>Exit code: ${code}</p>` +
            `<p>Check logs at:<br><code>${LOG_FILE}</code></p>` +
            `<p>Please restart ModexBot.</p>` +
            `</body></html>`,
        )}`,
      );
    }
  });

  logStream.on("close", () => {
    log("Log stream closed.");
  });

  log(`Python bot started (PID: ${pythonProcess.pid})`);
}

function killPythonBot() {
  // `modexbot start` daemonizes: the spawned launcher exits immediately
  // (code 0) and the real bot runs detached. So pythonProcess is usually
  // null by the time we shut down. Use `modexbot stop` — the CLI finds and
  // stops the background bot regardless of how it was started.
  log("Stopping bot via modexbot stop...");
  try {
    execSync(`"${BUNDLED_PYTHON}" -m modexbot stop`, {
      cwd: BOT_PROJECT,
      stdio: "ignore",
      windowsHide: true,
      timeout: 10000,
    });
    log("modexbot stop completed.");
  } catch (e) {
    log(`modexbot stop failed: ${e.message}`);
    // Fallback: if the launcher process is somehow still alive, kill its
    // tree directly (only relevant if daemonization never happened).
    if (pythonProcess) {
      const pid = pythonProcess.pid;
      log(`Falling back to taskkill PID ${pid}...`);
      try {
        if (process.platform === "win32") {
          execSync(`taskkill /pid ${pid} /f /t`, {
            stdio: "ignore",
            windowsHide: true,
          });
        } else {
          pythonProcess.kill("SIGTERM");
        }
      } catch (_) {
        // already dead
      }
    }
  }

  pythonProcess = null;
  log("Bot stop signal sent.");
}

// ── Window ──────────────────────────────────────────────────────────────────

function createWindow() {
  const iconPath = path.join(__dirname, "logo.ico");
  const iconOpts = fs.existsSync(iconPath) ? { icon: iconPath } : {};

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    title: "ModexBot",
    show: true,
    backgroundColor: "#fafaf9",
    ...iconOpts,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadURL(LOADING_HTML);

  mainWindow.once("ready-to-show", () => {
    log("Window shown (loading screen).");
  });

  // Open external https links in system browser, keep localhost in Electron
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://localhost:21800")) {
      return { action: "allow" };
    }
    return { action: "deny" };
  });

  // Prevent navigation away from the WebUI
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith("http://localhost:21800")) {
      event.preventDefault();
    }
  });

  mainWindow.on("closed", () => {
    log("Window closed.");
    mainWindow = null;
  });

  log("BrowserWindow created.");
}

// ── App lifecycle ───────────────────────────────────────────────────────────

// Single instance lock — second launch focuses existing window
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    log("Electron app ready.");

    // 1. Quick check: is the bot already running?
    const alreadyRunning = await isServerUp();
    if (alreadyRunning) {
      log("Server already running — skipping bot start.");
    } else {
      startPythonBot();
    }

    // 2. Create window (hidden, shows when content loads)
    createWindow();

    // 3. Wait for the HTTP server
    log("Waiting for server to be ready...");
    const ready = await waitForServer(MAX_WAIT_MS, POLL_INTERVAL_MS);

    if (ready) {
      log("Server is ready! Loading WebUI...");
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.loadURL(WEBUI_URL);
      }
    } else {
      log("ERROR: Server did not start within 90s");
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.loadURL(
          `data:text/html,${encodeURIComponent(
            `<html><body style="font-family:system-ui;padding:40px;color:#333">` +
              `<h2>Failed to start</h2>` +
              `<p>The bot server did not respond within 90 seconds.</p>` +
              `<p>Check logs at:<br><code>${LOG_FILE}</code></p>` +
              `</body></html>`,
          )}`,
        );
        mainWindow.show();
      }
    }
  });

  app.on("window-all-closed", () => {
    log("All windows closed. Initiating shutdown...");
    isQuitting = true;
    killPythonBot();
    // Give the bot a moment to clean up, then quit
    setTimeout(() => {
      log("Quitting Electron app.");
      app.quit();
    }, 2000);
  });

  app.on("before-quit", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      isQuitting = true;
      log("before-quit: killing Python bot...");
      killPythonBot();
      setTimeout(() => app.quit(), 2000);
    }
  });

  process.on("SIGINT", () => {
    log("SIGINT received.");
    killPythonBot();
    app.quit();
  });

  process.on("SIGTERM", () => {
    log("SIGTERM received.");
    killPythonBot();
    app.quit();
  });
}
