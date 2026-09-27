use super::*;

#[derive(Parser, Debug)]
#[command(name = "rustytransfer")]
#[command(about = "Encrypted file transfer over WebRTC or Iroh")]
pub(super) struct Cli {
    #[arg(long, global = true, value_enum)]
    pub(super) transport: Option<Transport>,

    #[arg(long, global = true, default_value_t = false)]
    pub(super) verbose: bool,

    #[command(subcommand)]
    pub(super) cmd: Command,
}

#[derive(Subcommand, Debug)]
pub(super) enum Command {
    /// Send a file using a share code or a direct Iroh invite.
    Send {
        /// Share a direct Iroh invite instead of using the rendezvous backend.
        #[arg(long)]
        direct: bool,

        /// Persistent sender Iroh identity (defaults to the user's data directory).
        #[arg(long)]
        identity_file: Option<PathBuf>,

        /// Optional override (must be 5 uppercase letters). If omitted, generated automatically.
        #[arg(long)]
        password: Option<String>,

        /// If omitted and --pick is not set, a TUI file picker opens.
        #[arg(long)]
        file: Option<PathBuf>,

        /// Force opening the TUI file picker (ignores --file)
        #[arg(long, default_value_t = false)]
        pick: bool,

        /// Plaintext chunk size for streaming SMT. Defaults to 8 KiB for WebRTC and 256 KiB for Iroh.
        #[arg(long, value_name = "BYTES")]
        chunk_size: Option<u32>,
    },

    /// Receive a file using a share code or a direct Iroh invite.
    Recv {
        /// Share code printed by sender (NNNN-ABCDE)
        #[arg(long, conflicts_with = "invite")]
        code: Option<String>,

        /// Copyable direct Iroh invite printed by sender.
        #[arg(long, conflicts_with = "code")]
        invite: Option<String>,

        #[arg(long)]
        out: PathBuf,
    },

    /// Save and list peer EndpointIds for direct Iroh transfers.
    #[command(alias = "contact")]
    Contacts {
        #[command(subcommand)]
        command: ContactCommand,
    },
}

#[derive(Subcommand, Debug)]
pub(super) enum ContactCommand {
    /// List saved peers.
    List,

    /// Save a peer EndpointId or direct invite under a short name.
    Add {
        name: String,
        #[arg(value_name = "ENDPOINT_ID_OR_INVITE")]
        peer: String,
    },

    /// Remove a saved peer.
    Remove { name: String },
}
