//! Server URL resolution for the widget window (W6).
//!
//! Precedence: the `AA_SERVER_URL` environment variable, then the XDG config
//! file (`$XDG_CONFIG_HOME/admin-agent-widget/config.toml`, with `$HOME/.config`
//! as fallback), then the default `http://127.0.0.1:8000`. Missing or malformed
//! input never aborts startup.

use std::path::{Path, PathBuf};
use tauri::Url;

/// Fallback used when neither the environment nor the config file resolve.
pub const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:8000";

/// Directory (under the XDG config home) that holds the widget config file.
const CONFIG_DIR: &str = "admin-agent-widget";

/// Config file name inside [`CONFIG_DIR`].
const CONFIG_FILE: &str = "config.toml";

/// Key read from the config file.
const CONFIG_KEY: &str = "server_url";

/// Returns the widget URL (`{server}/widget`) from the current environment.
///
/// Reads the environment and the XDG config file on a best-effort basis; the
/// default is used when neither yields a usable URL.
pub fn current_widget_url() -> Url {
    let env_value = std::env::var("AA_SERVER_URL").ok();
    let xdg_config_home = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let config_contents = config_file_path(xdg_config_home.as_deref(), home.as_deref())
        .and_then(|path| std::fs::read_to_string(path).ok());

    resolve_widget_url(env_value.as_deref(), config_contents.as_deref())
}

/// Resolves the widget URL (`{server}/widget`) from the two configuration sources.
pub fn resolve_widget_url(env_value: Option<&str>, config_contents: Option<&str>) -> Url {
    widget_url(&resolve_server_url(env_value, config_contents))
}

/// Resolves the server URL from the two configuration sources.
///
/// The first candidate that is a valid absolute `http(s)` URL wins; invalid or
/// empty candidates are skipped. Falls back to [`DEFAULT_SERVER_URL`].
pub fn resolve_server_url(env_value: Option<&str>, config_contents: Option<&str>) -> Url {
    let env_url = env_value.and_then(parse_server_url);
    let file_url = config_contents
        .and_then(server_url_from_toml)
        .and_then(|value| parse_server_url(&value));

    env_url.or(file_url).unwrap_or_else(|| {
        Url::parse(DEFAULT_SERVER_URL).expect("default server URL must be a valid URL")
    })
}

/// Joins the configured server URL with the widget path (`{server}/widget`).
///
/// Keeps any configured path prefix and drops query and fragment.
pub fn widget_url(server_url: &Url) -> Url {
    let mut url = server_url.clone();
    let path = format!("{}/widget", url.path().trim_end_matches('/'));
    url.set_path(&path);
    url.set_query(None);
    url.set_fragment(None);
    url
}

/// XDG config file path: `$XDG_CONFIG_HOME/admin-agent-widget/config.toml` with
/// `$HOME/.config` as fallback.
///
/// A relative or empty `XDG_CONFIG_HOME` is ignored, as required by the XDG
/// base directory specification.
pub fn config_file_path(xdg_config_home: Option<&Path>, home: Option<&Path>) -> Option<PathBuf> {
    let base = match xdg_config_home {
        Some(path) if path.is_absolute() => path.to_path_buf(),
        _ => home?.join(".config"),
    };
    Some(base.join(CONFIG_DIR).join(CONFIG_FILE))
}

/// Parses a candidate server URL, accepting only absolute `http(s)` URLs.
fn parse_server_url(value: &str) -> Option<Url> {
    let url = value.trim().parse::<Url>().ok()?;
    matches!(url.scheme(), "http" | "https").then_some(url)
}

/// Reads the `server_url` key from TOML contents; `None` when missing or malformed.
fn server_url_from_toml(contents: &str) -> Option<String> {
    let document: toml::Value = toml::from_str(contents).ok()?;
    let value = document.get(CONFIG_KEY)?.as_str()?.trim();
    (!value.is_empty()).then(|| value.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(value: &str) -> Url {
        Url::parse(value).expect("test URL must parse")
    }

    #[test]
    fn env_var_wins_over_config_file_and_default() {
        let resolved = resolve_server_url(
            Some("http://env.example:9000"),
            Some("server_url = \"http://file.example:9100\""),
        );
        assert_eq!(resolved.as_str(), "http://env.example:9000/");
    }

    #[test]
    fn env_var_is_trimmed() {
        let resolved = resolve_server_url(Some("  http://env.example:9000  "), None);
        assert_eq!(resolved.scheme(), "http");
        assert_eq!(resolved.host_str(), Some("env.example"));
        assert_eq!(resolved.port(), Some(9000));
    }

    #[test]
    fn empty_or_invalid_env_var_falls_back_to_config_file() {
        let config = "server_url = \"http://file.example:9100\"";

        for env in ["", "   ", "not a url", "ftp://file.example:9100"] {
            let resolved = resolve_server_url(Some(env), Some(config));
            assert_eq!(
                resolved.as_str(),
                "http://file.example:9100/",
                "env={env:?}"
            );
        }
    }

    #[test]
    fn config_file_wins_over_default() {
        let resolved =
            resolve_server_url(None, Some("server_url = \"https://example.com/console\""));
        assert_eq!(resolved.as_str(), "https://example.com/console");
    }

    #[test]
    fn malformed_config_file_falls_back_to_default() {
        for contents in [
            "server_url = \"http://unterminated",
            "server_url = ",
            "[\n",
            "",
            "# server_url = \"http://commented\"",
            "server_url = 42",
            "server_url = \"\"",
            "server_url = \"   \"",
            "server_url = \"ftp://file.example\"",
            "server_url = \"not a url\"",
            "other_key = \"http://other.example\"",
        ] {
            let resolved = resolve_server_url(None, Some(contents));
            assert_eq!(
                resolved.as_str(),
                "http://127.0.0.1:8000/",
                "contents={contents:?}"
            );
        }
    }

    #[test]
    fn missing_sources_fall_back_to_default() {
        assert_eq!(
            resolve_server_url(None, None).as_str(),
            "http://127.0.0.1:8000/"
        );
    }

    #[test]
    fn config_file_ignores_comments_and_extra_keys() {
        let contents =
            "# widget configuration\nother = true\nserver_url = \"http://file.example:9100\" # inline comment\n";
        let resolved = resolve_server_url(None, Some(contents));
        assert_eq!(resolved.host_str(), Some("file.example"));
        assert_eq!(resolved.port(), Some(9100));
    }

    #[test]
    fn default_server_url_is_a_valid_absolute_url() {
        let default = url(DEFAULT_SERVER_URL);
        assert_eq!(default.scheme(), "http");
        assert_eq!(default.port_or_known_default(), Some(8000));
    }

    #[test]
    fn widget_url_joins_the_path_on_a_bare_origin() {
        assert_eq!(
            widget_url(&url("http://127.0.0.1:8000")).as_str(),
            "http://127.0.0.1:8000/widget"
        );
    }

    #[test]
    fn widget_url_ignores_a_trailing_slash() {
        assert_eq!(
            widget_url(&url("http://127.0.0.1:8000/")).as_str(),
            "http://127.0.0.1:8000/widget"
        );
    }

    #[test]
    fn widget_url_keeps_a_configured_path_prefix() {
        assert_eq!(
            widget_url(&url("https://example.com/console")).as_str(),
            "https://example.com/console/widget"
        );
        assert_eq!(
            widget_url(&url("https://example.com/console/")).as_str(),
            "https://example.com/console/widget"
        );
    }

    #[test]
    fn widget_url_drops_query_and_fragment() {
        assert_eq!(
            widget_url(&url("https://example.com/console?a=1#top")).as_str(),
            "https://example.com/console/widget"
        );
    }

    #[test]
    fn resolve_widget_url_combines_precedence_and_join() {
        assert_eq!(
            resolve_widget_url(Some("http://env.example:9000/console/"), None).as_str(),
            "http://env.example:9000/console/widget"
        );
        assert_eq!(
            resolve_widget_url(None, Some("server_url = \"https://file.example\"")).as_str(),
            "https://file.example/widget"
        );
        assert_eq!(
            resolve_widget_url(Some("garbage"), Some("not toml")).as_str(),
            "http://127.0.0.1:8000/widget"
        );
    }

    #[test]
    fn config_file_path_prefers_xdg_config_home() {
        let path = config_file_path(
            Some(Path::new("/xdg/config")),
            Some(Path::new("/home/user")),
        );
        assert_eq!(
            path,
            Some(PathBuf::from("/xdg/config/admin-agent-widget/config.toml"))
        );
    }

    #[test]
    fn config_file_path_falls_back_to_home_dot_config() {
        let path = config_file_path(None, Some(Path::new("/home/user")));
        assert_eq!(
            path,
            Some(PathBuf::from(
                "/home/user/.config/admin-agent-widget/config.toml"
            ))
        );
    }

    #[test]
    fn config_file_path_ignores_relative_or_empty_xdg_config_home() {
        for xdg in [Some(Path::new("relative/config")), Some(Path::new(""))] {
            let path = config_file_path(xdg, Some(Path::new("/home/user")));
            assert_eq!(
                path,
                Some(PathBuf::from(
                    "/home/user/.config/admin-agent-widget/config.toml"
                )),
                "xdg={xdg:?}"
            );
        }
    }

    #[test]
    fn config_file_path_is_none_without_any_base() {
        assert_eq!(config_file_path(None, None), None);
    }
}
