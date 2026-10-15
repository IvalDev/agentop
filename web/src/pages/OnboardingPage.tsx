import { useEffect, useState, type FormEvent } from 'react';
import { Link, useParams } from 'react-router';
import { ApiError } from '../api/client';
import {
  getOnboardingState,
  invalidAnswerKeys,
  oauthAuthorizePath,
  restartOnboarding,
  submitStep,
  type OnboardingState,
  type OnboardingStep,
} from '../api/onboarding';
import { Alert } from '../components/atoms/Alert';
import { Button } from '../components/atoms/Button';
import { SelectField } from '../components/molecules/SelectField';
import { TextField } from '../components/molecules/TextField';
import { ConfirmPanel } from '../components/organisms/ConfirmPanel';
import { usePageTitle } from '../hooks/usePageTitle';
import { t, type MessageKey } from '../i18n';
import { paths } from '../routes/paths';
import { useSession } from '../session/SessionProvider';

const CONFIRM_TITLE_ID = 'onboarding-confirm-title';

/** Why loading the state failed; each maps to a catalog message, never to the server's text. */
type LoadFailure = 'unknownModule' | 'inactive' | 'network' | 'unexpected';

function loadFailureKind(error: unknown): LoadFailure {
  if (!(error instanceof ApiError)) {
    return 'unexpected';
  }
  switch (error.status) {
    case 0:
    case 502:
    case 503:
    case 504:
      return 'network';
    case 404:
      return 'unknownModule';
    case 409:
      return 'inactive';
    default:
      return 'unexpected';
  }
}

/** Why submitting the current step failed; `invalid` also carries the offending field keys. */
type SubmitFailure = 'invalid' | 'stale' | 'testFailed' | 'testUnavailable' | 'testIncomplete' | 'network' | 'unexpected';

function submitFailureKind(error: unknown, stepType: OnboardingStep['type']): SubmitFailure {
  if (!(error instanceof ApiError)) {
    return 'unexpected';
  }
  switch (error.status) {
    case 0:
    case 503:
    case 504:
      return 'network';
    case 409:
      return stepType === 'test_connection' ? 'testFailed' : 'stale';
    case 422:
      return 'invalid';
    case 501:
      return stepType === 'test_connection' ? 'testUnavailable' : 'unexpected';
    case 502:
      return stepType === 'test_connection' ? 'testIncomplete' : 'network';
    default:
      return 'unexpected';
  }
}

function submitFailureMessage(kind: SubmitFailure): string {
  const key: MessageKey =
    kind === 'invalid'
      ? 'onboarding.form.invalid'
      : kind === 'stale'
        ? 'onboarding.stale'
        : kind === 'testFailed'
          ? 'onboarding.errors.testFailed'
          : kind === 'testUnavailable'
            ? 'onboarding.errors.testUnavailable'
            : kind === 'testIncomplete'
              ? 'onboarding.errors.testIncomplete'
              : kind === 'network'
                ? 'onboarding.errors.network'
                : 'onboarding.errors.unexpected';
  return t(key);
}

function loadFailureMessage(kind: LoadFailure): string {
  const key: MessageKey =
    kind === 'unknownModule'
      ? 'onboarding.errors.unknownModule'
      : kind === 'inactive'
        ? 'onboarding.errors.inactive'
        : kind === 'network'
          ? 'onboarding.errors.network'
          : 'onboarding.errors.unexpected';
  return t(key);
}

/** DOM id of a step field's input, so focus can move to the first invalid one. */
function fieldDomId(key: string): string {
  return `onboarding-field-${key}`;
}

/**
 * Settings → onboarding wizard (every role): a GENERIC renderer of the server's
 * `StepSpec` — it interprets `redirect`, `test_connection`, `form`, and
 * `scope_select` with zero per-module branches, so a new module ships without
 * touching this file. The OAuth callback lands back here (D8).
 */
export function OnboardingPage() {
  const { module: moduleKey = '' } = useParams<{ module: string }>();
  const title = t('sections.onboarding.title');
  usePageTitle(title);
  const { api } = useSession();
  const [state, setState] = useState<OnboardingState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailure, setLoadFailure] = useState<LoadFailure | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [values, setValues] = useState<Record<string, string>>({});
  const [selectedScopes, setSelectedScopes] = useState<string[]>([]);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [submitFailure, setSubmitFailure] = useState<SubmitFailure | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmingRestart, setConfirmingRestart] = useState(false);
  const [restartFailure, setRestartFailure] = useState<SubmitFailure | null>(null);
  const [restarted, setRestarted] = useState(false);

  useEffect(() => {
    let current = true;
    getOnboardingState(api, moduleKey).then(
      (loaded) => {
        if (current) {
          setState(loaded);
          setLoading(false);
          setLoadFailure(null);
        }
      },
      (error) => {
        if (current) {
          setLoading(false);
          setLoadFailure(loadFailureKind(error));
        }
      },
    );
    return () => {
      current = false;
    };
  }, [api, moduleKey, attempt]);

  // A new step starts clean: values, selections, and errors are re-derived from
  // the server's declarative definition (secrets never linger between steps).
  // Adjusted during render, the React-endorsed alternative to an effect here.
  const [seededState, setSeededState] = useState<OnboardingState | null>(null);
  if (state !== seededState) {
    setSeededState(state);
    const step = state?.step;
    setValues(step === null || step === undefined ? {} : Object.fromEntries(step.fields.map((field) => [field.key, ''])));
    setSelectedScopes([]);
    setFieldErrors({});
    setSubmitFailure(null);
  }

  function reload() {
    setLoading(true);
    setRestarted(false);
    setAttempt((value) => value + 1);
  }

  async function runSubmit(step: OnboardingStep, answers: Record<string, unknown>): Promise<void> {
    setBusy(true);
    setSubmitFailure(null);
    try {
      const next = await submitStep(api, moduleKey, step.key, answers);
      setState(next);
    } catch (error) {
      const kind = submitFailureKind(error, step.type);
      setSubmitFailure(kind);
      if (kind === 'invalid' && error instanceof ApiError) {
        const keys = invalidAnswerKeys(error.detail).filter((key) => step.fields.some((field) => field.key === key));
        setFieldErrors(Object.fromEntries(keys.map((key) => [key, t('onboarding.form.fieldError')])));
        if (keys[0]) {
          document.getElementById(fieldDomId(keys[0]))?.focus();
        }
      }
      if (kind === 'stale') {
        reload();
      }
    } finally {
      setBusy(false);
    }
  }

  function handleFormSubmit(event: FormEvent<HTMLFormElement>, step: OnboardingStep) {
    event.preventDefault();
    if (busy) {
      return;
    }
    const answers: Record<string, unknown> = {};
    const errors: Record<string, string> = {};
    for (const field of step.fields) {
      const raw = (values[field.key] ?? '').trim();
      if (raw === '') {
        if (field.required) {
          errors[field.key] = t('onboarding.form.fieldRequired');
        }
        continue;
      }
      answers[field.key] = field.type === 'number' ? Number(raw) : raw;
    }
    setFieldErrors(errors);
    const firstInvalid = step.fields.find((field) => errors[field.key] !== undefined);
    if (firstInvalid) {
      document.getElementById(fieldDomId(firstInvalid.key))?.focus();
      return;
    }
    void runSubmit(step, answers);
  }

  function handleScopeSubmit(event: FormEvent<HTMLFormElement>, step: OnboardingStep) {
    event.preventDefault();
    if (busy) {
      return;
    }
    // The engine synthesises a single `scopes` field with the allowlisted options.
    const field = step.fields.find((candidate) => candidate.key === 'scopes') ?? step.fields[0];
    if (field === undefined) {
      void runSubmit(step, {});
      return;
    }
    if (field.required && selectedScopes.length === 0) {
      setFieldErrors({ [field.key]: t('onboarding.form.scopesRequired') });
      return;
    }
    setFieldErrors({});
    void runSubmit(step, { [field.key]: [...selectedScopes] });
  }

  async function handleRestart() {
    setBusy(true);
    setRestartFailure(null);
    try {
      const next = await restartOnboarding(api, moduleKey);
      setState(next);
      setConfirmingRestart(false);
      setRestarted(true);
    } catch (error) {
      setRestartFailure(submitFailureKind(error, 'form'));
    } finally {
      setBusy(false);
    }
  }

  let body = null;
  if (loading && state === null) {
    body = <p className="page__note">{t('onboarding.loading')}</p>;
  } else if (loadFailure !== null) {
    body = (
      <>
        <Alert tone="error">{loadFailureMessage(loadFailure)}</Alert>
        <div className="actions">
          {loadFailure !== 'unknownModule' && loadFailure !== 'inactive' && (
            <Button variant="secondary" aria-disabled={loading || undefined} onClick={loading ? undefined : reload}>
              {t('onboarding.retry')}
            </Button>
          )}
          <Link to={paths.myTools}>{t('onboarding.backToMyTools')}</Link>
        </div>
      </>
    );
  } else if (state !== null && (state.status === 'completed' || state.step === null)) {
    body = (
      <section className="section">
        <h2 className="section__title">{t('onboarding.completed.title')}</h2>
        <p>{t('onboarding.completed.description')}</p>
        {restartFailure !== null && <Alert tone="error">{submitFailureMessage(restartFailure)}</Alert>}
        {restarted && <Alert tone="info">{t('onboarding.completed.restarted')}</Alert>}
        {confirmingRestart ? (
          <ConfirmPanel
            titleId={CONFIRM_TITLE_ID}
            title={t('onboarding.completed.reconnectTitle')}
            confirmLabel={t('onboarding.completed.reconnectConfirm')}
            cancelLabel={t('onboarding.cancel')}
            busy={busy}
            onConfirm={() => void handleRestart()}
            onCancel={() => setConfirmingRestart(false)}
          >
            <p>{t('onboarding.completed.reconnectDescription')}</p>
          </ConfirmPanel>
        ) : (
          <div className="actions">
            <Button variant="secondary" onClick={() => setConfirmingRestart(true)}>
              {t('onboarding.completed.reconnect')}
            </Button>
          </div>
        )}
      </section>
    );
  } else if (state?.step !== null && state?.step !== undefined) {
    const step = state.step;
    const heading = <h2 className="section__title">{step.title}</h2>;
    const intro = <p>{step.description}</p>;
    const notice = submitFailure !== null && <Alert tone="error">{submitFailureMessage(submitFailure)}</Alert>;

    if (step.type === 'redirect') {
      body = (
        <section className="section">
          {heading}
          {intro}
          {notice}
          <div className="actions">
            {/* The browser leaves for the provider: a redirect step is never POSTed. */}
            <Button onClick={() => window.location.assign(oauthAuthorizePath(moduleKey))}>
              {t('onboarding.redirect.connect')}
            </Button>
          </div>
        </section>
      );
    } else if (step.type === 'test_connection') {
      body = (
        <section className="section">
          {heading}
          {intro}
          {notice}
          <div className="actions">
            <Button aria-disabled={busy || undefined} onClick={() => void runSubmit(step, {})}>
              {busy ? t('onboarding.test.running') : t('onboarding.test.action')}
            </Button>
          </div>
        </section>
      );
    } else if (step.type === 'scope_select') {
      const field = step.fields.find((candidate) => candidate.key === 'scopes') ?? step.fields[0];
      body = (
        <section className="section">
          {heading}
          {intro}
          {notice}
          <form noValidate onSubmit={(event) => handleScopeSubmit(event, step)}>
            {field !== undefined && (
              <fieldset className="field">
                <legend className="field__label">{field.label}</legend>
                {field.helpText && <p className="field__hint">{field.helpText}</p>}
                {field.options.map((option) => (
                  <label key={option}>
                    <input
                      type="checkbox"
                      value={option}
                      checked={selectedScopes.includes(option)}
                      aria-invalid={fieldErrors[field.key] ? true : undefined}
                      onChange={(event) =>
                        setSelectedScopes((current) =>
                          event.target.checked ? [...current, option] : current.filter((value) => value !== option),
                        )
                      }
                    />{' '}
                    {option}
                  </label>
                ))}
                {fieldErrors[field.key] && (
                  <p className="field__error" role="alert">
                    {fieldErrors[field.key]}
                  </p>
                )}
              </fieldset>
            )}
            <div className="actions">
              <Button type="submit" aria-disabled={busy || undefined}>
                {busy ? t('onboarding.form.submitting') : t('onboarding.form.submit')}
              </Button>
            </div>
          </form>
        </section>
      );
    } else {
      body = (
        <section className="section">
          {heading}
          {intro}
          {notice}
          <form className="member-form" noValidate onSubmit={(event) => handleFormSubmit(event, step)}>
            {step.fields.map((field) =>
              field.type === 'select' ? (
                <SelectField
                  key={field.key}
                  id={fieldDomId(field.key)}
                  label={field.label}
                  hint={field.helpText || undefined}
                  error={fieldErrors[field.key] ?? null}
                  options={[
                    { value: '', label: '—' },
                    ...field.options.map((option) => ({ value: option, label: option })),
                  ]}
                  value={values[field.key] ?? ''}
                  onChange={(event) =>
                    setValues((current) => ({ ...current, [field.key]: event.target.value }))
                  }
                  required={field.required}
                />
              ) : (
                <TextField
                  key={field.key}
                  id={fieldDomId(field.key)}
                  type={field.secret ? 'password' : field.type}
                  label={field.label}
                  hint={field.helpText || undefined}
                  error={fieldErrors[field.key] ?? null}
                  value={values[field.key] ?? ''}
                  onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))}
                  autoComplete={field.secret ? 'new-password' : 'off'}
                  required={field.required}
                />
              ),
            )}
            <div className="actions">
              <Button type="submit" aria-disabled={busy || undefined}>
                {busy ? t('onboarding.form.submitting') : t('onboarding.form.submit')}
              </Button>
            </div>
          </form>
        </section>
      );
    }
  }

  return (
    <div className="page onboarding">
      <h1 className="page__title">{title}</h1>
      {body}
    </div>
  );
}
