import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AXE_TEST_TIMEOUT_MS, expectNoAxeViolations } from '../test/axe';
import { installFakeApi, routesCalled, signedInRoutes, type FakeReply, type FakeRoutes } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';
import { detectPlatform } from './platform';

const LIST = 'GET /api/v1/widget/installers';
const ROUTE = '/instalar-widget';
const LINUX_UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36';
const WINDOWS_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36';
const MAC_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36';

interface InstallerBody {
  filename: string;
  package_type: 'deb' | 'appimage';
  version: string;
  arch: string;
  size_bytes: number;
  download_url: string;
}

function installer(fields: Partial<InstallerBody> & Pick<InstallerBody, 'filename' | 'package_type'>): InstallerBody {
  return {
    version: '0.1.0',
    arch: 'amd64',
    size_bytes: 5_870_000,
    download_url: `/api/v1/widget/installers/${fields.filename}`,
    ...fields,
  };
}

function installers(): InstallerBody[] {
  return [
    installer({ filename: 'Asistente_0.1.0_amd64.deb', package_type: 'deb', size_bytes: 6_159_974 }),
    installer({ filename: 'Asistente_0.1.0_amd64.AppImage', package_type: 'appimage', size_bytes: 91_610_213 }),
  ];
}

function listReply(items: InstallerBody[]): FakeReply {
  return { status: 200, body: { installers: items } };
}

function stubUserAgent(userAgent: string) {
  return vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(userAgent);
}

async function openInstallPage(routes: FakeRoutes = {}, userAgent = LINUX_UA) {
  stubUserAgent(userAgent);
  const fetchMock = installFakeApi({
    ...signedInRoutes('viewer'),
    [LIST]: listReply(installers()),
    ...routes,
  });
  const view = renderApp({ route: ROUTE, storedToken: 'viewer-token' });
  return { ...view, fetchMock };
}

describe('detectPlatform', () => {
  it('recognises the three platforms the page talks about', () => {
    expect(detectPlatform(LINUX_UA)).toBe('linux');
    expect(detectPlatform(WINDOWS_UA)).toBe('windows');
    expect(detectPlatform(MAC_UA)).toBe('macos');
    expect(detectPlatform('')).toBe('other');
  });

  it('does not mistake Android for Linux', () => {
    expect(detectPlatform('Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari/537.36')).toBe(
      'other',
    );
  });
});

describe('Install widget page', () => {
  it('is reachable from the navigation for every role', async () => {
    const { user } = await openInstallPage();
    const nav = await screen.findByRole('navigation', { name: 'Principal' });

    await user.click(within(nav).getByRole('link', { name: 'Instalar widget' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Instalar el widget' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent(ROUTE);
    expect(document.title).toBe('Instalar el widget · Asistente de trabajo');
  });

  it('offers both Linux packages with their version and size', async () => {
    await openInstallPage();

    const table = await screen.findByRole('table', { name: 'Instaladores disponibles para descargar' });
    const deb = within(table).getByRole('row', { name: /Asistente_0\.1\.0_amd64\.deb/ });
    const appimage = within(table).getByRole('row', { name: /Asistente_0\.1\.0_amd64\.AppImage/ });

    expect(within(deb).getByText('0.1.0')).toBeInTheDocument();
    expect(within(deb).getByText('5,87 MB')).toBeInTheDocument();
    expect(within(deb).getByRole('button', { name: /Descargar.*\.deb/ })).toBeInTheDocument();
    expect(within(appimage).getByRole('button', { name: /Descargar.*AppImage/ })).toBeInTheDocument();
  });

  it('downloads the chosen package through the authenticated API', async () => {
    const { user, fetchMock } = await openInstallPage({
      'GET /api/v1/widget/installers/Asistente_0.1.0_amd64.deb': {
        status: 200,
        body: { detail: 'not json' },
        headers: { 'content-type': 'application/vnd.debian.binary-package' },
      },
    });
    const createObjectURL = vi.fn(() => 'blob:installer');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    const table = await screen.findByRole('table', { name: 'Instaladores disponibles para descargar' });
    await user.click(
      within(table).getByRole('button', { name: /Descargar.*Asistente_0\.1\.0_amd64\.deb/ }),
    );

    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(routesCalled(fetchMock)).toContain(
      'GET /api/v1/widget/installers/Asistente_0.1.0_amd64.deb',
    );
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:installer');
  });

  it('tells Windows and macOS users the widget is not available yet', async () => {
    await openInstallPage({}, WINDOWS_UA);

    expect(
      await screen.findByText('Tu sistema todavía no tiene instalador'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Instaladores disponibles para descargar' })).not.toBeInTheDocument();
  });

  it('says so when nothing has been published', async () => {
    await openInstallPage({ [LIST]: listReply([]) });

    expect(await screen.findByText('Todavía no hay instaladores publicados.')).toBeInTheDocument();
  });

  it('shows an error with a retry when the list cannot be read', async () => {
    const { user, fetchMock } = await openInstallPage({
      [LIST]: { status: 500, body: { detail: 'boom' } },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'No se ha podido cargar la lista de instaladores.',
    );
    await user.click(screen.getByRole('button', { name: 'Reintentar' }));

    await waitFor(() =>
      expect(routesCalled(fetchMock).filter((call) => call === LIST)).toHaveLength(2),
    );
  });

  it('has no accessibility violations', async () => {
    await openInstallPage();
    await screen.findByRole('table', { name: 'Instaladores disponibles para descargar' });

    await expectNoAxeViolations();
  }, AXE_TEST_TIMEOUT_MS);
});
