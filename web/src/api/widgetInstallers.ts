import type { ApiClient } from './client';

/**
 * The desktop-widget installers API (`app/modules/widget/router.py`): the bundles
 * `make widget-package` built and `make widget-publish` copied to the server.
 * Every field is untrusted text; the page renders it as text, never as HTML.
 */

export type InstallerPackageType = 'deb' | 'appimage';

/** One published bundle (`WidgetInstallerResponse`). */
export interface WidgetInstaller {
  filename: string;
  packageType: InstallerPackageType;
  version: string;
  arch: string;
  sizeBytes: number;
  /** Same-origin API path; the download goes through the authenticated client. */
  downloadUrl: string;
}

interface InstallerBody {
  filename: string;
  package_type: InstallerPackageType;
  version: string;
  arch: string;
  size_bytes: number;
  download_url: string;
}

function toInstaller(body: InstallerBody): WidgetInstaller {
  return {
    filename: body.filename,
    packageType: body.package_type,
    version: body.version,
    arch: body.arch,
    sizeBytes: body.size_bytes,
    downloadUrl: body.download_url,
  };
}

/** `GET /api/v1/widget/installers`: the published bundles, ordered by file name. */
export async function listWidgetInstallers(api: ApiClient): Promise<WidgetInstaller[]> {
  const body = await api.get<{ installers: InstallerBody[] }>('/api/v1/widget/installers');
  return body.installers.map(toInstaller);
}

/**
 * Download one bundle through the API (the request carries the session token, so a
 * plain `<a href>` would be rejected) and hand the bytes to the browser's saver.
 */
export async function downloadWidgetInstaller(
  api: ApiClient,
  installer: WidgetInstaller,
): Promise<void> {
  const response = await api.send(installer.downloadUrl);
  const blob = await response.blob();
  saveBlob(blob, installer.filename);
}

/** Save `blob` under `filename` without leaving the page. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
