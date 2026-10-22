import { ApiError, type ApiClient } from './client';

/**
 * The local-workspace connector API (`app/modules/connectors/router.py`).
 *
 * A device pairs with a short-lived code and then connects out to the server: the web only
 * generates the code, lists the caller's devices, and revokes one. The folder itself never
 * reaches the server or this client, so nothing here carries a path or a file.
 */

/** A one-time code the widget exchanges for a connector token (`PairingCodeResponse`). */
export interface PairingCode {
  /** `XXXX-XXXX`, from an alphabet without look-alike characters. */
  code: string;
  /** ISO 8601 timestamp; the code is useless after it. */
  expiresAt: string;
}

/** One of the caller's paired devices (`ConnectorResponse`). */
export interface Connector {
  id: string;
  name: string;
  /** Whether the device currently holds the WebSocket connection (from the in-process hub). */
  connected: boolean;
  /** ISO 8601 timestamp, or `null` if it never connected. */
  lastSeenAt: string | null;
  /** ISO 8601 timestamp. */
  createdAt: string;
}

/** Why a connector call failed; the page maps it to its own message, never the server's text. */
export type ConnectorErrorKind = 'notFound' | 'network' | 'unexpected';

type Payload = Record<string, unknown>;

function isPayload(value: unknown): value is Payload {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** A date the browser can format; a malformed one is treated as a malformed reply. */
function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value));
}

/** A malformed reply is a `TypeError` (shown as an unexpected error), never a broken card. */
function toPairingCode(body: unknown): PairingCode {
  if (!isPayload(body) || typeof body.code !== 'string' || !isTimestamp(body.expires_at)) {
    throw new TypeError('Malformed pairing code reply.');
  }
  return { code: body.code, expiresAt: body.expires_at };
}

function toConnector(body: unknown): Connector {
  if (
    !isPayload(body) ||
    typeof body.id !== 'string' ||
    typeof body.name !== 'string' ||
    typeof body.connected !== 'boolean' ||
    !isTimestamp(body.created_at) ||
    (body.last_seen_at !== null && !isTimestamp(body.last_seen_at))
  ) {
    throw new TypeError('Malformed connector reply.');
  }
  return {
    id: body.id,
    name: body.name,
    connected: body.connected,
    lastSeenAt: body.last_seen_at,
    createdAt: body.created_at,
  };
}

/** `POST /api/v1/connectors/pairing-codes`: a fresh code for the caller (any role). */
export async function createPairingCode(api: ApiClient): Promise<PairingCode> {
  return toPairingCode(await api.post<unknown>('/api/v1/connectors/pairing-codes'));
}

/** `GET /api/v1/connectors`: only the caller's devices, oldest first. */
export async function listConnectors(api: ApiClient): Promise<Connector[]> {
  const body = await api.get<unknown>('/api/v1/connectors');
  if (!isPayload(body) || !Array.isArray(body.connectors)) {
    throw new TypeError('Malformed connector list reply.');
  }
  return body.connectors.map(toConnector);
}

/** `DELETE /api/v1/connectors/{id}`: revokes the device and closes its WebSocket (204). */
export async function revokeConnector(api: ApiClient, id: string): Promise<void> {
  await api.request<void>(`/api/v1/connectors/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** Map a failed call to a message key suffix; an unknown failure is "unexpected". */
export function connectorErrorKind(error: unknown): ConnectorErrorKind {
  if (!(error instanceof ApiError)) {
    return 'unexpected';
  }
  if (error.status === 0 || (error.status >= 502 && error.status <= 504)) {
    return 'network';
  }
  return error.status === 404 ? 'notFound' : 'unexpected';
}
