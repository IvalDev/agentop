import { useState } from 'react';
import { ingestDocument, type DocumentSummary } from '../api/documents';
import { Alert } from '../components/atoms/Alert';
import { Announcer } from '../components/atoms/Announcer';
import { Button } from '../components/atoms/Button';
import { UploadProgressList } from '../components/molecules/UploadProgressList';
import { DocumentTable } from '../components/organisms/DocumentTable';
import { DocumentUploadForm } from '../components/organisms/DocumentUploadForm';
import { useAnnouncer } from '../hooks/useAnnouncer';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';
import {
  ingestErrorKind,
  uploadErrorMessage,
  uploadStateLabel,
  type UploadItem,
  type UploadState,
} from '../knowledge/documents';
import { useDocumentList } from '../knowledge/useDocumentList';
import { useUploadQueue } from '../knowledge/useUploadQueue';
import { canUploadDocuments } from '../session/roles';
import { useCurrentUser, useSession } from '../session/SessionProvider';

const UPLOAD_TITLE_ID = 'knowledge-upload-title';
const LIST_TITLE_ID = 'knowledge-list-title';

function countIn(items: UploadItem[], state: UploadState): number {
  return items.filter((item) => item.state === state).length;
}

/**
 * Knowledge base section. Every role sees the documents; operator and up also upload (each file
 * is uploaded and then ingested, one after another) and ingest failed documents again.
 */
export function KnowledgePage() {
  const title = t('sections.knowledge.title');
  usePageTitle(title);
  const { api } = useSession();
  const canEdit = canUploadDocuments(useCurrentUser().role);
  const list = useDocumentList(api);
  const { reload } = list;
  const [announcement, announce] = useAnnouncer();
  const [ingesting, setIngesting] = useState<ReadonlySet<string>>(new Set());
  const [ingestError, setIngestError] = useState<string | null>(null);

  const uploads = useUploadQueue(api, {
    onProgress: (item) => announce(t('knowledge.upload.progress', { name: item.name, state: uploadStateLabel(item) })),
    onFinished: (items) => {
      announce(t('knowledge.upload.finished', { done: countIn(items, 'done'), failed: countIn(items, 'failed') }));
      reload();
    },
  });

  async function ingestAgain(document: DocumentSummary) {
    setIngesting((current) => new Set(current).add(document.id));
    setIngestError(null);
    announce(t('knowledge.ingest.started', { name: document.filename }));
    try {
      await ingestDocument(api, document.id);
      announce(t('knowledge.ingest.done', { name: document.filename }));
    } catch (error) {
      const reason = uploadErrorMessage(ingestErrorKind(error));
      setIngestError(t('knowledge.ingest.failed', { name: document.filename, reason }));
    } finally {
      setIngesting((current) => {
        const next = new Set(current);
        next.delete(document.id);
        return next;
      });
      reload();
    }
  }

  let body = null;
  if (list.documents === null) {
    body = list.loading && <p className="page__note">{t('knowledge.list.loading')}</p>;
  } else if (list.documents.length === 0) {
    body = <p className="page__note">{t('knowledge.list.empty')}</p>;
  } else {
    body = (
      <DocumentTable
        documents={list.documents}
        canIngest={canEdit}
        ingesting={ingesting}
        onIngest={(document) => void ingestAgain(document)}
      />
    );
  }

  return (
    <div className="page knowledge">
      <h1 className="page__title">{title}</h1>
      <p className="page__lead">
        {t(canEdit ? 'sections.knowledge.description' : 'sections.knowledge.descriptionReadOnly')}
      </p>
      {canEdit && (
        <section className="section" aria-labelledby={UPLOAD_TITLE_ID}>
          <h2 id={UPLOAD_TITLE_ID} className="section__title">
            {t('knowledge.upload.title')}
          </h2>
          <DocumentUploadForm onFiles={uploads.enqueue} />
          {uploads.items.length > 0 && <UploadProgressList items={uploads.items} />}
        </section>
      )}
      <section className="section" aria-labelledby={LIST_TITLE_ID}>
        <div className="section__header">
          <h2 id={LIST_TITLE_ID} className="section__title">
            {t('knowledge.list.title')}
          </h2>
          <Button
            variant="secondary"
            aria-disabled={list.loading || undefined}
            onClick={list.loading ? undefined : reload}
          >
            {t('knowledge.list.refresh')}
          </Button>
        </div>
        {ingestError && <Alert tone="error">{ingestError}</Alert>}
        {list.failed && <Alert tone="error">{t('knowledge.list.loadError')}</Alert>}
        {body}
      </section>
      <Announcer announcement={announcement} />
    </div>
  );
}
