import type { ApiClient } from './client';

/** Lifecycle of a document through ingest (`app/modules/documents/models.py`). */
export type DocumentStatus = 'uploaded' | 'processing' | 'processed' | 'failed';

/** A document of the caller's company, as `GET /api/v1/documents/` lists it. */
export interface DocumentSummary {
  id: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  status: DocumentStatus;
  /** ISO 8601 timestamp. */
  createdAt: string;
  /** Why ingest failed: the server's English text, only for troubleshooting. */
  errorMessage: string | null;
}

interface DocumentBody {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  status: string;
  created_at: string;
  error_message: string | null;
}

const STATUSES: readonly DocumentStatus[] = ['uploaded', 'processing', 'processed', 'failed'];

export const DOCX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

/** The type ingest expects for each extension it reads (`app/modules/documents/service.py`). */
const CONTENT_TYPES_BY_EXTENSION: Record<string, string> = {
  '.md': 'text/markdown',
  '.txt': 'text/plain',
  '.pdf': 'application/pdf',
  '.docx': DOCX_CONTENT_TYPE,
};

/** Lowercase extension with its dot (`".pdf"`), or `""`; a leading dot alone is not one. */
export function fileExtension(filename: string): string {
  const dot = filename.lastIndexOf('.');
  return dot > 0 ? filename.slice(dot).toLowerCase() : '';
}

function toDocument(body: DocumentBody): DocumentSummary {
  return {
    id: body.id,
    filename: body.filename,
    contentType: body.content_type,
    sizeBytes: body.size_bytes,
    // Anything unknown is shown as not processed yet.
    status: (STATUSES as readonly string[]).includes(body.status) ? (body.status as DocumentStatus) : 'uploaded',
    createdAt: body.created_at,
    errorMessage: body.error_message,
  };
}

/** `GET /api/v1/documents/` (any role): newest first. */
export async function listDocuments(api: ApiClient): Promise<DocumentSummary[]> {
  const body = await api.get<{ documents: DocumentBody[] }>('/api/v1/documents/');
  return body.documents.map(toDocument);
}

/**
 * `POST /api/v1/documents/` (operator and up) as multipart `file`. Browsers report Markdown
 * as `text/x-markdown`, `""`, or `text/markdown`; the file is sent with the type ingest reads.
 */
export async function uploadDocument(api: ApiClient, file: File): Promise<DocumentSummary> {
  const type = CONTENT_TYPES_BY_EXTENSION[fileExtension(file.name)] ?? file.type;
  const form = new FormData();
  form.append('file', type === file.type ? file : new File([file], file.name, { type }));
  return toDocument(await api.post<DocumentBody>('/api/v1/documents/', form));
}

/** `POST /api/v1/documents/{id}/ingest` (operator and up): synchronous extract, chunk, and embed. */
export async function ingestDocument(api: ApiClient, id: string): Promise<DocumentSummary> {
  return toDocument(await api.post<DocumentBody>(`/api/v1/documents/${encodeURIComponent(id)}/ingest`));
}
