import { useCallback, useState } from 'react';

export interface Announcement {
  /** Changes on every call, so the same text twice is announced twice. */
  id: number;
  text: string;
}

/** State for an `Announcer`: `announce(text)` replaces what the polite live region says. */
export function useAnnouncer(): [Announcement, (text: string) => void] {
  const [announcement, setAnnouncement] = useState<Announcement>({ id: 0, text: '' });
  const announce = useCallback((text: string) => {
    setAnnouncement((current) => ({ id: current.id + 1, text }));
  }, []);
  return [announcement, announce];
}
