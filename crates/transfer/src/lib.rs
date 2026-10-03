use anyhow::{Result, anyhow};
use std::{io, path::Path, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    time::{Instant, timeout},
};

pub mod config;
pub mod error;
pub use config::TransferConfig;
mod metrics;
mod output;
mod session;
pub use session::{receive_file, receive_file_direct, send_file, send_file_direct};
#[cfg(test)]
use session::{receive_file_with_profile, send_file_with_profile};
mod transport_api;
pub(crate) use metrics::PayloadStage;
pub(crate) use metrics::payload_profile_enabled_from_env;
pub use metrics::{PayloadProfile, TransferMetrics};
pub use transport_api::{PathObservation, TransferTransport};

use crate::error::{CompletionState, Phase, TransferError, TransferErrorKind};
use rustytransfer_protocol::{
    fsm::{Role, State, StepError},
    receiver::ReceiverFsm,
    sender::SenderFsm,
};

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(90);
const CHUNK_TIMEOUT: Duration = Duration::from_secs(90);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(90);
const FIN_ACK: &[u8] = b"FIN_ACK";
const GCM_TAG_LEN: usize = 16;
pub const MAX_CHUNK_SIZE: usize = 1024 * 1024;
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(5);
#[derive(Clone, Copy)]
struct TransferContext {
    phase: Phase,
    bytes_transferred: u64,
    completion: CompletionState,
    payload_profile_enabled: bool,
}

impl TransferContext {
    const fn new() -> Self {
        Self {
            phase: Phase::Input,
            bytes_transferred: 0,
            completion: CompletionState::NotStarted,
            payload_profile_enabled: false,
        }
    }

    fn error(self, kind: TransferErrorKind) -> TransferError {
        TransferError::new(kind, self.phase, self.bytes_transferred, self.completion)
    }

    fn protocol(self, error: StepError) -> TransferError {
        self.error(TransferErrorKind::Protocol(error))
    }

    fn transport(self, error: anyhow::Error) -> TransferError {
        self.error(TransferErrorKind::Transport(error))
    }
}

async fn receive_with_timeout<T: TransferTransport>(
    transport: &mut T,
    duration: Duration,
    context: TransferContext,
) -> std::result::Result<Vec<u8>, TransferError> {
    timeout(duration, transport.receive_message())
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.transport(error))
}

async fn send_with_timeout<T: TransferTransport>(
    transport: &mut T,
    data: Vec<u8>,
    duration: Duration,
    context: TransferContext,
) -> std::result::Result<(), TransferError> {
    timeout(duration, transport.send_message(data))
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.transport(error))
}

async fn abort_transport<T: TransferTransport>(
    transport: &mut T,
) -> std::result::Result<(), String> {
    match timeout(CLEANUP_TIMEOUT, transport.abort()).await {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => Err(format!("transport abort failed: {error}")),
        Err(_) => Err("transport abort timed out".to_owned()),
    }
}

fn required_output(label: &str, output: Option<Vec<u8>>) -> Result<Vec<u8>> {
    output.ok_or_else(|| anyhow!("{label}: expected an output message"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use anyhow::{Context as AnyhowContext, ensure};
    use std::{
        io::Cursor,
        path::{Path, PathBuf},
        sync::atomic::{AtomicU64, Ordering},
    };
    use tokio::sync::{mpsc, watch};

    const TEST_TIMEOUT: Duration = Duration::from_secs(15);
    static NEXT_OUTPUT: AtomicU64 = AtomicU64::new(0);

    #[derive(Clone, Copy)]
    enum PayloadChange {
        Corrupt,
        Truncate,
        Abort,
    }

    #[derive(Clone, Copy)]
    enum SendFault {
        None,
        ChangeThirdMessage(PayloadChange),
        FailOnMessage(usize),
    }

    struct MemoryTransport {
        tx: Option<mpsc::Sender<Vec<u8>>>,
        rx: mpsc::Receiver<Vec<u8>>,
        peer_closed: watch::Receiver<bool>,
        local_closed: watch::Sender<bool>,
        receive_delay: Duration,
        fin_send_delay: Duration,
        fin_ack_receive_delay: Duration,
        send_fault: SendFault,
        sent_messages: usize,
    }

    impl MemoryTransport {
        fn pair(
            capacity: usize,
            receive_delay: Duration,
            fin_send_delay: Duration,
            fin_ack_receive_delay: Duration,
        ) -> (Self, Self) {
            let (a_to_b_tx, a_to_b_rx) = mpsc::channel(capacity);
            let (b_to_a_tx, b_to_a_rx) = mpsc::channel(capacity);
            let (a_closed_tx, a_closed_rx) = watch::channel(false);
            let (b_closed_tx, b_closed_rx) = watch::channel(false);
            (
                Self {
                    tx: Some(a_to_b_tx),
                    rx: b_to_a_rx,
                    peer_closed: b_closed_rx,
                    local_closed: a_closed_tx,
                    receive_delay: Duration::ZERO,
                    fin_send_delay: Duration::ZERO,
                    fin_ack_receive_delay: Duration::ZERO,
                    send_fault: SendFault::None,
                    sent_messages: 0,
                },
                Self {
                    tx: Some(b_to_a_tx),
                    rx: a_to_b_rx,
                    peer_closed: a_closed_rx,
                    local_closed: b_closed_tx,
                    receive_delay,
                    fin_send_delay,
                    fin_ack_receive_delay,
                    send_fault: SendFault::None,
                    sent_messages: 0,
                },
            )
        }

        fn close_send_half(&mut self) {
            self.tx.take();
        }
    }

    #[async_trait::async_trait]
    impl TransferTransport for MemoryTransport {
        async fn send_message(&mut self, mut data: Vec<u8>) -> Result<()> {
            self.sent_messages += 1;
            match self.send_fault {
                SendFault::None => {}
                SendFault::ChangeThirdMessage(change) if self.sent_messages == 3 => match change {
                    PayloadChange::Corrupt => {
                        let last = data
                            .last_mut()
                            .ok_or_else(|| anyhow!("test ciphertext was empty"))?;
                        *last ^= 0x80;
                    }
                    PayloadChange::Truncate => data.truncate(data.len().saturating_sub(1)),
                    PayloadChange::Abort => return Err(anyhow!("injected sender abort")),
                },
                SendFault::ChangeThirdMessage(_) => {}
                SendFault::FailOnMessage(fail_on) if self.sent_messages == fail_on => {
                    return Err(anyhow!("injected connection interruption"));
                }
                SendFault::FailOnMessage(_) => {}
            }
            if data == b"FIN" && !self.fin_send_delay.is_zero() {
                tokio::time::sleep(self.fin_send_delay).await;
            }
            self.tx
                .as_ref()
                .ok_or_else(|| anyhow!("local send half is closed"))?
                .send(data)
                .await
                .map_err(|error| anyhow!("peer stopped receiving: {error}"))
        }

        async fn receive_message(&mut self) -> Result<Vec<u8>> {
            let message = self
                .rx
                .recv()
                .await
                .ok_or_else(|| anyhow!("peer closed its sending half"))?;
            if message == b"FIN_ACK" && !self.fin_ack_receive_delay.is_zero() {
                tokio::time::sleep(self.fin_ack_receive_delay).await;
            }
            if !self.receive_delay.is_zero() {
                tokio::time::sleep(self.receive_delay).await;
            }
            Ok(message)
        }

        async fn close_send_half(&mut self) -> Result<()> {
            self.close_send_half();
            Ok(())
        }

        async fn finish_sending(&mut self) -> Result<()> {
            self.close_send_half();
            Ok(())
        }

        async fn finish_receiving(&mut self) -> Result<()> {
            while self.rx.recv().await.is_some() {}
            Ok(())
        }

        async fn wait_for_peer_close(&mut self) -> Result<()> {
            while !*self.peer_closed.borrow() {
                self.peer_closed
                    .changed()
                    .await
                    .context("peer close watch channel ended")?;
            }
            Ok(())
        }

        async fn close_transport(&mut self) -> Result<()> {
            self.local_closed.send_replace(true);
            Ok(())
        }
    }

    fn output_path() -> PathBuf {
        std::env::temp_dir().join(format!(
            "rustytransfer-transfer-{}-{}.bin",
            std::process::id(),
            NEXT_OUTPUT.fetch_add(1, Ordering::Relaxed)
        ))
    }

    fn partial_path(output: &Path) -> Result<PathBuf> {
        let name = output
            .file_name()
            .ok_or_else(|| anyhow!("test output has no file name"))?;
        let mut part_name = std::ffi::OsString::from(".");
        part_name.push(name);
        part_name.push(".rustytransfer.part");
        Ok(output.with_file_name(part_name))
    }

    async fn transfer_bytes(
        data: Vec<u8>,
        chunk_size: usize,
        receive_delay: Duration,
        fin_send_delay: Duration,
        fin_ack_receive_delay: Duration,
    ) -> Result<Vec<u8>> {
        let (sender, receiver) =
            MemoryTransport::pair(1, receive_delay, fin_send_delay, fin_ack_receive_delay);
        let expected_len = u64::try_from(data.len()).context("test input length exceeds u64")?;
        let output = output_path();
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(data),
                expected_len,
                b"ABCDE",
                TransferConfig { chunk_size },
                |_, _| {},
            )
            .await
        };
        let output_for_receiver = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &output_for_receiver, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("in-memory transfer timed out")?;
        let _sender_metrics = sender_result?;
        let _receiver_metrics = receiver_result?;
        let bytes = tokio::fs::read(&output)
            .await
            .with_context(|| format!("failed to read test output {}", output.display()))?;
        tokio::fs::remove_file(&output).await?;
        Ok(bytes)
    }

    async fn transfer_metrics_with_profile(
        data: Vec<u8>,
        chunk_size: usize,
        profile_enabled: bool,
    ) -> Result<(TransferMetrics, TransferMetrics)> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let expected_len = u64::try_from(data.len()).context("test input length exceeds u64")?;
        let output = output_path();
        let sender_future = async move {
            let mut sender = sender;
            send_file_with_profile(
                &mut sender,
                Cursor::new(data),
                expected_len,
                b"ABCDE",
                TransferConfig { chunk_size },
                |_, _| {},
                profile_enabled,
            )
            .await
        };
        let output_for_receiver = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file_with_profile(
                &mut receiver,
                b"ABCDE",
                &output_for_receiver,
                |_, _| {},
                profile_enabled,
            )
            .await
        };
        let (sender_metrics, receiver_metrics) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("in-memory profiled transfer timed out")?;
        tokio::fs::remove_file(&output).await?;
        Ok((sender_metrics?, receiver_metrics?))
    }

    #[tokio::test]
    async fn payload_profile_is_opt_in_and_counts_payload_chunks() -> Result<()> {
        let (sender_metrics, receiver_metrics) =
            transfer_metrics_with_profile(vec![7; 9], 4, false).await?;
        assert!(sender_metrics.payload_profile.is_none());
        assert!(receiver_metrics.payload_profile.is_none());

        for length in [0, 8, 9] {
            let (sender_metrics, receiver_metrics) =
                transfer_metrics_with_profile(vec![7; length], 4, true).await?;
            let expected_chunks = u64::try_from(length.div_ceil(4))?;
            for (metrics, profile) in [(sender_metrics, "sender"), (receiver_metrics, "receiver")] {
                let profile_data = metrics
                    .payload_profile
                    .as_ref()
                    .with_context(|| format!("{profile} profile was not enabled"))?;
                assert_eq!(profile_data.chunk_count, expected_chunks);
                assert_eq!(profile_data.payload_bytes, u64::try_from(length)?);
                let stage_sum = profile_data.source_read_seconds
                    + profile_data.allocation_copy_encrypt_seconds
                    + profile_data.send_wait_seconds
                    + profile_data.receive_wait_seconds
                    + profile_data.decrypt_seconds
                    + profile_data.destination_write_seconds;
                assert!(stage_sum <= metrics.payload_seconds + 1e-6);
                assert!(
                    profile_data.sender_allocation_copy_seconds
                        + profile_data.sender_encrypt_seconds
                        <= profile_data.allocation_copy_encrypt_seconds + 1e-6
                );
            }
        }
        Ok(())
    }

    async fn transfer_bytes_direct(data: Vec<u8>, chunk_size: usize) -> Result<Vec<u8>> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let expected_len = u64::try_from(data.len()).context("test input length exceeds u64")?;
        let output = output_path();
        let token = [0x5a; 16];
        let sender_future = async move {
            let mut sender = sender;
            send_file_direct(
                &mut sender,
                Cursor::new(data),
                expected_len,
                &token,
                TransferConfig { chunk_size },
                |_, _| {},
            )
            .await
        };
        let output_for_receiver = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file_direct(&mut receiver, &token, &output_for_receiver, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("direct in-memory transfer timed out")?;
        let _sender_metrics = sender_result?;
        let _receiver_metrics = receiver_result?;
        let bytes = tokio::fs::read(&output)
            .await
            .with_context(|| format!("failed to read test output {}", output.display()))?;
        tokio::fs::remove_file(&output).await?;
        Ok(bytes)
    }

    #[tokio::test]
    async fn memory_transfer_covers_empty_and_chunk_boundaries() -> Result<()> {
        const CHUNK: usize = 4096;
        for size in [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, CHUNK * 3 + 19] {
            let input: Vec<u8> = (0..size)
                .map(|index| u8::try_from(index % 251))
                .collect::<std::result::Result<_, _>>()
                .context("test byte pattern exceeds u8")?;
            let output = transfer_bytes(
                input.clone(),
                CHUNK,
                Duration::ZERO,
                Duration::ZERO,
                Duration::ZERO,
            )
            .await?;
            ensure!(output == input, "content mismatch for {size}-byte transfer");
        }
        Ok(())
    }

    #[tokio::test]
    async fn direct_transfer_reuses_kem_payload_and_commit_flow() -> Result<()> {
        const CHUNK: usize = 1024;
        for size in [0, 1, CHUNK, CHUNK + 1, CHUNK * 2 + 7] {
            let input: Vec<u8> = (0..size)
                .map(|index| u8::try_from(index % 251))
                .collect::<std::result::Result<_, _>>()
                .context("test byte pattern exceeds u8")?;
            let output = transfer_bytes_direct(input.clone(), CHUNK).await?;
            ensure!(
                output == input,
                "direct content mismatch for {size}-byte transfer"
            );
        }
        Ok(())
    }

    #[tokio::test]
    async fn direct_transfer_rejects_wrong_token_before_creating_output() -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let output = output_path();
        let sender_token = [0x11; 16];
        let receiver_token = [0x22; 16];
        let sender_future = async move {
            let mut sender = sender;
            send_file_direct(
                &mut sender,
                Cursor::new(vec![1, 2, 3]),
                3,
                &sender_token,
                TransferConfig::default(),
                |_, _| {},
            )
            .await
        };
        let receiver_output = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file_direct(&mut receiver, &receiver_token, &receiver_output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("wrong direct-token case timed out")?;
        ensure!(
            sender_result.is_err(),
            "sender accepted the wrong direct token"
        );
        ensure!(
            receiver_result.is_err(),
            "receiver accepted the wrong direct token"
        );
        ensure!(
            !output.exists(),
            "wrong direct token created an output file"
        );
        Ok(())
    }

    #[tokio::test]
    async fn wrong_password_returns_an_error() -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(vec![42]),
                1,
                b"ABCDE",
                TransferConfig::default(),
                |_, _| {},
            )
            .await
        };
        let output = output_path();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"WRONG", &output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("wrong-password case timed out")?;
        ensure!(sender_result.is_err(), "sender accepted the wrong password");
        ensure!(
            receiver_result.is_err(),
            "receiver accepted the wrong password"
        );
        Ok(())
    }

    async fn changed_ciphertext_is_rejected(change: PayloadChange) -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let output = output_path();
        let sender_future = async move {
            let mut sender = sender;
            sender.send_fault = SendFault::ChangeThirdMessage(change);
            send_file(
                &mut sender,
                Cursor::new(vec![1, 2, 3, 4, 5, 6, 7, 8]),
                8,
                b"ABCDE",
                TransferConfig { chunk_size: 8 },
                |_, _| {},
            )
            .await
        };
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("ciphertext fault case timed out")?;
        ensure!(sender_result.is_err(), "sender unexpectedly completed");
        ensure!(
            receiver_result.is_err(),
            "receiver accepted invalid ciphertext"
        );
        Ok(())
    }

    #[tokio::test]
    async fn corrupted_and_truncated_ciphertext_are_rejected() -> Result<()> {
        changed_ciphertext_is_rejected(PayloadChange::Corrupt).await?;
        changed_ciphertext_is_rejected(PayloadChange::Truncate).await?;
        changed_ciphertext_is_rejected(PayloadChange::Abort).await?;
        Ok(())
    }

    #[tokio::test]
    async fn early_source_eof_is_reported() -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let output = output_path();
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(vec![1, 2, 3]),
                5,
                b"ABCDE",
                TransferConfig { chunk_size: 8 },
                |_, _| {},
            )
            .await
        };
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("early-EOF case timed out")?;
        let sender_error = match sender_result {
            Ok(_) => return Err(anyhow!("sender accepted an early EOF")),
            Err(error) => error,
        };
        ensure!(
            format!("{sender_error:#}").contains("source ended after 3 of 5 bytes"),
            "sender returned an unexpected early-EOF error: {sender_error:#}"
        );
        ensure!(
            receiver_result.is_err(),
            "receiver accepted a truncated file"
        );
        Ok(())
    }

    #[tokio::test]
    async fn bounded_slow_receiver_and_delayed_fin_ack_complete() -> Result<()> {
        let input: Vec<u8> = (0..64 * 1024)
            .map(|index| u8::try_from(index % 239))
            .collect::<std::result::Result<_, _>>()
            .context("test byte pattern exceeds u8")?;
        let output = transfer_bytes(
            input.clone(),
            1024,
            Duration::from_millis(1),
            Duration::from_millis(20),
            Duration::from_millis(20),
        )
        .await?;
        ensure!(output == input, "slow-receiver content mismatch");
        Ok(())
    }

    #[tokio::test]
    async fn peer_aborts_are_reported() -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        drop(receiver);
        let mut sender = sender;
        let sender_result = timeout(
            TEST_TIMEOUT,
            send_file(
                &mut sender,
                Cursor::new(vec![1]),
                1,
                b"ABCDE",
                TransferConfig::default(),
                |_, _| {},
            ),
        )
        .await
        .context("sender-abort case timed out")?;
        ensure!(sender_result.is_err(), "sender ignored receiver abort");

        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        drop(sender);
        let output = output_path();
        let mut receiver = receiver;
        let receiver_result = timeout(
            TEST_TIMEOUT,
            receive_file(&mut receiver, b"ABCDE", &output, |_, _| {}),
        )
        .await
        .context("receiver-abort case timed out")?;
        ensure!(receiver_result.is_err(), "receiver ignored sender abort");
        Ok(())
    }

    #[tokio::test]
    async fn interrupted_transfer_resumes_saved_prefix_on_next_invocation() -> Result<()> {
        const CHUNK: usize = 16;
        let data: Vec<u8> = (0..96)
            .map(|index| u8::try_from(index % 251))
            .collect::<std::result::Result<_, _>>()
            .context("test byte pattern exceeds u8")?;
        let file_len = u64::try_from(data.len()).context("test input length exceeds u64")?;
        let first_chunk = u64::try_from(CHUNK).context("test chunk length exceeds u64")?;
        let output = output_path();
        let part = partial_path(&output)?;
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let sender_data = data.clone();
        let sender_future = async move {
            let mut sender = sender;
            sender.send_fault = SendFault::FailOnMessage(4);
            send_file(
                &mut sender,
                Cursor::new(sender_data.clone()),
                file_len,
                b"ABCDE",
                TransferConfig { chunk_size: CHUNK },
                |_, _| {},
            )
            .await
        };
        let receiver_output = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &receiver_output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("interrupted transfer timed out")?;
        ensure!(
            sender_result.is_err(),
            "injected connection error was ignored"
        );
        ensure!(
            receiver_result.is_err(),
            "receiver accepted an incomplete file"
        );
        let partial = tokio::fs::read(&part).await?;
        ensure!(
            partial == data[..CHUNK],
            "partial file does not contain one chunk"
        );

        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let sender_data = data.clone();
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(sender_data.clone()),
                file_len,
                b"ABCDE",
                TransferConfig { chunk_size: CHUNK },
                |_, _| {},
            )
            .await
        };
        let receiver_output = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &receiver_output, |_, _| {}).await
        };
        let (sender_metrics, receiver_metrics) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("resumed transfer timed out")?;
        let sender_metrics = sender_metrics?;
        let receiver_metrics = receiver_metrics?;
        ensure!(sender_metrics.bytes_transferred == file_len - first_chunk);
        ensure!(receiver_metrics.bytes_transferred == sender_metrics.bytes_transferred);
        ensure!(receiver_metrics.file_size == file_len);
        ensure!(tokio::fs::read(&output).await? == data);
        ensure!(!tokio::fs::try_exists(&part).await?);
        tokio::fs::remove_file(&output).await?;
        Ok(())
    }

    #[tokio::test]
    async fn corrupted_prefix_resets_and_full_part_sends_zero_suffix() -> Result<()> {
        const CHUNK: usize = 8;
        let data: Vec<u8> = (0..40)
            .map(|index| u8::try_from(index + 1))
            .collect::<std::result::Result<_, _>>()
            .context("test byte pattern exceeds u8")?;
        let file_len = u64::try_from(data.len()).context("test input length exceeds u64")?;
        let output = output_path();
        let part = partial_path(&output)?;
        tokio::fs::write(&part, vec![0_u8; CHUNK * 2]).await?;
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let sender_data = data.clone();
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(sender_data.clone()),
                file_len,
                b"ABCDE",
                TransferConfig { chunk_size: CHUNK },
                |_, _| {},
            )
            .await
        };
        let receiver_output = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &receiver_output, |_, _| {}).await
        };
        let (_sender_metrics, receiver_metrics) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("corrupted-prefix reset timed out")?;
        let receiver_metrics = receiver_metrics?;
        ensure!(receiver_metrics.bytes_transferred == file_len);
        ensure!(tokio::fs::read(&output).await? == data);
        tokio::fs::remove_file(&output).await?;

        let full_output = output_path();
        let full_part = partial_path(&full_output)?;
        tokio::fs::write(&full_part, &data).await?;
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let sender_data = data.clone();
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(sender_data.clone()),
                file_len,
                b"ABCDE",
                TransferConfig { chunk_size: CHUNK },
                |_, _| {},
            )
            .await
        };
        let receiver_output = full_output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &receiver_output, |_, _| {}).await
        };
        let (sender_metrics, receiver_metrics) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("full-prefix transfer timed out")?;
        ensure!(sender_metrics?.bytes_transferred == 0);
        let receiver_metrics = receiver_metrics?;
        ensure!(receiver_metrics.bytes_transferred == 0);
        ensure!(receiver_metrics.file_size == file_len);
        ensure!(tokio::fs::read(&full_output).await? == data);
        tokio::fs::remove_file(&full_output).await?;
        Ok(())
    }

    #[tokio::test]
    async fn failed_authentication_preserves_existing_partial_file() -> Result<()> {
        let output = output_path();
        let part = partial_path(&output)?;
        let partial = b"keep this verified prefix";
        tokio::fs::write(&part, partial).await?;
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let sender_future = async move {
            let mut sender = sender;
            send_file_direct(
                &mut sender,
                Cursor::new(b"keep this verified prefix and finish it".to_vec()),
                39,
                &[0x41; 16],
                TransferConfig::default(),
                |_, _| {},
            )
            .await
        };
        let receiver_output = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file_direct(&mut receiver, &[0x42; 16], &receiver_output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("wrong-auth resume case timed out")?;
        ensure!(sender_result.is_err());
        ensure!(receiver_result.is_err());
        ensure!(tokio::fs::read(&part).await? == partial);
        ensure!(!tokio::fs::try_exists(&output).await?);
        tokio::fs::remove_file(&part).await?;
        Ok(())
    }
}
