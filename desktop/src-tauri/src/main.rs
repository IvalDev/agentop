mod config;
mod navigation;
mod shortcut;

use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::{NewWindowResponse, WebviewWindowBuilder},
    Manager, WebviewUrl,
};

use config::{
    current_preferences_state, current_widget_url, persist_current_preference, PreferenceKey,
    Preferences,
};
use navigation::{navigation_decision, NavigationDecision};

/// Checkable tray items kept in managed state so menu clicks can read the
/// state the click left them in.
struct TrayMenu {
    always_on_top: CheckMenuItem<tauri::Wry>,
    autostart: CheckMenuItem<tauri::Wry>,
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

/// Opens a navigation target with the system handler (browser or mail client).
fn open_external(app: &tauri::AppHandle, url: &tauri::Url) {
    use tauri_plugin_opener::OpenerExt;

    if let Err(error) = app.opener().open_url(url.as_str(), None::<&str>) {
        eprintln!("widget: could not open {url} with the system handler: {error}");
    }
}

/// Applies the new "Siempre visible" state and persists it.
fn toggle_always_on_top(app: &tauri::AppHandle) {
    let value = app
        .try_state::<TrayMenu>()
        .and_then(|menu| menu.always_on_top.is_checked().ok())
        .unwrap_or(false);

    if let Some(window) = app.get_webview_window("main") {
        if let Err(error) = window.set_always_on_top(value) {
            eprintln!("widget: could not change always-on-top: {error}");
        }
    }

    persist_current_preference(PreferenceKey::AlwaysOnTop, value);
}

/// Applies the new "Arranque con el sistema" state and persists it.
fn toggle_autostart(app: &tauri::AppHandle) {
    let value = app
        .try_state::<TrayMenu>()
        .and_then(|menu| menu.autostart.is_checked().ok())
        .unwrap_or(false);

    apply_autostart(app, value);
    persist_current_preference(PreferenceKey::Autostart, value);
}

/// Enables or disables autostart through the plugin, logging failures.
fn apply_autostart(app: &tauri::AppHandle, enabled: bool) {
    use tauri_plugin_autostart::ManagerExt;

    let manager = app.autolaunch();
    let result = if enabled {
        manager.enable()
    } else {
        manager.disable()
    };
    if let Err(error) = result {
        eprintln!(
            "widget: could not {} autostart: {error}",
            if enabled { "enable" } else { "disable" }
        );
    }
}

/// Makes the plugin state match `desired` and returns what the menu must show.
///
/// Runs on every start: the persisted preference is the source of truth and
/// the plugin is just the mechanism that applies it.
fn reconcile_autostart(app: &tauri::AppHandle, desired: bool) -> bool {
    use tauri_plugin_autostart::ManagerExt;

    let manager = app.autolaunch();
    let current = match manager.is_enabled() {
        Ok(current) => current,
        Err(error) => {
            eprintln!("widget: could not read the autostart state: {error}");
            false
        }
    };

    if current == desired {
        return current;
    }

    match if desired {
        manager.enable()
    } else {
        manager.disable()
    } {
        Ok(()) => desired,
        Err(error) => {
            eprintln!(
                "widget: could not {} autostart: {error}",
                if desired { "enable" } else { "disable" }
            );
            current
        }
    }
}

/// Builds the tray icon and its menu: toggles first, separator, exit last.
fn build_tray(app: &tauri::AppHandle, always_on_top: bool, autostart: bool) -> tauri::Result<()> {
    let toggle_item = MenuItem::with_id(app, "toggle", "Mostrar / Ocultar", true, None::<&str>)?;
    let always_on_top_item = CheckMenuItem::with_id(
        app,
        "always_on_top",
        "Siempre visible",
        true,
        always_on_top,
        None::<&str>,
    )?;
    let autostart_item = CheckMenuItem::with_id(
        app,
        "autostart",
        "Arranque con el sistema",
        true,
        autostart,
        None::<&str>,
    )?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit_item = MenuItem::with_id(app, "quit", "Salir", true, None::<&str>)?;

    let menu = Menu::with_items(
        app,
        &[
            &toggle_item,
            &always_on_top_item,
            &autostart_item,
            &separator,
            &quit_item,
        ],
    )?;

    app.manage(TrayMenu {
        always_on_top: always_on_top_item.clone(),
        autostart: autostart_item.clone(),
    });

    TrayIconBuilder::with_id("main-tray")
        .icon(app.default_window_icon().unwrap().clone())
        .tooltip("Asistente")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "toggle" => toggle_main(app),
            "always_on_top" => toggle_always_on_top(app),
            "autostart" => toggle_autostart(app),
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
/// previous declaration (440x700, centered, resizable). Closing the window
/// hides it: the tray keeps the widget alive and only "Salir" exits.
fn build_main_window(app: &mut tauri::App, preferences: &Preferences) -> tauri::Result<()> {
    let widget_url = current_widget_url();

    let navigation_handle = app.handle().clone();
    let navigation_server = widget_url.clone();
    let new_window_handle = navigation_handle.clone();
    let new_window_server = widget_url.clone();

    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(widget_url))
        .title("Asistente")
        .inner_size(440.0, 700.0)
        .resizable(true)
        .center()
        .always_on_top(preferences.always_on_top)
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

    let close_handle = window.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            if let Err(error) = close_handle.hide() {
                eprintln!("widget: could not hide the window: {error}");
            }
        }
    });

    Ok(())
}

/// Registers the native global shortcut (X11 only).
///
/// The plugin is only initialized when the session is X11, so this function is
/// never reached on Wayland; the documented mechanism there is the GNOME custom
/// shortcut that runs `admin-agent-widget --toggle`.
fn register_global_shortcut(app: &tauri::AppHandle) {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut};

    match shortcut::TOGGLE_SHORTCUT.parse::<Shortcut>() {
        Ok(accelerator) => {
            if let Err(error) = app.global_shortcut().register(accelerator) {
                eprintln!(
                    "widget: could not register the {} shortcut: {error}",
                    shortcut::TOGGLE_SHORTCUT
                );
            }
        }
        Err(error) => eprintln!(
            "widget: invalid shortcut constant {}: {error}",
            shortcut::TOGGLE_SHORTCUT
        ),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let test_notification = args.iter().any(|a| a == "--test-notification");

    // The native shortcut only exists on X11. On Wayland the plugin is not even
    // initialized (its setup opens an X11 connection) and the supported path is
    // the GNOME custom shortcut calling `--toggle` (W4).
    let native_shortcut = shortcut::should_register_global_shortcut(
        std::env::var("XDG_SESSION_TYPE").ok().as_deref(),
    );

    let builder = tauri::Builder::default()
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
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ));

    let builder = if native_shortcut {
        builder.plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state == tauri_plugin_global_shortcut::ShortcutState::Pressed {
                        toggle_main(app);
                    }
                })
                .build(),
        )
    } else {
        builder
    };

    builder
        .setup(move |app| {
            let handle = app.handle().clone();
            let preferences_state = current_preferences_state();

            build_main_window(app, &preferences_state.preferences)?;

            // First run: the autostart key is absent, so the resolved default
            // (enabled) is persisted before reconciling, keeping the file and
            // the plugin in sync. An explicit `false` always wins.
            if !preferences_state.autostart_persisted {
                persist_current_preference(
                    PreferenceKey::Autostart,
                    preferences_state.preferences.autostart,
                );
            }

            let autostart = reconcile_autostart(&handle, preferences_state.preferences.autostart);

            build_tray(
                &handle,
                preferences_state.preferences.always_on_top,
                autostart,
            )?;

            if native_shortcut {
                register_global_shortcut(&handle);
            }

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
