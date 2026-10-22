(() => {
  "use strict";

  const tauri = window.__TAURI__;
  const elements = {
    deviceName: document.getElementById("device-name"),
    badge: document.getElementById("connection-badge"),
    server: document.getElementById("server"),
    folderState: document.getElementById("folder-state"),
    grantFolder: document.getElementById("grant-folder"),
    pairForm: document.getElementById("pair-form"),
    pairCode: document.getElementById("pair-code"),
    deviceInput: document.getElementById("device-input"),
    pairButton: document.getElementById("pair-button"),
    reconnect: document.getElementById("reconnect"),
    disconnect: document.getElementById("disconnect"),
    forget: document.getElementById("forget"),
    error: document.getElementById("error"),
    notice: document.getElementById("notice"),
  };

  if (!tauri || !tauri.core || typeof tauri.core.invoke !== "function") {
    showError("No se pudo iniciar el conector del escritorio.");
    return;
  }

  const invoke = (command, args) => tauri.core.invoke(command, args);
  let busy = false;

  function hideMessages() {
    elements.error.hidden = true;
    elements.notice.hidden = true;
  }

  function showError(message) {
    elements.notice.hidden = true;
    elements.error.textContent = String(message);
    elements.error.hidden = false;
  }

  function showNotice(message) {
    elements.error.hidden = true;
    elements.notice.textContent = message;
    elements.notice.hidden = false;
  }

  function render(status) {
    elements.deviceName.textContent = status.paired
      ? status.name || "Equipo sin nombre"
      : "Sin emparejar";

    const label = !status.paired
      ? "Sin emparejar"
      : status.connected
        ? "Conectado"
        : "Desconectado";
    elements.badge.textContent = label;
    elements.badge.className = "badge " + (
      !status.paired ? "badge-idle" : status.connected ? "badge-on" : "badge-off"
    );

    elements.server.textContent = status.paired
      ? "Servidor: " + status.server_url
      : "";

    elements.folderState.textContent = status.folder_ready
      ? "Carpeta lista"
      : "Falta elegir una carpeta";
    elements.folderState.classList.toggle("ready", status.folder_ready);

    elements.pairButton.disabled = !status.folder_ready;
    elements.reconnect.disabled = !status.paired || status.connected || !status.folder_ready;
    elements.disconnect.disabled = !status.paired;
    elements.forget.disabled = !status.paired;

    if (status.paired && !elements.deviceInput.value) {
      elements.deviceInput.value = status.name || "";
    }
  }

  async function refresh() {
    try {
      render(await invoke("connector_status"));
    } catch (error) {
      showError("No se pudo leer el estado del conector.");
    }
  }

  async function withBusy(action) {
    if (busy) {
      return;
    }
    busy = true;
    hideMessages();
    try {
      await action();
    } catch (error) {
      showError(error);
    } finally {
      busy = false;
      await refresh();
    }
  }

  elements.grantFolder.addEventListener("click", () => {
    withBusy(async () => {
      const result = await invoke("grant_folder");
      if (result && result.granted) {
        showNotice("Carpeta concedida. Ya puedes conectar el equipo.");
      }
    });
  });

  elements.pairForm.addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy(async () => {
      const code = elements.pairCode.value.trim();
      const name = elements.deviceInput.value.trim();
      if (!code) {
        throw "Ingresa el código de emparejamiento.";
      }
      if (!name) {
        throw "Ponle un nombre a este equipo.";
      }
      await invoke("pair_connector", { code, name });
      elements.pairCode.value = "";
      showNotice("Equipo emparejado. Esperando la conexión con el servidor…");
    });
  });

  elements.reconnect.addEventListener("click", () => {
    withBusy(async () => {
      await invoke("reconnect_connector");
      showNotice("Reconectando el equipo…");
    });
  });

  elements.disconnect.addEventListener("click", () => {
    withBusy(async () => {
      await invoke("disconnect_connector");
      showNotice("Equipo desconectado. La carpeta sigue concedida.");
    });
  });

  elements.forget.addEventListener("click", () => {
    withBusy(async () => {
      const confirmed = window.confirm(
        "¿Olvidar este equipo? Se borrará el emparejamiento y la carpeta quedará en tu equipo.",
      );
      if (!confirmed) {
        return;
      }
      await invoke("forget_connector");
      showNotice("Equipo olvidado.");
    });
  });

  if (tauri.event && typeof tauri.event.listen === "function") {
    tauri.event.listen("connector:error", (event) => {
      showError(event.payload);
    });
  }

  refresh();
  window.setInterval(refresh, 3000);
})();
