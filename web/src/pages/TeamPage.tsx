import { useEffect, useRef, useState } from 'react';
import { changeMemberRole, createMember, setMemberActive, type TeamMember } from '../api/team';
import { Alert } from '../components/atoms/Alert';
import { Button } from '../components/atoms/Button';
import { SelectField } from '../components/molecules/SelectField';
import { ConfirmPanel } from '../components/organisms/ConfirmPanel';
import { CreateMemberForm } from '../components/organisms/CreateMemberForm';
import { TeamTable } from '../components/organisms/TeamTable';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';
import { parseRole, type Role } from '../session/roles';
import { useCurrentUser, useSession } from '../session/SessionProvider';
import { assignableRoles, memberActionId, teamErrorKind } from '../team/members';
import { useTeamMembers } from '../team/useTeamMembers';

const LIST_TITLE_ID = 'team-list-title';
const CREATE_TITLE_ID = 'team-create-title';
const CONFIRM_TITLE_ID = 'team-confirm-title';

/** The confirmation step waiting for an answer. */
type PendingChange = { kind: 'role'; member: TeamMember } | { kind: 'deactivate'; member: TeamMember };

interface Notice {
  tone: 'info' | 'error';
  text: string;
}

/**
 * Settings → Team (owner and admin): the company's people, adding one, changing a role, and
 * deactivating or reactivating an account. Controls the rules forbid are not offered; the API
 * enforces every rule anyway and its refusals are shown in Spanish.
 */
export function TeamPage() {
  const title = t('sections.team.title');
  usePageTitle(title);
  const { api } = useSession();
  const user = useCurrentUser();
  const caller = { id: user.id, role: user.role };
  const roles = assignableRoles(user.role);
  const team = useTeamMembers(api);
  const { upsert, reload } = team;
  const [pending, setPending] = useState<PendingChange | null>(null);
  const [newRole, setNewRole] = useState<Role>('viewer');
  const [roleError, setRoleError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  // After a confirmation closes, focus returns to the row button that opened it.
  const focusTargetRef = useRef<string | null>(null);

  useEffect(() => {
    const id = focusTargetRef.current;
    if (id !== null) {
      focusTargetRef.current = null;
      document.getElementById(id)?.focus();
    }
  });

  function close(member: TeamMember, action: 'role' | 'state') {
    setPending(null);
    setRoleError(null);
    focusTargetRef.current = memberActionId(member.id, action);
  }

  async function apply(
    member: TeamMember,
    action: 'role' | 'state',
    change: () => Promise<TeamMember>,
    done: (updated: TeamMember) => string,
  ) {
    setBusyId(member.id);
    setNotice(null);
    try {
      const updated = await change();
      upsert(updated);
      setNotice({ tone: 'info', text: done(updated) });
    } catch (error) {
      const kind = teamErrorKind(error);
      setNotice({ tone: 'error', text: t(`team.errors.${kind}`) });
      if (kind === 'notFound') {
        reload();
      }
    } finally {
      setBusyId(null);
      close(member, action);
    }
  }

  function confirmRole(member: TeamMember) {
    if (newRole === member.role) {
      setRoleError(t('team.roleChange.sameRole'));
      return;
    }
    void apply(
      member,
      'role',
      () => changeMemberRole(api, member.id, newRole),
      (updated) => t('team.roleChange.done', { name: updated.fullName, role: t(`roles.${updated.role}`) }),
    );
  }

  function confirmDeactivation(member: TeamMember) {
    void apply(
      member,
      'state',
      () => setMemberActive(api, member.id, false),
      (updated) => t('team.deactivation.done', { name: updated.fullName }),
    );
  }

  function reactivate(member: TeamMember) {
    void apply(
      member,
      'state',
      () => setMemberActive(api, member.id, true),
      (updated) => t('team.reactivation.done', { name: updated.fullName }),
    );
  }

  let confirmation = null;
  if (pending?.kind === 'role') {
    const { member } = pending;
    confirmation = (
      <ConfirmPanel
        key={`role-${member.id}`}
        titleId={CONFIRM_TITLE_ID}
        title={t('team.roleChange.title', { name: member.fullName })}
        confirmLabel={t('team.roleChange.confirm')}
        cancelLabel={t('team.cancel')}
        busy={busyId !== null}
        onConfirm={() => confirmRole(member)}
        onCancel={() => close(member, 'role')}
      >
        <p>{t('team.roleChange.currentRole', { role: t(`roles.${member.role}`) })}</p>
        <SelectField
          id="member-new-role"
          label={t('team.roleChange.newRoleLabel')}
          options={roles.map((role) => ({ value: role, label: t(`roles.${role}`) }))}
          value={newRole}
          error={roleError}
          onChange={(event) => {
            setNewRole(parseRole(event.target.value));
            setRoleError(null);
          }}
        />
      </ConfirmPanel>
    );
  } else if (pending?.kind === 'deactivate') {
    const { member } = pending;
    confirmation = (
      <ConfirmPanel
        key={`deactivate-${member.id}`}
        titleId={CONFIRM_TITLE_ID}
        title={t('team.deactivation.title', { name: member.fullName })}
        confirmLabel={t('team.deactivation.confirm')}
        cancelLabel={t('team.cancel')}
        busy={busyId !== null}
        onConfirm={() => confirmDeactivation(member)}
        onCancel={() => close(member, 'state')}
      >
        <p>{t('team.deactivation.description')}</p>
      </ConfirmPanel>
    );
  }

  return (
    <div className="page team">
      <h1 className="page__title">{title}</h1>
      <p className="page__lead">{t('sections.team.description')}</p>
      <section className="section" aria-labelledby={LIST_TITLE_ID}>
        <h2 id={LIST_TITLE_ID} className="section__title">
          {t('team.list.title')}
        </h2>
        {notice && <Alert tone={notice.tone}>{notice.text}</Alert>}
        {team.failed && (
          <>
            <Alert tone="error">{t('team.list.loadError')}</Alert>
            <div className="actions">
              <Button
                variant="secondary"
                aria-disabled={team.loading || undefined}
                onClick={team.loading ? undefined : reload}
              >
                {t('team.list.retry')}
              </Button>
            </div>
          </>
        )}
        {confirmation}
        {team.members === null ? (
          team.loading && <p className="page__note">{t('team.list.loading')}</p>
        ) : (
          <TeamTable
            members={team.members}
            caller={caller}
            busyId={busyId}
            onChangeRole={(member) => {
              setNotice(null);
              setNewRole(member.role);
              setRoleError(null);
              setPending({ kind: 'role', member });
            }}
            onDeactivate={(member) => {
              setNotice(null);
              setPending({ kind: 'deactivate', member });
            }}
            onReactivate={reactivate}
          />
        )}
      </section>
      <section className="section" aria-labelledby={CREATE_TITLE_ID}>
        <h2 id={CREATE_TITLE_ID} className="section__title">
          {t('team.create.title')}
        </h2>
        <CreateMemberForm
          roles={roles}
          onSubmit={async (member) => {
            try {
              upsert(await createMember(api, member));
              return null;
            } catch (error) {
              return teamErrorKind(error);
            }
          }}
        />
      </section>
    </div>
  );
}
