import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../api/client';
import { listDocuments, type DocumentSummary } from '../api/documents';

interface DocumentListState {
  /** `null` until the first successful load; kept while reloading or after a failed reload. */
  documents: DocumentSummary[] | null;
  loading: boolean;
  /** The last load failed. */
  failed: boolean;
}

export interface DocumentList extends DocumentListState {
  reload(): void;
}

/** The company's documents, loaded on mount and again on `reload()`. */
export function useDocumentList(api: ApiClient): DocumentList {
  const [state, setState] = useState<DocumentListState>({ documents: null, loading: true, failed: false });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    listDocuments(api).then(
      (documents) => {
        if (active) {
          setState({ documents, loading: false, failed: false });
        }
      },
      () => {
        if (active) {
          setState((current) => ({ documents: current.documents, loading: false, failed: true }));
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api, attempt]);

  const reload = useCallback(() => {
    setState((current) => ({ ...current, loading: true }));
    setAttempt((current) => current + 1);
  }, []);

  return { ...state, reload };
}
