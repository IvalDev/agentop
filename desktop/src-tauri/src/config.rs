//! Configuration for the widget: server URL resolution (W6) and the persisted
//! preferences (T4).
//!
//! The server URL precedence is: the `AA_SERVER_URL` environment variable, then
//! the XDG config file (`$XDG_CONFIG_HOME/admin-agent-widget/config.toml`, with
//! `$HOME/.config` as fallback), then the default `http://127.0.0.1:8000`.
//! Preferences live in the same file and are written key by key without
//! destroying anything else in it. Missing or malformed input never aborts
//! startup.

use std::io::Write;
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

/// Managed preference key: keep the widget window above other windows.
pub const ALWAYS_ON_TOP_KEY: &str = "always_on_top";

/// Managed preference key: launch the widget when the user logs in.
pub const AUTOSTART_KEY: &str = "autostart";

/// User preferences persisted in the XDG config file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Preferences {
    /// Keep the widget window above other windows.
    pub always_on_top: bool,
    /// Launch the widget when the user logs in.
    pub autostart: bool,
}

/// Preferences plus whether `autostart` was already persisted as a boolean.
///
/// When the key is absent (first run), the resolved default must be written so
/// later explicit changes can win.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PreferencesState {
    /// Resolved preferences.
    pub preferences: Preferences,
    /// False on first run, or when the persisted value is not a boolean.
    pub autostart_persisted: bool,
}

/// A preference key managed by the widget inside the config file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PreferenceKey {
    /// [`ALWAYS_ON_TOP_KEY`].
    AlwaysOnTop,
    /// [`AUTOSTART_KEY`].
    Autostart,
}

impl PreferenceKey {
    /// Name of the key inside the TOML document.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AlwaysOnTop => ALWAYS_ON_TOP_KEY,
            Self::Autostart => AUTOSTART_KEY,
        }
    }
}

/// Resolves the preferences and the persistence state of `autostart`.
///
/// `always_on_top` defaults to `false`. `autostart` defaults to `true`: its
/// first-run semantics are "enabled unless explicitly disabled", so only a
/// literal `autostart = false` turns it off. Missing, malformed or wrongly
/// typed values fall back to the defaults instead of aborting startup.
///
/// [`PreferencesState::autostart_persisted`] is true only when the file holds a
/// boolean `autostart` value; the caller uses it to persist the resolved
/// first-run default exactly once.
pub fn preferences_state_from_toml(contents: Option<&str>) -> PreferencesState {
    let document = contents.and_then(|contents| toml::from_str::<toml::Table>(contents).ok());
    let autostart = document
        .as_ref()
        .and_then(|document| document.get(AUTOSTART_KEY))
        .and_then(toml::Value::as_bool);
    let always_on_top = document
        .as_ref()
        .and_then(|document| document.get(ALWAYS_ON_TOP_KEY))
        .and_then(toml::Value::as_bool)
        .unwrap_or(false);

    PreferencesState {
        preferences: Preferences {
            always_on_top,
            autostart: autostart.unwrap_or(true),
        },
        autostart_persisted: autostart.is_some(),
    }
}

/// Reads the preferences from the current environment.
///
/// A missing or unreadable file yields the defaults, exactly like `None`
/// contents: the caller cannot tell "file absent" from "file empty", and both
/// are first-run for `autostart`.
pub fn current_preferences_state() -> PreferencesState {
    let xdg_config_home = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let contents = config_file_path(xdg_config_home.as_deref(), home.as_deref())
        .and_then(|path| std::fs::read_to_string(path).ok());

    preferences_state_from_toml(contents.as_deref())
}

/// Persists one preference in the current environment.
///
/// Failures are logged to stderr and never propagated: the widget must keep
/// running without config access.
pub fn persist_current_preference(key: PreferenceKey, value: bool) {
    let xdg_config_home = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let Some(path) = config_file_path(xdg_config_home.as_deref(), home.as_deref()) else {
        eprintln!(
            "widget: no config directory available; {}={value} not persisted",
            key.as_str()
        );
        return;
    };

    if let Err(error) = save_preference(&path, key, value) {
        eprintln!(
            "widget: could not persist {}={value}: {error}",
            key.as_str()
        );
    }
}

/// Persists one managed key, preserving every other key in the file.
///
/// Writes a sibling temporary file and renames it over the target, so a failed
/// write never leaves a half-written config. An existing file that is not
/// valid TOML is reported as an error and left untouched rather than replaced.
pub fn save_preference(path: &Path, key: PreferenceKey, value: bool) -> std::io::Result<()> {
    let mut document: toml::Table = match std::fs::read_to_string(path) {
        Ok(contents) => toml::from_str(&contents).map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("existing config is not valid TOML: {error}"),
            )
        })?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => toml::Table::new(),
        Err(error) => return Err(error),
    };

    document.insert(key.as_str().to_string(), toml::Value::Boolean(value));
    let serialized = toml::to_string_pretty(&document).map_err(|error| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("config is not serializable: {error}"),
        )
    })?;

    write_atomically(path, &serialized)
}

/// Writes `contents` to a sibling temp file and renames it over `path`.
fn write_atomically(path: &Path, contents: &str) -> std::io::Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty());
    if let Some(parent) = parent {
        std::fs::create_dir_all(parent)?;
    }

    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| CONFIG_FILE.to_string());
    let temp_path = match parent {
        Some(parent) => parent.join(format!("{file_name}.tmp")),
        None => PathBuf::from(format!("{file_name}.tmp")),
    };

    let write_result = (|| -> std::io::Result<()> {
        let mut file = std::fs::File::create(&temp_path)?;
        file.write_all(contents.as_bytes())?;
        file.flush()
    })();

    let result = match write_result {
        Ok(()) => std::fs::rename(&temp_path, path),
        Err(error) => Err(error),
    };

    if result.is_err() {
        let _ = std::fs::remove_file(&temp_path);
    }

    result
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

    // --- preferences (T4) ---

    fn unique_temp_dir(name: &str) -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static COUNTER: AtomicUsize = AtomicUsize::new(0);

        let dir = std::env::temp_dir().join(format!(
            "admin-agent-widget-test-{}-{}-{name}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).expect("test directory must be creatable");
        dir
    }

    #[test]
    fn first_run_defaults_to_autostart_enabled() {
        let state = preferences_state_from_toml(None);
        assert_eq!(
            state.preferences,
            Preferences {
                always_on_top: false,
                autostart: true,
            }
        );
        assert!(!state.autostart_persisted);
    }

    #[test]
    fn empty_file_is_still_a_first_run() {
        let state = preferences_state_from_toml(Some(""));
        assert!(state.preferences.autostart);
        assert!(!state.autostart_persisted);
    }

    #[test]
    fn explicit_autostart_false_is_respected_and_persisted() {
        let state = preferences_state_from_toml(Some("autostart = false"));
        assert!(!state.preferences.autostart);
        assert!(state.autostart_persisted);
    }

    #[test]
    fn explicit_always_on_top_is_respected() {
        let state = preferences_state_from_toml(Some("always_on_top = true"));
        assert!(state.preferences.always_on_top);
        assert!(state.preferences.autostart);
        assert!(!state.autostart_persisted);
    }

    #[test]
    fn malformed_or_wrongly_typed_values_fall_back_to_defaults() {
        for contents in [
            "not toml [",
            "server_url = \"http://unterminated",
            "autostart = \"yes\"",
            "always_on_top = 1",
        ] {
            let state = preferences_state_from_toml(Some(contents));
            assert_eq!(
                state.preferences,
                Preferences {
                    always_on_top: false,
                    autostart: true,
                },
                "contents={contents:?}"
            );
        }
    }

    #[test]
    fn menu_check_states_follow_the_persisted_values() {
        // The tray builds its CheckMenuItem states straight from these values:
        // first run checks "Arranque con el sistema" and not "Siempre visible".
        assert_eq!(
            preferences_state_from_toml(None).preferences,
            Preferences {
                always_on_top: false,
                autostart: true,
            }
        );
        assert_eq!(
            preferences_state_from_toml(Some("always_on_top = true\nautostart = false"))
                .preferences,
            Preferences {
                always_on_top: true,
                autostart: false,
            }
        );
    }

    #[test]
    fn save_preference_creates_missing_directories_and_round_trips() {
        let dir = unique_temp_dir("save-create");
        let path = dir.join("admin-agent-widget").join("config.toml");

        save_preference(&path, PreferenceKey::Autostart, true).expect("save must succeed");

        let contents = std::fs::read_to_string(&path).expect("saved file must exist");
        let state = preferences_state_from_toml(Some(&contents));
        assert!(state.preferences.autostart);
        assert!(state.autostart_persisted);
        assert!(
            !dir.join("admin-agent-widget")
                .join("config.toml.tmp")
                .exists(),
            "no temp file must be left behind"
        );
    }

    #[test]
    fn save_preference_preserves_server_url_and_unknown_keys() {
        let dir = unique_temp_dir("save-preserve");
        let path = dir.join("config.toml");
        std::fs::write(
            &path,
            "server_url = \"http://file.example:9100\"\n# keep me\nother = true\n",
        )
        .expect("fixture must be written");

        save_preference(&path, PreferenceKey::AlwaysOnTop, true).expect("save must succeed");

        let contents = std::fs::read_to_string(&path).expect("file must exist");
        let document: toml::Table = toml::from_str(&contents).expect("file must stay valid TOML");
        assert_eq!(
            document.get("server_url").and_then(toml::Value::as_str),
            Some("http://file.example:9100")
        );
        assert_eq!(
            document.get("other").and_then(toml::Value::as_bool),
            Some(true)
        );
        assert_eq!(
            document.get("always_on_top").and_then(toml::Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn save_preference_overwrites_only_the_managed_key() {
        let dir = unique_temp_dir("save-overwrite");
        let path = dir.join("config.toml");
        std::fs::write(
            &path,
            "server_url = \"http://file.example:9100\"\nautostart = true\n",
        )
        .expect("fixture must be written");

        save_preference(&path, PreferenceKey::Autostart, false).expect("save must succeed");

        let contents = std::fs::read_to_string(&path).expect("file must exist");
        let document: toml::Table = toml::from_str(&contents).expect("file must stay valid TOML");
        assert_eq!(
            document.get("autostart").and_then(toml::Value::as_bool),
            Some(false)
        );
        assert_eq!(
            document.get("server_url").and_then(toml::Value::as_str),
            Some("http://file.example:9100")
        );
        assert!(!dir.join("config.toml.tmp").exists());
    }

    #[test]
    fn save_preference_leaves_a_malformed_file_untouched() {
        let dir = unique_temp_dir("save-malformed");
        let path = dir.join("config.toml");
        let original = "server_url = \"http://unterminated\n";
        std::fs::write(&path, original).expect("fixture must be written");

        let result = save_preference(&path, PreferenceKey::Autostart, true);

        assert!(result.is_err(), "malformed config must not be overwritten");
        assert_eq!(
            std::fs::read_to_string(&path).expect("file must exist"),
            original
        );
        assert!(
            !dir.join("config.toml.tmp").exists(),
            "no temp file must be left behind"
        );
    }

    #[test]
    fn first_run_with_only_server_url_still_defaults_autostart_enabled() {
        let state =
            preferences_state_from_toml(Some("server_url = \"http://file.example:9100\"\n"));
        assert!(state.preferences.autostart);
        assert!(!state.autostart_persisted);
        assert!(!state.preferences.always_on_top);
    }

    #[test]
    fn save_preference_accepts_an_existing_empty_file() {
        let dir = unique_temp_dir("save-empty");
        let path = dir.join("config.toml");
        std::fs::write(&path, "").expect("fixture must be written");

        save_preference(&path, PreferenceKey::Autostart, true).expect("save must succeed");

        let contents = std::fs::read_to_string(&path).expect("file must exist");
        let state = preferences_state_from_toml(Some(&contents));
        assert!(state.preferences.autostart);
        assert!(state.autostart_persisted);
    }

    #[test]
    fn preferences_do_not_regress_server_url_resolution() {
        let contents =
            "always_on_top = true\nautostart = false\nserver_url = \"http://file.example:9100\"\n";
        assert_eq!(
            resolve_server_url(None, Some(contents)).as_str(),
            "http://file.example:9100/"
        );
        assert_eq!(
            preferences_state_from_toml(Some(contents)).preferences,
            Preferences {
                always_on_top: true,
                autostart: false,
            }
        );
    }
}
