use std::sync::atomic::Ordering;

use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

use crate::log;
use crate::AppState;

const WINDOW_LABEL: &str = "main";

pub fn create_window(app: &AppHandle) -> tauri::Result<()> {
    let _builder = WebviewWindowBuilder::new(
        app,
        WINDOW_LABEL,
        WebviewUrl::App("index.html".into()),
    )
    .title("ModexBot")
    .inner_size(1400.0, 900.0)
    .min_inner_size(800.0, 600.0)
    .visible(true)
    .on_navigation(|url| {
        let s = url.as_str();
        s.starts_with("http://tauri.localhost")
            || s.starts_with("http://localhost")
            || s.starts_with("http://127.0.0.1")
            || s.starts_with("data:")
    })
    .build()?;

    log(app, "Window created.");
    Ok(())
}

pub fn show_main_window(app: &AppHandle) {
    match app.get_webview_window(WINDOW_LABEL) {
        Some(w) => {
            if w.is_minimized().unwrap_or(false) {
                let _ = w.unminimize();
            }
            let _ = w.show();
            let _ = w.set_focus();
        }
        None => {
            let _ = create_window(app);
            let st = app.state::<AppState>();
            if st.bot_was_ready.load(Ordering::SeqCst) {
                if let Some(w) = app.get_webview_window(WINDOW_LABEL) {
                    let _ = w.eval(&format!(
                        "window.location.replace('{}')",
                        st.paths.webui_url
                    ));
                }
            }
        }
    }
}
