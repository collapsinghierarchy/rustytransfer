# Firefox extension

This is a Firefox desktop WebExtension for the `org.rustytransfer.host` native
messaging application.

## Install in Firefox

1. Build and register the RustyTransfer native host for Firefox, or run the
   installer from a GitHub release archive on the same OS as Firefox.
2. Install the RustyTransfer add-on from
   [Mozilla Add-ons](https://addons.mozilla.org/firefox/addon/rustytransfer/).
3. Open the RustyTransfer toolbar button to send or receive a file.

For local development, open `about:debugging#/runtime/this-firefox`, choose
**Load Temporary Add-on**, and select this directory's `manifest.json`.
The desktop host remains a separate install.

The extension ID is `rustytransfer@collapsinghierarchy`; the Firefox native host
manifest must allow that ID. The extension keeps transfer state and invitation
text in background and popup memory only. It does not write invitation secrets
to browser storage.

The background script keeps a native messaging port and requests `status` on
popup open and periodically during a transfer. A nonsecret pulse in Firefox's
session-only storage keeps the MV3 event page active during long transfers;
invitation text remains in memory only and is never written to browser storage.
