mod config;
mod navigation;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::{NewWindowResponse, WebviewWindowBuilder},
    Manager, WebviewUrl,
};

use config::current_widget_url;
use navigation::{navigation_decision, NavigationDecision};

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

/// Opens a navigation target with the system handler (browser or mail client).
fn open_external(app: &tauri::AppHandle, url: &tauri::Url) {
    use tauri_plugin_opener::OpenerExt;

    if let Err(error) = app.opener().open_url(url.as_str(), None::<&str>) {
        eprintln!("widget: could not open {url} with the system handler: {error}");
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

/// Creates the `main` window on `{server}/widget` with restricted navigation.
///
/// The window is created here instead of `tauri.conf.json` because
/// `on_navigation` is only available on the builder; the attributes match the
/// previous declaration (440x700, centered, resizable).
fn build_main_window(app: &mut tauri::App) -> tauri::Result<()> {
    let widget_url = current_widget_url();

    let navigation_handle = app.handle().clone();
    let navigation_server = widget_url.clone();
    let new_window_handle = navigation_handle.clone();
    let new_window_server = widget_url.clone();

    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(widget_url))
        .title("Asistente")
        .inner_size(440.0, 700.0)
        .resizable(true)
        .center()
        .on_navigation(
            move |target| match navigation_decision(target, &navigation_server) {
                NavigationDecision::Allow => true,
                NavigationDecision::OpenExternal => {
                    open_external(&navigation_handle, target);
                    false
                }
                NavigationDecision::Block => false,
            },
        )
        .on_new_window(move |target, _features| {
            // The widget is a single window: never create popups, but still
            // send external targets to the system handler.
            if matches!(
                navigation_decision(&target, &new_window_server),
                NavigationDecision::OpenExternal
            ) {
                open_external(&new_window_handle, &target);
            }
            NewWindowResponse::Deny
        })
        .build()?;

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
        .plugin(
            // The webview never opens links through the plugin's JS bridge:
            // navigations are decided in `build_main_window` instead.
            tauri_plugin_opener::Builder::new()
                .open_js_links_on_click(false)
                .build(),
        )
        .setup(move |app| {
            let handle = app.handle().clone();

            build_main_window(app)?;

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
