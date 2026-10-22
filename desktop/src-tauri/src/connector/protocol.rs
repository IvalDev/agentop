//! Wire protocol for the connector: pairing HTTP payloads and WebSocket
//! messages.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::Url;

use super::paths::OpError;

/// Path of the pairing endpoint.
pub const PAIR_PATH: &str = "/api/v1/connectors/pair";

/// Path of the connector WebSocket.
pub const WS_PATH: &str = "/api/v1/connectors/ws";

/// Maximum device name length accepted by the server.
pub const MAX_NAME_LEN: usize = 80;

/// Body of `POST /api/v1/connectors/pair`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PairRequest {
    pub code: String,
    pub name: String,
}

/// Successful pairing response.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct PairResponse {
    pub connector_id: String,
    pub token: String,
    pub name: String,
}

/// Message sent by the connector over the WebSocket.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum OutgoingMessage {
    Pong,
    Response {
        id: String,
        ok: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        result: Option<Value>,
        #[serde(skip_serializing_if = "Option::is_none")]
        error: Option<OpError>,
    },
}

impl OutgoingMessage {
    /// `{"type":"response","id":...,"ok":true,"result":...}`.
    pub fn success(id: impl Into<String>, result: Value) -> Self {
        Self::Response {
            id: id.into(),
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    /// `{"type":"response","id":...,"ok":false,"error":{...}}`.
    pub fn failure(id: impl Into<String>, error: OpError) -> Self {
        Self::Response {
            id: id.into(),
            ok: false,
            result: None,
            error: Some(error),
        }
    }
}

/// Message sent by the server over the WebSocket.
///
/// Unknown message types still deserialize so the client can ignore them
/// without dropping the connection.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct IncomingMessage {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub op: Option<String>,
    #[serde(default)]
    pub args: Option<Value>,
}

/// Parses an incoming WebSocket text frame; invalid JSON is ignored.
pub fn parse_incoming(text: &str) -> Option<IncomingMessage> {
    serde_json::from_str(text).ok()
}

/// Builds `{server}/api/v1/connectors/pair`, keeping a configured path prefix.
pub fn pair_url(server: &Url) -> Url {
    with_path(server, PAIR_PATH)
}

/// Builds the WebSocket URL for the configured server (`ws`/`wss`).
pub fn websocket_url(server: &Url) -> Result<Url, String> {
    let scheme = match server.scheme() {
        "http" => "ws",
        "https" => "wss",
        other => {
            return Err(format!(
                "El servidor configurado usa un esquema no soportado: {other}"
            ));
        }
    };

    let mut url = server.clone();
    url.set_scheme(scheme)
        .map_err(|_| "No se pudo construir la URL del WebSocket.".to_owned())?;
    Ok(with_path(&url, WS_PATH))
}

/// Trims and uppercases the pairing code; the server normalizes hyphens.
pub fn normalize_pairing_code(raw: &str) -> String {
    raw.trim().to_ascii_uppercase()
}

/// Trims and validates the device name (1..=80 characters).
pub fn validate_name(raw: &str) -> Result<String, String> {
    let name = raw.trim();
    let length = name.chars().count();
    if !(1..=MAX_NAME_LEN).contains(&length) {
        return Err(format!(
            "El nombre del equipo debe tener entre 1 y {MAX_NAME_LEN} caracteres."
        ));
    }
    Ok(name.to_owned())
}

/// Parses a successful pairing response.
pub fn parse_pair_response(body: &str) -> Result<PairResponse, String> {
    serde_json::from_str(body)
        .map_err(|_| "El servidor devolvió una respuesta de emparejamiento inválida.".to_owned())
}

/// Readable message for a failed pairing request.
pub fn pair_error_message(status: u16, body: &str) -> String {
    let detail = serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|value| {
            value
                .get("detail")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .filter(|detail| !detail.trim().is_empty());

    detail.unwrap_or_else(|| format!("No se pudo emparejar el equipo (HTTP {status})."))
}

/// Replaces the URL path with `{prefix}{path}`, dropping query and fragment.
fn with_path(server: &Url, path: &str) -> Url {
    let mut url = server.clone();
    let joined = format!("{}{}", url.path().trim_end_matches('/'), path);
    url.set_path(&joined);
    url.set_query(None);
    url.set_fragment(None);
    url
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn url(value: &str) -> Url {
        Url::parse(value).expect("test URL must parse")
    }

    #[test]
    fn pair_url_joins_the_path_and_keeps_a_prefix() {
        assert_eq!(
            pair_url(&url("http://127.0.0.1:8000")).as_str(),
            "http://127.0.0.1:8000/api/v1/connectors/pair"
        );
        assert_eq!(
            pair_url(&url("https://example.com/console/")).as_str(),
            "https://example.com/console/api/v1/connectors/pair"
        );
        assert_eq!(
            pair_url(&url("https://example.com/console?x=1#top")).as_str(),
            "https://example.com/console/api/v1/connectors/pair"
        );
    }

    #[test]
    fn websocket_url_maps_http_to_ws() {
        assert_eq!(
            websocket_url(&url("http://127.0.0.1:8000")).expect("URL must build"),
            url("ws://127.0.0.1:8000/api/v1/connectors/ws")
        );
    }

    #[test]
    fn websocket_url_maps_https_to_wss_and_keeps_the_prefix() {
        assert_eq!(
            websocket_url(&url("https://example.com/console/")).expect("URL must build"),
            url("wss://example.com/console/api/v1/connectors/ws")
        );
    }

    #[test]
    fn websocket_url_drops_query_and_fragment() {
        assert_eq!(
            websocket_url(&url("http://127.0.0.1:8000/widget?a=1#top"))
                .expect("URL must build")
                .as_str(),
            "ws://127.0.0.1:8000/widget/api/v1/connectors/ws"
        );
    }

    #[test]
    fn websocket_url_rejects_non_http_schemes() {
        assert!(websocket_url(&url("ftp://example.com")).is_err());
        assert!(websocket_url(&url("ws://example.com")).is_err());
    }

    #[test]
    fn pairing_code_is_trimmed_and_uppercased() {
        assert_eq!(normalize_pairing_code("  abcd-efgh  "), "ABCD-EFGH");
        assert_eq!(normalize_pairing_code("ABCDEFGH"), "ABCDEFGH");
        assert_eq!(normalize_pairing_code("   "), "");
    }

    #[test]
    fn name_is_trimmed_and_bounded_to_eighty_characters() {
        assert_eq!(
            validate_name("  Mi equipo  ").expect("name must be valid"),
            "Mi equipo"
        );
        assert!(validate_name("").is_err());
        assert!(validate_name("   ").is_err());
        assert!(validate_name(&"a".repeat(MAX_NAME_LEN)).is_ok());
        assert!(validate_name(&"a".repeat(MAX_NAME_LEN + 1)).is_err());
    }

    #[test]
    fn pair_request_serializes_the_contract_body() {
        let request = PairRequest {
            code: "ABCD-EFGH".to_owned(),
            name: "Mi equipo".to_owned(),
        };

        assert_eq!(
            serde_json::to_string(&request).expect("request must serialize"),
            r#"{"code":"ABCD-EFGH","name":"Mi equipo"}"#
        );
    }

    #[test]
    fn pair_response_parses_the_contract_payload() {
        let response =
            parse_pair_response(r#"{"connector_id":"id-1","token":"token-1","name":"Mi equipo"}"#)
                .expect("response must parse");

        assert_eq!(
            response,
            PairResponse {
                connector_id: "id-1".to_owned(),
                token: "token-1".to_owned(),
                name: "Mi equipo".to_owned(),
            }
        );
    }

    #[test]
    fn malformed_pair_responses_are_rejected() {
        assert!(parse_pair_response("not json").is_err());
        assert!(parse_pair_response(r#"{"token":"only-token"}"#).is_err());
    }

    #[test]
    fn pair_errors_use_the_server_detail() {
        assert_eq!(
            pair_error_message(400, r#"{"detail":"Código inválido o vencido"}"#),
            "Código inválido o vencido"
        );
        assert_eq!(
            pair_error_message(500, r#"{"detail":"Error interno"}"#),
            "Error interno"
        );
    }

    #[test]
    fn pair_errors_fall_back_to_a_readable_message() {
        assert_eq!(
            pair_error_message(502, "upstream unavailable"),
            "No se pudo emparejar el equipo (HTTP 502)."
        );
        assert_eq!(
            pair_error_message(400, r#"{"detail":""}"#),
            "No se pudo emparejar el equipo (HTTP 400)."
        );
    }

    #[test]
    fn pong_serializes_exactly() {
        assert_eq!(
            serde_json::to_string(&OutgoingMessage::Pong).expect("message must serialize"),
            r#"{"type":"pong"}"#
        );
    }

    #[test]
    fn success_response_serializes_the_expected_shape() {
        let message = OutgoingMessage::success("req-1", json!({"path": "", "entries": []}));
        let value = serde_json::to_value(&message).expect("message must serialize");

        assert_eq!(value["type"], "response");
        assert_eq!(value["id"], "req-1");
        assert_eq!(value["ok"], true);
        assert_eq!(value["result"], json!({"path": "", "entries": []}));
        assert!(value.get("error").is_none());
    }

    #[test]
    fn error_response_serializes_exactly() {
        let message = OutgoingMessage::failure(
            "req-1",
            OpError::invalid_path("La ruta está fuera de la carpeta concedida."),
        );

        assert_eq!(
            serde_json::to_string(&message).expect("message must serialize"),
            r#"{"type":"response","id":"req-1","ok":false,"error":{"code":"invalid_path","message":"La ruta está fuera de la carpeta concedida."}}"#
        );
    }

    #[test]
    fn incoming_messages_parse_ready_requests_and_pings() {
        let ready =
            parse_incoming(r#"{"type":"ready","connector_id":"id-1"}"#).expect("ready must parse");
        assert_eq!(ready.kind, "ready");

        let request = parse_incoming(
            r#"{"type":"request","id":"req-1","op":"read_file","args":{"path":"a.md"}}"#,
        )
        .expect("request must parse");
        assert_eq!(request.kind, "request");
        assert_eq!(request.id.as_deref(), Some("req-1"));
        assert_eq!(request.op.as_deref(), Some("read_file"));
        assert_eq!(request.args, Some(json!({"path": "a.md"})));

        let ping = parse_incoming(r#"{"type":"ping"}"#).expect("ping must parse");
        assert_eq!(ping.kind, "ping");
    }

    #[test]
    fn unknown_message_types_and_invalid_json_are_tolerated() {
        assert_eq!(
            parse_incoming(r#"{"type":"something-new"}"#)
                .expect("unknown type must parse")
                .kind,
            "something-new"
        );
        assert!(parse_incoming("not json").is_none());
        assert!(parse_incoming("").is_none());
    }
}
