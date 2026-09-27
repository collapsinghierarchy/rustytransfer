const NATIVE_HOST = "org.rustytransfer.host";
const HEARTBEAT_MS = 10_000;
const STATUS_INTERVAL_MS = 15_000;
const KEEPALIVE_KEY = "background_keepalive_pulse";

let nativePort = null;
let nextCommandId = 1;
let heartbeatTimer = null;
let lastStatusAt = 0;
let latestStatusId = null;
let activeTransferId = null;
let activeCancelId = null;
let acceptsUntrackedTransferEvents = false;

const state = {
  phase: "idle",
  direction: null,
  invite: null,
  fileName: null,
  fileSize: null,
  done: 0,
  total: 0,
  error: null,
  hostAvailable: null,
  hostError: null,
};

function snapshot() {
  return { ...state };
}

function broadcastState() {
  // The popup may be closed. The background keeps the native connection alive
  // independently and the next popup reads the latest in-memory snapshot.
  browser.runtime.sendMessage({ type: "state", state: snapshot() }).catch(() => {});
}

function isActive() {
  return ["starting", "busy", "waiting", "transferring", "cancelling"].includes(state.phase);
}

function updateHeartbeat() {
  if (isActive() && nativePort && heartbeatTimer === null) {
    heartbeatTimer = setInterval(() => {
      if (!nativePort || !isActive()) return;

      // Firefox MV3 event pages can idle while a native port is open. A changing
      // session-only marker generates storage events during a long transfer;
      // it contains no invite, token, or transfer data.
      browser.storage.session
        .set({ [KEEPALIVE_KEY]: Date.now() })
        .catch(() => {});

      if (Date.now() - lastStatusAt >= STATUS_INTERVAL_MS) requestStatus();
    }, HEARTBEAT_MS);
  } else if ((!isActive() || !nativePort) && heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function requestStatus() {
  const id = nextCommandId++;
  latestStatusId = id;
  lastStatusAt = Date.now();
  return postNative({ id, type: "status" });
}

function clearOperationIds() {
  activeTransferId = null;
  activeCancelId = null;
  acceptsUntrackedTransferEvents = false;
}

function isCurrentTransferEvent(event, allowCancelId = false) {
  if (activeTransferId !== null && event.id === activeTransferId) return true;
  if (allowCancelId && activeCancelId !== null && event.id === activeCancelId) return true;
  return activeTransferId === null && acceptsUntrackedTransferEvents;
}

function setState(changes) {
  Object.assign(state, changes);
  updateHeartbeat();
  broadcastState();
}

function hostUnavailable(error) {
  const details = error instanceof Error ? error.message : String(error || "Unknown error");
  const hadActiveTransfer = isActive();
  nativePort = null;
  clearOperationIds();
  setState({
    hostAvailable: false,
    hostError: details,
    error: `Could not connect to ${NATIVE_HOST}: ${details}`,
    phase: hadActiveTransfer ? "error" : state.phase,
  });
}

function connectNative() {
  if (nativePort) return true;

  try {
    const port = browser.runtime.connectNative(NATIVE_HOST);
    nativePort = port;

    port.onMessage.addListener(handleNativeMessage);
    port.onDisconnect.addListener(() => {
      const reason = browser.runtime.lastError?.message || "The native host disconnected.";
      const hadActiveTransfer = isActive();
      if (nativePort === port) nativePort = null;
      clearOperationIds();
      setState({
        hostAvailable: false,
        hostError: reason,
        error: `Connection to ${NATIVE_HOST} was lost: ${reason}`,
        phase: hadActiveTransfer ? "error" : state.phase,
      });
    });

    setState({ hostAvailable: true, hostError: null });
    return true;
  } catch (error) {
    hostUnavailable(error);
    return false;
  }
}

function postNative(command) {
  if (!nativePort && !connectNative()) return false;

  try {
    nativePort.postMessage(command);
    return true;
  } catch (error) {
    hostUnavailable(error);
    return false;
  }
}

function handleNativeMessage(event) {
  if (!event || typeof event.type !== "string" || !Number.isInteger(event.id)) return;

  switch (event.type) {
    case "invite":
      if (!isCurrentTransferEvent(event)) return;
      setState({
        phase: "waiting",
        direction: "send",
        invite: typeof event.invite === "string" ? event.invite : null,
        fileName: typeof event.file_name === "string" ? event.file_name : null,
        fileSize: Number.isFinite(event.file_size) ? event.file_size : null,
        done: 0,
        total: 0,
        error: null,
        hostAvailable: true,
        hostError: null,
      });
      break;
    case "progress":
      if (!isCurrentTransferEvent(event)) return;
      setState({
        phase: "transferring",
        direction: event.direction || state.direction,
        done: Number.isFinite(event.done) ? event.done : state.done,
        total: Number.isFinite(event.total) ? event.total : state.total,
        error: null,
        hostAvailable: true,
        hostError: null,
      });
      break;
    case "complete":
      if (!isCurrentTransferEvent(event)) return;
      clearOperationIds();
      setState({
        phase: "complete",
        direction: event.direction || state.direction,
        fileName: typeof event.file_name === "string" ? event.file_name : state.fileName,
        invite: null,
        error: null,
        hostAvailable: true,
        hostError: null,
      });
      break;
    case "error":
      if (!isCurrentTransferEvent(event, true)) return;
      clearOperationIds();
      setState({
        phase: "error",
        error: typeof event.message === "string" ? event.message : "The transfer failed.",
        invite: null,
      });
      break;
    case "cancelled":
      if (!isCurrentTransferEvent(event, true)) return;
      clearOperationIds();
      setState({ phase: "cancelled", invite: null, error: null });
      break;
    case "status":
      if (event.id !== latestStatusId) return;
      latestStatusId = null;
      if (event.state === "busy") {
        if (activeTransferId === null) acceptsUntrackedTransferEvents = true;
        setState({
          phase: isActive() ? (state.phase === "starting" ? "busy" : state.phase) : "busy",
          direction: event.direction || state.direction,
          invite: event.direction === "send" && typeof event.invite === "string"
            ? event.invite : null,
          fileName: typeof event.file_name === "string" ? event.file_name : state.fileName,
          error: null,
          hostAvailable: true,
          hostError: null,
        });
      } else if (event.state === "idle" && isActive()) {
        clearOperationIds();
        setState({
          phase: "idle",
          direction: null,
          invite: null,
          fileName: null,
          fileSize: null,
          done: 0,
          total: 0,
          error: null,
          hostAvailable: true,
          hostError: null,
        });
      }
      break;
    default:
      break;
  }
}

function beginOperation(command) {
  const id = nextCommandId++;
  latestStatusId = null;
  if (command.type === "cancel") {
    activeCancelId = id;
  } else {
    activeTransferId = id;
    activeCancelId = null;
    acceptsUntrackedTransferEvents = false;
  }
  setState({
    phase: command.type === "cancel" ? "cancelling" : "starting",
    direction: command.type === "start_send" ? "send" : command.type === "start_receive" ? "receive" : state.direction,
    invite: command.type === "start_receive" ? null : state.invite,
    done: 0,
    total: 0,
    error: null,
  });
  if (!postNative({ id, ...command })) return snapshot();
  return snapshot();
}

browser.runtime.onMessage.addListener((message) => {
  if (!message || typeof message.type !== "string") return undefined;

  if (message.type === "get_state") {
    if (connectNative()) requestStatus();
    return Promise.resolve(snapshot());
  }

  if (message.type === "start_send") {
    if (isActive()) return Promise.resolve(snapshot());
    return Promise.resolve(beginOperation({ type: "start_send" }));
  }

  if (message.type === "start_receive") {
    if (isActive()) return Promise.resolve(snapshot());
    const invite = typeof message.invite === "string" ? message.invite.trim() : "";
    if (!invite) return Promise.resolve({ ...snapshot(), error: "Paste an invitation first." });
    return Promise.resolve(beginOperation({ type: "start_receive", invite }));
  }

  if (message.type === "cancel") {
    if (!isActive() || state.phase === "cancelling") return Promise.resolve(snapshot());
    return Promise.resolve(beginOperation({ type: "cancel" }));
  }

  return undefined;
});

// Listening to storage session changes is part of the Firefox MV3 keepalive
// workaround above. The value is ephemeral and contains no transfer secret.
browser.storage.session.onChanged.addListener(() => {});
