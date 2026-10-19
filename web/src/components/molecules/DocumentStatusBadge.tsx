import type { DocumentStatus } from '../../api/documents';
import { t } from '../../i18n';

/** A symbol per status, so the state never depends on colour alone. Decorative: hidden from AT. */
const SYMBOLS: Record<DocumentStatus, string> = {
  uploaded: '○',
  processing: '◔',
  processed: '✓',
  failed: '✕',
};

export function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <span className={`status status--${status}`}>
      <span className="status__symbol" aria-hidden="true">
        {SYMBOLS[status]}
      </span>
      {t(`knowledge.status.${status}`)}
    </span>
  );
}
