import type { ReactNode } from 'react';
import { t } from '../../i18n';
import type { Role } from '../../session/roles';
import { Button } from '../atoms/Button';

export interface AppHeaderProps {
  userName: string;
  role: Role;
  onLogout: () => void;
  navigation: ReactNode;
}

export function AppHeader({ userName, role, onLogout, navigation }: AppHeaderProps) {
  return (
    <header className="app-header">
      <div className="app-header__bar">
        <p className="app-header__brand">{t('app.name')}</p>
        <div className="app-header__user">
          <p className="app-header__identity">
            <span className="visually-hidden">{t('header.signedInAs')} </span>
            <span className="app-header__name">{userName}</span>{' '}
            <span className="app-header__role">{t(`roles.${role}`)}</span>
          </p>
          <Button variant="secondary" onClick={onLogout}>
            {t('header.logout')}
          </Button>
        </div>
      </div>
      {navigation}
    </header>
  );
}
