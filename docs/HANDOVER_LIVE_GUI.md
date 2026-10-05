# Handover: vom Replay-MVP zur ersten funktionierenden Netzwerk-GUI

## Auftrag des Nutzers

Die nächste Sitzung soll eine tatsächlich nutzbare native GUI implementieren:
eigenes Gerät, reales Netzwerk, erreichbare Rustytransfer-Geräte und gespeicherte
Kontakte anzeigen; einen Empfänger auswählen und eine echte Datei übertragen.
Nicht bei einer weiteren Demo oder einem Architekturplan stehen bleiben.

Repository: `C:\Users\wasil\Documents\GitHub\rustytransfer`.
Zuerst `AGENTS.md`, dieses Handover, `desktop/README.md` und
[`desktop-roadmap.md`](desktop-roadmap.md) lesen. Der Nutzer erlaubt Luna für
mechanische Teilaufgaben. Assets und bestehende native Darstellung wiederverwenden.

## Git- und Prüfstand am 2026-10-05

- Aktueller Branch: `main`.
- Cleanup-Commits: `19f62a0` (Archive/Retirement), `33cf68a` (Dokumentation),
  `18c86fa` (inventarisierte Einmal-Helfer), `d955039` (Handover).
- Der Nutzer hat ausdrücklich „Cleanup und GUI nach main“ bestätigt. Beide sind
  mit Merge-Commit `878af5b663b25dd744ada784290199e6248fd390` zusammengeführt.
  Das Merge-Ergebnis entspricht vollständig dem geprüften Cleanup-Branch.
- Enthaltener GUI-MVP: `a67b23b` (`feature/native-desktop-mvp`).
- Vorheriger veröffentlichter `main`: `5762d37` mit Security-Fix und Push-Guard.
  Die Veröffentlichung des Merge-/Handover-Stands wird nach erneut bestandenem
  Guard in der abschließenden Chatantwort bestätigt. Vor dem Weiterarbeiten
  `git status`, `git log -3 --oneline` und `origin/main` prüfen.
- Draft-Worktree: `C:\Users\wasil\Documents\GitHub\rustytransfer-desktop-draft`,
  Branch `feature/desktop-draft`, Commit `d654b0b`. Nicht anfassen.
- Vier Windows/WSL-Statuszeilen betreffen `crates/firefox-host/src/main.rs`,
  `crates/native/src/direct.rs`, `crates/native/src/lib.rs` und
  `crates/transfer/src/error.rs`. Tatsächliche Diffs waren leer. Nicht resetten
  oder fremde Dateien zur Normalisierung anfassen; erneut echte Diffs prüfen.

Cleanup vor dem Handover: versionierter Dateibestand 21,3 auf 13,0 MB reduziert,
1192 auf 940 Dateien. Dieses zusätzliche Handover ergibt 941 Dateien.
254 Diagnose-/Inventardateien und drei überholte Pläne/Experimentdateien sind
bytegenau archiviert. Alle scored/raw Messreihen und langsamen Samples bleiben
erhalten. Neun Archive/551 Mitglieder verifiziert; 27 Provenienzdateien lassen
sich bytegenau für alte Leser wiederherstellen. Einstieg:
[`benchmarks/results/README.md`](../benchmarks/results/README.md) und
[`benchmarks/archives/README.md`](../benchmarks/archives/README.md).

Bestanden: 57 Workspace-Tests, 30 Desktop-Tests, 49 Python-Tests, Formatierung,
138 lokale Markdown-Links, Archiv-/Wiederherstellungsprüfung. Betterleaks prüfte
die vollständige Cleanup-Historie ohne Findings, Warnungen oder Fehler.
Temporäre Security-Prüfworktrees und zehn Einmal-Helfer sind entfernt; ihre
finalen Commits und redigierten Berichte liegen weiterhin im Hauptrepository.

Für die Live-GUI vom aktuellen bereinigten `main` aus weiterarbeiten, etwa auf
einem neuen `feature/native-desktop-live`-Branch. Die GUI und der Cleanup sind
jetzt Bestandteil dieses gemeinsamen Ausgangsstands.

## Was bereits funktioniert und erhalten bleiben muss

`desktop/` ist ein separates Rust-Workspace: Iced **0.14.0**, wgpu, eigener Shader,
kein WebView. Das Warping und die große Linse sind dem Nutzer besonders wichtig.
Die Bild- und Geometrieschichten nicht durch eine gewöhnliche Geräteliste ersetzen.

- Drag löst keinen Klick aus; die Auswahl erfolgt beim Loslassen unter der
  Bewegungsschwelle. Überschreiten bleibt auch nach Zurückziehen ein Drag.
- Ein ausgewähltes Gerät wird in die Linse eingesogen und folgt ihr; Verbindungen
  bleiben im Feld. Hover hebt den ganzen getroffenen Verbindungspfad hervor.
- Aktionen sind objektabhängig innerhalb der Linse. Oben rechts nur Sichtfilter.
- Das eigene Gerät ist groß oben links. Andere Peers rechts; eindeutig lokale
  Peers darunter links. Direct oder `nearby` allein ist kein LAN-Nachweis.
- Tastaturbedienung, Reduced Motion, Hit-Testing unter Verformung und gecachte
  Feldgeometrie bleiben bestehen. Bestehende 30 Tests weiter verwenden.

Der aktuelle Standardstart lädt ausschließlich Replay-Fixtures. Es gibt keinen
Live-Backendadapter, keine Geräteerkennung und keine echte Dateiübertragung in
der GUI. `desktop/src/actions.rs` enthält Demo-/Replay-Aktionen. Diese im Live-Modus
durch echte Aktionen ersetzen; Replay nur als expliziter Test-/Demo-Modus behalten.

## Reale Bausteine und Integrationslücken

| Pfad | Wiederverwenden / beachten |
| --- | --- |
| `desktop/src/main.rs`, `model.rs` | Iced-App, Ereignisse und Scene; Echtzeitereignisse auf dieses Modell adaptieren |
| `desktop/src/field.rs`, `gpu.rs`, `field.wgsl` | Geometrie, Auswahl und Warping unverändert als visuelle Grundlage |
| `src/cli/contacts.rs` | Privater CLI-Kontaktspeicher; noch keine gemeinsam nutzbare Service-API |
| `crates/native/src/transport/iroh.rs` | Persistente Identität, Endpoint-Aufbau, Direct-Verbindungen und echte Pfadbeobachtung |
| `crates/native/src/direct.rs` | DirectInvite, maximal vier Versuche, Retry-Klassifikation und Backoff |
| `crates/transfer/src/` | Produktions-Senden/Empfangen, Fortschritt, Resume, Verifikation und Finalisierung |
| `crates/firefox-host/src/main.rs` | Vorhandene Tokio-Worker, Fortschritt, Abbruch und Warten auf Worker-Ende als Referenz; Browserprozess nicht als GUI-Backend missbrauchen |

Kontakte nutzen `contacts.json` neben `default_identity_path()`, Version 1 mit
Name-zu-EndpointId-Mapping. Unter Windows liegt die Identität unter
`%LOCALAPPDATA%\rustytransfer\iroh-identity.key`, unter Linux im XDG-Datenverzeichnis.
Die CLI entfernt beim Speichern eines Invites dessen Transfer-Token. Speicher/API
in einen gemeinsam verwendbaren nativen Baustein herausziehen und bestehende
Dateien/CLI-Semantik erhalten, statt einen zweiten Kontaktbestand anzulegen.

**Wichtig:** Eine EndpointId im Kontaktbuch ist keine Transferautorisierung und
noch kein empfangsbereites Gerät. Im aktuellen Direct-Flow lauscht der Sender;
der Empfänger verbindet sich mit einem pro Datei erzeugten Invite-Token. Dafür
existieren `bind_direct_sender`, `accept_direct`, `connect_direct`,
`send_file_direct` und `receive_file_direct`. Der Direct-Empfänger nutzt aktuell
eine frische Endpoint-Identität. Persistente Geräteidentität und diese temporäre
Transferidentität deshalb nicht ungeprüft gleichsetzen.

Für „Kontakt wählen und senden“ fehlt ein kleiner authentifizierter Angebots- und
Zustimmungsweg zur Empfänger-GUI. Diesen getrennt vom unveränderten Dateiprotokoll
entwerfen und implementieren: Angebot anzeigen, annehmen/ablehnen, Ziel wählen,
dann vorhandenen Transfer starten. Keine beliebigen Dateien automatisch annehmen
und keine Namens-/mDNS-Ankündigung als Vertrauensnachweis verwenden.

## Implementierungsfolge und Abnahme

1. **Live-Grundlage:** eigener echter Endpoint/Gerätename, vorhandene Kontakte und
   lokal ermittelte Netzwerkdaten. Asynchroner Backend-Lebenszyklus mit Events zur
   Iced-App; keine blockierenden Discovery-/Datei-/Transferoperationen im UI-Thread.
   Noch unbekannte Werte ausdrücklich unbekannt lassen. App-Shutdown und Abbruch
   müssen Tasks/Endpoints geordnet beenden.
2. **Erreichbare Ziele:** vorhandene Iroh-1.2.0-Funktionen und geeignete LAN-Service-
   Discovery prüfen; bei neuen Abhängigkeiten aktuelle Primärdokumentation lesen.
   Tatsächliche Rustytransfer-Ankündigungen, Identitätsprüfung und Verfall umsetzen.
   Kontakte, entdeckte Peers, bestätigte Identität und beobachtete Erreichbarkeit
   getrennt modellieren; keine synthetischen Peer-Knoten im Live-Modus.
3. **Echter Transfer:** Datei am Gerät/Kontakt auswählen, Angebot/Empfängerzustimmung,
   Zielpfad und echte Übertragung mit Fortschritt, Fehlern und Abbruch. Invite-
   Import als kompatiblen Einstieg unterstützen, aber nicht als Ersatz für den
   angeforderten Geräte-/Kontaktworkflow stehen bleiben.
4. **Validieren:** zwei reale App-Instanzen, möglichst zwei Rechner, in beide
   Richtungen übertragen; Größe/SHA-256 extern prüfen. Empfang ablehnen, Abbruch,
   Wiederaufnahme, Zielkonflikt, verschwundener Peer und unerreichbarer Kontakt
   testen. Gemessene Direkt-/Relay-Pfade darstellen; bestehende CLI/Firefox-
   Regressionen weiter bestehen lassen. Klare Grenze bei nicht getesteter WAN-
   Discovery oder anderen Betriebssystemen nennen.

„Mein Netzwerk“ darf nur belegbare Daten zeigen: eigenes Gerät/Interfaces,
entdeckte unterstützte Peers, Kontakte und beobachtete Verbindungen. Andere LAN-
Geräte sind nicht automatisch Rustytransfer-Empfänger. Router, IP, RTT, Durchsatz,
Status und Vertrauen nicht aus der Darstellung erfinden. Sichtbarkeit aller
beliebigen LAN-Geräte gegebenenfalls mit dem Nutzer abgrenzen, während die
Rustytransfer-Geräte-/Kontaktintegration bereits voranschreitet.

Windows ist der primäre echte GUI-/LAN-Testfall. Unter WSL läuft der Backendcode
im WSL-Netzwerk und hat Linux-Identität/Speicherpfade; das nicht als beobachtetes
Windows-Hostnetz ausgeben. Native Windows-Ausführung und Discovery über relevante
Interfaces prüfen, keine ungefragten Firewall-/Routeränderungen durchführen.

## Prüfungen, Betrieb und Veröffentlichung

```sh
cargo test --workspace --locked
cargo test --manifest-path desktop/Cargo.toml --locked
cargo fmt --all -- --check
cargo fmt --manifest-path desktop/Cargo.toml -- --check
python3 -m unittest discover -s benchmarks -p test_performance.py
python3 -m unittest discover -s .github/scripts/tests
python3 benchmarks/archives/verify.py
```

Für Desktop unter WSL ist `/home/wasilij/rustytransfer-desktop-target` ein vorhandener
Cache. Windows-/WSLg-Start, Clippy und Smoke-Optionen stehen in `desktop/README.md`.
Push-Guard-Tests mit dem echten Scanner: `scripts/PUSH_GUARD.md`.

Produktions-Defaults, Kryptografie, Wire-Protokoll, Resume und dauerhafte
Finalisierung beibehalten. Sicherheits-/Clippy-CI, Releases, Firefox-Artefakte,
Assets und Draft-Worktree erhalten. Kleine prüfbare Commits; ohne ausdrücklichen
Auftrag nicht veröffentlichen. Vor jedem Push den unveränderten Betterleaks-Guard
auf den vollständigen Objekt-IDs ausführen. Keine Unterdrückung, kein Bypass.
Berichte lokal/redigiert halten; keine privaten Keys oder vollständigen Invites
in Replay-Events, Screenshots, Logs oder Commits schreiben.

Die feste Performance-Baseline ist
`47c338c19e9751ec0a47414f8d323e77fc974b08`; CI bleibt Rustytransfer-only und
report-only. Nicht eigenständig die Baseline wechseln oder Croc zurück einbauen.
