//! Navigation policy for the widget window.
//!
//! The webview may only navigate inside the configured server origin; external
//! `http(s)` and `mailto` targets go to the system handler instead, and any
//! other scheme is dropped.

use tauri::Url;

/// What to do with a navigation target.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NavigationDecision {
    /// Same origin as the configured server: keep it in the widget window.
    Allow,
    /// External `http(s)`/`mailto` target: open it with the system handler.
    OpenExternal,
    /// Anything else: drop it without opening anything.
    Block,
}

/// Decides what to do with a navigation target inside the widget window.
///
/// `server_url` is the configured server URL: only navigations sharing its
/// origin (scheme, host and port) stay in the webview.
pub fn navigation_decision(target: &Url, server_url: &Url) -> NavigationDecision {
    if target.origin() == server_url.origin() {
        NavigationDecision::Allow
    } else if matches!(target.scheme(), "http" | "https" | "mailto") {
        NavigationDecision::OpenExternal
    } else {
        NavigationDecision::Block
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(value: &str) -> Url {
        Url::parse(value).expect("test URL must parse")
    }

    fn server() -> Url {
        url("http://127.0.0.1:8000")
    }

    #[test]
    fn same_origin_navigations_are_allowed() {
        for target in [
            "http://127.0.0.1:8000/widget",
            "http://127.0.0.1:8000/api/v1/auth/refresh",
            "http://127.0.0.1:8000/",
        ] {
            assert_eq!(
                navigation_decision(&url(target), &server()),
                NavigationDecision::Allow,
                "target={target}"
            );
        }
    }

    #[test]
    fn different_port_is_external() {
        assert_eq!(
            navigation_decision(&url("http://127.0.0.1:9000/widget"), &server()),
            NavigationDecision::OpenExternal
        );
    }

    #[test]
    fn different_scheme_is_external() {
        assert_eq!(
            navigation_decision(&url("https://127.0.0.1:8000/widget"), &server()),
            NavigationDecision::OpenExternal
        );
    }

    #[test]
    fn different_host_is_external() {
        for target in [
            "https://example.com/",
            "http://docs.example.com:8000/",
            "http://127.0.0.2:8000/",
        ] {
            assert_eq!(
                navigation_decision(&url(target), &server()),
                NavigationDecision::OpenExternal,
                "target={target}"
            );
        }
    }

    #[test]
    fn mailto_targets_open_externally() {
        assert_eq!(
            navigation_decision(&url("mailto:help@example.com"), &server()),
            NavigationDecision::OpenExternal
        );
    }

    #[test]
    fn non_web_schemes_are_blocked() {
        for target in [
            "about:blank",
            "tauri://localhost/",
            "data:text/plain,hello",
            "tel:+123456",
            "file:///etc/hosts",
        ] {
            assert_eq!(
                navigation_decision(&url(target), &server()),
                NavigationDecision::Block,
                "target={target}"
            );
        }
    }
}
