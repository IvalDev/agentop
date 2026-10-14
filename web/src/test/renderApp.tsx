import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { App } from '../App';
import { TOKEN_STORAGE_KEY } from '../session/tokenStorage';

/** Renders the current pathname (hidden, outside the a11y tree) so tests can assert redirects. */
export function LocationProbe() {
  const { pathname } = useLocation();
  return (
    <output data-testid="location" hidden>
      {pathname}
    </output>
  );
}

export interface RenderAppOptions {
  /** Initial URL. */
  route?: string;
  /** History state of the initial URL, e.g. `{ from }` as set by the auth guard. */
  state?: unknown;
  /** Token already stored in this tab, as after a reload. */
  storedToken?: string;
}

/** Render the real app inside a memory router, optionally with a stored session. */
export function renderApp({ route = '/', state, storedToken }: RenderAppOptions = {}) {
  if (storedToken !== undefined) {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, storedToken);
  }
  const user = userEvent.setup();
  const view = render(
    <MemoryRouter initialEntries={[state === undefined ? route : { pathname: route, state }]}>
      <App />
      <LocationProbe />
    </MemoryRouter>,
  );
  return { user, ...view };
}
