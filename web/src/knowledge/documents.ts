import { ApiError } from '../api/client';
import { DOCX_CONTENT_TYPE, fileExtension, type DocumentSummary } from '../api/documents';
import { formatNumber, t, type MessageKey } from '../i18n';

/** What the upload accepts: the formats ingest can read. */
export const ACCEPTED_EXTENSIONS: readonly string[] = ['.md', '.txt', '.pdf', '.docx'];

/** The API's default `max_upload_bytes`; the server still enforces its own limit. */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

/** Why a file could not be added; each maps to a catalog message, never to the server's text. */
export type UploadErrorKind =
  | 'tooLarge'
  | 'unsupportedType'
  | 'invalidName'
  | 'unreadable'
  | 'processingUnavailable'
  | 'forbidden'
  | 'notFound'
  | 'network'
  | 'unexpected';

export type UploadState = 'pending' | 'uploading' | 'processing' | 'done' | 'failed';

/** One file of an upload batch and how far it got. */
export interface UploadItem {
  id: number;
  name: string;
  state: UploadState;
  error: UploadErrorKind | null;
}

/** Checks made before sending anything: format by extension, then size. */
export function checkFile(file: File): UploadErrorKind | null {
  if (!ACCEPTED_EXTENSIONS.includes(fileExtension(file.name))) {
    return 'unsupportedType';
  }
  return file.size > MAX_UPLOAD_BYTES ? 'tooLarge' : null;
}

function commonErrorKind(error: unknown): UploadErrorKind {
  if (!(error instanceof ApiError)) {
    return 'unexpected';
  }
  if (error.status === 0 || error.status === 503 || error.status === 504) {
    return 'network';
  }
  return error.status === 403 ? 'forbidden' : 'unexpected';
}

/** `POST /api/v1/documents/`: 413 over the limit, 422 for a name the server refuses. */
export function uploadErrorKind(error: unknown): UploadErrorKind {
  if (error instanceof ApiError) {
    if (error.status === 413) {
      return 'tooLarge';
    }
    if (error.status === 422) {
      return 'invalidName';
    }
    if (error.status === 502) {
      return 'network';
    }
  }
  return commonErrorKind(error);
}

/** `POST /api/v1/documents/{id}/ingest`: 422 unreadable, 502 embedding backend unavailable. */
export function ingestErrorKind(error: unknown): UploadErrorKind {
  if (error instanceof ApiError) {
    if (error.status === 422) {
      return 'unreadable';
    }
    if (error.status === 502) {
      return 'processingUnavailable';
    }
    if (error.status === 404) {
      return 'notFound';
    }
  }
  return commonErrorKind(error);
}

/** The reason, lowercase, to follow "Error:" or a colon. */
export function uploadErrorMessage(kind: UploadErrorKind): string {
  return t(`knowledge.errors.${kind}`);
}

/** The progress label of one file: "Subiendo…", "Procesando…", "Listo", or "Error: …". */
export function uploadStateLabel(item: UploadItem): string {
  if (item.state === 'failed') {
    return t('knowledge.upload.state.failed', { reason: uploadErrorMessage(item.error ?? 'unexpected') });
  }
  return t(`knowledge.upload.state.${item.state}`);
}

const SIZE_UNITS = [
  ['gigabyte', 1024 ** 3],
  ['megabyte', 1024 ** 2],
  ['kilobyte', 1024],
] as const;

/** A size in the locale's format, in binary units: "512 B", "12,5 kB", "1,5 MB". */
export function formatFileSize(bytes: number): string {
  const [unit, size] = SIZE_UNITS.find(([, unitSize]) => bytes >= unitSize) ?? ['byte', 1];
  return formatNumber(bytes / size, {
    style: 'unit',
    unit,
    unitDisplay: 'short',
    maximumFractionDigits: unit === 'byte' ? 0 : 1,
  });
}

const TYPE_LABELS: Record<string, MessageKey> = {
  'text/markdown': 'knowledge.types.markdown',
  'text/plain': 'knowledge.types.text',
  'application/pdf': 'knowledge.types.pdf',
  [DOCX_CONTENT_TYPE]: 'knowledge.types.word',
};

/** A readable type: known formats by name, others by their extension ("ODT"). */
export function documentTypeLabel(document: DocumentSummary): string {
  const mime = (document.contentType.split(';')[0] ?? '').trim().toLowerCase();
  const known = TYPE_LABELS[mime];
  if (known) {
    return t(known);
  }
  const extension = fileExtension(document.filename).slice(1);
  return extension ? extension.toUpperCase() : t('knowledge.types.other');
}

/** A Spanish summary of why ingest failed; the server's text stays in "Detalle técnico". */
export function failureSummary(errorMessage: string | null): string {
  const message = (errorMessage ?? '').toLowerCase();
  if (message.startsWith('unsupported content type')) {
    return t('knowledge.failure.unsupported');
  }
  if (message.includes('embedding')) {
    return t('knowledge.failure.embeddings');
  }
  return t('knowledge.failure.generic');
}
