import { useRef, useState, type FormEvent } from 'react';
import type { Credentials } from '../../api/auth';
import { t, type MessageKey } from '../../i18n';
import { Alert } from '../atoms/Alert';
import { Button } from '../atoms/Button';
import { TextField } from '../molecules/TextField';

export interface LoginFormProps {
  onSubmit: (credentials: Credentials) => void | Promise<void>;
  busy: boolean;
  /** Translated server-side error, announced as an alert. */
  error: string | null;
  /** Translated reason for being here (session expired, logged out). */
  notice: string | null;
  defaultTenant?: string;
}

type FieldName = keyof Credentials;
type FieldErrors = Partial<Record<FieldName, MessageKey>>;

const FIELD_ORDER: readonly FieldName[] = ['tenantSlug', 'email', 'password'];
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function validate(values: Credentials): FieldErrors {
  const errors: FieldErrors = {};
  if (!values.tenantSlug.trim()) {
    errors.tenantSlug = 'login.errors.tenantRequired';
  }
  const email = values.email.trim();
  if (!email) {
    errors.email = 'login.errors.emailRequired';
  } else if (!EMAIL_PATTERN.test(email)) {
    errors.email = 'login.errors.emailInvalid';
  }
  if (!values.password) {
    errors.password = 'login.errors.passwordRequired';
  }
  return errors;
}

export function LoginForm({ onSubmit, busy, error, notice, defaultTenant = 'demo' }: LoginFormProps) {
  const [values, setValues] = useState<Credentials>({ tenantSlug: defaultTenant, email: '', password: '' });
  const [errors, setErrors] = useState<FieldErrors>({});
  const tenantRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  function update(name: FieldName, value: string) {
    setValues((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: undefined }));
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    const found = validate(values);
    setErrors(found);
    const firstInvalid = FIELD_ORDER.find((name) => found[name]);
    if (firstInvalid) {
      const refs = { tenantSlug: tenantRef, email: emailRef, password: passwordRef };
      refs[firstInvalid].current?.focus();
      return;
    }
    void onSubmit(values);
  }

  const message = (name: FieldName) => {
    const key = errors[name];
    return key ? t(key) : null;
  };

  return (
    <form className="login-form" noValidate onSubmit={handleSubmit}>
      {error ? <Alert tone="error">{error}</Alert> : notice && <Alert tone="info">{notice}</Alert>}
      <TextField
        ref={tenantRef}
        id="login-tenant"
        label={t('login.tenantLabel')}
        hint={t('login.tenantHint')}
        error={message('tenantSlug')}
        value={values.tenantSlug}
        onChange={(event) => update('tenantSlug', event.target.value)}
        autoComplete="organization"
        autoCapitalize="none"
        spellCheck={false}
        required
      />
      <TextField
        ref={emailRef}
        id="login-email"
        type="email"
        label={t('login.emailLabel')}
        error={message('email')}
        value={values.email}
        onChange={(event) => update('email', event.target.value)}
        autoComplete="username"
        inputMode="email"
        spellCheck={false}
        required
      />
      <TextField
        ref={passwordRef}
        id="login-password"
        type="password"
        label={t('login.passwordLabel')}
        error={message('password')}
        value={values.password}
        onChange={(event) => update('password', event.target.value)}
        autoComplete="current-password"
        required
      />
      <Button type="submit" aria-disabled={busy || undefined}>
        {busy ? t('login.submitting') : t('login.submit')}
      </Button>
    </form>
  );
}
