use std::net::{SocketAddr, TcpStream};
use std::process::{Command, Stdio};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};

use crate::log;
use crate::AppState;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub fn is_server_up(port: u16) -> bool {
    let addr: SocketAddr = format!("127.0.0.1:{}", port).parse().unwrap();
    TcpStream::connect_timeout(&addr, Duration::from_secs(2)).is_ok()
}

/// Check whether the process listening on *port* belongs to this install.
///
/// Calls the bundled Python to run `modexbot status`, which uses the same
/// PID-file + port-scan discovery as the CLI. If the discovered process's
/// command line contains our bot_project path, it's ours; otherwise it's a
/// foreign instance (e.g. a dev-env bot on the same port) and we must NOT
/// silently attach to it.
pub fn is_port_owned_by_us(app: &AppHandle, port: u16) -> bool {
    let st = app.state::<AppState>();
    let p = &st.paths;

    // One-shot Python script: find PIDs on the port, print each command line.
    // We then check whether any of them reference our bot_project directory.
    let script = format!(
        r#"
import subprocess, sys, os
try:
    r = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True, text=True, timeout=5,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    pids = set()
    for line in r.stdout.splitlines():
        if ":{port}" in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                pids.add(parts[-1])
    for pid in pids:
        try:
            import psutil
            p = psutil.Process(int(pid))
            cl = " ".join(p.cmdline())
            cwd = str(p.cwd())
            print(f"PID={{pid}} CWD={{cwd}} CMD={{cl}}")
        except Exception:
            pass
except Exception:
    pass
"#,
        port = port
    );

    let mut cmd = Command::new(&p.bundled_python);
    cmd.arg("-c").arg(&script);
    cmd.current_dir(&p.bot_project);
    cmd.stdin(Stdio::null());
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::null());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let output = match cmd.output() {
        Ok(o) => o,
        Err(_) => return false,
    };

    let stdout = String::from_utf8_lossy(&output.stdout);
    let our_path = p.bot_project.to_string_lossy().to_lowercase();
    for line in stdout.lines() {
        if line.to_lowercase().contains(&our_path) {
            return true;
        }
    }
    false
}

pub fn wait_for_server(port: u16, max_wait_ms: u64, interval_ms: u64) -> bool {
    let start = Instant::now();
    let max_wait = Duration::from_millis(max_wait_ms);
    let interval = Duration::from_millis(interval_ms);
    while start.elapsed() < max_wait {
        if is_server_up(port) {
            return true;
        }
        std::thread::sleep(interval);
    }
    false
}

pub fn start_bot(app: &AppHandle) {
    let st = app.state::<AppState>();
    let p = &st.paths;

    log(app, "Starting Python bot subprocess...");
    log(app, &format!("  Python: {}", p.bundled_python.display()));
    log(app, &format!("  CWD:    {}", p.bot_project.display()));
    log(app, &format!("  Port:   {}", p.webui_port));

    let mut cmd = Command::new(&p.bundled_python);
    cmd.args(["-m", "modexbot", "start", "--port", &p.webui_port.to_string()]);
    cmd.current_dir(&p.bot_project);
    cmd.stdin(Stdio::null());
    cmd.stdout(Stdio::null());
    cmd.stderr(Stdio::null());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    match cmd.spawn() {
        Ok(child) => {
            let pid = child.id();
            *st.python_pid.lock().unwrap() = Some(pid);
            log(app, &format!("Python bot started (PID: {})", pid));

            let h = app.clone();
            std::thread::spawn(move || {
                let output = child.wait_with_output();
                let st = h.state::<AppState>();
                *st.python_pid.lock().unwrap() = None;

                let code = match output {
                    Ok(o) => o.status.code().unwrap_or(-1),
                    Err(_) => -1,
                };
                log(&h, &format!("Python process exited: code={}", code));

                if code != 0 && !st.is_quitting.load(Ordering::SeqCst) {
                    if let Some(w) = h.get_webview_window("main") {
                        let log_path =
                            st.paths.log_file.to_string_lossy().replace('\\', "/");
                        let js = format!(
                            "document.documentElement.innerHTML = \
                             `<html><body style='font-family:system-ui;padding:40px;color:#333'>\
                             <h2>Bot failed to start</h2>\
                             <p>Exit code: {}</p>\
                             <p>Check logs at:<br><code>{}</code></p>\
                             <p>Please restart ModexBot.</p>\
                             </body></html>`",
                            code, log_path
                        );
                        let _ = w.eval(&js);
                    }
                }
            });
        }
        Err(e) => {
            log(app, &format!("Failed to start Python bot: {}", e));
        }
    }
}

pub fn kill_bot(app: &AppHandle) {
    let st = app.state::<AppState>();
    let p = &st.paths;

    log(app, "Stopping bot via modexbot stop...");

    let mut cmd = Command::new(&p.bundled_python);
    cmd.args(["-m", "modexbot", "stop", "--port", &p.webui_port.to_string()]);
    cmd.current_dir(&p.bot_project);
    cmd.stdin(Stdio::null());
    cmd.stdout(Stdio::null());
    cmd.stderr(Stdio::null());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    match cmd.output() {
        Ok(_) => log(app, "modexbot stop completed."),
        Err(e) => {
            log(app, &format!("modexbot stop failed: {}", e));
            let pid = st.python_pid.lock().unwrap();
            if let Some(pid) = *pid {
                log(app, &format!("Falling back to taskkill PID {}...", pid));
                let mut kill_cmd = Command::new("taskkill");
                kill_cmd.args(["/pid", &pid.to_string(), "/f", "/t"]);
                #[cfg(windows)]
                kill_cmd.creation_flags(CREATE_NO_WINDOW);
                let _ = kill_cmd.output();
            }
        }
    }

    *st.python_pid.lock().unwrap() = None;
    log(app, "Bot stop signal sent.");
}
