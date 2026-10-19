import { useRef, useState, type DragEvent, type FormEvent } from 'react';
import { t } from '../../i18n';
import { ACCEPTED_EXTENSIONS } from '../../knowledge/documents';
import { Button } from '../atoms/Button';
import { TextField } from '../molecules/TextField';

export interface DocumentUploadFormProps {
  /** Receives the chosen or dropped files; the page checks, uploads, and ingests them. */
  onFiles: (files: File[]) => void;
}

/**
 * A labelled multi-file input and its submit button. Dropping files on the form sends them at
 * once, as an enhancement for pointer users; the input works on its own with a keyboard.
 */
export function DocumentUploadForm({ onFiles }: DocumentUploadFormProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (files.length === 0) {
      setError(t('knowledge.upload.filesRequired'));
      inputRef.current?.focus();
      return;
    }
    onFiles(files);
    setFiles([]);
    if (inputRef.current) {
      inputRef.current.value = '';
    }
  }

  function handleDragOver(event: DragEvent<HTMLFormElement>) {
    // Allows the drop; without it the browser opens the file instead.
    event.preventDefault();
    setDragging(true);
  }

  function handleDrop(event: DragEvent<HTMLFormElement>) {
    event.preventDefault();
    setDragging(false);
    const dropped = Array.from(event.dataTransfer.files);
    if (dropped.length > 0) {
      setError(null);
      onFiles(dropped);
    }
  }

  return (
    // Dropping duplicates the labelled file input, which keyboards and screen readers use.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- drop is an enhancement only
    <form
      className="upload-form dropzone"
      noValidate
      data-active={dragging || undefined}
      onSubmit={handleSubmit}
      onDragOver={handleDragOver}
      onDragLeave={(event) => {
        // Moving between the form's own children is not leaving it.
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setDragging(false);
        }
      }}
      onDrop={handleDrop}
    >
      <TextField
        ref={inputRef}
        id="document-files"
        type="file"
        multiple
        accept={ACCEPTED_EXTENSIONS.join(',')}
        label={t('knowledge.upload.filesLabel')}
        hint={t('knowledge.upload.filesHint')}
        error={error}
        onChange={(event) => {
          setFiles(Array.from(event.target.files ?? []));
          setError(null);
        }}
      />
      <div className="actions">
        <Button type="submit">{t('knowledge.upload.submit')}</Button>
      </div>
    </form>
  );
}
