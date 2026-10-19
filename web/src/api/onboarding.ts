import type { ApiClient } from './client';

/** The caller's own onboarding status for one module (`OnboardingStateResponse.status`). */
export type OnboardingStatus = 'not_started' | 'in_progress' | 'completed';

/** Step kinds the generic renderer interprets (`OnboardingStepResponse.type`). */
export type OnboardingStepType = 'form' | 'redirect' | 'scope_select' | 'test_connection';

/** Widget types of a step field (`OnboardingFieldResponse.type`). */
export type OnboardingFieldType = 'text' | 'email' | 'password' | 'url' | 'number' | 'select';

/** One input of the current step, for the generic renderer (`OnboardingFieldResponse`). */
export interface OnboardingField {
  key: string;
  label: string;
  type: OnboardingFieldType;
  required: boolean;
  secret: boolean;
  helpText: string;
  options: string[];
}

/** Declarative definition of the step the user is parked on (`OnboardingStepResponse`). */
export interface OnboardingStep {
  key: string;
  type: OnboardingStepType;
  title: string;
  description: string;
  fields: OnboardingField[];
}

/** The caller's own onboarding state plus the current step (`OnboardingStateResponse`). */
export interface OnboardingState {
  moduleKey: string;
  status: OnboardingStatus;
  currentStep: string | null;
  /** `null` once the flow is completed (and for modules that declare no steps). */
  step: OnboardingStep | null;
  /** NON-secret answers only; the contract has no field for secrets. */
  data: Record<string, unknown>;
}

/** Wire shapes: the API answers snake_case; the client speaks camelCase. */
interface OnboardingFieldBody {
  key: string;
  label: string;
  type: OnboardingFieldType;
  required: boolean;
  secret: boolean;
  help_text: string;
  options: string[];
}

interface OnboardingStepBody {
  key: string;
  type: OnboardingStepType;
  title: string;
  description: string;
  fields: OnboardingFieldBody[];
}

interface OnboardingStateBody {
  module_key: string;
  status: OnboardingStatus;
  current_step: string | null;
  step: OnboardingStepBody | null;
  data: Record<string, unknown>;
}

function toField(body: OnboardingFieldBody): OnboardingField {
  return {
    key: body.key,
    label: body.label,
    type: body.type,
    required: body.required,
    secret: body.secret,
    helpText: body.help_text,
    options: [...body.options],
  };
}

function toStep(body: OnboardingStepBody): OnboardingStep {
  return {
    key: body.key,
    type: body.type,
    title: body.title,
    description: body.description,
    fields: body.fields.map(toField),
  };
}

function toState(body: OnboardingStateBody): OnboardingState {
  return {
    moduleKey: body.module_key,
    status: body.status,
    currentStep: body.current_step,
    step: body.step === null ? null : toStep(body.step),
    data: { ...body.data },
  };
}

function modulePath(moduleKey: string): string {
  return `/api/v1/onboarding/${encodeURIComponent(moduleKey)}`;
}

/** Where the browser starts the module's OAuth flow (`GET /api/v1/oauth/{key}/authorize`). */
export function oauthAuthorizePath(moduleKey: string): string {
  return `/api/v1/oauth/${encodeURIComponent(moduleKey)}/authorize`;
}

/**
 * `POST /api/v1/oauth/{key}/authorize`: the Google consent URL as JSON. The
 * wizard fetches it here (with the bearer header) and navigates itself, since
 * a top-level `window.location` navigation cannot carry that header.
 */
export async function startAuthorization(
  api: ApiClient,
  moduleKey: string,
): Promise<{ url: string }> {
  return api.post<{ url: string }>(oauthAuthorizePath(moduleKey));
}

/** `GET /api/v1/onboarding/{key}`: the caller's own state and the current step to render. */
export async function getOnboardingState(api: ApiClient, moduleKey: string): Promise<OnboardingState> {
  return toState(await api.get<OnboardingStateBody>(modulePath(moduleKey)));
}

/**
 * `POST /api/v1/onboarding/{key}/steps/{step}`: submit one step's answers.
 * `test_connection` steps take no answers (the engine reads an empty dict);
 * `redirect` steps are refused by the engine — the browser navigates instead.
 */
export async function submitStep(
  api: ApiClient,
  moduleKey: string,
  stepKey: string,
  answers: Record<string, unknown> = {},
): Promise<OnboardingState> {
  const path = `${modulePath(moduleKey)}/steps/${encodeURIComponent(stepKey)}`;
  return toState(await api.post<OnboardingStateBody>(path, { answers }));
}

/** `POST /api/v1/onboarding/{key}/restart`: reset a completed flow to its first step. */
export async function restartOnboarding(api: ApiClient, moduleKey: string): Promise<OnboardingState> {
  return toState(await api.post<OnboardingStateBody>(`${modulePath(moduleKey)}/restart`));
}

/**
 * Field keys named by a 422 answer-validation detail. The service messages name
 * keys and constraints but never submitted values: "missing required answer
 * 'key'", "answer 'key' must be …", "unknown answer field(s): a, b". Anything
 * unrecognised yields no keys (the caller shows a generic message).
 */
export function invalidAnswerKeys(detail: unknown): string[] {
  if (typeof detail !== 'string') {
    return [];
  }
  const keys = new Set<string>();
  for (const match of detail.matchAll(/answer '([^']+)'/g)) {
    keys.add(match[1]!);
  }
  const unknown = detail.match(/^unknown answer field\(s\): (.+)$/);
  if (unknown?.[1]) {
    for (const key of unknown[1].split(', ')) {
      keys.add(key.trim());
    }
  }
  return [...keys];
}
