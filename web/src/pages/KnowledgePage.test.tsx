import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Role } from '../session/roles';
import { expectNoAxeViolations } from '../test/axe';
import { createGate, installFakeApi, routesCalled, signedInRoutes, type FakeRoutes } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

const ME = 'GET /api/v1/auth/me';
const LIST = 'GET /api/v1/documents/';
const UPLOAD = 'POST /api/v1/documents/';
const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const TABLE_NAME = 'Documentos de la base de conocimiento';
const EMBEDDING_ERROR = 'ollama embeddings request failed: model "nomic-embed-text" not found';
const UNAVAILABLE =
  'el servicio que procesa los documentos no está disponible. Inténtalo de nuevo más tarde.';

function ingest(id: string): string {
  return `POST /api/v1/documents/${id}/ingest`;
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

function doc(fields: Partial<DocumentBody> & Pick<DocumentBody, 'id' | 'filename'>): DocumentBody {
  return {
    content_type: 'text/plain',
    size_bytes: 100,
    status: 'processed',
    created_at: '2026-10-14T12:00:00Z',
    error_message: null,
    ...fields,
  };
}

const FAILED_NAME = 'Contrato <b>marco</b>.docx';

const LISTED: DocumentBody[] = [
  doc({ id: 'd1', filename: 'Política de vacaciones.pdf', content_type: 'application/pdf', size_bytes: 1_572_864 }),
  doc({
    id: 'd2',
    filename: 'notas.md',
    content_type: 'text/markdown',
    size_bytes: 512,
    status: 'uploaded',
    created_at: '2026-10-13T12:00:00Z',
  }),
  doc({
    id: 'd3',
    filename: FAILED_NAME,
    content_type: DOCX,
    size_bytes: 12_800,
    status: 'failed',
    created_at: '2026-10-12T12:00:00Z',
    error_message: EMBEDDING_ERROR,
  }),
  doc({
    id: 'd4',
    filename: 'acta.odt',
    content_type: 'application/vnd.oasis.opendocument.text',
    size_bytes: 2048,
    status: 'failed',
    created_at: '2026-10-11T12:00:00Z',
    error_message: 'unsupported content type: application/vnd.oasis.opendocument.text',
  }),
];

function listReply(documents: DocumentBody[]) {
  return { status: 200, body: { documents } };
}

async function openKnowledge(role: Role, routes: FakeRoutes) {
  const fetchMock = installFakeApi({ ...signedInRoutes(role), ...routes });
  const view = renderApp({ route: '/conocimiento', storedToken: `${role}-token` });
  await screen.findByRole('heading', { level: 1, name: 'Base de conocimiento' });
  return { ...view, fetchMock };
}

function rowOf(filename: string): HTMLElement {
  return screen.getByRole('rowheader', { name: filename }).closest('tr')!;
}

/** Type, size, date, status, and (operator and up) actions. */
function cellsOf(filename: string): HTMLElement[] {
  return within(rowOf(filename)).getAllByRole('cell');
}

function fileInput(): HTMLElement {
  return screen.getByLabelText('Archivos');
}

function textFile(name: string, type = 'text/plain', content = 'Texto de prueba'): File {
  return new File([content], name, { type });
}

function progressOf(name: string): HTMLElement {
  const list = screen.getByRole('list', { name: 'Progreso de la subida' });
  return within(list).getByText(name).closest('li')!;
}

function announcer(): HTMLElement {
  return screen.getByRole('status');
}

async function releaseNext(gate: ReturnType<typeof createGate>) {
  await waitFor(() => expect(gate.pending).toBe(1));
  gate.release();
}

describe('KnowledgePage: the document list', () => {
  it('lists every document with its type, size, date, and status', async () => {
    await openKnowledge('viewer', { [LIST]: listReply(LISTED) });

    const table = await screen.findByRole('table', { name: TABLE_NAME });
    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Nombre',
      'Tipo',
      'Tamaño',
      'Fecha',
      'Estado',
    ]);

    const [pdfType, pdfSize, pdfDate, pdfStatus] = cellsOf('Política de vacaciones.pdf');
    expect(pdfType).toHaveTextContent(/^PDF$/);
    expect(pdfSize).toHaveTextContent(/^1,5 MB$/);
    expect(pdfDate).toHaveTextContent(/^14 oct 2026, \d{2}:\d{2}$/);
    expect(within(pdfDate!).getByText(/14 oct 2026/)).toHaveAttribute('datetime', '2026-10-14T12:00:00Z');
    expect(pdfStatus).toHaveTextContent(/^✓ ?Procesado$/);

    const [mdType, mdSize, mdDate, mdStatus] = cellsOf('notas.md');
    expect(mdType).toHaveTextContent(/^Markdown$/);
    expect(mdSize).toHaveTextContent(/^512 B$/);
    expect(mdDate).toHaveTextContent(/^13 oct 2026/);
    expect(mdStatus).toHaveTextContent(/Sin procesar$/);

    // A failed document: a Spanish summary, the server's text only behind "Detalle técnico".
    const [docxType, docxSize, , docxStatus] = cellsOf(FAILED_NAME);
    expect(docxType).toHaveTextContent(/^Word$/);
    expect(docxSize).toHaveTextContent(/^12,5 kB$/);
    expect(docxStatus).toHaveTextContent(/^✕ ?Error/);
    expect(docxStatus).toHaveTextContent(
      'El servicio que prepara los documentos para las búsquedas no estaba disponible.',
    );
    const detail = within(docxStatus!).getByText('Detalle técnico').closest('details')!;
    expect(detail).not.toHaveAttribute('open');
    expect(within(detail).getByText(EMBEDDING_ERROR)).not.toBeVisible();
    expect(within(detail).getByText(EMBEDDING_ERROR)).toHaveAttribute('lang', 'en');

    const [odtType, , , odtStatus] = cellsOf('acta.odt');
    expect(odtType).toHaveTextContent(/^ODT$/);
    expect(odtStatus).toHaveTextContent('El formato del archivo no está admitido.');

    // File names are text, never markup.
    expect(document.querySelector('main b')).toBeNull();
    await expectNoAxeViolations();
  });

  it('shows a viewer no upload area and no actions', async () => {
    await openKnowledge('viewer', { [LIST]: listReply(LISTED) });
    await screen.findByRole('table', { name: TABLE_NAME });

    expect(screen.queryByRole('heading', { name: 'Añadir documentos' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Archivos')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Reintentar|Procesar|Subir/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Acciones' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Actualizar' })).toBeInTheDocument();
  });

  it.each<Role>(['owner', 'admin', 'operator'])('offers a %s the upload area and the actions', async (role) => {
    await openKnowledge(role, { [LIST]: listReply(LISTED) });
    await screen.findByRole('table', { name: TABLE_NAME });

    expect(screen.getByRole('heading', { level: 2, name: 'Añadir documentos' })).toBeInTheDocument();
    expect(fileInput()).toHaveAttribute('type', 'file');
    expect(fileInput()).toHaveAttribute('multiple');
    expect(fileInput()).toHaveAttribute('accept', '.md,.txt,.pdf,.docx');
    expect(fileInput()).toHaveAccessibleDescription(/\.md, \.txt, \.pdf y \.docx.*20 MB/);
    expect(screen.getByRole('columnheader', { name: 'Acciones' })).toBeInTheDocument();
    expect(
      within(rowOf(FAILED_NAME)).getByRole('button', { name: `Reintentar «${FAILED_NAME}»` }),
    ).toBeInTheDocument();
    expect(within(rowOf('notas.md')).getByRole('button', { name: 'Procesar «notas.md»' })).toBeInTheDocument();
    expect(within(rowOf('Política de vacaciones.pdf')).queryByRole('button')).not.toBeInTheDocument();
  });

  it('shows an empty state', async () => {
    await openKnowledge('viewer', { [LIST]: listReply([]) });

    expect(await screen.findByText('Todavía no hay documentos en la base de conocimiento.')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('reports a failed load and recovers with Actualizar', async () => {
    let fail = true;
    const { user } = await openKnowledge('viewer', {
      [LIST]: () => (fail ? { status: 500, body: { detail: 'internal error' } } : listReply(LISTED)),
    });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('No se han podido cargar los documentos.');
    expect(alert).not.toHaveTextContent('internal error');
    expect(screen.queryByText('Cargando documentos…')).not.toBeInTheDocument();

    fail = false;
    await user.click(screen.getByRole('button', { name: 'Actualizar' }));

    expect(await screen.findByRole('table', { name: TABLE_NAME })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('KnowledgePage: uploads', () => {
  it('uploads several files one after another, ingesting each, with progress', async () => {
    const gate = createGate();
    const stored: DocumentBody[] = [];
    const uploaded: File[] = [];
    const processed = (id: string, filename: string) => () =>
      gate.hold(() => {
        const body = doc({ id, filename });
        stored.unshift(body);
        return { status: 200, body };
      });
    const { user, fetchMock } = await openKnowledge('operator', {
      [LIST]: () => listReply(stored),
      [UPLOAD]: (init) =>
        gate.hold(() => {
          const file = (init.body as FormData).get('file') as File;
          uploaded.push(file);
          return { status: 201, body: doc({ id: `new-${uploaded.length}`, filename: file.name, status: 'uploaded' }) };
        }),
      [ingest('new-1')]: processed('new-1', 'politica.md'),
      [ingest('new-2')]: processed('new-2', 'manual.pdf'),
    });
    expect(await screen.findByText('Todavía no hay documentos en la base de conocimiento.')).toBeInTheDocument();

    // No type, as some browsers report Markdown: the upload names the right one.
    await user.upload(fileInput(), [textFile('politica.md', ''), textFile('manual.pdf', 'application/pdf')]);
    await user.click(screen.getByRole('button', { name: 'Subir y procesar' }));

    await screen.findByRole('list', { name: 'Progreso de la subida' });
    expect(progressOf('politica.md')).toHaveTextContent('Subiendo…');
    expect(progressOf('manual.pdf')).toHaveTextContent('En espera');
    expect(announcer()).toHaveTextContent('politica.md: Subiendo…');

    await releaseNext(gate); // upload 1
    await waitFor(() => expect(progressOf('politica.md')).toHaveTextContent('Procesando…'));
    expect(announcer()).toHaveTextContent('politica.md: Procesando…');
    expect(progressOf('manual.pdf')).toHaveTextContent('En espera');

    await releaseNext(gate); // ingest 1
    await waitFor(() => expect(progressOf('manual.pdf')).toHaveTextContent('Subiendo…'));
    expect(progressOf('politica.md')).toHaveTextContent('Listo');

    await releaseNext(gate); // upload 2
    await waitFor(() => expect(progressOf('manual.pdf')).toHaveTextContent('Procesando…'));

    await releaseNext(gate); // ingest 2
    await waitFor(() =>
      expect(announcer()).toHaveTextContent('Subida terminada. Archivos listos: 2. Con errores: 0.'),
    );
    expect(progressOf('manual.pdf')).toHaveTextContent('Listo');

    // The list refreshes once, at the end.
    expect(await screen.findByRole('rowheader', { name: 'manual.pdf' })).toBeInTheDocument();
    expect(screen.getByRole('rowheader', { name: 'politica.md' })).toBeInTheDocument();
    expect(routesCalled(fetchMock)).toEqual([
      ME,
      LIST,
      UPLOAD,
      ingest('new-1'),
      UPLOAD,
      ingest('new-2'),
      LIST,
    ]);
    expect(uploaded.map((file) => [file.name, file.type])).toEqual([
      ['politica.md', 'text/markdown'],
      ['manual.pdf', 'application/pdf'],
    ]);
  });

  it('explains 413, 422, and 502 in Spanish, never with the server text', async () => {
    const { user } = await openKnowledge('operator', {
      [LIST]: listReply([]),
      [UPLOAD]: (init) => {
        const file = (init.body as FormData).get('file') as File;
        if (file.name === 'enorme.pdf') {
          return { status: 413, body: { detail: 'file is 30000000 bytes; limit is 20971520 bytes' } };
        }
        const id = file.name === 'roto.pdf' ? 'broken' : 'offline';
        return { status: 201, body: doc({ id, filename: file.name, status: 'uploaded' }) };
      },
      [ingest('broken')]: { status: 422, body: { detail: 'unsupported content type: application/x-broken' } },
      [ingest('offline')]: { status: 502, body: { detail: EMBEDDING_ERROR } },
    });

    await user.upload(fileInput(), [
      textFile('enorme.pdf', 'application/pdf'),
      textFile('roto.pdf', 'application/pdf'),
      textFile('notas.txt'),
    ]);
    await user.click(screen.getByRole('button', { name: 'Subir y procesar' }));

    await waitFor(() => expect(announcer()).toHaveTextContent('Archivos listos: 0. Con errores: 3.'));
    expect(progressOf('enorme.pdf')).toHaveTextContent('Error: el archivo supera el límite de 20 MB.');
    expect(progressOf('roto.pdf')).toHaveTextContent(
      'Error: no se ha podido leer el contenido del archivo o su formato no está admitido.',
    );
    expect(progressOf('notas.txt')).toHaveTextContent(`Error: ${UNAVAILABLE}`);
    const progress = screen.getByRole('list', { name: 'Progreso de la subida' });
    expect(progress).not.toHaveTextContent(/bytes|unsupported|ollama/);
    await expectNoAxeViolations();
  });

  it('checks the selection, size, and format before sending, and accepts dropped files', async () => {
    const { user, fetchMock } = await openKnowledge('operator', {
      [LIST]: listReply([]),
      [UPLOAD]: { status: 201, body: doc({ id: 'dropped', filename: 'guia.txt', status: 'uploaded' }) },
      [ingest('dropped')]: { status: 200, body: doc({ id: 'dropped', filename: 'guia.txt' }) },
    });
    await screen.findByText('Todavía no hay documentos en la base de conocimiento.');

    await user.click(screen.getByRole('button', { name: 'Subir y procesar' }));
    expect(fileInput()).toHaveAttribute('aria-invalid', 'true');
    expect(fileInput()).toHaveAccessibleDescription(/Selecciona al menos un archivo\./);
    expect(fileInput()).toHaveFocus();

    const big = textFile('grande.pdf', 'application/pdf');
    Object.defineProperty(big, 'size', { value: 20 * 1024 * 1024 + 1 });
    await user.upload(fileInput(), big);
    expect(fileInput()).not.toHaveAttribute('aria-invalid');
    await user.click(screen.getByRole('button', { name: 'Subir y procesar' }));
    await waitFor(() =>
      expect(progressOf('grande.pdf')).toHaveTextContent('Error: el archivo supera el límite de 20 MB.'),
    );

    const zone = fileInput().closest('.dropzone')!;
    fireEvent.dragOver(zone);
    expect(zone).toHaveAttribute('data-active');
    fireEvent.drop(zone, {
      dataTransfer: { files: [textFile('programa.exe', 'application/x-msdownload'), textFile('guia.txt')] },
    });
    expect(zone).not.toHaveAttribute('data-active');

    await waitFor(() => expect(progressOf('guia.txt')).toHaveTextContent('Listo'));
    expect(progressOf('programa.exe')).toHaveTextContent(
      'Error: el formato no está admitido. Usa archivos .md, .txt, .pdf o .docx.',
    );
    expect(routesCalled(fetchMock).filter((route) => route === UPLOAD)).toHaveLength(1);
  });
});

describe('KnowledgePage: retry', () => {
  it('ingests a failed document again and shows its new status', async () => {
    let documents = LISTED;
    const { user, fetchMock } = await openKnowledge('operator', {
      [LIST]: () => listReply(documents),
      [ingest('d3')]: () => {
        documents = documents.map((item) =>
          item.id === 'd3' ? { ...item, status: 'processed', error_message: null } : item,
        );
        return { status: 200, body: documents[2] };
      },
    });
    await screen.findByRole('table', { name: TABLE_NAME });

    await user.click(within(rowOf(FAILED_NAME)).getByRole('button', { name: `Reintentar «${FAILED_NAME}»` }));

    await waitFor(() => expect(cellsOf(FAILED_NAME)[3]).toHaveTextContent(/Procesado$/));
    expect(within(rowOf(FAILED_NAME)).queryByRole('button')).not.toBeInTheDocument();
    expect(announcer()).toHaveTextContent(`«${FAILED_NAME}» se ha procesado correctamente.`);
    expect(routesCalled(fetchMock)).toEqual([ME, LIST, ingest('d3'), LIST]);
  });

  it('explains a failed retry in Spanish', async () => {
    const { user } = await openKnowledge('operator', {
      [LIST]: listReply(LISTED),
      [ingest('d3')]: { status: 502, body: { detail: EMBEDDING_ERROR } },
    });
    await screen.findByRole('table', { name: TABLE_NAME });

    await user.click(within(rowOf(FAILED_NAME)).getByRole('button', { name: `Reintentar «${FAILED_NAME}»` }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(`No se ha podido procesar «${FAILED_NAME}»: ${UNAVAILABLE}`);
    expect(alert).not.toHaveTextContent('ollama');
    expect(cellsOf(FAILED_NAME)[3]).toHaveTextContent(/^✕ ?Error/);
  });
});
