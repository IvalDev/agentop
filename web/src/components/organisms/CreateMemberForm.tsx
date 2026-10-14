import { useRef, useState, type FormEvent } from 'react';
import type { NewMember } from '../../api/team';
import { t, type MessageKey } from '../../i18n';
import { parseRole, type Role } from '../../session/roles';
import { passwordProblem, type TeamErrorKind } from '../../team/members';
import { Alert } from '../atoms/Alert';
import { Button } from '../atoms/Button';
import { SelectField } from '../molecules/SelectField';
import { TextField } from '../molecules/TextField';

export interface CreateMemberFormProps {
  /** The roles the signed-in user may give: an admin never sees "Propietario". */
  roles: Role[];
  /** Creates the member; resolves `null` on success or why it failed. */
  onSubmit: (member: NewMember) => Promise<TeamErrorKind | null>;
}

type FieldName = 'fullName' | 'email' | 'password';
type FieldErrors = Partial<Record<FieldName, MessageKey>>;

const FIELD_ORDER: readonly FieldName[] = ['fullName', 'email', 'password'];
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const EMPTY: NewMember = { fullName: '', email: '', role: 'viewer', password: '' };

function validate(values: NewMember): FieldErrors {
  const errors: FieldErrors = {};
  if (!values.fullName.trim()) {
    errors.fullName = 'team.create.errors.nameRequired';
  }
  const email = values.email.trim();
  if (!email) {
    errors.email = 'team.create.errors.emailRequired';
  } else if (!EMAIL_PATTERN.test(email)) {
    errors.email = 'team.create.errors.emailInvalid';
  }
  const password = passwordProblem(values.password);
  if (password) {
    errors.password = `team.create.errors.${password}`;
  }
  return errors;
}

/** Full name, email, role, and an initial password with its rules shown. */
export function CreateMemberForm({ roles, onSubmit }: CreateMemberFormProps) {
  const [values, setValues] = useState<NewMember>(EMPTY);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [failure, setFailure] = useState<TeamErrorKind | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  function update<K extends keyof NewMember>(name: K, value: NewMember[K]) {
    setValues((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: undefined }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    setFailure(null);
    setCreated(null);
    const found = validate(values);
    setErrors(found);
    const firstInvalid = FIELD_ORDER.find((name) => found[name]);
    if (firstInvalid) {
      const refs = { fullName: nameRef, email: emailRef, password: passwordRef };
      refs[firstInvalid].current?.focus();
      return;
    }
    setBusy(true);
    const error = await onSubmit(values);
    setBusy(false);
    if (error === null) {
      setCreated(t('team.create.done', { name: values.fullName.trim() }));
      setValues(EMPTY);
    } else if (error === 'emailTaken') {
      setErrors({ email: 'team.errors.emailTaken' });
      emailRef.current?.focus();
    } else {
      setFailure(error);
    }
  }

  const message = (name: FieldName) => {
    const key = errors[name];
    return key ? t(key) : null;
  };

  return (
    <form className="member-form" noValidate onSubmit={(event) => void handleSubmit(event)}>
      {failure && <Alert tone="error">{t(`team.errors.${failure}`)}</Alert>}
      {created && <Alert tone="info">{created}</Alert>}
      <TextField
        ref={nameRef}
        id="member-name"
        label={t('team.create.nameLabel')}
        error={message('fullName')}
        value={values.fullName}
        onChange={(event) => update('fullName', event.target.value)}
        autoComplete="off"
        maxLength={200}
        required
      />
      <TextField
        ref={emailRef}
        id="member-email"
        type="email"
        label={t('team.create.emailLabel')}
        error={message('email')}
        value={values.email}
        onChange={(event) => update('email', event.target.value)}
        autoComplete="off"
        inputMode="email"
        spellCheck={false}
        maxLength={320}
        required
      />
      <SelectField
        id="member-role"
        label={t('team.create.roleLabel')}
        options={roles.map((role) => ({ value: role, label: t(`roles.${role}`) }))}
        value={values.role}
        onChange={(event) => update('role', parseRole(event.target.value))}
      />
      <TextField
        ref={passwordRef}
        id="member-password"
        type="password"
        label={t('team.create.passwordLabel')}
        hint={t('team.create.passwordHint')}
        error={message('password')}
        value={values.password}
        onChange={(event) => update('password', event.target.value)}
        autoComplete="new-password"
        required
      />
      <div className="actions">
        <Button type="submit" aria-disabled={busy || undefined}>
          {busy ? t('team.create.submitting') : t('team.create.submit')}
        </Button>
      </div>
    </form>
  );
}
