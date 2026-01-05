use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use std::{path::PathBuf, time::Duration};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::time::timeout;
use uuid::Uuid;

use rustytransfer::protocol::fsm::{Role, State};
use rustytransfer::protocol::receiver::ReceiverFsm;
use rustytransfer::protocol::sender::SenderFsm;

use rustytransfer::transport::answerer::connect_answerer;
use rustytransfer::transport::offerer::connect_offerer;

fn must_some(label: &str, v: Option<Vec<u8>>) -> Vec<u8> {
    v.unwrap_or_else(|| panic!("{label}: expected Some(Vec<u8>), got None"))
}

const NET_TIMEOUT: Duration = Duration::from_secs(90);
const CHUNK_TIMEOUT: Duration = Duration::from_secs(90);

// Good default that stays well under typical DC message limits even after GCM tag.
const DEFAULT_CHUNK_SIZE: u32 = 8 * 1024;

#[derive(Parser, Debug)]
#[command(name = "rustytransfer")]
#[command(about = "WebRTC encrypted file transfer (PAKE/KEM/SMT over datachannel)")]
struct Cli {
    #[command(subcommand)]
    cmd: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Send a file (offerer / side A). Prints an App ID you share with receiver.
    Send {
        #[arg(long)]
        password: String,

        #[arg(long)]
        file: PathBuf,

        /// Plaintext chunk size for streaming SMT
        #[arg(long, default_value_t = DEFAULT_CHUNK_SIZE)]
        chunk_size: u32,
    },

    /// Receive a file (answerer / side B)
    Recv {
        #[arg(long)]
        password: String,

        #[arg(long)]
        app_id: String,

        #[arg(long)]
        out: PathBuf,
    },
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.cmd {
        Command::Send {
            password,
            file,
            chunk_size,
        } => send_cmd(password, file, chunk_size).await?,
        Command::Recv {
            password,
            app_id,
            out,
        } => recv_cmd(password, app_id, out).await?,
    }

    Ok(())
}

async fn send_cmd(password: String, file: PathBuf, chunk_size: u32) -> Result<()> {
    let app_id = Uuid::new_v4().to_string();
    println!("App ID (share this with receiver): {app_id}");

    // Open file for streaming
    let mut f = tokio::fs::File::open(&file)
        .await
        .with_context(|| format!("failed to open file: {}", file.display()))?;
    let meta = f
        .metadata()
        .await
        .with_context(|| format!("failed to stat file: {}", file.display()))?;
    let file_len = meta.len();

    println!("File is ready! Size: {file_len} bytes");
    println!("Waiting for receiver to join room...");

    // Connect offerer (includes room-full barrier + datachannel open in your connect_offerer)
    let st = timeout(NET_TIMEOUT, connect_offerer(&app_id))
        .await
        .context("offerer connect timeout")??;

    println!("Connected! Running sender FSM...");

    // Sender FSM init
    let mut sender = SenderFsm::new(password.into_bytes(), file_len, chunk_size);

    // --- PAKE ---
    let pake_msg_1 = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for PAKE_START")??;

    let out = sender
        .step("PAKE_START", Some(pake_msg_1))
        .context("sender step(PAKE_START) failed")?;
    if out.is_some() {
        bail!("sender PAKE_START unexpectedly produced output");
    }

    // Extract PAKE_ANSWER and send (same pattern you already had)
    let pake_msg_2 = match &mut sender.state {
        State::Pake {
            role: Role::Sender,
            pake_state,
            ..
        } => pake_state.take_outbound_msg(),
        other => bail!("sender not in Pake(Sender) after PAKE_START; got: {other:?}"),
    };
    st.send_vec(pake_msg_2).await?;

    // --- AUTH_KEM from receiver ---
    let auth_kem = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for AUTH_KEM")??;

    // Sender consumes AUTH_KEM and emits SMT HEADER (small)
    let smt_header = must_some(
        "sender RECEIVED_AUTH_KEM outbox (SMT header)",
        sender
            .step("RECEIVED_AUTH_KEM", Some(auth_kem))
            .context("sender step(RECEIVED_AUTH_KEM) failed")?,
    );
    st.send_vec(smt_header).await?;

    // --- SMT streaming: read chunks, encrypt, send ---
    let chunk_sz = chunk_size as usize;
    let mut buf = vec![0u8; chunk_sz];

    let mut last_report: u64 = 0;
    loop {
        let n = f
            .read(&mut buf)
            .await
            .with_context(|| format!("failed reading file: {}", file.display()))?;
        if n == 0 {
            break;
        }

        let pt = buf[..n].to_vec();
        let ct = must_some(
            "sender SMT chunk outbox",
            sender
                .step("SMT", Some(pt))
                .context("sender step(SMT chunk) failed")?,
        );

        st.send_vec(ct).await?;
        let sent = sender.bytes_sent();
        if sent - last_report >= 1024 * 1024 || sent == file_len {
            println!("sent {sent}/{file_len} bytes");
            last_report = sent;
        }
    }

    // --- FIN ---
    let fin = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for FIN")??;

    let out = sender.step("FIN", Some(fin)).context("sender step(FIN) failed")?;
    if out.is_some() {
        bail!("sender FIN unexpectedly produced output");
    }
    if !matches!(sender.state, State::Success(_)) {
        bail!("sender did not reach Success state");
    }

    println!("sent encrypted {}", file.display());
    Ok(())
}

async fn recv_cmd(password: String, app_id: String, out: PathBuf) -> Result<()> {
    // Connect answerer
    let st = timeout(NET_TIMEOUT, connect_answerer(&app_id))
        .await
        .context("answerer connect timeout")??;
    println!("Connected! Running receiver FSM...");

    let mut receiver = ReceiverFsm::new(password.into_bytes());

    // --- PAKE ---
    let pake_msg_1 = must_some(
        "receiver PAKE_START outbox",
        receiver
            .step("PAKE_START", None)
            .context("receiver step(PAKE_START) failed")?,
    );
    st.send_vec(pake_msg_1).await?;

    let pake_msg_2 = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for PAKE_ANSWER")??;

    // Receiver consumes PAKE_ANSWER and emits AUTH_KEM
    let auth_kem = must_some(
        "receiver PAKE_ANSWER outbox (AUTH_KEM)",
        receiver
            .step("PAKE_ANSWER", Some(pake_msg_2))
            .context("receiver step(PAKE_ANSWER) failed")?,
    );
    st.send_vec(auth_kem).await?;

    // --- SMT header ---
    let smt_header = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for SMT header")??;

    let outbox = receiver
        .step("SMT", Some(smt_header))
        .context("receiver step(SMT header) failed")?;
    if outbox.is_some() {
        bail!("receiver SMT header unexpectedly produced output");
    }

    // Prepare output file
    let mut out_f = tokio::fs::File::create(&out)
        .await
        .with_context(|| format!("failed to create output: {}", out.display()))?;

    let file_len = receiver.file_len(); // needs to exist (or use your actual accessor/field)
    println!(
        "Receiving {} bytes (chunk_size={})...",
        file_len,
        receiver.chunk_size()
    );

    // --- SMT chunks: recv, decrypt, write ---
    let mut last_report: u64 = 0;
    while receiver.bytes_received() < file_len {
        let ct = timeout(CHUNK_TIMEOUT, st.recv_vec())
            .await
            .context("timeout waiting for SMT chunk")??;

        let pt = must_some(
            "receiver SMT chunk outbox (plaintext)",
            receiver
                .step("SMT", Some(ct))
                .context("receiver step(SMT chunk) failed")?,
        );

        out_f
            .write_all(&pt)
            .await
            .with_context(|| format!("failed writing output: {}", out.display()))?;

        // progress every 1 MiB
        let got = receiver.bytes_received();
        if got - last_report >= 1024 * 1024 || got == file_len {
            println!("received {got}/{file_len} bytes");
            last_report = got;
        }
    }

    out_f.flush().await.ok();

    // Finalize SMT -> FIN
    let fin = must_some(
        "receiver SMT finalize outbox (FIN)",
        receiver.step("SMT", None).context("receiver SMT finalize failed")?,
    );
    st.send_vec(fin).await?;

    if !matches!(receiver.state, State::Success(_)) {
        bail!("receiver did not reach Success state");
    }

    println!("wrote {}", out.display());
    Ok(())
}
