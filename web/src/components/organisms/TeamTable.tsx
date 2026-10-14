import type { TeamMember } from '../../api/team';
import { formatDate, t } from '../../i18n';
import type { Role } from '../../session/roles';
import { memberActionId, memberRestriction } from '../../team/members';
import { RowActionButton } from '../molecules/RowActionButton';

export interface TeamTableProps {
  members: TeamMember[];
  /** The signed-in user: their own row, and owners for an admin, offer no actions. */
  caller: { id: number; role: Role };
  /** The member whose change is running; their buttons wait. */
  busyId: number | null;
  onChangeRole: (member: TeamMember) => void;
  onDeactivate: (member: TeamMember) => void;
  onReactivate: (member: TeamMember) => void;
}

/** The company's people as a table; stacks into cards on a phone like every `.data-table`. */
export function TeamTable({ members, caller, busyId, onChangeRole, onDeactivate, onReactivate }: TeamTableProps) {
  const labels = {
    email: t('team.list.columns.email'),
    role: t('team.list.columns.role'),
    state: t('team.list.columns.state'),
    createdAt: t('team.list.columns.createdAt'),
    actions: t('team.list.columns.actions'),
  };
  return (
    <table className="data-table">
      <caption className="visually-hidden">{t('team.list.caption')}</caption>
      <thead>
        <tr>
          <th scope="col">{t('team.list.columns.name')}</th>
          <th scope="col">{labels.email}</th>
          <th scope="col">{labels.role}</th>
          <th scope="col">{labels.state}</th>
          <th scope="col">{labels.createdAt}</th>
          <th scope="col">{labels.actions}</th>
        </tr>
      </thead>
      <tbody>
        {members.map((member) => {
          const restriction = memberRestriction(caller, member);
          const target = t('team.list.actionTarget', { name: member.fullName });
          const busy = busyId === member.id;
          return (
            <tr key={member.id}>
              <th scope="row" className="data-table__primary">
                {restriction === 'self' ? t('team.list.you', { name: member.fullName }) : member.fullName}
              </th>
              <td data-label={labels.email} className="data-table__primary">
                {member.email}
              </td>
              <td data-label={labels.role}>{t(`roles.${member.role}`)}</td>
              <td data-label={labels.state}>
                <span className={`status status--${member.isActive ? 'active' : 'inactive'}`}>
                  <span className="status__symbol" aria-hidden="true">
                    {member.isActive ? '●' : '○'}
                  </span>
                  {t(member.isActive ? 'team.list.active' : 'team.list.inactive')}
                </span>
              </td>
              <td data-label={labels.createdAt}>
                <time dateTime={member.createdAt}>{formatDate(member.createdAt)}</time>
              </td>
              <td data-label={labels.actions}>
                {restriction ? (
                  <p className="data-table__note">
                    {t(restriction === 'self' ? 'team.list.selfLocked' : 'team.list.ownerLocked')}
                  </p>
                ) : (
                  <div className="actions">
                    <RowActionButton
                      id={memberActionId(member.id, 'role')}
                      label={t('team.list.changeRole')}
                      target={target}
                      busy={busy}
                      onClick={() => onChangeRole(member)}
                    />
                    <RowActionButton
                      id={memberActionId(member.id, 'state')}
                      label={t(member.isActive ? 'team.list.deactivate' : 'team.list.reactivate')}
                      target={target}
                      busy={busy}
                      onClick={() => (member.isActive ? onDeactivate(member) : onReactivate(member))}
                    />
                  </div>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
