import { describe, expect, it } from 'vitest';
import { headersOf, installFakeApi, routesCalled } from '../test/fakeApi';
import {
  approvalErrorKind,
  approveApproval,
  executeApproval,
  getApproval,
  precheckReason,
  rejectApproval,
} from './approvals';
import { ApiError, createApiClient } from './client';

const ID = '7f3e2a10-1111-4000-8000-00000000abcd';
const BASE = `/api/v1/approvals/${ID}`;

/** An `ApprovalResponse` as `app/modules/approvals/router.py` writes it. */
function approvalBody(overrides: Record<string, unknown> = {}) {
  return {
    id: ID,
    tool_name: 'send_email',
    input: { to: ['ana@example.com'], subject: 'Hola', body: '¿Cómo estás?', cc: [], bcc: [], thread_id: null },
    action_key: 'send_email:abc',
    required_role: 'operator',
    status: 'pending',
    requested_by: 1,
    created_at: '2026-10-18T09:00:00Z',
    expires_at: '2026-10-18T10:00:00Z',
    decided_by: null,
    decided_at: null,
    decision_reason: null,
    executed_by: null,
    executed_at: null,
    execution_result: null,
    execution_error: null,
    ...overrides,
  };
}

function client() {
  return createApiClient({ getToken: () => 'session-token', onUnauthorized: () => undefined });
}

describe('approvals API client', () => {
  it('reads one approval with the session token and maps it to camelCase', async () => {
    const fetchMock = installFakeApi({ [`GET ${BASE}`]: { status: 200, body: approvalBody() } });

    const details = await getApproval(client(), ID);

    expect(details).toEqual({
      id: ID,
      toolName: 'send_email',
      input: { to: ['ana@example.com'], subject: 'Hola', body: '¿Cómo estás?', cc: [], bcc: [], thread_id: null },
      status: 'pending',
      expiresAt: '2026-10-18T10:00:00Z',
      executionError: null,
    });
    expect(headersOf(fetchMock.mock.calls[0]?.[1]).get('Authorization')).toBe('Bearer session-token');
  });

  it('approves, rejects, and executes with bodiless POSTs to the request own routes', async () => {
    const fetchMock = installFakeApi({
      [`POST ${BASE}/approve`]: { status: 200, body: approvalBody({ status: 'approved' }) },
      [`POST ${BASE}/reject`]: { status: 200, body: approvalBody({ status: 'rejected' }) },
      [`POST ${BASE}/execute`]: {
        status: 200,
        body: approvalBody({ status: 'failed', execution_error: 'precheck_failed: reconnect Google' }),
      },
    });

    const approved = await approveApproval(client(), ID);
    const rejected = await rejectApproval(client(), ID);
    const executed = await executeApproval(client(), ID);

    expect(routesCalled(fetchMock)).toEqual([`POST ${BASE}/approve`, `POST ${BASE}/reject`, `POST ${BASE}/execute`]);
    expect(fetchMock.mock.calls.map(([, init]) => init?.body)).toEqual([undefined, undefined, undefined]);
    expect([approved.status, rejected.status, executed.status]).toEqual(['approved', 'rejected', 'failed']);
    expect(executed.executionError).toBe('precheck_failed: reconnect Google');
  });

  it('escapes the id into a single path segment', async () => {
    const fetchMock = installFakeApi({});

    await expect(getApproval(client(), '../users')).rejects.toBeInstanceOf(ApiError);

    expect(routesCalled(fetchMock)).toEqual(['GET /api/v1/approvals/..%2Fusers']);
  });

  it.each([
    ['an unknown status', { status: 'teleported' }],
    ['a missing tool name', { tool_name: 42 }],
    ['an input that is not an object', { input: ['x'] }],
  ])('refuses a reply with %s', async (_, overrides) => {
    installFakeApi({ [`GET ${BASE}`]: { status: 200, body: approvalBody(overrides) } });

    const error = await getApproval(client(), ID).catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(TypeError);
    expect(approvalErrorKind(error)).toBe('unexpected');
  });

  it.each([
    [0, 'network'],
    [403, 'forbidden'],
    [404, 'notFound'],
    [409, 'conflict'],
    [410, 'expired'],
    [503, 'network'],
    [500, 'unexpected'],
  ] as const)('maps HTTP %i to the error kind "%s"', (status, kind) => {
    expect(approvalErrorKind(new ApiError(status, 'detail'))).toBe(kind);
  });

  it('extracts only the tool reason of a precheck refusal', () => {
    expect(precheckReason('precheck_failed: google mail connection expired')).toBe('google mail connection expired');
    expect(precheckReason('tool_failed')).toBeNull();
    expect(precheckReason('precheck_failed: ')).toBeNull();
    expect(precheckReason(null)).toBeNull();
  });
});
