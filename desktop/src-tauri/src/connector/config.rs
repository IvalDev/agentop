//! Local connector configuration (`connector.json`).
//!
//! The file lives in the app config directory and is private to the user
//! (`0600` on Unix): it holds the connector token, the server it paired with
//! and the canonical path of the granted folder. None of it is ever returned
//! to the web page.

use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// File name inside the app config directory.
pub const CONNECTOR_FILE: &str = "connector.json";

/// Persisted connector state.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConnectorConfig {
    /// Server the connector paired with; the WebSocket connects there.
    #[serde(default)]
    pub server_url: String,
    /// Identifier returned by `POST /api/v1/connectors/pair`.
    #[serde(default)]
    pub connector_id: String,
    /// Bearer token returned once by the pairing endpoint.
    #[serde(default)]
    pub token: String,
    /// Device name shown in the web app and the local window.
    #[serde(default)]
    pub name: String,
    /// Canonical path of the granted folder. Never leaves the machine.
    #[serde(default)]
    pub folder_path: String,
}

impl ConnectorConfig {
    /// Whether the device holds a connector id and token.
    pub fn is_paired(&self) -> bool {
        !self.connector_id.is_empty() && !self.token.is_empty()
    }

    /// Whether a folder has been granted.
    pub fn folder_ready(&self) -> bool {
        !self.folder_path.is_empty()
    }

    /// The granted folder, when one was saved.
    pub fn folder(&self) -> Option<&Path> {
        self.folder_ready().then(|| Path::new(&self.folder_path))
    }
}

/// Reads the connector config, falling back to an unpaired default.
///
/// A missing, unreadable or malformed file never aborts startup; it simply
/// leaves the connector unpaired until the user pairs again.
pub fn load_from(path: &Path) -> ConnectorConfig {
    match std::fs::read_to_string(path) {
        Ok(contents) => serde_json::from_str(&contents).unwrap_or_else(|error| {
            eprintln!(
                "widget: ignoring malformed connector config at {}: {error}",
                path.display()
            );
            ConnectorConfig::default()
        }),
        Err(error) => {
            if error.kind() != std::io::ErrorKind::NotFound {
                eprintln!(
                    "widget: could not read connector config at {}: {error}",
                    path.display()
                );
            }
            ConnectorConfig::default()
        }
    }
}

/// Writes the connector config atomically, with `0600` permissions on Unix.
pub fn save_to(path: &Path, config: &ConnectorConfig) -> std::io::Result<()> {
    let serialized = serde_json::to_string_pretty(config).map_err(|error| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("connector config is not serializable: {error}"),
        )
    })?;

    write_private(path, &serialized)
}

/// Writes `contents` to a sibling temp file and renames it over `path`.
fn write_private(path: &Path, contents: &str) -> std::io::Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty());
    if let Some(parent) = parent {
        std::fs::create_dir_all(parent)?;
    }

    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| CONNECTOR_FILE.to_owned());
    let temp_path = match parent {
        Some(parent) => parent.join(format!("{file_name}.tmp")),
        None => PathBuf::from(format!("{file_name}.tmp")),
    };

    let write_result = (|| -> std::io::Result<()> {
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }

        let mut file = options.open(&temp_path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
        }
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

/// Resolved path of `connector.json` inside an app config directory.
pub fn config_path(app_config_dir: &Path) -> PathBuf {
    app_config_dir.join(CONNECTOR_FILE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::test_support::TempDir;
    use std::fs;

    fn sample() -> ConnectorConfig {
        ConnectorConfig {
            server_url: "http://127.0.0.1:8000/".to_owned(),
            connector_id: "8b1c0d5e-0000-4000-8000-000000000001".to_owned(),
            token: "token-value".to_owned(),
            name: "Mi equipo".to_owned(),
            folder_path: "/home/user/workspace".to_owned(),
        }
    }

    #[test]
    fn save_and_load_round_trip() {
        let dir = TempDir::new("config-roundtrip");
        let path = config_path(dir.path());

        save_to(&path, &sample()).expect("save must succeed");

        assert_eq!(load_from(&path), sample());
        assert!(!dir.path().join("connector.json.tmp").exists());
    }

    #[test]
    fn missing_file_is_an_unpaired_default() {
        let dir = TempDir::new("config-missing");

        let config = load_from(&config_path(dir.path()));

        assert_eq!(config, ConnectorConfig::default());
        assert!(!config.is_paired());
        assert!(!config.folder_ready());
    }

    #[test]
    fn malformed_or_incomplete_files_fall_back_to_the_default() {
        let dir = TempDir::new("config-malformed");

        for contents in ["", "not json", "{\"connector_id\": 1}", "[]"] {
            let path = dir.path().join("connector.json");
            fs::write(&path, contents).expect("fixture must be written");

            assert_eq!(
                load_from(&path),
                ConnectorConfig::default(),
                "contents={contents:?}"
            );
        }
    }

    #[test]
    fn save_creates_the_app_config_directory() {
        let dir = TempDir::new("config-create-dir");
        let path = config_path(&dir.path().join("nested").join("config"));

        save_to(&path, &sample()).expect("save must succeed");

        assert!(path.exists());
    }

    #[cfg(unix)]
    #[test]
    fn saved_file_is_private_to_the_user() {
        use std::os::unix::fs::PermissionsExt;

        let dir = TempDir::new("config-private");
        let path = config_path(dir.path());

        save_to(&path, &sample()).expect("save must succeed");

        let mode = fs::metadata(&path)
            .expect("saved file must exist")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[cfg(unix)]
    #[test]
    fn saving_overwrites_an_existing_private_file() {
        use std::os::unix::fs::PermissionsExt;

        let dir = TempDir::new("config-overwrite");
        let path = config_path(dir.path());
        fs::write(&path, "{\"token\":\"old\"}").expect("fixture must be written");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644))
            .expect("fixture permissions must change");

        let mut updated = sample();
        updated.token = "new-token".to_owned();
        save_to(&path, &updated).expect("save must succeed");

        assert_eq!(load_from(&path).token, "new-token");
        let mode = fs::metadata(&path)
            .expect("saved file must exist")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
    }
}
