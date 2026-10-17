//! X11-only native global shortcut (W4).
//!
//! On Wayland the Tauri global-shortcut plugin cannot register shortcuts: the
//! supported path there is a GNOME custom shortcut that runs
//! `admin-agent-widget --toggle` against the single instance. This module owns
//! the accelerator and the session-type decision, so the registration itself
//! stays a thin, smoke-verified step.

/// Accelerator registered while the session is X11.
///
/// `Ctrl+Alt+W` is deliberately avoided by GNOME's shell keybindings (unlike
/// most `Super`-based combinations), so it can be grabbed without conflicting
/// with the desktop.
pub const TOGGLE_SHORTCUT: &str = "Ctrl+Alt+W";

/// Whether the native global shortcut should be registered.
///
/// Only an X11 session is supported: `XDG_SESSION_TYPE` is trusted when it
/// reads `x11` (case-insensitive, trimmed). Anything else - `wayland`, unset or
/// an unexpected value - skips registration and leaves the GNOME custom
/// shortcut as the documented mechanism.
pub fn should_register_global_shortcut(session_type: Option<&str>) -> bool {
    session_type
        .map(str::trim)
        .map(|session| session.eq_ignore_ascii_case("x11"))
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut};

    #[test]
    fn x11_sessions_register_the_shortcut() {
        for session in [Some("x11"), Some("X11"), Some(" x11 ")] {
            assert!(
                should_register_global_shortcut(session),
                "session={session:?}"
            );
        }
    }

    #[test]
    fn wayland_and_unknown_sessions_skip_the_shortcut() {
        for session in [
            Some("wayland"),
            Some("Wayland"),
            Some("tty"),
            Some(""),
            None,
        ] {
            assert!(
                !should_register_global_shortcut(session),
                "session={session:?}"
            );
        }
    }

    #[test]
    fn toggle_shortcut_constant_is_a_valid_accelerator() {
        let parsed: Shortcut = TOGGLE_SHORTCUT
            .parse()
            .expect("TOGGLE_SHORTCUT must be a valid accelerator");

        assert_eq!(
            parsed,
            Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyW)
        );
    }
}
