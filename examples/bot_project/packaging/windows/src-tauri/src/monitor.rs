use std::sync::atomic::Ordering;
use std::time::Duration;

use tauri::{AppHandle, Manager};

use crate::bot;
use crate::log;
use crate::{quit_app, AppState};

const MONITOR_INTERVAL_MS: u64 = 5000;
const MAX_FAILURES: u32 = 3;

pub fn start_monitor(app: AppHandle) {
    let h = app.clone();
    std::thread::spawn(move || {
        log(&h, "Bot monitor started.");
        loop {
            std::thread::sleep(Duration::from_millis(MONITOR_INTERVAL_MS));

            let st = h.state::<AppState>();
            if st.is_quitting.load(Ordering::SeqCst) {
                break;
            }

            if bot::is_server_up(st.paths.webui_port) {
                st.consecutive_failures.store(0, Ordering::SeqCst);
                continue;
            }

            let failures = st.consecutive_failures.fetch_add(1, Ordering::SeqCst) + 1;
            log(
                &h,
                &format!("Bot monitor: probe failed ({}/{})", failures, MAX_FAILURES),
            );

            if failures >= MAX_FAILURES {
                log(&h, "Bot appears to have been stopped externally — quitting.");
                quit_app(&h, "Bot stopped externally");
                break;
            }
        }
        log(&h, "Bot monitor stopped.");
    });
}
