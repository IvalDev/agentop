import { useCallback, useEffect, useRef, useState } from 'react';
import type { ApiClient } from '../api/client';
import { ingestDocument, uploadDocument } from '../api/documents';
import { checkFile, ingestErrorKind, uploadErrorKind, type UploadItem } from './documents';

export interface UploadQueueCallbacks {
  /** A file changed state (uploading, processing, done, or failed). */
  onProgress(item: UploadItem): void;
  /** Every queued file has finished, well or not. */
  onFinished(items: UploadItem[]): void;
}

interface Job {
  id: number;
  file: File;
}

/**
 * Uploads files one after another, ingesting each right after its upload. Files added while a
 * batch runs join it; a new batch replaces the finished one. Leaving the page lets the current
 * file finish and starts no other.
 */
export function useUploadQueue(api: ApiClient, callbacks: UploadQueueCallbacks) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const batchRef = useRef<UploadItem[]>([]);
  const queueRef = useRef<Job[]>([]);
  const runningRef = useRef(false);
  const mountedRef = useRef(false);
  const nextIdRef = useRef(1);
  const callbacksRef = useRef(callbacks);

  useEffect(() => {
    callbacksRef.current = callbacks;
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const update = useCallback((item: UploadItem) => {
    batchRef.current = batchRef.current.map((current) => (current.id === item.id ? item : current));
    setItems(batchRef.current);
    callbacksRef.current.onProgress(item);
  }, []);

  const process = useCallback(
    async ({ id, file }: Job) => {
      const item = { id, name: file.name, error: null };
      update({ ...item, state: 'uploading' });
      let documentId: string;
      try {
        documentId = (await uploadDocument(api, file)).id;
      } catch (error) {
        update({ ...item, state: 'failed', error: uploadErrorKind(error) });
        return;
      }
      update({ ...item, state: 'processing' });
      try {
        await ingestDocument(api, documentId);
        update({ ...item, state: 'done' });
      } catch (error) {
        update({ ...item, state: 'failed', error: ingestErrorKind(error) });
      }
    },
    [api, update],
  );

  const run = useCallback(async () => {
    runningRef.current = true;
    for (let job = queueRef.current.shift(); job && mountedRef.current; job = queueRef.current.shift()) {
      await process(job);
    }
    queueRef.current = [];
    runningRef.current = false;
    if (mountedRef.current) {
      callbacksRef.current.onFinished(batchRef.current);
    }
  }, [process]);

  const enqueue = useCallback(
    (files: File[]) => {
      if (files.length === 0) {
        return;
      }
      const jobs = files.map((file) => ({ id: nextIdRef.current++, file, error: checkFile(file) }));
      const added = jobs.map(
        ({ id, file, error }): UploadItem => ({ id, name: file.name, state: error ? 'failed' : 'pending', error }),
      );
      batchRef.current = runningRef.current ? [...batchRef.current, ...added] : added;
      setItems(batchRef.current);
      queueRef.current.push(...jobs.filter((job) => job.error === null));
      if (!runningRef.current) {
        void run();
      }
    },
    [run],
  );

  return { items, enqueue };
}
