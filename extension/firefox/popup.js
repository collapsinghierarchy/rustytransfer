const statusText = document.getElementById("status");
const transferState = document.querySelector(".transfer-state");
const connectionState = document.getElementById("connection-state");
const fileNameText = document.getElementById("file-name");
const hostErrorText = document.getElementById("host-error");
const progressArea = document.getElementById("progress-area");
const progressBar = document.getElementById("progress");
const progressLabel = document.getElementById("progress-label");
const invitationArea = document.getElementById("invitation-area");
const invitationOutput = document.getElementById("invitation-output");
const copyStatus = document.getElementById("copy-status");
const receiveInvitation = document.getElementById("receive-invitation");
const sendButton = document.getElementById("send-button");
const receiveButton = document.getElementById("receive-button");
const cancelButton = document.getElementById("cancel-button");
const sendMode = document.getElementById("send-mode");
const receiveMode = document.getElementById("receive-mode");
const sendPanel = document.getElementById("send-panel");
const receivePanel = document.getElementById("receive-panel");
const modeSwitch = document.querySelector(".mode-switch");

let currentState = null;
let currentMode = "send";

function setMode(mode) {
  currentMode = mode;
  const active = Boolean(currentState && ["starting", "busy", "waiting", "transferring", "cancelling"].includes(currentState.phase));
  sendMode.setAttribute("aria-pressed", String(mode === "send"));
  receiveMode.setAttribute("aria-pressed", String(mode === "receive"));
  modeSwitch.hidden = active;
  sendPanel.hidden = active || mode !== "send";
  receivePanel.hidden = active || mode !== "receive";
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
}

function describeState(state) {
  if (!state.hostAvailable && state.hostError) {
    return "RustyTransfer desktop host is unavailable. Install or register the native host, then try again.";
  }

  switch (state.phase) {
    case "starting":
      return state.direction === "receive" ? "Connecting to the sender…" : "Starting file send…";
    case "busy":
      return state.direction === "receive" ? "Waiting for the incoming file…" : "Preparing the file…";
    case "waiting":
      return "Invitation ready. Share it with the receiver.";
    case "transferring":
      return state.direction === "receive" ? "Receiving file…" : "Sending file…";
    case "cancelling":
      return "Cancelling transfer…";
    case "complete":
      return state.direction === "receive" ? "File received." : "File sent.";
    case "cancelled":
      return "Transfer cancelled.";
    case "error":
      return state.error || "The transfer failed.";
    default:
      return "Ready to send or receive a file.";
  }
}

function render(state) {
  currentState = state;
  setMode(state.direction || currentMode);
  transferState.hidden = state.phase === "idle" && state.hostAvailable === true;
  if (state.direction === "receive" && ["complete", "cancelled"].includes(state.phase)) {
    receiveInvitation.value = "";
  }
  connectionState.textContent = state.hostAvailable === true
    ? "Desktop ready"
    : state.hostAvailable === false ? "Desktop unavailable" : "Connecting";
  connectionState.classList.toggle("ready", state.hostAvailable === true);
  connectionState.classList.toggle("unavailable", state.hostAvailable === false);
  statusText.textContent = describeState(state);
  statusText.classList.toggle("host-error", !state.hostAvailable && Boolean(state.hostError));

  const hostError = !state.hostAvailable && state.hostError
    ? `Native host detail: ${state.hostError}`
    : "";
  hostErrorText.textContent = hostError;
  hostErrorText.hidden = !hostError;

  const visibleFileName = state.fileName || "";
  fileNameText.textContent = visibleFileName;
  fileNameText.hidden = !visibleFileName;

  const showProgress = state.phase === "transferring";
  progressArea.hidden = !showProgress;
  if (showProgress && Number.isFinite(state.total) && state.total > 0) {
    progressBar.max = state.total;
    progressBar.value = Math.min(state.done || 0, state.total);
    const percent = Math.floor((Math.min(state.done || 0, state.total) / state.total) * 100);
    const done = formatBytes(state.done || 0);
    const total = formatBytes(state.total);
    progressLabel.textContent = `${percent}% · ${done} of ${total}`;
  } else {
    progressBar.removeAttribute("value");
    progressLabel.textContent = showProgress ? "Transfer in progress" : "";
  }

  const invitation = typeof state.invite === "string" ? state.invite : "";
  invitationArea.hidden = !invitation;
  if (invitationOutput.value !== invitation) invitationOutput.value = invitation;
  copyStatus.textContent = "";

  const active = ["starting", "busy", "waiting", "transferring", "cancelling"].includes(state.phase);
  sendButton.disabled = active;
  receiveButton.disabled = active;
  receiveInvitation.disabled = active;
  cancelButton.hidden = !active || state.phase === "cancelling";
}

async function sendAction(message) {
  try {
    const result = await browser.runtime.sendMessage(message);
    if (result?.error && result.phase !== "error") {
      transferState.hidden = false;
      statusText.textContent = result.error;
      statusText.classList.add("host-error");
      return;
    }
    if (result) render(result);
  } catch (error) {
    render({
      ...(currentState || {}),
      phase: "error",
      hostAvailable: false,
      hostError: error.message,
      error: `Could not reach the extension background: ${error.message}`,
    });
  }
}

sendButton.addEventListener("click", () => {
  sendAction({ type: "start_send" });
});

sendMode.addEventListener("click", () => setMode("send"));
receiveMode.addEventListener("click", () => setMode("receive"));

receiveButton.addEventListener("click", () => {
  sendAction({ type: "start_receive", invite: receiveInvitation.value });
});

cancelButton.addEventListener("click", () => {
  sendAction({ type: "cancel" });
});

document.getElementById("copy-invitation").addEventListener("click", async () => {
  const invitation = invitationOutput.value;
  if (!invitation) return;

  try {
    await navigator.clipboard.writeText(invitation);
    copyStatus.textContent = "Invitation copied.";
  } catch {
    invitationOutput.focus();
    invitationOutput.select();
    const copied = document.execCommand("copy");
    copyStatus.textContent = copied
      ? "Invitation copied."
      : "Copy failed. Select and copy the invitation manually.";
  }
});

browser.runtime.onMessage.addListener((message) => {
  if (message?.type === "state" && message.state) render(message.state);
});

browser.runtime.sendMessage({ type: "get_state" }).then(render).catch((error) => {
  render({
    phase: "error",
    hostAvailable: false,
    hostError: error.message,
    error: `Could not connect to the RustyTransfer desktop host: ${error.message}`,
  });
});
