import { ChatProvider } from '../chat/ChatProvider';
import { Button } from '../components/atoms/Button';
import { DashboardLayout } from '../components/templates/DashboardLayout';
import { t } from '../i18n';
import { useSession } from '../session/SessionProvider';
import { ChatPage } from './ChatPage';

/**
 * Chat-only shell loaded by the desktop widget: the product name and a way out of the
 * session, then the same `ChatPage` the dashboard mounts, and none of the console chrome.
 */
export function WidgetPage() {
  const { logout } = useSession();
  return (
    <DashboardLayout
      header={
        <header className="widget-header">
          <div className="widget-header__bar">
            <p className="widget-header__brand">{t('app.name')}</p>
            <Button variant="secondary" onClick={logout}>
              {t('header.logout')}
            </Button>
          </div>
        </header>
      }
    >
      <ChatProvider>
        <ChatPage />
      </ChatProvider>
    </DashboardLayout>
  );
}
