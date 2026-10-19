// Development console for the admin-agent API, served at /ui only in development.
//
// Security rules (tests/test_ui.py guards them):
// - The access token lives only in `session` below: never in web storage or cookies.
// - Every server-provided string (document text, filenames, chat tokens, errors) is
//   rendered as text with textContent / createElement, never parsed as HTML, because
//   document content is untrusted.
"use strict";

const API = "/api/v1";
const HISTORY_LIMIT = 10; // prior messages replayed with each chat turn, as app/demo/chat.py
const MESSAGE_MAX_CHARS = 4000; // CHAT_MESSAGE_MAX_CHARS in packages/contracts/schemas.py
const session = { token: null, user: null, history: [] };

const byId = (id) => document.getElementById(id);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function setStatus(id, text, isError = false) {
  const node = byId(id);
  node.textContent = text;
  node.classList.toggle("error", isError);
}

async function errorMessage(response) {
  let detail = "";
  try {
    const body = await response.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    // Not a JSON error body: the status code alone is the message.
  }
  return detail ? `HTTP ${response.status}: ${detail}` : `HTTP ${response.status}`;
}

// fetch() against the API with the bearer token; throws Error("HTTP <code>: <detail>").
async function api(path, { method = "GET", json, body } = {}) {
  const headers = {};
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }
  const response = await fetch(API + path, { method, headers, body });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response;
}

// ---- 1. Login -------------------------------------------------------------

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = event.target.elements;
  const tenant = fields.tenant_slug.value.trim();
  setStatus("login-status", "Logging in...");
  try {
    const response = await api("/auth/login", {
      method: "POST",
      json: {
        tenant_slug: tenant,
        email: fields.email.value.trim(),
        password: fields.password.value,
      },
    });
    const body = await response.json();
    session.token = body.access_token;
    session.user = body.user || {};
    fields.password.value = "";
    showSession(tenant);
    setStatus("login-status", "");
    await refreshDocuments();
  } catch (error) {
    setStatus("login-status", error.message, true);
  }
});

function showSession(tenant) {
  const role = session.user.role || "unknown";
  const who = `${session.user.email} (role ${role}, tenant ${tenant})`;
  byId("session-info").textContent = `Logged in as ${who}.`;
  byId("login-form").hidden = true;
  byId("session").hidden = false;
  byId("panels").hidden = false;
  // The backend refuses viewer uploads anyway; hide the form only when the role is known.
  byId("upload-form").hidden = role === "viewer";
}

byId("logout").addEventListener("click", () => {
  session.token = null;
  session.user = null;
  session.history = [];
  for (const id of ["documents", "hits", "chat-log"]) byId(id).replaceChildren();
  for (const id of ["upload-status", "documents-status", "search-status", "chat-status"]) {
    setStatus(id, "");
  }
  byId("panels").hidden = true;
  byId("session").hidden = true;
  byId("login-form").hidden = false;
  setStatus("login-status", "Logged out.");
});

// ---- 2. Documents ---------------------------------------------------------

async function refreshDocuments() {
  setStatus("documents-status", "Loading...");
  try {
    const { documents } = await (await api("/documents/")).json();
    byId("documents").replaceChildren(
      ...documents.map((doc) => {
        const row = el("tr");
        const cells = [doc.filename, doc.status, doc.content_type, doc.size_bytes];
        row.append(...cells.map((value) => el("td", value)), el("td", doc.error_message));
        return row;
      }),
    );
    setStatus("documents-status", `${documents.length} document(s).`);
  } catch (error) {
    setStatus("documents-status", error.message, true);
  }
}

byId("refresh-documents").addEventListener("click", refreshDocuments);

byId("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const file = form.elements.file.files[0];
  if (!file) return;
  const data = new FormData();
  data.append("file", file);
  setStatus("upload-status", `Uploading ${file.name}...`);
  try {
    const uploaded = await (await api("/documents/", { method: "POST", body: data })).json();
    setStatus("upload-status", `Uploaded as ${uploaded.content_type}; ingesting...`);
    const path = `/documents/${encodeURIComponent(uploaded.id)}/ingest`;
    const ingested = await (await api(path, { method: "POST" })).json();
    const outcome = `${ingested.status} (${ingested.content_type})`;
    setStatus("upload-status", `${ingested.filename}: ${outcome}.`);
    form.reset();
  } catch (error) {
    setStatus("upload-status", error.message, true);
  }
  await refreshDocuments();
});

// ---- 3. Search ------------------------------------------------------------

byId("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = event.target.elements;
  setStatus("search-status", "Searching...");
  try {
    const response = await api("/search", {
      method: "POST",
      json: {
        query: fields.query.value,
        mode: fields.mode.value,
        top_k: Number(fields.top_k.value),
      },
    });
    const { hits, mode } = await response.json();
    byId("hits").replaceChildren(
      ...hits.map((hit) => {
        const item = el("li");
        const where = `chars ${hit.start_offset}-${hit.end_offset}`;
        item.append(
          el("div", `${hit.document_filename} | score ${hit.score.toFixed(4)} | ${where}`, "meta"),
          el("pre", hit.content),
        );
        return item;
      }),
    );
    setStatus("search-status", `${hits.length} hit(s), mode ${mode}.`);
  } catch (error) {
    setStatus("search-status", error.message, true);
  }
});

// ---- 4. Chat --------------------------------------------------------------

// Parse a Server-Sent Events body into {type, data} events (EventSource cannot POST).
async function* readEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let type = "message";
  let data = [];
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop(); // incomplete last line, kept for the next read
    if (done) lines.push(buffer, ""); // flush the tail and end the last event
    for (const raw of lines) {
      const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
      if (line === "") {
        if (data.length) yield { type, data: JSON.parse(data.join("\n")) };
        type = "message";
        data = [];
      } else if (line.startsWith("event:")) {
        type = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        data.push(line.slice("data:".length).replace(/^ /, ""));
      }
    }
    if (done) return;
  }
}

function renderCitations(list, citations) {
  list.replaceChildren(
    ...citations.map((citation) => {
      const item = el("li");
      const details = el("details");
      const where = `chars ${citation.start_offset}-${citation.end_offset}`;
      details.append(
        el("summary", `[${citation.number}] ${citation.document_filename} (${where})`),
        el("pre", citation.content),
      );
      item.append(details);
      return item;
    }),
  );
}

// Render one event of a run; returns the final answer on `done`, else undefined.
function handleEvent({ type, data }, view) {
  const note = (text, className = "event") => view.events.append(el("li", text, className));
  if (type === "run_started") {
    note(`run ${data.run_id}`);
  } else if (type === "token") {
    view.answer.append(data.text || ""); // a string argument becomes a text node
  } else if (type === "tool_call") {
    note(`[tool] ${data.tool_name} (call ${data.call_id})`);
  } else if (type === "tool_result") {
    note(`[tool] ${data.tool_name} -> ${data.status}${data.error ? `: ${data.error}` : ""}`);
  } else if (type === "approval_required") {
    note(
      `[approval required] ${data.tool_name}: approval id ${data.approval_id} ` +
        `(expires ${data.expires_at}). Not executed until an authorized user approves it.`,
    );
  } else if (type === "citations") {
    renderCitations(view.citations, data.citations || []);
  } else if (type === "done") {
    view.answer.textContent = data.answer || ""; // authoritative over the streamed tokens
    renderCitations(view.citations, data.citations || []);
    if (data.stop_reason === "step_limit") note("stopped at the step limit");
    return data.answer;
  } else if (type === "error") {
    note(`[error] ${data.message || "unknown error"}`, "event error");
  }
  return undefined;
}

byId("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const message = form.elements.message.value.trim();
  if (!message) return;
  const turn = el("div", null, "turn");
  const view = {
    events: el("ul", null, "events"),
    answer: el("div", "", "answer"),
    citations: el("ol"),
  };
  turn.append(el("div", `you: ${message}`, "you"), view.events, view.answer, view.citations);
  byId("chat-log").append(turn);
  const send = form.querySelector("button[type=submit]");
  send.disabled = true;
  setStatus("chat-status", "Waiting for the agent...");
  let answer;
  try {
    const response = await api("/chat/stream", {
      method: "POST",
      json: { message, history: session.history },
    });
    for await (const item of readEvents(response)) {
      answer = handleEvent(item, view) ?? answer;
    }
    setStatus("chat-status", "");
  } catch (error) {
    setStatus("chat-status", error.message, true);
  } finally {
    send.disabled = false;
  }
  if (answer) {
    session.history.push(
      { role: "user", content: message },
      { role: "assistant", content: answer.slice(0, MESSAGE_MAX_CHARS) },
    );
    session.history = session.history.slice(-HISTORY_LIMIT);
    form.elements.message.value = "";
  }
});

byId("chat-reset").addEventListener("click", () => {
  session.history = [];
  byId("chat-log").replaceChildren();
  setStatus("chat-status", "Conversation reset.");
});
