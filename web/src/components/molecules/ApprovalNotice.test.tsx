import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import type { ApprovalDetails } from '../../api/approvals';
import type { ChatApproval } from '../../api/chat';
import { expectNoAxeViolations } from '../../test/axe';
import { ApprovalNoticeView, type ApprovalNoticeState } from './ApprovalNotice';

const APPROVAL: ChatApproval = {
  approvalId: '7f3e2a10-1111-4000-8000-00000000abcd',
  toolName: 'send_email',
  expiresAt: '2026-10-15T10:00:00Z',
};

const SEND: ApprovalDetails = {
  id: APPROVAL.approvalId,
  toolName: 'send_email',
  input: {
    to: ['ana@example.com', 'bruno@example.com'],
    cc: ['caro@example.com'],
    bcc: [],
    subject: 'Factura 42',
    body: 'Hola Ana,\n\nadjunto la factura 42.',
    thread_id: null,
  },
  status: 'pending',
  expiresAt: '2026-10-15T10:00:00Z',
  executionError: null,
};

const READY: ApprovalNoticeState = { details: SEND, busy: null, error: null };

/** The chat page around the notice (title, landmark, headings), so axe checks it in place. */
function ChatPageFrame({ children }: { children: ReactNode }) {
  return (
    <main>
      <h1>Chat</h1>
      <h2>Conversación</h2>
      {children}
    </main>
  );
}

function renderNotice(state: ApprovalNoticeState, approval: ChatApproval = APPROVAL) {
  document.title = 'Chat';
  const handlers = { onConfirm: vi.fn(), onReject: vi.fn(), onRetry: vi.fn() };
  const view = render(
    <ApprovalNoticeView approval={approval} state={state} announcement={{ id: 0, text: '' }} {...handlers} />,
    { wrapper: ChatPageFrame },
  );
  return { ...view, ...handlers, user: userEvent.setup() };
}

/** The notice is a group named by what the action does. */
function notice(name: string) {
  return screen.getByRole('group', { name });
}

/** Label → value pairs of the notice's description list. */
function details(group: HTMLElement): Record<string, string> {
  return Object.fromEntries(
    within(group)
      .queryAllByRole('term')
      .map((term) => [term.textContent, term.nextElementSibling?.textContent ?? '']),
  );
}

describe('ApprovalNoticeView', () => {
  it('says what sending an email does: recipients, subject, and a body preview', async () => {
    const { user, onConfirm, onReject } = renderNotice(READY);

    const group = notice('Enviar un correo');
    expect(details(group)).toEqual({
      Para: 'ana@example.com, bruno@example.com',
      CC: 'caro@example.com',
      Asunto: 'Factura 42',
      Mensaje: 'Hola Ana, adjunto la factura 42.',
    });
    expect(group).toHaveTextContent('No se hará nada hasta que lo confirmes.');
    expect(group).toHaveTextContent(`Identificador de la aprobación: ${APPROVAL.approvalId}`);

    await user.click(within(group).getByRole('button', { name: 'Aprobar y ejecutar' }));
    await user.click(within(group).getByRole('button', { name: 'Rechazar' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
    await expectNoAxeViolations();
  });

  it('shows hidden copies and cuts a long body to a short preview', () => {
    const body = `${'Palabra '.repeat(60)}final secreto`;
    renderNotice({ ...READY, details: { ...SEND, input: { ...SEND.input, cc: [], bcc: ['dan@example.com'], body } } });

    const shown = details(notice('Enviar un correo'));
    expect(shown.CCO).toBe('dan@example.com');
    expect(shown).not.toHaveProperty('CC');
    expect(Array.from(shown.Mensaje ?? '').length).toBeLessThanOrEqual(161);
    expect(shown.Mensaje).toMatch(/…$/);
    expect(screen.queryByText(/final secreto/)).not.toBeInTheDocument();
  });

  it('lists the workspace files an email attaches', () => {
    const attachments = ['Facturas/f42.pdf', 'datos/raw.bin'];
    renderNotice({ ...READY, details: { ...SEND, input: { ...SEND.input, attachments } } });

    expect(details(notice('Enviar un correo')).Adjuntos).toBe('Facturas/f42.pdf, datos/raw.bin');
  });

  it.each([
    ['archive_message', { message_id: 'm1' }, 'Archivar un correo', { 'Correo (identificador)': 'm1' }],
    [
      'add_label',
      { message_id: 'm1', label_id: 'Label_9' },
      'Añadir una etiqueta a un correo',
      { 'Correo (identificador)': 'm1', Etiqueta: 'Label_9' },
    ],
    [
      'save_attachment',
      { message_id: 'm1', part_id: '1', path: 'Facturas/f42.pdf' },
      'Guardar un adjunto en tu carpeta',
      { 'Correo (identificador)': 'm1', 'Adjunto (parte)': '1', 'Guardar en': 'Facturas/f42.pdf' },
    ],
  ])('says which message %s acts on', (toolName, input, title, rows) => {
    renderNotice({ ...READY, details: { ...SEND, toolName, input } }, { ...APPROVAL, toolName });

    expect(details(notice(title))).toEqual(rows);
  });

  it('describes an unknown tool generically, never by its internal name', () => {
    renderNotice(
      { ...READY, details: { ...SEND, toolName: 'register_invoice', input: { amount: 10 } } },
      { ...APPROVAL, toolName: 'register_invoice' },
    );

    expect(details(notice('Confirmar una acción'))).toEqual({});
    expect(screen.queryByText(/register_invoice/)).not.toBeInTheDocument();
  });

  it.each([
    ['approving', 'Aprobando la acción…'],
    ['executing', 'Ejecutando la acción…'],
    ['rejecting', 'Rechazando la acción…'],
  ] as const)('keeps both buttons focusable but inert while %s', async (busy, text) => {
    const { user, onConfirm, onReject } = renderNotice({ ...READY, busy });

    const group = notice('Enviar un correo');
    expect(group).toHaveTextContent(text);
    for (const name of ['Aprobar y ejecutar', 'Rechazar']) {
      const button = within(group).getByRole('button', { name });
      expect(button).toHaveAttribute('aria-disabled', 'true');
      await user.click(button);
    }
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onReject).not.toHaveBeenCalled();
  });

  it.each([
    ['executed', null, 'Correo enviado.'],
    ['rejected', null, 'Acción rechazada. No se ha hecho nada.'],
    ['expired', null, 'La solicitud ha caducado sin ejecutarse.'],
    ['executing', null, 'La acción se está ejecutando.'],
    ['failed', 'tool_failed', 'No se ha podido ejecutar la acción.'],
    [
      'failed',
      'precheck_failed: google mail connection expired',
      'No se ha podido ejecutar la acción. Motivo: google mail connection expired',
    ],
  ] as const)('shows the final state "%s" and disables the buttons', async (status, executionError, text) => {
    const { user, onConfirm } = renderNotice({ ...READY, details: { ...SEND, status, executionError } });

    const group = notice('Enviar un correo');
    expect(group).toHaveTextContent(text);
    const confirm = within(group).getByRole('button', { name: 'Aprobar y ejecutar' });
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(within(group).getByRole('button', { name: 'Rechazar' })).toHaveAttribute('aria-disabled', 'true');
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('names the other completed Gmail actions by what they did', () => {
    renderNotice(
      { ...READY, details: { ...SEND, toolName: 'archive_message', input: { message_id: 'm1' }, status: 'executed' } },
      { ...APPROVAL, toolName: 'archive_message' },
    );

    expect(notice('Archivar un correo')).toHaveTextContent('Correo archivado.');
  });

  it('names a saved attachment by what it did', () => {
    const input = { message_id: 'm1', part_id: '1', path: 'f42.pdf' };
    renderNotice(
      { ...READY, details: { ...SEND, toolName: 'save_attachment', input, status: 'executed' } },
      { ...APPROVAL, toolName: 'save_attachment' },
    );

    expect(notice('Guardar un adjunto en tu carpeta')).toHaveTextContent('Adjunto guardado.');
  });

  it('refuses to decide after a forbidden reply, with a clear message', () => {
    renderNotice({ ...READY, error: 'forbidden' });

    const group = notice('Enviar un correo');
    expect(group).toHaveTextContent('No tienes permiso para confirmar ni rechazar esta acción.');
    expect(within(group).getByRole('button', { name: 'Aprobar y ejecutar' })).toHaveAttribute('aria-disabled', 'true');
  });

  it('lets the user try again after a network error', async () => {
    const { user, onConfirm } = renderNotice({ ...READY, details: { ...SEND, status: 'approved' }, error: 'network' });

    const group = notice('Enviar un correo');
    expect(group).toHaveTextContent('No se ha podido conectar con el servidor.');
    await user.click(within(group).getByRole('button', { name: 'Aprobar y ejecutar' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('shows no decision buttons until the details are known', async () => {
    const { rerender, user, onRetry } = renderNotice({ details: null, busy: 'loading', error: null });

    const group = notice('Enviar un correo');
    expect(group).toHaveTextContent('Cargando los detalles de la acción…');
    expect(within(group).queryByRole('button')).not.toBeInTheDocument();

    rerender(
      <ApprovalNoticeView
        approval={APPROVAL}
        state={{ details: null, busy: null, error: 'network' }}
        announcement={{ id: 1, text: '' }}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
        onRetry={onRetry}
      />,
    );
    expect(within(group).queryByRole('button', { name: 'Aprobar y ejecutar' })).not.toBeInTheDocument();
    await user.click(within(group).getByRole('button', { name: 'Volver a cargar' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    await expectNoAxeViolations();
  });

  it('cannot be retried when the request no longer exists', () => {
    renderNotice({ details: null, busy: null, error: 'notFound' });

    const group = notice('Enviar un correo');
    expect(group).toHaveTextContent('Esta solicitud ya no existe.');
    expect(within(group).queryByRole('button')).not.toBeInTheDocument();
  });

  it('announces through its own polite status region', () => {
    render(
      <ApprovalNoticeView
        approval={APPROVAL}
        state={{ ...READY, details: { ...SEND, status: 'executed' } }}
        announcement={{ id: 3, text: 'Correo enviado.' }}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    );

    expect(within(notice('Enviar un correo')).getByRole('status')).toHaveTextContent('Correo enviado.');
  });
});
