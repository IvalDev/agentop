//! Local workspace connector (D1).
//!
//! The connector links the desktop app to the server: the user grants one
//! folder, pairs the device with a code shown in the web app, and the server
//! drives `list_files`/`read_file` over an outgoing WebSocket. The absolute
//! folder path never leaves the machine and never reaches the web page.

pub mod client;
pub mod config;
pub mod paths;
pub mod protocol;

#[cfg(test)]
pub(crate) mod test_support {
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Temporary directory removed on drop, keeping the tests self-contained.
    pub struct TempDir {
        path: PathBuf,
    }

    impl TempDir {
        pub fn new(name: &str) -> Self {
            static COUNTER: AtomicUsize = AtomicUsize::new(0);

            let path = std::env::temp_dir().join(format!(
                "admin-agent-widget-connector-{}-{}-{name}",
                std::process::id(),
                COUNTER.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&path).expect("test directory must be creatable");
            Self { path }
        }

        pub fn path(&self) -> &Path {
            &self.path
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }
}

use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard};

use serde::Serialize;
use tauri::{AppHandle, Manager, State, Url};

use config::ConnectorConfig;

/// Event emitted to the local window when the connector stops on an error.
pub const ERROR_EVENT: &str = "connector:error";

/// State shared by the commands and the WebSocket session.
pub struct ConnectorState {
    config_path: PathBuf,
    inner: Arc<Mutex<ConnectorInner>>,
}

/// Values guarded by [`ConnectorState`]'s mutex.
pub(crate) struct ConnectorInner {
    pub(crate) config: ConnectorConfig,
    pub(crate) connected: bool,
    /// Incremented on every session change; stale sessions may not mutate state.
    pub(crate) generation: u64,
    pub(crate) task: Option<tauri::async_runtime::JoinHandle<()>>,
}

impl ConnectorState {
    fn new(config_path: PathBuf, config: ConnectorConfig) -> Self {
        Self {
            config_path,
            inner: Arc::new(Mutex::new(ConnectorInner {
                config,
                connected: false,
                generation: 0,
                task: None,
            })),
        }
    }

    pub(crate) fn shared(&self) -> Arc<Mutex<ConnectorInner>> {
        Arc::clone(&self.inner)
    }

    fn lock(&self) -> MutexGuard<'_, ConnectorInner> {
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn set_task(&self, task: tauri::async_runtime::JoinHandle<()>) {
        let mut inner = self.lock();
        if let Some(previous) = inner.task.replace(task) {
            previous.abort();
        }
    }

    /// Invalidates the running session and returns the new generation.
    fn next_generation(&self) -> u64 {
        let mut inner = self.lock();
        inner.generation = inner.generation.wrapping_add(1);
        inner.generation
    }
}

/// Loads the connector config and, when the device is paired, starts the
/// outgoing WebSocket. The connector keeps running while the app stays in the
/// tray; the local window is only a view over this state.
pub fn initialize(app: &AppHandle) {
    let config_path = match app.path().app_config_dir() {
        Ok(dir) => config::config_path(&dir),
        Err(error) => {
            eprintln!("widget: no app config directory for the connector: {error}");
            PathBuf::from(config::CONNECTOR_FILE)
        }
    };
    let config = config::load_from(&config_path);
    let state = ConnectorState::new(config_path, config.clone());
    app.manage(state);

    if config.is_paired() {
        if let Some(state) = app.try_state::<ConnectorState>() {
            start_session(app, &state, config);
        }
    }
}

/// Starts the WebSocket session for `config`, replacing any previous one.
fn start_session(app: &AppHandle, state: &ConnectorState, config: ConnectorConfig) {
    let shared = state.shared();
    let generation = state.next_generation();
    let handle = app.clone();
    let task = tauri::async_runtime::spawn(client::run_session(handle, shared, config, generation));
    state.set_task(task);
}

/// Result of the native folder picker; the path never crosses the boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct GrantFolderResult {
    pub granted: bool,
}

/// Opens the native folder picker and stores the canonical granted root.
#[tauri::command]
pub fn grant_folder(app: AppHandle) -> Result<GrantFolderResult, String> {
    use tauri_plugin_dialog::DialogExt;

    let picked = app
        .dialog()
        .file()
        .set_title("Elige la carpeta del espacio de trabajo")
        .blocking_pick_folder();
    let Some(picked) = picked else {
        return Ok(GrantFolderResult { granted: false });
    };

    let path = picked
        .into_path()
        .map_err(|_| "No se pudo leer la carpeta seleccionada.".to_owned())?;
    let canonical = paths::canonicalize_root(&path).map_err(|error| error.message)?;
    let folder = canonical
        .to_str()
        .ok_or_else(|| "La ruta de la carpeta no es válida.".to_owned())?
        .to_owned();

    let state = app.state::<ConnectorState>();
    let mut inner = state.lock();
    let mut config = inner.config.clone();
    config.folder_path = folder;
    config::save_to(&state.config_path, &config)
        .map_err(|_| "No se pudo guardar la carpeta concedida.".to_owned())?;
    inner.config = config;

    Ok(GrantFolderResult { granted: true })
}

/// Pairs this device with the code shown in the web app and starts the socket.
#[tauri::command]
pub async fn pair_connector(
    app: AppHandle,
    code: String,
    name: String,
) -> Result<ConnectorStatus, String> {
    let code = protocol::normalize_pairing_code(&code);
    if code.is_empty() {
        return Err("Ingresa el código de emparejamiento.".to_owned());
    }
    let name = protocol::validate_name(&name)?;

    {
        let state = app.state::<ConnectorState>();
        if !state.lock().config.folder_ready() {
            return Err("Primero elige la carpeta del espacio de trabajo.".to_owned());
        }
    }

    let server = crate::config::current_server_url();
    let paired = client::pair(&server, &code, &name).await?;

    let state = app.state::<ConnectorState>();
    let config = {
        let mut inner = state.lock();
        let mut config = inner.config.clone();
        config.server_url = server.as_str().to_owned();
        config.connector_id = paired.connector_id;
        config.token = paired.token;
        config.name = paired.name;
        config::save_to(&state.config_path, &config)
            .map_err(|_| "No se pudo guardar el emparejamiento.".to_owned())?;
        inner.config = config.clone();
        inner.connected = false;
        inner.generation = inner.generation.wrapping_add(1);
        if let Some(task) = inner.task.take() {
            task.abort();
        }
        config
    };

    start_session(&app, &state, config);

    let inner = state.lock();
    Ok(status_from(&inner.config, inner.connected, &server))
}

/// Current connector status; never includes the folder path or the token.
#[tauri::command]
pub fn connector_status(state: State<'_, ConnectorState>) -> ConnectorStatus {
    let inner = state.lock();
    status_from(
        &inner.config,
        inner.connected,
        &crate::config::current_server_url(),
    )
}

/// Closes the WebSocket but keeps the pairing and the granted folder.
#[tauri::command]
pub fn disconnect_connector(state: State<'_, ConnectorState>) -> ConnectorStatus {
    let mut inner = state.lock();
    inner.generation = inner.generation.wrapping_add(1);
    if let Some(task) = inner.task.take() {
        task.abort();
    }
    inner.connected = false;
    status_from(
        &inner.config,
        inner.connected,
        &crate::config::current_server_url(),
    )
}

/// Restarts the WebSocket for an already paired device without a new code.
#[tauri::command]
pub fn reconnect_connector(
    app: AppHandle,
    state: State<'_, ConnectorState>,
) -> Result<ConnectorStatus, String> {
    let config = {
        let inner = state.lock();
        if !inner.config.is_paired() {
            return Err("El equipo no está emparejado.".to_owned());
        }
        if !inner.config.folder_ready() {
            return Err("Primero elige la carpeta del espacio de trabajo.".to_owned());
        }
        inner.config.clone()
    };

    start_session(&app, &state, config);

    let inner = state.lock();
    Ok(status_from(
        &inner.config,
        inner.connected,
        &crate::config::current_server_url(),
    ))
}

/// Closes the WebSocket and forgets the connector id, token and name; the
/// granted folder stays on the machine.
#[tauri::command]
pub fn forget_connector(state: State<'_, ConnectorState>) -> Result<ConnectorStatus, String> {
    let mut inner = state.lock();
    let mut config = inner.config.clone();
    config.connector_id.clear();
    config.token.clear();
    config.name.clear();
    config::save_to(&state.config_path, &config)
        .map_err(|_| "No se pudo olvidar el equipo.".to_owned())?;

    inner.generation = inner.generation.wrapping_add(1);
    if let Some(task) = inner.task.take() {
        task.abort();
    }
    inner.config = config;
    inner.connected = false;

    Ok(status_from(
        &inner.config,
        inner.connected,
        &crate::config::current_server_url(),
    ))
}

/// Status returned to the local window: never includes the folder path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ConnectorStatus {
    pub paired: bool,
    pub connected: bool,
    pub name: String,
    pub server_url: String,
    pub folder_ready: bool,
}

/// Builds the status shown to the local window.
///
/// `fallback_server` is the currently resolved server URL; the stored pairing
/// URL wins so the window reports the server the connector is talking to.
pub fn status_from(
    config: &config::ConnectorConfig,
    connected: bool,
    fallback_server: &Url,
) -> ConnectorStatus {
    let server_url = if config.server_url.is_empty() {
        fallback_server.as_str().to_owned()
    } else {
        Url::parse(&config.server_url)
            .map(|url| url.as_str().to_owned())
            .unwrap_or_else(|_| config.server_url.clone())
    };

    ConnectorStatus {
        paired: config.is_paired(),
        connected,
        name: config.name.clone(),
        server_url,
        folder_ready: config.folder_ready(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use config::ConnectorConfig;

    fn url(value: &str) -> Url {
        Url::parse(value).expect("test URL must parse")
    }

    fn config(folder: &str) -> ConnectorConfig {
        ConnectorConfig {
            server_url: "http://file.example:9100".to_owned(),
            connector_id: "connector-1".to_owned(),
            token: "secret-token".to_owned(),
            name: "Mi equipo".to_owned(),
            folder_path: folder.to_owned(),
        }
    }

    #[test]
    fn status_reports_pairing_and_folder_without_the_path() {
        let folder = "/home/user/private workspace";
        let status = status_from(&config(folder), true, &url("http://127.0.0.1:8000"));

        assert!(status.paired);
        assert!(status.connected);
        assert!(status.folder_ready);
        assert_eq!(status.name, "Mi equipo");
        assert_eq!(status.server_url, "http://file.example:9100/");
    }

    #[test]
    fn status_never_exposes_the_folder_path_or_token() {
        let folder = "/home/user/private workspace";
        let status = status_from(&config(folder), false, &url("http://127.0.0.1:8000"));
        let json = serde_json::to_string(&status).expect("status must serialize");

        assert!(!json.contains(folder), "json: {json}");
        assert!(!json.contains("secret-token"), "json: {json}");
        let value: serde_json::Value = serde_json::from_str(&json).expect("json must parse");
        let keys: Vec<&String> = value
            .as_object()
            .expect("status is an object")
            .keys()
            .collect();
        assert_eq!(
            keys,
            ["connected", "folder_ready", "name", "paired", "server_url"]
        );
    }

    #[test]
    fn an_unpaired_connector_reports_the_current_server() {
        let empty = ConnectorConfig::default();
        let status = status_from(&empty, false, &url("http://127.0.0.1:8000"));

        assert!(!status.paired);
        assert!(!status.folder_ready);
        assert_eq!(status.name, "");
        assert_eq!(status.server_url, "http://127.0.0.1:8000/");
    }
}
