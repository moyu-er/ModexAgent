// Hide the console window in release builds on Windows.
// Without this, Windows allocates a console for the GUI app and all
// eprint!/println! output appears in a visible terminal window.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// ModexBot Tauri desktop shell — main entry point.
//
// Replaces electron/main.js (479 lines JS) with equivalent Rust logic:
//   - Path resolution (packaged vs dev)
//   - Python bot subprocess management
//   - Window creation with loading → WebUI navigation
//   - System tray (left-click: show, right-click: Show/Exit menu)
//   - Close-to-tray, single-instance lock, bot health monitor
//
// Lifecycle mirrors the Electron app:
//   1. Check if bot is already running (TCP probe)
//   2. If not, spawn bundled Python: python.exe -m modexbot start
//   3. Create window (shows loading page from po-dist/index.html)
//   4. Poll localhost:PORT until ready
//   5. Navigate window to http://localhost:PORT/webui/
//   6. Start bot monitor (polls every 5s; 3 failures → quit)
//   7. On window close → hide to tray (not quit)
//   8. On tray Exit / bot death / SIGINT → modexbot stop → exit
//
// Port is read from config/bot_config.yml (webui.port) via a Python
// one-liner — the single source of truth lives in the YAML, not here.

mod bot;
mod monitor;
mod tray;
mod window;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use tauri::{AppHandle, Manager, RunEvent, WindowEvent};

// ── Shared State ────────────────────────────────────────────────────────────

/// All filesystem paths and runtime constants, resolved at startup.
/// Held inside `AppState` and accessible via `app.state::<AppState>()`.
pub struct AppPaths {
    pub bundled_python: PathBuf,
    pub bot_project: PathBuf,
    pub log_dir: PathBuf,
    pub log_file: PathBuf,
    pub webui_port: u16,
    pub webui_url: String,
    pub is_packaged: bool,
}

/// Global mutable state managed by Tauri.
pub struct AppState {
    pub paths: AppPaths,
    pub is_quitting: AtomicBool,
    pub bot_was_ready: AtomicBool,
    pub consecutive_failures: AtomicU32,
    pub python_pid: Mutex<Option<u32>>,
}

// ── Logging ─────────────────────────────────────────────────────────────────

/// Append a timestamped line to the log file and stderr.
pub fn log(app: &AppHandle, msg: &str) {
    let state = app.state::<AppState>();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let line = format!(
        "[{}.{:03}] {}\n",
        now.as_secs(),
        now.subsec_millis(),
        msg
    );

    let _ = std::fs::create_dir_all(&state.paths.log_dir);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(&state.paths.log_file)
    {
        let _ = std::io::Write::write_all(&mut f, line.as_bytes());
    }
    eprint!("{}", line);
}

// ── Path Resolution ─────────────────────────────────────────────────────────

fn read_port_from_config(python: &std::path::Path, bot_project: &std::path::Path) -> u16 {
    let script = "from bot.config.webui_config import load_webui_port; print(load_webui_port())";
    let mut cmd = std::process::Command::new(python);
    cmd.arg("-c").arg(script);
    cmd.current_dir(bot_project);
    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::null());
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let output = cmd.output();
    match output {
        Ok(o) if o.status.success() => {
            let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
            s.parse::<u16>().unwrap_or(21800)
        }
        _ => 21800,
    }
}

fn resolve_paths() -> AppPaths {
    let exe_dir = std::env::current_exe()
        .expect("failed to get current exe")
        .parent()
        .expect("failed to get exe parent")
        .to_path_buf();

    // Production: exe is at {app}/tauri/ModexBot.exe
    // app_root = exe_dir/.. = {app}/
    let app_root = exe_dir.join("..");
    let is_packaged = app_root.join("python").join("python.exe").exists();

    if is_packaged {
        let bundled_python = app_root.join("python").join("python.exe");
        let bot_project = app_root
            .join("app")
            .join("examples")
            .join("bot_project");
        let log_dir = bot_project.join("logs");
        let log_file = log_dir.join("tauri-launcher.log");
        let webui_port = read_port_from_config(&bundled_python, &bot_project);
        let webui_url = format!("http://localhost:{}/webui/", webui_port);
        AppPaths {
            bundled_python,
            bot_project,
            log_dir,
            log_file,
            webui_port,
            webui_url,
            is_packaged,
        }
    } else {
        // Dev: CARGO_MANIFEST_DIR = .../packaging/windows/src-tauri
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent()
            .unwrap() // windows/
            .parent()
            .unwrap() // packaging/
            .parent()
            .unwrap() // bot_project/
            .parent()
            .unwrap() // examples/
            .parent()
            .unwrap(); // repo_root

        let bundled_python = repo_root
            .join(".venv")
            .join("Scripts")
            .join("python.exe");
        let bot_project = manifest_dir
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .to_path_buf();
        let log_dir = bot_project.join("logs");
        let log_file = log_dir.join("tauri-launcher.log");
        let webui_port = read_port_from_config(&bundled_python, &bot_project);
        let webui_url = format!("http://localhost:{}/webui/", webui_port);
        AppPaths {
            bundled_python,
            bot_project,
            log_dir,
            log_file,
            webui_port,
            webui_url,
            is_packaged,
        }
    }
}

// ── Quit ────────────────────────────────────────────────────────────────────

/// Graceful shutdown: stop bot, destroy tray + window, exit.
/// Idempotent — safe to call multiple times.
pub fn quit_app(app: &AppHandle, reason: &str) {
    let state = app.state::<AppState>();
    if state.is_quitting.load(Ordering::SeqCst) {
        return;
    }
    state.is_quitting.store(true, Ordering::SeqCst);
    log(app, &format!("Quitting: {}", reason));

    bot::kill_bot(app);

    if let Some(w) = app.get_webview_window("main") {
        let _ = w.destroy();
    }

    log(app, "Exiting Tauri app.");
    app.exit(0);
}

// ── Main ────────────────────────────────────────────────────────────────────

fn main() {
    let paths = resolve_paths();
    let state = AppState {
        paths,
        is_quitting: AtomicBool::new(false),
        bot_was_ready: AtomicBool::new(false),
        consecutive_failures: AtomicU32::new(0),
        python_pid: Mutex::new(None),
    };

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Second instance: focus existing window
            window::show_main_window(app);
        }))
        .manage(state)
        .on_window_event(|window, event| {
            // Close button → hide to tray (unless quitting)
            if let WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle();
                let st = app.state::<AppState>();
                if !st.is_quitting.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .setup(|app| {
            let handle = app.handle().clone();

            log(&handle, "Tauri app ready.");

            // 1. Create tray (left-click: show, right-click: Show/Exit)
            tray::create_tray(&handle)?;

            // 2. Create window (loads po-dist/index.html = loading spinner)
            window::create_window(&handle)?;

            // 3. Bot startup sequence in a background thread
            //    (setup callback must return quickly)
            let h = handle.clone();
            std::thread::spawn(move || {
                let st = h.state::<AppState>();
                let p = &st.paths;

                // Quick check: is the bot already running?
                let already_up = bot::is_server_up(p.webui_port);
                if already_up {
                    // Port is occupied — but is it OUR bot or a foreign
                    // instance (e.g. dev-env bot on the same port)?
                    // Silently attaching to a foreign instance is a bug:
                    // the user sees the dev backend instead of the installed one.
                    let ours = bot::is_port_owned_by_us(&h, p.webui_port);
                    if ours {
                        log(&h, "Server already running (our instance) — skipping bot start.");
                        st.bot_was_ready.store(true, Ordering::SeqCst);
                    } else {
                        log(&h, "Port is occupied by a FOREIGN process (not our install).");
                        log(&h, "Cannot start — another ModexBot instance is using the port.");
                        if let Some(w) = h.get_webview_window("main") {
                            let port = p.webui_port;
                            let js = format!(
                                "document.documentElement.innerHTML = \
                                 `<html><body style='font-family:system-ui;padding:40px;color:#333'>\
                                 <h2>Port {} is already in use</h2>\
                                 <p>Another ModexBot instance (likely a development environment) \
                                 is running on port {}.</p>\
                                 <p>Please stop it first, then restart ModexBot.</p>\
                                 </body></html>`",
                                port, port
                            );
                            let _ = w.eval(&js);
                        }
                        return;
                    }
                } else {
                    bot::start_bot(&h);
                }

                // Wait for the HTTP server (up to 90s, poll every 1s)
                log(&h, "Waiting for server to be ready...");
                let ready = bot::wait_for_server(p.webui_port, 90_000, 1_000);

                if ready {
                    st.bot_was_ready.store(true, Ordering::SeqCst);
                    log(&h, "Server is ready! Loading WebUI...");
                    if let Some(w) = h.get_webview_window("main") {
                        let _ = w.eval(&format!(
                            "window.location.replace('{}')",
                            p.webui_url
                        ));
                    }
                    // Start bot health monitor
                    monitor::start_monitor(h.clone());
                } else {
                    log(&h, "ERROR: Server did not start within 90s");
                    if let Some(w) = h.get_webview_window("main") {
                        let log_path =
                            p.log_file.to_string_lossy().replace('\\', "/");
                        let js = format!(
                            "document.documentElement.innerHTML = \
                             `<html><body style='font-family:system-ui;padding:40px;color:#333'>\
                             <h2>Failed to start</h2>\
                             <p>The bot server did not respond within 90 seconds.</p>\
                             <p>Check logs at:<br><code>{}</code></p>\
                             </body></html>`",
                            log_path
                        );
                        let _ = w.eval(&js);
                        let _ = w.show();
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|_app_handle, event| {
        if let RunEvent::Exit = event {}
    });
}
