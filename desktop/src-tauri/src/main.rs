use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:8000";

fn server_url() -> String {
    std::env::var("AA_SERVER_URL").unwrap_or_else(|_| DEFAULT_SERVER_URL.to_string())
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn toggle_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            show_main(app);
        }
    }
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let toggle_item = MenuItem::with_id(app, "toggle", "Mostrar / Ocultar", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Salir", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&toggle_item, &quit_item])?;

    TrayIconBuilder::with_id("main-tray")
        .icon(app.default_window_icon().unwrap().clone())
        .tooltip("Asistente")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "toggle" => toggle_main(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let test_notification = args.iter().any(|a| a == "--test-notification");

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            if argv.iter().any(|a| a == "--toggle") {
                toggle_main(app);
            } else {
                show_main(app);
            }
        }))
        .plugin(tauri_plugin_notification::init())
        .setup(move |app| {
            let handle = app.handle().clone();

            if let Ok(url) = server_url().parse::<tauri::Url>() {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.navigate(url);
                }
            }

            build_tray(&handle)?;

            if test_notification {
                use tauri_plugin_notification::NotificationExt;
                let _ = handle
                    .notification()
                    .builder()
                    .title("Asistente")
                    .body("Notificación de prueba del widget")
                    .show();
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error al iniciar el widget");
}
