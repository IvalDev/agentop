//! Pairing HTTP call and the outgoing WebSocket session.
//!
//! The connector never listens: it opens one authenticated connection to the
//! server and serves the file operations the server asks for. A rejected token
//! (HTTP 401/403 or close code 4401) stops the session instead of retrying; any
//! other failure reconnects with exponential backoff while the app stays open.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{Sink, SinkExt, StreamExt};
use serde_json::Value;
use tauri::{AppHandle, Emitter, Url};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};

use super::config::ConnectorConfig;
use super::paths::{self, OpError};
use super::protocol::{self, OutgoingMessage};
use super::{ConnectorInner, ERROR_EVENT};

/// Timeout of the pairing HTTP request.
const PAIRING_TIMEOUT: Duration = Duration::from_secs(15);

/// First reconnect delay after a lost connection.
const INITIAL_BACKOFF: Duration = Duration::from_secs(1);

/// Cap of the reconnect backoff.
const MAX_BACKOFF: Duration = Duration::from_secs(30);

/// Close code the server uses for a missing or revoked token.
const REVOKED_CLOSE_CODE: u16 = 4401;

/// Calls `POST /api/v1/connectors/pair` against the configured server.
pub async fn pair(server: &Url, code: &str, name: &str) -> Result<protocol::PairResponse, String> {
    let client = reqwest::Client::builder()
        .timeout(PAIRING_TIMEOUT)
        .build()
        .map_err(|_| "No se pudo preparar la conexión con el servidor.".to_owned())?;

    let response = client
        .post(protocol::pair_url(server))
        .json(&protocol::PairRequest {
            code: code.to_owned(),
            name: name.to_owned(),
        })
        .send()
        .await
        .map_err(|error| format!("No se pudo conectar con el servidor: {error}"))?;

    let status = response.status().as_u16();
    let body = response.text().await.unwrap_or_default();
    if (200..300).contains(&status) {
        protocol::parse_pair_response(&body)
    } else {
        Err(protocol::pair_error_message(status, &body))
    }
}

/// How one connection attempt ended.
enum SessionEnd {
    /// The server rejected the connector; retrying cannot help.
    Rejected(String),
    /// The connection failed or closed; retry unless it was revoked.
    Closed { ready: bool },
}

/// Keeps the connector connected until it is disconnected, forgotten or
/// rejected. Runs on the Tauri async runtime for the app's whole lifetime.
pub async fn run_session(
    app: AppHandle,
    shared: Arc<Mutex<ConnectorInner>>,
    config: ConnectorConfig,
    generation: u64,
) {
    let mut backoff = INITIAL_BACKOFF;

    loop {
        if !is_current(&shared, generation) {
            return;
        }

        let server = match Url::parse(&config.server_url) {
            Ok(server) => server,
            Err(error) => {
                report_error(&app, &format!("La URL del servidor no es válida: {error}"));
                return;
            }
        };
        let url = match protocol::websocket_url(&server) {
            Ok(url) => url,
            Err(message) => {
                report_error(&app, &message);
                return;
            }
        };

        match connect_once(&shared, &url, &config.token, generation).await {
            SessionEnd::Rejected(message) => {
                set_connected(&shared, generation, false);
                report_error(&app, &message);
                return;
            }
            SessionEnd::Closed { ready } => {
                set_connected(&shared, generation, false);
                if ready {
                    backoff = INITIAL_BACKOFF;
                }
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(MAX_BACKOFF);
            }
        }
    }
}

/// One connection attempt; returns when the socket closes or cannot connect.
async fn connect_once(
    shared: &Arc<Mutex<ConnectorInner>>,
    url: &Url,
    token: &str,
    generation: u64,
) -> SessionEnd {
    let mut request = match url.as_str().into_client_request() {
        Ok(request) => request,
        Err(_) => {
            return SessionEnd::Rejected(
                "No se pudo preparar la conexión con el servidor.".to_owned(),
            );
        }
    };
    let authorization = match HeaderValue::from_str(&format!("Bearer {token}")) {
        Ok(value) => value,
        Err(_) => {
            return SessionEnd::Rejected(
                "El token guardado no es válido. Vuelve a emparejar el equipo.".to_owned(),
            );
        }
    };
    request.headers_mut().insert("authorization", authorization);

    let (socket, _response) = match tokio_tungstenite::connect_async(request).await {
        Ok(connection) => connection,
        Err(WsError::Http(response)) if matches!(response.status().as_u16(), 401 | 403) => {
            return SessionEnd::Rejected(
                "El servidor rechazó el token del equipo. Vuelve a emparejarlo.".to_owned(),
            );
        }
        Err(_) => return SessionEnd::Closed { ready: false },
    };

    let (mut writer, mut reader) = socket.split();
    let mut ready = false;

    while let Some(message) = reader.next().await {
        match message {
            Ok(Message::Text(text)) => {
                let Some(incoming) = protocol::parse_incoming(text.as_str()) else {
                    continue;
                };

                match incoming.kind.as_str() {
                    "ready" => {
                        ready = true;
                        set_connected(shared, generation, true);
                    }
                    "ping" => {
                        if send_json(&mut writer, &OutgoingMessage::Pong)
                            .await
                            .is_err()
                        {
                            return SessionEnd::Closed { ready };
                        }
                    }
                    "request" => {
                        let Some(id) = incoming.id else { continue };
                        if !is_current(shared, generation) {
                            return SessionEnd::Closed { ready };
                        }
                        let response = handle_request(shared, id, incoming.op, incoming.args).await;
                        if send_json(&mut writer, &response).await.is_err() {
                            return SessionEnd::Closed { ready };
                        }
                    }
                    _ => {}
                }
            }
            Ok(Message::Ping(payload)) => {
                if writer.send(Message::Pong(payload)).await.is_err() {
                    return SessionEnd::Closed { ready };
                }
            }
            Ok(Message::Close(frame)) => {
                let code = frame.map(|frame| u16::from(frame.code)).unwrap_or(0);
                if code == REVOKED_CLOSE_CODE {
                    return SessionEnd::Rejected(
                        "El servidor rechazó la conexión del equipo. Vuelve a emparejarlo."
                            .to_owned(),
                    );
                }
                return SessionEnd::Closed { ready };
            }
            Ok(_) => {}
            Err(_) => return SessionEnd::Closed { ready },
        }
    }

    SessionEnd::Closed { ready }
}

/// Executes one request and builds the response message.
async fn handle_request(
    shared: &Arc<Mutex<ConnectorInner>>,
    id: String,
    op: Option<String>,
    args: Option<Value>,
) -> OutgoingMessage {
    let Some(root) = folder_root(shared) else {
        return OutgoingMessage::failure(
            id,
            OpError::invalid_path("No hay una carpeta concedida."),
        );
    };

    let op = op.unwrap_or_default();
    let args = args.unwrap_or(Value::Null);

    match tokio::task::spawn_blocking(move || paths::execute(&root, &op, &args)).await {
        Ok(Ok(result)) => OutgoingMessage::success(id, result),
        Ok(Err(error)) => OutgoingMessage::failure(id, error),
        Err(_) => OutgoingMessage::failure(
            id,
            OpError::new("io_error", "No se pudo ejecutar la operación."),
        ),
    }
}

/// Sends one JSON message over the socket.
async fn send_json<S>(writer: &mut S, message: &OutgoingMessage) -> Result<(), ()>
where
    S: Sink<Message> + Unpin,
{
    let json = serde_json::to_string(message).map_err(|_| ())?;
    writer.send(Message::text(json)).await.map_err(|_| ())
}

fn folder_root(shared: &Arc<Mutex<ConnectorInner>>) -> Option<PathBuf> {
    let inner = shared
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    inner.config.folder().map(Path::to_path_buf)
}

fn set_connected(shared: &Arc<Mutex<ConnectorInner>>, generation: u64, connected: bool) {
    let mut inner = shared
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if inner.generation == generation {
        inner.connected = connected;
    }
}

/// Whether `generation` is still the active session.
fn is_current(shared: &Arc<Mutex<ConnectorInner>>, generation: u64) -> bool {
    let inner = shared
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    inner.generation == generation
}

/// Logs the failure and tells the local window, which is the only place that
/// shows connector errors (the status payload stays path-free).
fn report_error(app: &AppHandle, message: &str) {
    eprintln!("widget: connector error: {message}");
    let _ = app.emit(ERROR_EVENT, message);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::config::ConnectorConfig;
    use crate::connector::test_support::TempDir;
    use futures_util::{SinkExt, StreamExt};
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_hdr_async;
    use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};

    #[tokio::test]
    async fn serves_ready_requests_and_carries_the_bearer_token() {
        let dir = TempDir::new("ws-session");
        std::fs::write(dir.path().join("note.txt"), b"hola").expect("fixture must be written");
        let root = std::fs::canonicalize(dir.path()).expect("root must canonicalize");

        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("listener must bind");
        let address = listener.local_addr().expect("address must be known");
        let (header_tx, header_rx) = tokio::sync::oneshot::channel();

        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("connection must arrive");
            let mut socket = accept_hdr_async(stream, |request: &Request, response: Response| {
                let header = request
                    .headers()
                    .get("authorization")
                    .and_then(|value| value.to_str().ok())
                    .map(str::to_owned);
                let _ = header_tx.send(header);
                Ok(response)
            })
            .await
            .expect("handshake must succeed");

            socket
                .send(Message::text(r#"{"type":"ready","connector_id":"id-1"}"#))
                .await
                .expect("ready must be sent");
            socket
                .send(Message::text(r#"{"type":"ping"}"#))
                .await
                .expect("ping must be sent");
            let pong = socket
                .next()
                .await
                .expect("pong must arrive")
                .expect("pong must be a frame");
            assert_eq!(
                pong.to_text().expect("pong must be text"),
                r#"{"type":"pong"}"#
            );

            socket
                .send(Message::text(
                    r#"{"type":"request","id":"req-1","op":"read_file","args":{"path":"note.txt"}}"#,
                ))
                .await
                .expect("request must be sent");
            let response = socket
                .next()
                .await
                .expect("response must arrive")
                .expect("response must be a frame");
            socket.close(None).await.ok();
            response
                .to_text()
                .expect("response must be text")
                .to_owned()
        });

        let shared = Arc::new(Mutex::new(ConnectorInner {
            config: ConnectorConfig {
                folder_path: root.to_string_lossy().into_owned(),
                ..ConnectorConfig::default()
            },
            connected: false,
            generation: 1,
            task: None,
        }));
        let url =
            Url::parse(&format!("ws://{address}/api/v1/connectors/ws")).expect("URL must parse");

        let end = connect_once(&shared, &url, "secret-token", 1).await;

        assert!(matches!(end, SessionEnd::Closed { ready: true }));
        assert_eq!(
            header_rx.await.expect("header must be reported").as_deref(),
            Some("Bearer secret-token")
        );

        let response: Value = serde_json::from_str(&server.await.expect("server must finish"))
            .expect("response must be JSON");
        assert_eq!(response["type"], "response");
        assert_eq!(response["id"], "req-1");
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["size"], 4);
        assert_eq!(response["result"]["data_b64"], "aG9sYQ==");
        assert!(shared.lock().unwrap().connected);
    }

    #[tokio::test]
    async fn a_stale_generation_cannot_flip_the_status() {
        let shared = Arc::new(Mutex::new(ConnectorInner {
            config: ConnectorConfig::default(),
            connected: false,
            generation: 2,
            task: None,
        }));

        set_connected(&shared, 1, true);

        assert!(!shared.lock().unwrap().connected);
        assert!(!is_current(&shared, 1));
        assert!(is_current(&shared, 2));
    }
}
