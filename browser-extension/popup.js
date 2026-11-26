const statusEl = document.getElementById("status");
const outputInput = document.getElementById("outputPath");

function setStatus(text) {
  statusEl.textContent = text;
}

function sendToBridge(payload) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(payload, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(resp || { ok: false, error: "no_response" });
    });
  });
}

document.getElementById("btnPing").addEventListener("click", async () => {
  setStatus("Verificando Native Messaging Host...");
  const resp = await sendToBridge({ action: "ping" });
  setStatus(resp.ok ? `Conectado: ${resp.app} v${resp.version}` : `Error: ${resp.error}`);
});

document.getElementById("btnCapture").addEventListener("click", async () => {
  const outPath = (outputInput.value || "").trim();
  if (!outPath) {
    setStatus("Indica una ruta local .pdf de salida.");
    return;
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    setStatus("No hay pestana activa.");
    return;
  }
  const injected = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => document.documentElement.outerHTML
  });
  const html = (injected && injected[0] && injected[0].result) || "";
  setStatus("Generando PDF local...");
  const resp = await sendToBridge({
    action: "webpage_to_pdf",
    url: tab.url || "",
    html,
    output: outPath,
    dpi: 150
  });
  setStatus(resp.ok ? `PDF guardado: ${resp.output}` : `Error: ${resp.error}`);
});

document.getElementById("btnOpenLocal").addEventListener("click", async () => {
  const docPath = (outputInput.value || "").trim();
  if (!docPath) {
    setStatus("Indica la ruta local del archivo PDF o MD.");
    return;
  }
  const resp = await sendToBridge({ action: "open_pdf", path: docPath, forward_to_instance: true });
  setStatus(resp.ok ? `Validado: ${resp.path}` : `Error: ${resp.error}`);
});
