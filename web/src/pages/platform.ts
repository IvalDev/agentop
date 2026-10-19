/**
 * Which desktop the visitor is on, from the user agent.
 *
 * The installer page offers Linux packages only (T6 builds `.deb` and AppImage);
 * Windows and macOS get an honest "not yet". Android reports "Linux" in its user
 * agent, so it is excluded before the Linux check — a phone is not a desktop.
 */
export type Platform = 'linux' | 'windows' | 'macos' | 'other';

export function detectPlatform(userAgent: string): Platform {
  const ua = userAgent.toLowerCase();
  if (ua.includes('android')) {
    return 'other';
  }
  if (ua.includes('linux') || ua.includes('x11')) {
    return 'linux';
  }
  if (ua.includes('windows')) {
    return 'windows';
  }
  if (ua.includes('mac os') || ua.includes('macintosh')) {
    return 'macos';
  }
  return 'other';
}
