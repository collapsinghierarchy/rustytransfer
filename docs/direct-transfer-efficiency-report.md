# Direct transfer efficiency report - 2026-10-03

The efficiency milestone is complete. A small Cargo configuration enables the
pinned AES/POLYVAL crates' runtime ARM acceleration on Linux/macOS AArch64, with
software fallback. Encryption, nonce rules, payload framing, and chunk sizes are
unchanged. Release CI now includes Cargo configuration changes in its path filter.
Desktop and device work is stopped; unvalidated drafts remain in the separate
`feature/desktop-draft` worktree. The submitted Firefox extension and pending
0.1.0 artifacts were not replaced.

The [matched ARM experiment](../benchmarks/results/oracle-20261003-arm-crypto/README.md)
contains 80 measured direct transfers across two reversed-order series, both
directions, and 64/512 MiB fixtures. Every accepted transfer passed full size/hash
checks and direct payload-route verification at both endpoints. Pooled 512 MiB
Oracle CPU cost fell **54.3% when sending** (27.5 to 12.6 CPU seconds/GiB) and
**23.7% when receiving** (63.9 to 48.8). WAN throughput was broadly similar;
there is no general speed-improvement claim. Peak Oracle RSS remained around
30 MiB with overlapping variation.

The first forward 64 MiB series was 3.217% slower, crossing the plan's guardrail.
The additional reversed-order series was 0.376% faster; pooled ten-trial medians
were 0.540% faster. This is an explicit, measured acceptance exception in
[acceptance.json](../benchmarks/results/oracle-20261003-arm-crypto/acceptance.json),
with the original crossing retained and no asserted cause for the variation.

Ten small-file trials per variant showed no meaningful startup regression:
forward 4 KiB medians were 3.0606/3.0604 seconds, and reverse 64 KiB medians
3.9311/3.9346 seconds (baseline/candidate). A reverse 4 KiB warmup lacked receiver
STREAM-frame evidence and was excluded without weakening the route check.
A real interrupted 512 MiB transfer resumed its verified 149.25 MiB prefix,
sent only the remaining 362.75 MiB, and passed complete SHA-256/direct-route
checks on current-source builds. Resume is correctness evidence, not a scored
full-file throughput sample.

The final comparison uses current Rustytransfer builds and official Croc 11.5.4
release binaries, with identical incompressible fixtures, one warmup and five
alternating measured transfers per tool/size. These are **Oracle-to-WSL direct
payload transfers only**; rates include process startup and completion.

| File | Rustytransfer median MiB/s | Croc direct median MiB/s | Croc rate advantage |
| --- | ---: | ---: | ---: |
| 64 MiB | 12.48 | 19.28 | 54.5% |
| 512 MiB | 23.02 | 28.87 | 25.4% |

Croc was faster and used less sender CPU in this pair. It used four TCP data
channels directly into its embedded listener on the Oracle sender; Rustytransfer
used Iroh/QUIC. The comparison does not isolate the cause of the difference or
establish universal transport optimality. Croc's global `--local`, `send
--transport auto`, and receiver `--ip 141.147.1.21:9009` prevent a third-party
payload relay; actual endpoint connection logs were independently rechecked.
See the [final comparison record](../benchmarks/results/oracle-20261003-arm-crypto/final-validation/README.md)
for provenance, CPU/RSS, ranges/MAD, route excerpts, startup/resume records, and
limitations. OCI already allowed TCP 9009-9013. A temporary instance rule allowed
only this client's IPv4 /32; the original INPUT chain was restored exactly, and
no benchmark endpoints or Croc listeners remained.

Validation passed: workspace tests, formatting, wasm check, the existing
Clippy/SARIF identity/report gates with three existing findings, cargo-deny,
44 Python benchmark tests, and 14 Oracle crypto tests with ARM dispatch enabled.
Matched A/B results use frozen source `8f7d8e8`; current `d654b0b` plus the Cargo
configuration is separately validated by the final comparison and resume check.
No physical LAN, native Windows, macOS performance, or physical ARM software
fallback measurement is claimed. Oracle permissions prevented CPU `perf`
sampling; stage diagnostics measure elapsed time.
