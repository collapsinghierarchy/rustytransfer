# Owned chunk candidate (not applied)

Replace the sender's reusable zero-filled `buffer` plus `read`/`extend_from_slice` path with one owned plaintext allocation per protocol chunk. Keep all changes after the diagnostic snapshot is frozen.

```rust
let allocation_started = payload_profile
    .as_ref()
    .map(crate::PayloadProfile::start_stage);
let capacity = read_limit.checked_add(GCM_TAG_LEN).ok_or_else(|| {
    context.error(TransferErrorKind::Internal("plaintext chunk capacity overflow"))
})?;
let mut plaintext = Vec::new();
plaintext.try_reserve_exact(capacity).map_err(|_source| {
    context.error(TransferErrorKind::Config("plaintext chunk allocation failed"))
})?;
if let (Some(profile), Some(started)) = (&mut payload_profile, allocation_started) {
    profile.record_sender_allocation_copy(started);
    profile.record_stage(PayloadStage::AllocationCopyEncrypt, started);
}

let read_limit_u64 = u64::try_from(read_limit)
    .map_err(|_error| context.error(TransferErrorKind::Internal("source read size is invalid")))?;
let read_started = payload_profile
    .as_ref()
    .map(crate::PayloadProfile::start_stage);
let mut limited_source = (&mut source).take(read_limit_u64);
let bytes_read = timeout(CHUNK_TIMEOUT, limited_source.read_buf(&mut plaintext))
    .await
    .map_err(|_source| context.error(TransferErrorKind::Timeout))?
    .map_err(|error| context.error(TransferErrorKind::SourceIo(error)))?;
if let (Some(profile), Some(started)) = (&mut payload_profile, read_started) {
    profile.record_stage(PayloadStage::SourceRead, started);
}
if bytes_read == 0 {
    return Err(context.error(TransferErrorKind::SourceIo(io::Error::new(
        io::ErrorKind::UnexpectedEof,
        format!(
            "source ended after {} of {} bytes",
            sender.bytes_sent(),
            remaining_len
        ),
    ))));
}
```

Use `plaintext` directly in the existing `sender.step("SMT", Some(plaintext))`; no extra copy is needed. Keep the existing encryption timer around only `sender.step`, and keep the legacy aggregate timer around allocation plus encryption so older profile consumers retain the same field. Remove the initial reusable buffer allocation. Replace its EOF probe with `let mut extra_buffer = [0_u8; 1];` and the existing timed `source.read(&mut extra_buffer)` check.

After the bounded read and its `SourceRead` timing have finished, start `encrypt_started` immediately before `sender.step`. On success, record `sender_encrypt_seconds` first, then record the legacy `AllocationCopyEncrypt` stage using that same timer. The legacy field is accumulated twice per chunk (allocation and encryption spans); the source-read timer stays disjoint from both. This preserves the existing stage-sum invariant and guarantees the two sender substage totals fit inside the legacy aggregate.

`AsyncReadExt::read_buf` writes safely into `Vec` spare capacity and updates its length. `Take<&mut R>` caps each read at the current remaining payload, while a short read is sent as a smaller encrypted message and the next loop iteration continues. The one-byte probe still rejects inputs longer than the advertised length. No unsafe code or uninitialized-byte access is needed.

Existing regression coverage to retain: `early_source_eof_is_reported` checks truncation, `interrupted_transfer_resumes_saved_prefix_on_next_invocation` checks resume, and the focused transfer profile test checks byte/chunk accounting. Add a short-reading `AsyncRead + AsyncSeek` test wrapper that caps each poll to a few bytes and an overlong-source case (actual input longer than `file_len`) to verify short reads and the EOF probe. Benchmark resume and payload correctness at 256, 512, and 1024 KiB chunk sizes after root's snapshot build.
