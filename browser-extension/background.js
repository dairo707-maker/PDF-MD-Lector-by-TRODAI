const NATIVE_HOST = "com.trodai.pdf_md_lector";

function sendNative(payload) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
          return;
        }
        resolve(response || { ok: false, error: "empty_response" });
      });
    } catch (err) {
      resolve({ ok: false, error: String(err) });
    }
  });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "trodai-capture-page-pdf",
    title: "Guardar pagina como PDF en PDF/MD Lector",
    contexts: ["page"]
  });
  chrome.contextMenus.create({
    id: "trodai-ping-host",
    title: "Verificar conexion local PDF/MD Lector",
    contexts: ["action"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "trodai-ping-host") {
    await sendNative({ action: "ping" });
    return;
  }
  if (info.menuItemId === "trodai-capture-page-pdf" && tab && tab.id) {
    const injected = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => document.documentElement.outerHTML
    });
    const html = (injected && injected[0] && injected[0].result) || "";
    await sendNative({
      action: "webpage_to_pdf",
      url: tab.url || "",
      title: tab.title || "Web Capture",
      html,
      dpi: 150
    });
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message !== "object") {
    sendResponse({ ok: false, error: "invalid_message" });
    return false;
  }
  sendNative(message).then(sendResponse);
  return true;
});
