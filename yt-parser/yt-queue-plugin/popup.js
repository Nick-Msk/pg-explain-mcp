const API = "http://127.0.0.1:8765";
const urlEl = document.getElementById("url");
const statusEl = document.getElementById("status");
const btn = document.getElementById("add");

let currentUrl = "";

function setStatus(text, cls = "") {
  statusEl.textContent = text;
  statusEl.className = cls;
}

(async () => {
  try {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    currentUrl = tab?.url || "";
    urlEl.textContent = currentUrl || "(нет URL)";
    if (!/^https?:\/\/(www\.)?(youtube\.com|youtu\.be)\//.test(currentUrl)) {
      btn.disabled = true;
      setStatus("Это не YouTube-видео.", "err");
    }
  } catch (e) {
    setStatus(`Не удалось прочитать URL: ${e.message}`, "err");
    btn.disabled = true;
  }
})();

btn.addEventListener("click", async () => {
  btn.disabled = true;
  setStatus("Отправляю...");
  try {
    const r = await fetch(`${API}/enqueue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: currentUrl }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      setStatus(data.message || "OK", "ok");
    } else {
      setStatus(`Ошибка: ${data.detail || r.status}`, "err");
    }
  } catch (e) {
    setStatus(`Сеть: ${e.message} (enqueue_api запущен?)`, "err");
  } finally {
    btn.disabled = false;
  }
});

