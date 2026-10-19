import { ApiError, type ApiClient } from './client';

/**
 * The approvals API (`app/modules/approvals/router.py`): read one request, approve or reject
 * it, then execute the call the server stored. The client never sends a payload, key, or
 * grant: execution always runs exactly the stored call. The tenant and the caller come from
 * the session; for a tool that acts on the requester's own account (the Gmail actions) only
 * the requester may decide or execute it, so anyone else gets 403.
 * Everything here is untrusted text: the UI renders it as text, never as HTML.
 */

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired' | 'executing' | 'executed' | 'failed';

/** One approval request (`ApprovalResponse`), reduced to what the chat shows. */
export interface ApprovalDetails {
  id: string;
  toolName: string;
  /** The stored call's validated input: exactly what will run. */
  input: Record<string, unknown>;
  status: ApprovalStatus;
  /** ISO 8601 timestamp. */
  expiresAt: string;
  /** Why the execution failed (`failed` only), e.g. `precheck_failed: <reason>`. */
  executionError: string | null;
}

/** Why an approvals call failed; each maps to a catalog message, never to the server's text. */
export type ApprovalErrorKind = 'forbidden' | 'notFound' | 'conflict' | 'expired' | 'network' | 'unexpected';

const STATUSES: readonly ApprovalStatus[] = [
  'pending',
  'approved',
  'rejected',
  'expired',
  'executing',
  'executed',
  'failed',
];

/** The executor's prefix for a precheck refusal (`app/modules/tools/executor.py`). */
const PRECHECK_PREFIX = 'precheck_failed: ';

type Payload = Record<string, unknown>;

function isPayload(value: unknown): value is Payload {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStatus(value: unknown): value is ApprovalStatus {
  return typeof value === 'string' && (STATUSES as readonly string[]).includes(value);
}

/** Map a reply; a malformed one is a `TypeError` (shown as an unexpected error). */
function toDetails(body: unknown): ApprovalDetails {
  if (
    !isPayload(body) ||
    typeof body.id !== 'string' ||
    typeof body.tool_name !== 'string' ||
    !isPayload(body.input) ||
    !isStatus(body.status)
  ) {
    throw new TypeError('Malformed approval reply.');
  }
  return {
    id: body.id,
    toolName: body.tool_name,
    input: body.input,
    status: body.status,
    expiresAt: typeof body.expires_at === 'string' ? body.expires_at : '',
    executionError: typeof body.execution_error === 'string' ? body.execution_error : null,
  };
}

function approvalPath(id: string, action?: 'approve' | 'reject' | 'execute'): string {
  const base = `/api/v1/approvals/${encodeURIComponent(id)}`;
  return action === undefined ? base : `${base}/${action}`;
}

/** `GET /api/v1/approvals/{id}`: what will run and the request's current state. */
export async function getApproval(api: ApiClient, id: string, signal?: AbortSignal): Promise<ApprovalDetails> {
  return toDetails(await api.get<unknown>(approvalPath(id), { signal }));
}

/** `POST /api/v1/approvals/{id}/approve`: a pending request becomes `approved` (not run yet). */
export async function approveApproval(api: ApiClient, id: string): Promise<ApprovalDetails> {
  return toDetails(await api.post<unknown>(approvalPath(id, 'approve')));
}

/** `POST /api/v1/approvals/{id}/reject` (no reason): the request never runs. */
export async function rejectApproval(api: ApiClient, id: string): Promise<ApprovalDetails> {
  return toDetails(await api.post<unknown>(approvalPath(id, 'reject')));
}

/**
 * `POST /api/v1/approvals/{id}/execute`: run the approved call once. A tool that ran but
 * failed still answers 200, with status `failed` and its `executionError`.
 */
export async function executeApproval(api: ApiClient, id: string): Promise<ApprovalDetails> {
  return toDetails(await api.post<unknown>(approvalPath(id, 'execute')));
}

/** Why a call failed, from the router's status mapping (one place: `_HTTP_STATUS`). */
export function approvalErrorKind(error: unknown): ApprovalErrorKind {
  if (!(error instanceof ApiError)) {
    return 'unexpected';
  }
  if (error.status === 0 || (error.status >= 502 && error.status <= 504)) {
    return 'network';
  }
  switch (error.status) {
    case 403:
      return 'forbidden';
    case 404:
      return 'notFound';
    case 409:
      return 'conflict';
    case 410:
      return 'expired';
    default:
      return 'unexpected';
  }
}

/**
 * The tool's own reason when execution failed its precheck (`precheck_failed: <reason>`):
 * short, user-safe, and actionable by contract (`ToolPrecheckError`). `null` for any other
 * error, whose code says nothing useful to the user.
 */
export function precheckReason(executionError: string | null): string | null {
  if (executionError === null || !executionError.startsWith(PRECHECK_PREFIX)) {
    return null;
  }
  const reason = executionError.slice(PRECHECK_PREFIX.length).trim();
  return reason === '' ? null : reason;
}
