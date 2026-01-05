use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use std::{path::PathBuf, time::Duration};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::time::timeout;

use rustytransfer::protocol::fsm::{Role, State};
use rustytransfer::protocol::receiver::ReceiverFsm;
use rustytransfer::protocol::sender::SenderFsm;
use rustytransfer::cli_ui::graphics::spinner;
use rustytransfer::cli_ui::graphics::bytes_bar; 

use rustytransfer::transport::answerer::connect_answerer;
use rustytransfer::transport::offerer::connect_offerer;
use rustytransfer::transport::rendezvous;

const NET_TIMEOUT: Duration = Duration::from_secs(90);
const CHUNK_TIMEOUT: Duration = Duration::from_secs(90);

// plaintext chunk size (ciphertext will be +16 bytes for GCM tag)
const DEFAULT_CHUNK_SIZE: u32 = 8 * 1024;

fn must_some(label: &str, v: Option<Vec<u8>>) -> Vec<u8> {
    v.unwrap_or_else(|| panic!("{label}: expected Some(Vec<u8>), got None"))
}

#[derive(Parser, Debug)]
#[command(name = "rustytransfer")]
#[command(about = "WebRTC encrypted file transfer (PAKE/MAC/KEM/DEM/SMT over datachannel)")]
struct Cli {
    #[command(subcommand)]
    cmd: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Send a file. Prints a share code NNNN-ABCDE (digits = rendezvous, letters = password).
    Send {
        #[arg(long)]
        file: PathBuf,

        /// Optional override (must be 5 uppercase letters). If omitted, generated automatically.
        #[arg(long)]
        password: Option<String>,

        /// Plaintext chunk size for streaming SMT (defaults to 8KiB)
        #[arg(long, default_value_t = DEFAULT_CHUNK_SIZE)]
        chunk_size: u32,
    },

    /// Receive a file using a share code NNNN-ABCDE.
    Recv {
        /// Share code printed by sender (NNNN-ABCDE)
        #[arg(long)]
        code: String,

        #[arg(long)]
        out: PathBuf,
    },
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Command::Send {
            file,
            password,
            chunk_size,
        } => send_cmd(file, password, chunk_size).await,
        Command::Recv { code, out } => recv_cmd(code, out).await,
    }
}

async fn send_cmd(file: PathBuf, password: Option<String>, chunk_size: u32) -> Result<()> {
    // password: 5 uppercase letters
    let pw5 = match password {
        Some(p) => {
            // reuse rendezvous validation helpers
            // (format_share_code() validates both components)
            // We just validate here by trying to normalize with a dummy code.
            if p.len() != 5 || !p.chars().all(|c| c.is_ascii_uppercase()) {
                bail!("--password must be exactly 5 uppercase letters (e.g. ABCDE)");
            }
            p
        }
        None => rendezvous::gen_password_5(),
    };

    // open file + get length
    let mut f = tokio::fs::File::open(&file)
        .await
        .with_context(|| format!("failed to open file: {}", file.display()))?;
    let meta = f
        .metadata()
        .await
        .with_context(|| format!("failed to stat file: {}", file.display()))?;
    let file_len = meta.len();

    // rendezvous: request code+app_id
    let spin = spinner("Requesting rendezvous code…");
    let rr = rendezvous::request_code().await?;
    spin.finish_and_clear();

    let share = rendezvous::format_share_code(&rr.code, &pw5)?;
    println!("Share this code: {share}");
    if let Some(exp) = rr.expires_at.as_ref() {
        println!("(expires at: {exp})");
    }

    println!("File: {} ({} bytes)", file.display(), file_len);
    println!("Waiting for receiver to join…");

    // connect
    let spin = spinner("Connecting WebRTC…");
    let st = timeout(NET_TIMEOUT, connect_offerer(&rr.app_id))
        .await
        .context("offerer connect timeout")??;
    spin.finish_and_clear();

    // FSM
    // Expected: SenderFsm::new(pw, file_len, chunk_size)
    // If your SenderFsm::new still only takes (pw), change the next line accordingly and call your setter.
    let mut sender = SenderFsm::new(pw5.into_bytes(), file_len, chunk_size);

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

    let pake_msg_2 = match &mut sender.state {
        State::Pake {
            role: Role::Sender,
            pake_state,
            ..
        } => pake_state.take_outbound_msg(),
        other => bail!("sender not in Pake(Sender) after PAKE_START; got: {other:?}"),
    };
    st.send_vec(pake_msg_2).await?;

    // --- AUTH_KEM ---
    let auth_kem = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for AUTH_KEM")??;

    // --- SMT header (small) ---
    let smt_header = must_some(
        "sender RECEIVED_AUTH_KEM outbox (SMT header)",
        sender
            .step("RECEIVED_AUTH_KEM", Some(auth_kem))
            .context("sender step(RECEIVED_AUTH_KEM) failed")?,
    );
    st.send_vec(smt_header).await?;

    // --- SMT chunks (streaming) ---
    let pb = bytes_bar(file_len, "Sending");
    let mut buf = vec![0u8; chunk_size as usize];

    loop {
        let n = f
            .read(&mut buf)
            .await
            .with_context(|| format!("failed reading file: {}", file.display()))?;
        if n == 0 {
            break;
        }

        let ct = must_some(
            "sender SMT chunk outbox",
            sender
                .step("SMT", Some(buf[..n].to_vec()))
                .context("sender step(SMT chunk) failed")?,
        );

        st.send_vec(ct).await?;
        pb.set_position(sender.bytes_sent());
    }
    pb.finish_and_clear();

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

    println!("Sent encrypted {}", file.display());
    Ok(())
}

async fn recv_cmd(code: String, out: PathBuf) -> Result<()> {
    // parse NNNN-ABCDE
    let (code4, pw5) = rendezvous::parse_share_code(&code)?;

    // redeem NNNN -> app_id
    let spin = spinner("Redeeming rendezvous code…");
    let redeem = rendezvous::redeem(&code4).await?;
    spin.finish_and_clear();

    if let Some(exp) = redeem.expires_at.as_ref() {
        println!("Rendezvous redeemed (expires at: {exp})");
    }

    // connect
    let spin = spinner("Connecting WebRTC…");
    let st = timeout(NET_TIMEOUT, connect_answerer(&redeem.app_id))
        .await
        .context("answerer connect timeout")??;
    spin.finish_and_clear();

    let mut receiver = ReceiverFsm::new(pw5.into_bytes());

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

    // open output file
    let mut out_f = tokio::fs::File::create(&out)
        .await
        .with_context(|| format!("failed to create output: {}", out.display()))?;

    // These should exist on ReceiverFsm:
    //   fn file_len(&self) -> u64
    //   fn bytes_received(&self) -> u64
    let total = receiver.file_len();
    let pb = bytes_bar(total, "Receiving");

    // --- SMT chunks: recv -> decrypt -> write ---
    while receiver.bytes_received() < total {
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

        pb.set_position(receiver.bytes_received());
    }
    pb.finish_and_clear();
    let _ = out_f.flush().await;

    // finalize -> FIN
    let fin = must_some(
        "receiver SMT finalize outbox (FIN)",
        receiver.step("SMT", None).context("receiver SMT finalize failed")?,
    );
    st.send_vec(fin).await?;

    if !matches!(receiver.state, State::Success(_)) {
        bail!("receiver did not reach Success state");
    }

    println!("Wrote {}", out.display());
    Ok(())
}