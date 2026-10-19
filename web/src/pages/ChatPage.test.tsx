import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { expectNoAxeViolations } from '../test/axe';
import { ControlledStream, doneFrame, installChatApi, sseFrame, type ChatReply } from '../test/fakeChat';
import { renderApp } from '../test/renderApp';

const CITATION = {
  number: 1,
  document_id: '9d1c6a52-0000-4000-8000-000000000001',
  document_filename: 'Política de vacaciones.pdf',
  chunk_id: '9d1c6a52-0000-4000-8000-000000000002',
  chunk_index: 0,
  start_offset: 0,
  end_offset: 45,
  content: 'Cada persona dispone de 23 días laborables.',
};

const APPROVAL = {
  approval_id: '7f3e2a10-1111-4000-8000-00000000abcd',
  tool_name: 'register_invoice',
  expires_at: '2026-10-15T10:00:00Z',
};

async function openChat(replies: ChatReply[]) {
  const api = installChatApi('viewer', replies);
  const view = renderApp({ route: '/chat', storedToken: 'viewer-token' });
  await screen.findByRole('heading', { level: 1, name: 'Chat' });
  const input = screen.getByLabelText('Mensaje');
  return { ...api, ...view, input };
}

function chatLog() {
  return screen.getByRole('log', { name: 'Conversación' });
}

function announcer() {
  return screen.getByRole('status');
}

describe('ChatPage', () => {
  it('streams the answer, then replaces it with the authoritative `done` text', async () => {
    const stream = new ControlledStream();
    const { user, input, requests } = await openChat([stream]);

    await user.type(input, '¿Cuántos días de vacaciones tengo?{Enter}');

    expect(within(chatLog()).getByText('¿Cuántos días de vacaciones tengo?')).toBeInTheDocument();
    expect(input).toHaveValue('');
    expect(input).toHaveFocus();
    expect(requests[0]).toMatchObject({ message: '¿Cuántos días de vacaciones tengo?', history: [] });

    // Cut every 3 bytes: inside lines and inside "í" / "ñ".
    stream.push(sseFrame('run_started', { run_id: 'r1' }) + sseFrame('token', { text: 'Tienes 23 días ' }), 3);
    expect(await within(chatLog()).findByText('Tienes 23 días', { exact: false })).toBeInTheDocument();
    expect(announcer()).not.toHaveTextContent('Tienes 23 días');

    stream.push(sseFrame('token', { text: 'de año [1].' }), 3);
    expect(await within(chatLog()).findByText(/Tienes 23 días de año/)).toBeInTheDocument();

    stream.push(sseFrame('citations', { citations: [CITATION], sources: [CITATION] }), 3);
    stream.push(doneFrame('Tienes 23 días laborables al año [1].', { citations: [CITATION] }), 3);
    stream.close();

    expect(await within(chatLog()).findByText(/Tienes 23 días laborables al año/)).toBeInTheDocument();
    expect(within(chatLog()).queryByText(/de año/)).not.toBeInTheDocument();
    expect(announcer()).toHaveTextContent('Respuesta del asistente: Tienes 23 días laborables al año [1].');
    expect(input).toHaveFocus();
  });

  it('lists the citations under the answer, collapsed, and links the [n] markers to them', async () => {
    const answer = 'Son 23 días [1]. **No** es <img src=x onerror=alert(1)> HTML [7].';
    const { user, input } = await openChat([doneFrame(answer, { citations: [CITATION] })]);

    await user.type(input, 'Vacaciones{Enter}');

    const sources = await screen.findByRole('list', { name: 'Fuentes' });
    const summary = within(sources).getByText('[1] Política de vacaciones.pdf');
    const details = summary.closest('details')!;
    expect(details).not.toHaveAttribute('open');
    expect(within(sources).getByText(CITATION.content)).not.toBeVisible();

    // Plain text: the markup is shown as typed, and only cited markers become links.
    expect(within(chatLog()).getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
    expect(within(chatLog()).getByText(/\*\*No\*\*/)).toBeInTheDocument();
    expect(within(chatLog()).queryByRole('link', { name: /\[7\]/ })).not.toBeInTheDocument();

    const marker = within(chatLog()).getByRole('link', { name: '[1] Fuente: Política de vacaciones.pdf' });
    expect(marker).toHaveTextContent('[1]');
    expect(marker).toHaveAttribute('href', `#${details.id}`);
    await user.click(marker);

    expect(details).toHaveAttribute('open');
    expect(summary).toHaveFocus();
    expect(within(sources).getByText(CITATION.content)).toBeVisible();
  });

  it('shows what the agent is doing while it works, without tool names', async () => {
    const stream = new ControlledStream();
    const { user, input } = await openChat([stream]);
    await user.type(input, 'Busca la política{Enter}');

    expect(await within(chatLog()).findByText('Preparando la respuesta…')).toBeInTheDocument();

    stream.push(sseFrame('tool_call', { call_id: 'c1', tool_name: 'search_documents' }));
    expect(await within(chatLog()).findByText('Buscando en la base de conocimiento…')).toBeInTheDocument();
    expect(announcer()).toHaveTextContent('Buscando en la base de conocimiento…');

    stream.push(sseFrame('tool_result', { call_id: 'c1', tool_name: 'search_documents', status: 'ok' }));
    expect(await within(chatLog()).findByText('Preparando la respuesta…')).toBeInTheDocument();

    stream.push(sseFrame('tool_call', { call_id: 'c2', tool_name: 'register_invoice' }));
    expect(await within(chatLog()).findByText('Consultando una herramienta…')).toBeInTheDocument();
    expect(screen.queryByText(/register_invoice/)).not.toBeInTheDocument();

    stream.push(doneFrame('Hecho.'));
    stream.close();
    expect(await within(chatLog()).findByText('Hecho.')).toBeInTheDocument();
    expect(within(chatLog()).queryByText('Consultando una herramienta…')).not.toBeInTheDocument();
  });

  it('shows an approval request once, with its id', async () => {
    const { user, input } = await openChat([
      sseFrame('tool_call', { call_id: 'c1', tool_name: 'register_invoice' }) +
        sseFrame('approval_required', APPROVAL) +
        sseFrame('tool_result', { call_id: 'c1', tool_name: 'register_invoice', status: 'approval_required' }) +
        doneFrame('He solicitado la aprobación.', { pending_approvals: [APPROVAL] }),
    ]);

    await user.type(input, 'Registra la factura{Enter}');

    const notices = await within(chatLog()).findAllByText(/necesita aprobación/);
    expect(notices).toHaveLength(1);
    expect(notices[0]).toHaveTextContent(`Identificador de la aprobación: ${APPROVAL.approval_id}`);
    expect(screen.queryByText(/register_invoice/)).not.toBeInTheDocument();
  });

  it.each([
    ['language model unavailable', 'El asistente no está disponible en este momento. Inténtalo de nuevo más tarde.'],
    ['internal error', 'Se ha producido un error inesperado. Inténtalo de nuevo.'],
  ])('translates the stream error "%s" instead of showing it', async (message, shown) => {
    const { user, input } = await openChat([sseFrame('run_started', { run_id: 'r' }) + sseFrame('error', { message })]);

    await user.type(input, 'Hola{Enter}');

    expect(await within(chatLog()).findByText(shown)).toBeInTheDocument();
    expect(announcer()).toHaveTextContent(shown);
    expect(screen.queryByText(message)).not.toBeInTheDocument();
  });

  it('reports a request that fails before streaming, and a stream cut before `done`', async () => {
    const { user, input } = await openChat([
      { status: 503, body: { detail: 'upstream down' } },
      sseFrame('token', { text: 'Empiezo a' }),
    ]);

    await user.type(input, 'Primera{Enter}');
    expect(
      await within(chatLog()).findByText(/No se ha podido conectar con el servidor/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/upstream down/)).not.toBeInTheDocument();

    await user.type(input, 'Segunda{Enter}');
    expect(
      await within(chatLog()).findByText('La respuesta se ha interrumpido. Inténtalo de nuevo.'),
    ).toBeInTheDocument();
    expect(within(chatLog()).getByText('Empiezo a')).toBeInTheDocument();
  });

  it('flags a stop at the step limit politely instead of the English fallback', async () => {
    const english = 'I could not complete this request within the allowed number of steps.';
    const { user, input } = await openChat([
      sseFrame('token', { text: english }) + doneFrame(english, { stop_reason: 'step_limit' }),
    ]);

    await user.type(input, 'Algo largo{Enter}');

    expect(
      await within(chatLog()).findByText(/no ha podido terminar la respuesta en el número de pasos permitido/),
    ).toBeInTheDocument();
    expect(screen.queryByText(english)).not.toBeInTheDocument();
  });

  it('stops a running answer with Detener and keeps what had arrived', async () => {
    const stream = new ControlledStream();
    const { user, input, requests } = await openChat([stream, doneFrame('Otra respuesta.')]);
    await user.type(input, 'Cuéntame algo largo{Enter}');
    stream.push(sseFrame('token', { text: 'Respuesta a medias' }));
    await within(chatLog()).findByText('Respuesta a medias');

    // A second Enter while the agent works sends nothing.
    await user.type(input, 'Otra{Enter}');
    expect(requests).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: 'Detener' }));

    expect(requests[0]!.signal?.aborted).toBe(true);
    expect(await within(chatLog()).findByText('Has detenido la respuesta.')).toBeInTheDocument();
    expect(within(chatLog()).getByText('Respuesta a medias')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Detener' })).not.toBeInTheDocument();
    expect(input).toHaveFocus();

    // The stopped turn is not replayed as history.
    await user.type(input, '{Enter}');
    expect(await within(chatLog()).findByText('Otra respuesta.')).toBeInTheDocument();
    expect(requests[1]).toMatchObject({ message: 'Otra', history: [] });
  });

  it('sends with Enter and the button, and adds a line with Shift+Enter', async () => {
    const { user, input, requests } = await openChat([doneFrame('Uno.'), doneFrame('Dos.')]);

    await user.type(input, '{Enter}');
    expect(input).toHaveAccessibleDescription(/Escribe un mensaje antes de enviarlo/);
    expect(requests).toHaveLength(0);

    await user.type(input, 'Primera línea{Shift>}{Enter}{/Shift}Segunda línea');
    expect(input).toHaveValue('Primera línea\nSegunda línea');
    expect(requests).toHaveLength(0);
    expect(input).not.toHaveAccessibleDescription(/Escribe un mensaje/);

    await user.keyboard('{Enter}');
    expect(await within(chatLog()).findByText('Uno.')).toBeInTheDocument();
    expect(requests[0]!.message).toBe('Primera línea\nSegunda línea');

    await user.type(input, '  Por botón  ');
    await user.click(screen.getByRole('button', { name: 'Enviar' }));
    expect(await within(chatLog()).findByText('Dos.')).toBeInTheDocument();
    expect(requests[1]!.message).toBe('Por botón');
    expect(input).toHaveFocus();
  });

  it('replays the last 10 messages as history, with assistant text capped at 4000 characters', async () => {
    const long = `${'a'.repeat(3999)}ñ😀 y más texto`;
    const replies = [doneFrame(long), ...Array.from({ length: 6 }, (_, index) => doneFrame(`Respuesta ${index + 2}`))];
    const { user, input, requests } = await openChat(replies);

    for (let turn = 1; turn <= 7; turn += 1) {
      await user.type(input, `Pregunta ${turn}{Enter}`);
      await waitFor(() => expect(within(chatLog()).queryAllByText(/Respuesta|a{100}/)).toHaveLength(turn));
    }

    expect(requests[1]!.history).toEqual([
      { role: 'user', content: 'Pregunta 1' },
      { role: 'assistant', content: `${'a'.repeat(3999)}ñ` },
    ]);
    const last = requests[6]!.history;
    expect(last).toHaveLength(10);
    expect(last[0]).toEqual({ role: 'user', content: 'Pregunta 2' });
    expect(last[9]).toEqual({ role: 'assistant', content: 'Respuesta 6' });
  });

  it('starts a new conversation that forgets the history', async () => {
    const { user, input, requests } = await openChat([doneFrame('Primera respuesta.'), doneFrame('Desde cero.')]);
    expect(within(chatLog()).getByText(/Todavía no hay mensajes/)).toBeInTheDocument();
    await user.type(input, 'Hola{Enter}');
    await within(chatLog()).findByText('Primera respuesta.');

    await user.click(screen.getByRole('button', { name: 'Nueva conversación' }));

    expect(within(chatLog()).queryByText('Primera respuesta.')).not.toBeInTheDocument();
    expect(within(chatLog()).getByText(/Todavía no hay mensajes/)).toBeInTheDocument();
    expect(announcer()).toHaveTextContent('Has empezado una conversación nueva.');
    expect(input).toHaveFocus();

    await user.type(input, 'Otra vez{Enter}');
    await within(chatLog()).findByText('Desde cero.');
    expect(requests[1]!.history).toEqual([]);
  });

  it('keeps the conversation while you visit another section', async () => {
    const { user, input } = await openChat([doneFrame('Sigo aquí.')]);
    await user.type(input, 'Hola{Enter}');
    await within(chatLog()).findByText('Sigo aquí.');

    const nav = screen.getByRole('navigation', { name: 'Principal' });
    await user.click(within(nav).getByRole('link', { name: 'Base de conocimiento' }));
    await screen.findByRole('heading', { level: 1, name: 'Base de conocimiento' });
    await user.click(within(nav).getByRole('link', { name: 'Chat' }));

    expect(await within(chatLog()).findByText('Sigo aquí.')).toBeInTheDocument();
  });

  it('announces a completed response with a capped preview, and stays silent on errors and cuts', async () => {
    const long = `${'a'.repeat(150)} final`;
    const { user, input } = await openChat([
      doneFrame(`  ${long}  `),
      sseFrame('run_started', { run_id: 'r' }) + sseFrame('error', { message: 'internal error' }),
      sseFrame('token', { text: 'Cortada' }),
    ]);

    const details: unknown[] = [];
    const listener = (event: Event) => {
      details.push((event as CustomEvent).detail);
    };
    window.addEventListener('aa:assistant-response-done', listener);
    try {
      await user.type(input, 'Uno{Enter}');
      await waitFor(() => expect(details).toHaveLength(1));
      expect(details[0]).toStrictEqual({ preview: 'a'.repeat(140) });

      await user.type(input, 'Dos{Enter}');
      await within(chatLog()).findByText('Se ha producido un error inesperado. Inténtalo de nuevo.');
      expect(details).toHaveLength(1);

      await user.type(input, 'Tres{Enter}');
      await within(chatLog()).findByText('La respuesta se ha interrumpido. Inténtalo de nuevo.');
      expect(details).toHaveLength(1);
    } finally {
      window.removeEventListener('aa:assistant-response-done', listener);
    }
  });

  it('has no axe violations, empty and with citations, an approval, and an error', async () => {
    const { user, input } = await openChat([
      sseFrame('approval_required', APPROVAL) +
        doneFrame('Mira [1].', { citations: [CITATION], pending_approvals: [APPROVAL] }),
      sseFrame('error', { message: 'language model unavailable' }),
    ]);
    await expectNoAxeViolations();

    await user.type(input, 'Uno{Enter}');
    await screen.findByRole('list', { name: 'Fuentes' });
    await user.type(input, 'Dos{Enter}');
    await within(chatLog()).findByText(/no está disponible/);
    await user.click(within(chatLog()).getByRole('link', { name: /\[1\]/ }));

    await expectNoAxeViolations();
  });
});
