use tauri::menu::{MenuBuilder, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, TrayIconBuilder, TrayIconEvent};
use tauri::AppHandle;

use crate::log;
use crate::{quit_app, window};

pub fn create_tray(app: &AppHandle) -> tauri::Result<()> {
    let show_item = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
    let exit_item = MenuItem::with_id(app, "exit", "Exit", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;

    let menu = MenuBuilder::new(app)
        .item(&show_item)
        .item(&separator)
        .item(&exit_item)
        .build()?;

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .tooltip("ModexBot")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => window::show_main_window(app),
            "exit" => quit_app(app, "Tray Exit"),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                ..
            } = event
            {
                window::show_main_window(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }

    builder.build(app)?;
    log(app, "Tray created.");
    Ok(())
}
