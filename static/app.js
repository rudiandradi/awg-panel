const peersEl = document.querySelector("#peers");
const template = document.querySelector("#peer-template");
const errorEl = document.querySelector("#error");
const refreshEl = document.querySelector("#refresh");
const addPeerForm = document.querySelector("#add-peer");
const peerTitleEl = document.querySelector("#peer-title");
const addPeerButton = document.querySelector("#add-peer-button");
const modal = document.querySelector("#config-modal");
const modalTitle = document.querySelector("#modal-title");
const modalClose = document.querySelector("#modal-close");
const qrImage = document.querySelector("#qr-image");
const qrCaption = document.querySelector("#qr-caption");
const vpnKeyText = document.querySelector("#vpn-key-text");
const configText = document.querySelector("#config-text");
const copyVpnKey = document.querySelector("#copy-vpn-key");
const copyConfig = document.querySelector("#copy-config");
let latest = null;
let busy = false;
let qrTimer = null;

function bytes(value) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let n = Number(value || 0);
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n >= 10 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}

function since(seconds) {
  if (seconds === null || seconds === undefined) return "no handshake";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function setError(message) {
  errorEl.hidden = !message;
  errorEl.textContent = message || "";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function updateSummary(data) {
  const peers = data.peers || [];
  const enabled = peers.filter((peer) => peer.enabled);
  const online = enabled.filter((peer) => peer.online);
  const rx = enabled.reduce((sum, peer) => sum + Number(peer.rxBytes || 0), 0);
  const tx = enabled.reduce((sum, peer) => sum + Number(peer.txBytes || 0), 0);
  document.querySelector("#container").textContent = `${data.container} / ${data.interface?.name || "awg0"}`;
  document.querySelector("#online-count").textContent = `${online.length} online`;
  document.querySelector("#peer-count").textContent = String(peers.length);
  document.querySelector("#peer-online").textContent = String(online.length);
  document.querySelector("#rx-total").textContent = bytes(rx);
  document.querySelector("#tx-total").textContent = bytes(tx);
  document.querySelector("#updated-at").textContent = new Date((data.updatedAt || 0) * 1000).toLocaleTimeString();
}

function render() {
  if (!latest) return;
  updateSummary(latest);
  peersEl.textContent = "";
  const peers = latest.peers || [];

  for (const peer of peers) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.classList.toggle("disabled", !peer.enabled);
    const dot = node.querySelector(".status-dot");
    dot.classList.toggle("online", peer.enabled && peer.online);
    dot.classList.toggle("disabled", !peer.enabled);

    const name = node.querySelector(".peer-name");
    name.value = peer.name || "";
    name.addEventListener("change", async () => {
      await runAction(async () => {
        latest = await api(`/api/peers/${encodeURIComponent(peer.publicKey)}/name`, {
          method: "POST",
          body: JSON.stringify({ name: name.value }),
        });
        render();
      });
    });

    const toggle = node.querySelector(".toggle");
    toggle.classList.toggle("is-active", peer.enabled);
    toggle.setAttribute("aria-pressed", String(peer.enabled));
    toggle.setAttribute("aria-label", peer.enabled ? "Disable peer" : "Enable peer");
    toggle.title = peer.enabled ? "Disable peer" : "Enable peer";
    toggle.innerHTML = `
      <span class="toggle-track" aria-hidden="true">
        <span class="toggle-glow"></span>
        <span class="toggle-thumb"></span>
      </span>
      <span class="toggle-state">${peer.enabled ? "On" : "Off"}</span>
    `;
    toggle.addEventListener("click", async () => {
      const action = peer.enabled ? "disable" : "enable";
      await runAction(async () => {
        latest = await api(`/api/peers/${encodeURIComponent(peer.publicKey)}/${action}`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        render();
      });
    });

    const configButton = node.querySelector(".config-button");
    configButton.disabled = !peer.hasConfig;
    configButton.textContent = peer.hasConfig ? "QR / Config" : "No QR";
    configButton.addEventListener("click", async () => {
      await showConfig(peer);
    });

    const deleteButton = node.querySelector(".delete-button");
    deleteButton.addEventListener("click", async () => {
      const label = peer.name || peer.allowedIps || peer.id;
      if (!confirm(`Delete peer "${label}" completely?`)) return;
      await runAction(async () => {
        latest = await api(`/api/peers/${encodeURIComponent(peer.publicKey)}/delete`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        render();
      });
    });

    node.querySelector(".allowed").textContent = peer.allowedIps || "no allowed IPs";
    node.querySelector(".endpoint").textContent = peer.endpoint || "no endpoint";
    node.querySelector(".handshake").textContent = peer.enabled
      ? `handshake ${since(peer.secondsSinceHandshake)}`
      : "disabled";
    node.querySelector(".rx").textContent = bytes(peer.rxBytes);
    node.querySelector(".tx").textContent = bytes(peer.txBytes);
    node.querySelector(".rx-rate").textContent = `${bytes(peer.rxRate)}/s`;
    node.querySelector(".tx-rate").textContent = `${bytes(peer.txRate)}/s`;
    node.querySelector(".key").textContent = `${peer.id} · ${peer.publicKey}`;
    peersEl.appendChild(node);
  }
}

async function refresh() {
  if (busy) return;
  await runAction(async () => {
    latest = await api("/api/status");
    render();
  }, true);
}

async function addPeer(event) {
  event.preventDefault();
  const name = peerTitleEl.value.trim();
  if (!name) return;
  await runAction(async () => {
    addPeerButton.disabled = true;
    const result = await api("/api/peers", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    latest = result.status;
    peerTitleEl.value = "";
    render();
    const peer = latest.peers.find((item) => item.publicKey === result.publicKey);
    if (peer) await showConfig(peer);
  });
}

async function showConfig(peer) {
  const data = await api(`/api/peers/${encodeURIComponent(peer.publicKey)}/config`);
  if (qrTimer) {
    clearInterval(qrTimer);
    qrTimer = null;
  }
  modalTitle.textContent = data.name;
  vpnKeyText.value = data.vpnKey || "";
  configText.value = data.config;
  qrImage.src = `/api/peers/${encodeURIComponent(peer.publicKey)}/qr.png?format=native&ts=${Date.now()}`;
  qrCaption.textContent = "Native AWG QR (PNG)";
  modal.showModal();
}

async function runAction(fn, quiet = false) {
  if (busy) return;
  busy = true;
  refreshEl.disabled = true;
  addPeerButton.disabled = true;
  if (!quiet) setError("");
  try {
    await fn();
    setError("");
  } catch (error) {
    setError(error.message);
  } finally {
    busy = false;
    refreshEl.disabled = false;
    addPeerButton.disabled = false;
  }
}

refreshEl.addEventListener("click", refresh);
addPeerForm.addEventListener("submit", addPeer);
modalClose.addEventListener("click", () => modal.close());
modal.addEventListener("close", () => {
  if (qrTimer) {
    clearInterval(qrTimer);
    qrTimer = null;
  }
});
copyConfig.addEventListener("click", async () => {
  await navigator.clipboard.writeText(configText.value);
  copyConfig.textContent = "Copied";
  setTimeout(() => {
    copyConfig.textContent = "Copy config";
  }, 1200);
});
copyVpnKey.addEventListener("click", async () => {
  await navigator.clipboard.writeText(vpnKeyText.value);
  copyVpnKey.textContent = "Copied";
  setTimeout(() => {
    copyVpnKey.textContent = "Copy vpn:// key";
  }, 1200);
});
refresh();
setInterval(refresh, 5000);
