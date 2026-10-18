//! Native notifications for completed assistant responses (W5).
//!
//! The widget window may be hidden, minimized or behind other windows while a
//! reply finishes. The SPA announces the completion as a plain DOM event
//! (`aa:assistant-response-done`, see `web/src/chat/responseNotifications.ts`)
//! and the bridge script injected into the window forwards it to the
//! `assistant_response_done` command, which shows a desktop notification and,
//! on click, brings the window back.

use tauri::AppHandle;

/// The DOM event the SPA dispatches when an assistant response completes.
pub const RESPONSE_DONE_EVENT: &str = "aa:assistant-response-done";

/// The command the bridge script invokes; registered in `main.rs`.
pub const RESPONSE_DONE_COMMAND: &str = "assistant_response_done";

/// Longest preview shown in the notification body, in code points (the SPA
/// clamps too: `ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS`).
pub const PREVIEW_MAX_CHARS: usize = 140;

/// Notification title, matching the tray tooltip and the window title.
pub const NOTIFICATION_TITLE: &str = "Asistente";

/// Notification body when the reply came without a usable preview.
pub const NOTIFICATION_FALLBACK_BODY: &str = "El asistente terminó de responder";

/// Whether a completed response deserves a notification: the window is not in
/// front (hidden, minimized, or unfocused).
pub fn should_notify(visible: bool, focused: bool, minimized: bool) -> bool {
    !visible || !focused || minimized
}

/// The preview carried into the notification body: trimmed and cut to
/// [`PREVIEW_MAX_CHARS`] code points. `None` when there is nothing to show.
pub fn clamp_preview(preview: Option<String>) -> Option<String> {
    let preview = preview?;
    let trimmed = preview.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(trimmed.chars().take(PREVIEW_MAX_CHARS).collect())
}

/// The script injected into the widget window before the page loads.
///
/// It listens for the SPA's completion event and forwards the preview to the
/// native side. Everything is guarded so a missing Tauri internals object never
/// throws into the page; the SPA itself stays free of Tauri imports.
pub fn bridge_script() -> String {
    format!(
        r#"(function () {{
  window.addEventListener('{event}', function (event) {{
    try {{
      var internals = window.__TAURI_INTERNALS__;
      if (!internals || typeof internals.invoke !== 'function') {{
        return;
      }}
      var detail = event ? event.detail : null;
      var preview = detail && typeof detail.preview === 'string' ? detail.preview : undefined;
      internals.invoke('{command}', {{ preview: preview }}).catch(function () {{
        // The native side did not accept the event; the page continues unaffected.
      }});
    }} catch (error) {{
      // The bridge must never throw into the page.
    }}
  }});
}})();"#,
        event = RESPONSE_DONE_EVENT,
        command = RESPONSE_DONE_COMMAND,
    )
}

/// Shows the "response completed" notification when the window is not in front,
/// and always leaves one trace line for the widget log. Shared by the
/// `assistant_response_done` command and the `--test-response-notification` flag.
pub fn notify_response_done(app: &AppHandle, preview: Option<String>) {
    use tauri::Manager;

    let preview = clamp_preview(preview);
    let Some(window) = app.get_webview_window("main") else {
        eprintln!(
            "widget: response done (window=missing, preview={})",
            preview.is_some()
        );
        return;
    };

    let visible = window.is_visible().unwrap_or(false);
    let focused = window.is_focused().unwrap_or(false);
    let minimized = window.is_minimized().unwrap_or(false);
    let notify = should_notify(visible, focused, minimized);
    eprintln!(
        "widget: response done (visible={visible}, focused={focused}, minimized={minimized}, notify={notify}, preview={})",
        preview.is_some()
    );
    if !notify {
        return;
    }

    use tauri_plugin_notification::NotificationExt;

    let body = preview.unwrap_or_else(|| NOTIFICATION_FALLBACK_BODY.to_owned());
    if let Err(error) = app
        .notification()
        .builder()
        .title(NOTIFICATION_TITLE)
        .body(body)
        .show()
    {
        eprintln!("widget: could not show the response notification: {error}");
    }
}

/// Registers the click handler of the app's notifications: a click brings the
/// window back. Must run before the first notification is shown, since only
/// reported notifications offer the `default` action the desktop servers send.
pub fn register_action_handler(app: &AppHandle) {
    use tauri_plugin_notification::NotificationExt;

    let handle = app.clone();
    if let Err(error) = app.notification().on_action(move |performed| {
        // A click on the notification itself arrives as `tap`, the desktop name
        // of the `default` action the plugin adds while something listens.
        if performed.action_id() == "tap" {
            eprintln!("widget: response notification clicked, showing the window");
            focus_main(&handle);
        }
    }) {
        eprintln!("widget: could not listen for notification actions: {error}");
    }
}

/// Brings the window back to the user after a notification click: unminimize
/// first so a minimized window comes back visible, then show and focus.
fn focus_main(app: &AppHandle) {
    use tauri::Manager;

    if let Some(window) = app.get_webview_window("main") {
        if let Err(error) = window.unminimize() {
            eprintln!("widget: could not unminimize the window: {error}");
        }
    }
    crate::show_main(app);
}

/// `--test-response-notification`: hides the window and sends the same event the
/// SPA sends when a response completes, so the smoke observes the real path
/// (bridge script -> command -> notification) without chatting.
pub fn test_response_notification(window: &tauri::WebviewWindow) {
    eprintln!("widget: test response notification reached (hiding and dispatching)");
    if let Err(error) = window.hide() {
        eprintln!("widget: could not hide the window for the notification test: {error}");
    }

    let event = RESPONSE_DONE_EVENT;
    let preview = "Prueba del widget: terminó una respuesta";
    let script =
        format!("window.dispatchEvent(new CustomEvent('{event}', {{ detail: {{ preview: '{preview}' }} }}))");
    if let Err(error) = window.eval(script) {
        eprintln!("widget: could not dispatch the test response event: {error}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_hidden_window_needs_a_notification_even_if_flagged_as_focused() {
        assert!(should_notify(false, true, false));
        assert!(should_notify(false, false, false));
    }

    #[test]
    fn a_visible_focused_window_needs_no_notification() {
        assert!(!should_notify(true, true, false));
    }

    #[test]
    fn a_minimized_or_unfocused_window_needs_a_notification() {
        assert!(should_notify(true, false, false));
        assert!(should_notify(true, true, true));
        assert!(should_notify(true, false, true));
    }

    #[test]
    fn a_short_preview_keeps_its_trimmed_text() {
        assert_eq!(
            clamp_preview(Some("  Tienes 23 días.  ".to_owned())),
            Some("Tienes 23 días.".to_owned())
        );
    }

    #[test]
    fn a_long_preview_is_cut_at_the_cap_counting_code_points() {
        let answer = "😀".repeat(PREVIEW_MAX_CHARS + 10);

        let clamped = clamp_preview(Some(answer)).expect("a non-empty preview is kept");

        assert_eq!(clamped.chars().count(), PREVIEW_MAX_CHARS);
        assert_eq!(clamped, "😀".repeat(PREVIEW_MAX_CHARS));
    }

    #[test]
    fn a_blank_or_missing_preview_has_nothing_to_show() {
        assert_eq!(clamp_preview(Some("   \n ".to_owned())), None);
        assert_eq!(clamp_preview(None), None);
    }

    #[test]
    fn the_bridge_script_forwards_the_event_to_the_command() {
        let script = bridge_script();

        assert!(script.contains(RESPONSE_DONE_EVENT), "script: {script}");
        assert!(script.contains(RESPONSE_DONE_COMMAND), "script: {script}");
        assert!(script.contains("__TAURI_INTERNALS__"), "script: {script}");
        assert!(script.contains("try"), "script: {script}");
        assert!(script.contains("catch"), "script: {script}");
    }
}
