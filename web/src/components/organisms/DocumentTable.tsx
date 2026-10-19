import type { DocumentSummary } from '../../api/documents';
import { formatDate, t } from '../../i18n';
import { documentTypeLabel, failureSummary, formatFileSize } from '../../knowledge/documents';
import { DocumentStatusBadge } from '../molecules/DocumentStatusBadge';
import { RowActionButton } from '../molecules/RowActionButton';

export interface DocumentTableProps {
  documents: DocumentSummary[];
  /** Operator and up: failed documents offer Reintentar, unprocessed ones Procesar. */
  canIngest: boolean;
  /** Ids being ingested again; their button waits. */
  ingesting: ReadonlySet<string>;
  onIngest: (document: DocumentSummary) => void;
}

/**
 * The documents as a table with a caption and headers. On a phone each row stacks into a card;
 * `data-label` repeats the column name there (see `.data-table` in the styles).
 */
export function DocumentTable({ documents, canIngest, ingesting, onIngest }: DocumentTableProps) {
  const labels = {
    type: t('knowledge.list.columns.type'),
    size: t('knowledge.list.columns.size'),
    date: t('knowledge.list.columns.date'),
    status: t('knowledge.list.columns.status'),
    actions: t('knowledge.list.columns.actions'),
  };
  return (
    <table className="data-table">
      <caption className="visually-hidden">{t('knowledge.list.caption')}</caption>
      <thead>
        <tr>
          <th scope="col">{t('knowledge.list.columns.name')}</th>
          <th scope="col">{labels.type}</th>
          <th scope="col">{labels.size}</th>
          <th scope="col">{labels.date}</th>
          <th scope="col">{labels.status}</th>
          {canIngest && <th scope="col">{labels.actions}</th>}
        </tr>
      </thead>
      <tbody>
        {documents.map((document) => {
          const canRun = canIngest && (document.status === 'failed' || document.status === 'uploaded');
          return (
            <tr key={document.id}>
              <th scope="row" className="data-table__primary">
                {document.filename}
              </th>
              <td data-label={labels.type}>{documentTypeLabel(document)}</td>
              <td data-label={labels.size}>{formatFileSize(document.sizeBytes)}</td>
              <td data-label={labels.date}>
                <time dateTime={document.createdAt}>
                  {formatDate(document.createdAt, { dateStyle: 'medium', timeStyle: 'short' })}
                </time>
              </td>
              <td data-label={labels.status}>
                <DocumentStatusBadge status={document.status} />
                {document.status === 'failed' && (
                  <div className="failure">
                    <p>{failureSummary(document.errorMessage)}</p>
                    {document.errorMessage && (
                      <details className="failure__detail">
                        <summary>{t('knowledge.list.technicalDetail')}</summary>
                        {/* The server's own text, verbatim, for troubleshooting. */}
                        <p lang="en" className="failure__text">
                          {document.errorMessage}
                        </p>
                      </details>
                    )}
                  </div>
                )}
              </td>
              {canIngest && (
                <td data-label={labels.actions}>
                  {canRun && (
                    <RowActionButton
                      label={t(document.status === 'failed' ? 'knowledge.list.retry' : 'knowledge.list.process')}
                      target={t('knowledge.list.actionTarget', { name: document.filename })}
                      busy={ingesting.has(document.id)}
                      onClick={() => onIngest(document)}
                    />
                  )}
                </td>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
