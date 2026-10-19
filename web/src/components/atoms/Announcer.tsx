import type { Announcement } from '../../hooks/useAnnouncer';

/** A visually hidden polite status: screen readers hear each announcement once. */
export function Announcer({ announcement }: { announcement: Announcement }) {
  return (
    <div role="status" className="visually-hidden">
      {/* A new node per announcement, so repeating the same text is still read. */}
      <p key={announcement.id}>{announcement.text}</p>
    </div>
  );
}
