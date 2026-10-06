const $ = (id) => document.getElementById(id);
const ask = (msg) => chrome.runtime.sendMessage(msg);

async function refresh() {
  const s = await ask({ type: "status" });
  const box = $("status");
  box.className = "status " + (s.connected ? "ok" : "bad");
  box.textContent = s.connected
    ? `Connected to Karya (port ${s.port}). ${s.tabs ? `Karya has ${s.tabs} tab(s) open.` : "Karya will open its own tab when it needs the browser."}`
    : `Not connected: ${s.error || "Karya isn't running."}`;
  $("port").value = s.port || 8765;
  $("token").placeholder = s.hasToken ? "saved (type to replace)" : "not set";
}

$("adopt").addEventListener("click", async () => {
  const r = await ask({ type: "adopt" });
  $("status").textContent = r.ok ? `Karya will work in this tab: ${r.title || ""}` : r.error;
});
$("reconnect").addEventListener("click", async () => { await ask({ type: "reconnect" }); setTimeout(refresh, 800); });
$("save").addEventListener("click", async () => {
  await ask({ type: "save", port: $("port").value, token: $("token").value });
  $("token").value = "";
  setTimeout(refresh, 800);
});
refresh();
