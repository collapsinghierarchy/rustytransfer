//! Manual probe for persistent Iroh identities and public-relay connectivity.

use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use iroh::{EndpointAddr, EndpointId};
use rustytransfer_native::transport::iroh::{
    IROH_PROBE_ALPN, accept_connection, bind_endpoint_with_key, load_or_create_key, state,
};
use std::{path::PathBuf, time::Duration};
use tokio::time::timeout;

#[derive(Parser)]
#[command(about = "Probe persistent Iroh IDs and direct dialing without rendezvous")]
struct Args {
    /// Local 32-byte Iroh secret key file; created if it does not exist.
    #[arg(long)]
    key_file: PathBuf,
    /// Disable direct IP paths so traffic must use an Iroh relay.
    #[arg(long)]
    relay_only: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Show the persistent EndpointId without connecting to the network.
    Id,
    /// Publish this endpoint and answer one ping.
    Listen,
    /// Resolve a known EndpointId and send one ping.
    Dial { peer_id: String },
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let key = load_or_create_key(&args.key_file)?;
    if matches!(&args.command, Command::Id) {
        println!("Local EndpointId: {}", key.public());
        return Ok(());
    }
    let endpoint = bind_endpoint_with_key(key, args.relay_only, IROH_PROBE_ALPN).await?;
    println!("Local EndpointId: {}", endpoint.id());
    timeout(Duration::from_secs(30), endpoint.online())
        .await
        .context("Iroh did not connect to a public relay within 30 seconds")?;

    match args.command {
        Command::Id => {}
        Command::Listen => {
            println!("Online; waiting for one peer...");
            let (connection, send, recv) = accept_connection(&endpoint).await?;
            let remote_id = connection.remote_id();
            let mut connection = state(endpoint, connection, send, recv);
            ensure!(
                connection.recv_vec().await? == b"PING",
                "unexpected probe message"
            );
            connection.finish_recv().await?;
            connection.send_vec(b"PONG".to_vec()).await?;
            println!(
                "Peer: {remote_id}; path: {}",
                connection.path_observation().path
            );
            connection.finish_send().await?;
            connection.close_transport().await?;
        }
        Command::Dial { peer_id } => {
            let peer_id: EndpointId = peer_id.parse().context("invalid peer EndpointId")?;
            let addr = EndpointAddr::new(peer_id);
            let connection = timeout(
                Duration::from_secs(90),
                endpoint.connect(addr, IROH_PROBE_ALPN),
            )
            .await
            .context("direct Iroh connection timed out")??;
            ensure!(
                connection.remote_id() == peer_id,
                "connected to an unexpected peer"
            );
            let (send, recv) = timeout(Duration::from_secs(90), connection.open_bi())
                .await
                .context("opening Iroh probe stream timed out")??;
            let mut connection = state(endpoint, connection, send, recv);
            connection.send_vec(b"PING".to_vec()).await?;
            connection.finish_send().await?;
            ensure!(
                connection.recv_vec().await? == b"PONG",
                "unexpected probe reply"
            );
            println!(
                "Peer: {peer_id}; path: {}",
                connection.path_observation().path
            );
            connection.finish_recv().await?;
            connection.close_transport().await?;
        }
    }
    Ok(())
}
