use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use std::{path::PathBuf, time::Duration};
use tokio::time::timeout;
use uuid::Uuid;

use rustytransfer::protocol::fsm::{Role, State};
use rustytransfer::protocol::receiver::ReceiverFsm;
use rustytransfer::protocol::sender::SenderFsm;

// Adjust these imports to your module layout if needed:
use rustytransfer::transport::answerer::connect_answerer;
use rustytransfer::transport::offerer::connect_offerer;

fn must_some(label: &str, v: Option<Vec<u8>>) -> Vec<u8> {
    v.unwrap_or_else(|| panic!("{label}: expected Some(Vec<u8>), got None"))
}

const NET_TIMEOUT: Duration = Duration::from_secs(90);

#[derive(Parser, Debug)]
#[command(name = "rustytransfer")]
#[command(about = "WebRTC encrypted file transfer (PAKE/KEM/SMT over datachannel)")]
struct Cli {
    #[command(subcommand)]
    cmd: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Send an encrypted file (offerer / side A). Generates an App ID and prints it.
    Send {
        /// Password / shared secret (same on both sides)
        #[arg(long)]
        password: String,

        /// Path to file to send
        #[arg(long)]
        file: PathBuf,
    },

    /// Receive an encrypted file (answerer / side B)
    Recv {
        /// Password / shared secret (same on both sides)
        #[arg(long)]
        password: String,

        /// Shared room UUID (paste what sender printed)
        #[arg(long)]
        app_id: String,

        /// Where to write the received bytes
        #[arg(long)]
        out: PathBuf,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.cmd {
        Command::Send { password, file } => send_cmd(password, file).await?,
        Command::Recv {
            password,
            app_id,
            out,
        } => recv_cmd(password, app_id, out).await?,
    }

    Ok(())
}

/// Side A (sender/offerer): waits for receiver's PAKE_START, runs FSM, sends encrypted payload.
async fn send_cmd(password: String, file: PathBuf) -> Result<()> {
    let app_id = Uuid::new_v4().to_string();
    println!("App ID (share this with receiver): {}", app_id);

    let file_bytes = std::fs::read(&file)
        .with_context(|| format!("failed to read file: {}", file.display()))?;
    println!("File is ready! Size: {} bytes", file_bytes.len());

    // 1) Connect WebRTC once (signaling + datachannel)
    let st = timeout(NET_TIMEOUT, connect_offerer(&app_id))
        .await
        .context("offerer connect timeout")??;

    // 2) Run Sender FSM
    let mut sender = SenderFsm::new(password.into_bytes());
    sender.file_data = file_bytes;

    // Wait for PAKE_START from receiver
    let pake_msg_1 = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for PAKE_START")??;

    let out = sender
        .step("PAKE_START", Some(pake_msg_1))
        .context("sender step(PAKE_START) failed")?;
    if out.is_some() {
        bail!("sender PAKE_START unexpectedly produced output");
    }

    // Extract PAKE_ANSWER from internal PakeState and send it
    let pake_msg_2 = match &mut sender.state {
        State::Pake {
            role: Role::Sender,
            pake_state,
            ..
        } => pake_state.take_outbound_msg(),
        other => bail!("sender not in Pake(Sender) after PAKE_START; got: {other:?}"),
    };
    st.send_vec(pake_msg_2).await?;

    // Wait for AUTH_KEM from receiver
    let auth_kem = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for AUTH_KEM")??;

    // Sender consumes AUTH_KEM and emits SMT payload (encrypted file)
    let smt_payload = must_some(
        "sender RECEIVED_AUTH_KEM outbox (SMT payload)",
        sender
            .step("RECEIVED_AUTH_KEM", Some(auth_kem))
            .context("sender step(RECEIVED_AUTH_KEM) failed")?,
    );
    st.send_vec(smt_payload).await?;

    // Wait for FIN
    let fin = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for FIN")??;

    let out = sender
        .step("FIN", Some(fin))
        .context("sender step(FIN) failed")?;
    if out.is_some() {
        bail!("sender FIN unexpectedly produced output");
    }
    if !matches!(sender.state, State::Success(_)) {
        bail!("sender did not reach Success state");
    }

    println!("sent encrypted {}", file.display());
    Ok(())
}

/// Side B (receiver/answerer): starts PAKE, receives encrypted payload, decrypts, writes file.
async fn recv_cmd(password: String, app_id: String, out: PathBuf) -> Result<()> {
    // 1) Connect WebRTC once (signaling + datachannel)
    let st = timeout(NET_TIMEOUT, connect_answerer(&app_id))
        .await
        .context("answerer connect timeout")??;
    println!("Connected! Running receiver FSM...");

    // 2) Run Receiver FSM
    let mut receiver = ReceiverFsm::new(password.into_bytes());

    // Receiver sends PAKE_START first
    let pake_msg_1 = must_some(
        "receiver PAKE_START outbox",
        receiver
            .step("PAKE_START", None)
            .context("receiver step(PAKE_START) failed")?,
    );
    st.send_vec(pake_msg_1).await?;

    // Wait for PAKE_ANSWER
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

    // Wait for SMT payload (encrypted file)
    let smt_payload = timeout(NET_TIMEOUT, st.recv_vec())
        .await
        .context("timeout waiting for SMT payload")??;

    // Receiver decrypts SMT payload -> plaintext
    let plaintext = must_some(
        "receiver SMT outbox (plaintext)",
        receiver
            .step("SMT", Some(smt_payload))
            .context("receiver step(SMT) failed")?,
    );

    // Receiver emits FIN 
    let fin = must_some(
        "receiver SMT(FIN) outbox",
        receiver.step("SMT", None).context("receiver FIN step failed")?,
    );
    st.send_vec(fin).await?;

    if !matches!(receiver.state, State::Success(_)) {
        bail!("receiver did not reach Success state");
    }

    std::fs::write(&out, &plaintext)
        .with_context(|| format!("failed to write output: {}", out.display()))?;

    println!("wrote {}", out.display());
    Ok(())
}
