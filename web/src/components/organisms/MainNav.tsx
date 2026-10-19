import { useEffect, useRef, useState } from 'react';
import { NavLink } from 'react-router';
import { t } from '../../i18n';
import { paths } from '../../routes/paths';

export interface MainNavProps {
  /** Equipo and Herramientas; only owners and admins get those two links. */
  showAdminSections: boolean;
}

const MENU_ID = 'main-nav-menu';

/**
 * Section links. On narrow screens the list collapses behind a "Menú" toggle
 * (`aria-expanded`); Escape closes it and returns focus to the toggle.
 * The settings group is for everyone (each user configures their own tools);
 * only its admin entries (Equipo, Herramientas) honour `showAdminSections`.
 */
export function MainNav({ showAdminSections }: MainNavProps) {
  const [open, setOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false);
        toggleRef.current?.focus();
      }
    }
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [open]);

  const close = () => setOpen(false);

  return (
    <nav className="main-nav" aria-label={t('nav.label')}>
      <button
        ref={toggleRef}
        type="button"
        className="main-nav__toggle"
        aria-expanded={open}
        aria-controls={MENU_ID}
        onClick={() => setOpen((current) => !current)}
      >
        {t('nav.toggle')}
      </button>
      <ul id={MENU_ID} className="main-nav__menu" data-open={open || undefined}>
        <li>
          <NavLink className="main-nav__link" to={paths.chat} onClick={close}>
            {t('nav.chat')}
          </NavLink>
        </li>
        <li>
          <NavLink className="main-nav__link" to={paths.knowledge} onClick={close}>
            {t('nav.knowledge')}
          </NavLink>
        </li>
        <li className="main-nav__group">
          <span id="main-nav-settings" className="main-nav__group-label">
            {t('nav.settings')}
          </span>
          <ul className="main-nav__submenu" aria-labelledby="main-nav-settings">
            <li>
              <NavLink className="main-nav__link" to={paths.myTools} onClick={close}>
                {t('nav.myTools')}
              </NavLink>
            </li>
            {showAdminSections && (
              <>
                <li>
                  <NavLink className="main-nav__link" to={paths.team} onClick={close}>
                    {t('nav.team')}
                  </NavLink>
                </li>
                <li>
                  <NavLink className="main-nav__link" to={paths.tools} onClick={close}>
                    {t('nav.tools')}
                  </NavLink>
                </li>
              </>
            )}
          </ul>
        </li>
      </ul>
    </nav>
  );
}
