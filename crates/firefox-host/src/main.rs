mod messaging;

use anyhow::{Context, Result, anyhow};
use messaging::{Direction, Event, InvalidRequest, Request, read_frame, write_frame};
use rustytransfer_native::direct::{
    DirectInvite, MAX_TRANSFER_ATTEMPTS, can_retry_transfer, retry_backoff as reconnect_backoff,
};
use rustytransfer_native::transport::DataTransport;
use rustytransfer_native::transport::iroh::{
    accept_direct, bind_direct_sender, connect_direct, default_identity_path,
};
use rustytransfer_transfer::{TransferConfig, receive_file_direct, send_file_direct};
use std::{
    io,
    path::{Path, PathBuf},
    sync::Arc,
    thread,
    time::{Duration, Instant},
};
use tokio::{
    sync::{Mutex, mpsc, watch},
    time::sleep,
};

#[derive(Debug)]
enum Input {
    Frame(Vec<u8>),
    Failure(String),
}

struct ActiveTransfer {
    id: u64,
    direction: Direction,
    invite: Option<String>,
    file_name: Option<String>,
    cancel: watch::Sender<bool>,
}

#[derive(Default)]
struct HostState {
    active: Option<ActiveTransfer>,
}

enum TransferResult {
    Complete(String),
    Cancelled,
    Failed(String),
}

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("rustytransfer Firefox host: {error:#}");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    let (input_tx, mut input_rx) = mpsc::unbounded_channel::<Input>();
    let input_thread = thread::Builder::new()
        .name("firefox-host-stdin".to_owned())
        .spawn(move || read_requests(input_tx))
        .context("failed to start native messaging input reader")?;

    let (event_tx, event_rx) = mpsc::unbounded_channel::<Event>();
    let output_thread = thread::Builder::new()
        .name("firefox-host-stdout".to_owned())
        .spawn(move || write_events(event_rx))
        .context("failed to start native messaging output writer")?;

    let state = Arc::new(Mutex::new(HostState::default()));
    while let Some(input) = input_rx.recv().await {
        match input {
            Input::Failure(message) => {
                emit(&event_tx, Event::Error { id: 0, message });
                break;
            }
            Input::Frame(frame) => match messaging::decode_request(&frame) {
                Ok(request) => {
                    handle_request(request, Arc::clone(&state), event_tx.clone()).await;
                }
                Err(InvalidRequest { id, message }) => {
                    emit(&event_tx, Event::Error { id, message });
                }
            },
        }
    }

    if let Some(active) = state.lock().await.active.as_ref() {
        let _cancel_result = active.cancel.send(true);
    }
    let _idle_result = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if state.lock().await.active.is_none() {
                break;
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await;

    drop(event_tx);
    let output_result = tokio::task::spawn_blocking(move || output_thread.join())
        .await
        .context("native messaging output writer task failed")?;
    let output_result = output_result
        .map_err(|error| anyhow!("native messaging output writer panicked: {error:?}"))?;
    output_result.context("native messaging output writer failed")?;
    let input_result = tokio::task::spawn_blocking(move || input_thread.join())
        .await
        .context("native messaging input reader task failed")?;
    if let Err(error) = input_result {
        eprintln!("native messaging input reader panicked: {error:?}");
    }
    Ok(())
}

fn read_requests(sender: mpsc::UnboundedSender<Input>) {
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    loop {
        match read_frame(&mut reader) {
            Ok(Some(frame)) => {
                if sender.send(Input::Frame(frame)).is_err() {
                    break;
                }
            }
            Ok(None) => break,
            Err(error) => {
                let _send_result = sender.send(Input::Failure(error.to_string()));
                break;
            }
        }
    }
}

fn write_events(mut receiver: mpsc::UnboundedReceiver<Event>) -> io::Result<()> {
    let stdout = io::stdout();
    let mut writer = stdout.lock();
    while let Some(event) = receiver.blocking_recv() {
        write_frame(&mut writer, &event)?;
    }
    Ok(())
}

fn emit(sender: &mpsc::UnboundedSender<Event>, event: Event) {
    if sender.send(event).is_err() {
        eprintln!("native messaging output writer is unavailable");
    }
}

async fn handle_request(
    request: Request,
    state: Arc<Mutex<HostState>>,
    events: mpsc::UnboundedSender<Event>,
) {
    match request {
        Request::StartSend { id } => {
            start_transfer(id, Direction::Send, None, state, events).await;
        }
        Request::StartReceive { id, invite } => match DirectInvite::parse(&invite) {
            Ok(_) => start_transfer(id, Direction::Receive, Some(invite), state, events).await,
            Err(error) => emit(
                &events,
                Event::Error {
                    id,
                    message: error.to_string(),
                },
            ),
        },
        Request::Cancel { id } => {
            let state = state.lock().await;
            if let Some(active) = &state.active {
                let _cancel_result = active.cancel.send(true);
            } else {
                emit(
                    &events,
                    Event::Error {
                        id,
                        message: "no transfer is active".to_owned(),
                    },
                );
            }
        }
        Request::Status { id } => {
            let event = {
                let state = state.lock().await;
                match &state.active {
                    Some(active) => Event::Status {
                        id,
                        state: "busy".to_owned(),
                        direction: Some(active.direction),
                        invite: active.invite.clone(),
                        file_name: active.file_name.clone(),
                    },
                    None => Event::Status {
                        id,
                        state: "idle".to_owned(),
                        direction: None,
                        invite: None,
                        file_name: None,
                    },
                }
            };
            emit(&events, event);
        }
    }
}

async fn start_transfer(
    id: u64,
    direction: Direction,
    invite: Option<String>,
    state: Arc<Mutex<HostState>>,
    events: mpsc::UnboundedSender<Event>,
) {
    let (cancel_tx, mut cancel_rx) = watch::channel(false);
    {
        let mut host = state.lock().await;
        if host.active.is_some() {
            emit(
                &events,
                Event::Error {
                    id,
                    message: "another transfer is already active".to_owned(),
                },
            );
            return;
        }
        host.active = Some(ActiveTransfer {
            id,
            direction,
            invite: invite.clone(),
            file_name: None,
            cancel: cancel_tx,
        });
    }
    emit(
        &events,
        Event::Status {
            id,
            state: "busy".to_owned(),
            direction: Some(direction),
            invite: invite.clone(),
            file_name: None,
        },
    );

    tokio::spawn(async move {
        let operation = async {
            match direction {
                Direction::Send => run_send(id, Arc::clone(&state), &events).await,
                Direction::Receive => {
                    let Some(invite) = invite else {
                        return TransferResult::Failed("missing direct invite".to_owned());
                    };
                    run_receive(id, &invite, Arc::clone(&state), &events).await
                }
            }
        };
        let outcome = tokio::select! {
            biased;
            result = operation => result,
            _ = cancel_rx.changed() => TransferResult::Cancelled,
        };
        let event = match outcome {
            TransferResult::Complete(file_name) => Event::Complete {
                id,
                direction,
                file_name,
            },
            TransferResult::Cancelled => Event::Cancelled { id },
            TransferResult::Failed(message) => Event::Error { id, message },
        };
        let mut host = state.lock().await;
        if host.active.as_ref().is_some_and(|active| active.id == id) {
            host.active = None;
            emit(&events, event);
        }
    });
}

async fn update_active(
    state: &Arc<Mutex<HostState>>,
    id: u64,
    invite: Option<String>,
    file_name: String,
) {
    let mut state = state.lock().await;
    if let Some(active) = state.active.as_mut().filter(|active| active.id == id) {
        if let Some(invite) = invite {
            active.invite = Some(invite);
        }
        active.file_name = Some(file_name);
    }
}

async fn run_send(
    id: u64,
    state: Arc<Mutex<HostState>>,
    events: &mpsc::UnboundedSender<Event>,
) -> TransferResult {
    let selected = rfd::AsyncFileDialog::new()
        .set_title("Select a file to send")
        .pick_file()
        .await;
    let Some(selected) = selected else {
        return TransferResult::Cancelled;
    };
    let file = selected.path().to_path_buf();
    let file_name = display_file_name(&file);
    let source = match tokio::fs::File::open(&file).await {
        Ok(source) => source,
        Err(error) => {
            return TransferResult::Failed(format!("failed to open selected file: {error}"));
        }
    };
    let file_size = match source.metadata().await {
        Ok(metadata) => metadata.len(),
        Err(error) => {
            return TransferResult::Failed(format!(
                "failed to read selected file metadata: {error}"
            ));
        }
    };
    let identity_path = match default_identity_path() {
        Ok(path) => path,
        Err(error) => return TransferResult::Failed(format!("Iroh identity path: {error}")),
    };
    let endpoint = match bind_direct_sender(&identity_path).await {
        Ok(endpoint) => endpoint,
        Err(error) => return TransferResult::Failed(format!("failed to start Iroh: {error:#}")),
    };
    let invite = DirectInvite::generate(endpoint.id().to_string());
    let invite_text = invite.format();
    update_active(&state, id, Some(invite_text.clone()), file_name.clone()).await;
    emit(
        events,
        Event::Invite {
            id,
            invite: invite_text,
            file_name: file_name.clone(),
            file_size,
        },
    );

    let transport = match accept_direct(endpoint, &invite.token).await {
        Ok(transport) => DataTransport::Iroh(transport),
        Err(error) => return TransferResult::Failed(format!("waiting for receiver: {error:#}")),
    };
    transfer_send(
        id,
        file,
        file_size,
        identity_path,
        invite,
        transport,
        events,
    )
    .await
}

async fn transfer_send(
    id: u64,
    file: PathBuf,
    file_size: u64,
    identity_path: PathBuf,
    invite: DirectInvite,
    mut transport: DataTransport,
    events: &mpsc::UnboundedSender<Event>,
) -> TransferResult {
    let mut attempts = 1_usize;
    loop {
        let source = match tokio::fs::File::open(&file).await {
            Ok(source) => source,
            Err(error) => {
                return TransferResult::Failed(format!("failed to reopen selected file: {error}"));
            }
        };
        let result = {
            let mut on_progress = progress_reporter(id, Direction::Send, events.clone());
            send_file_direct(
                &mut transport,
                source,
                file_size,
                &invite.token,
                TransferConfig::default(),
                &mut on_progress,
            )
            .await
        };
        match result {
            Ok(_) => return TransferResult::Complete(display_file_name(&file)),
            Err(error) if can_retry_transfer(&error) && attempts < MAX_TRANSFER_ATTEMPTS => {
                let mut next_attempt = attempts.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                loop {
                    sleep(reconnect_backoff(next_attempt)).await;
                    match reconnect_sender(&identity_path, &invite).await {
                        Ok(reconnected) => {
                            transport = reconnected;
                            attempts = next_attempt;
                            break;
                        }
                        Err(error) if next_attempt < MAX_TRANSFER_ATTEMPTS => {
                            eprintln!("direct sender reconnect failed: {error:#}");
                            next_attempt =
                                next_attempt.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                        }
                        Err(error) => {
                            return TransferResult::Failed(format!(
                                "failed to reconnect: {error:#}"
                            ));
                        }
                    }
                }
            }
            Err(error) => return TransferResult::Failed(error.to_string()),
        }
    }
}

async fn reconnect_sender(identity_path: &Path, invite: &DirectInvite) -> Result<DataTransport> {
    let identity = tokio::fs::read(identity_path)
        .await
        .context("failed to read the existing Iroh identity for reconnect")?;
    if identity.len() != 32 {
        return Err(anyhow!("Iroh identity file must contain exactly 32 bytes"));
    }
    drop(identity);
    let endpoint = bind_direct_sender(identity_path).await?;
    if endpoint.id().to_string() != invite.sender_id {
        return Err(anyhow!("Iroh sender identity changed during reconnect"));
    }
    let transport = accept_direct(endpoint, &invite.token).await?;
    Ok(DataTransport::Iroh(transport))
}

async fn run_receive(
    id: u64,
    invite_text: &str,
    state: Arc<Mutex<HostState>>,
    events: &mpsc::UnboundedSender<Event>,
) -> TransferResult {
    let invite = match DirectInvite::parse(invite_text) {
        Ok(invite) => invite,
        Err(error) => return TransferResult::Failed(error.to_string()),
    };
    let selected = rfd::AsyncFileDialog::new()
        .set_title("Choose where to save the received file")
        .set_file_name("received-file")
        .save_file()
        .await;
    let Some(selected) = selected else {
        return TransferResult::Cancelled;
    };
    let output = selected.path().to_path_buf();
    let file_name = display_file_name(&output);
    update_active(&state, id, Some(invite_text.to_owned()), file_name.clone()).await;
    transfer_receive(id, output, file_name, invite, events).await
}

async fn transfer_receive(
    id: u64,
    output: PathBuf,
    file_name: String,
    invite: DirectInvite,
    events: &mpsc::UnboundedSender<Event>,
) -> TransferResult {
    let mut attempts = 1_usize;
    let mut transport = match connect_direct(&invite.sender_id, &invite.token).await {
        Ok(transport) => DataTransport::Iroh(transport),
        Err(error) => {
            return TransferResult::Failed(format!("failed to connect to sender: {error:#}"));
        }
    };
    loop {
        let result = {
            let mut on_progress = progress_reporter(id, Direction::Receive, events.clone());
            receive_file_direct(&mut transport, &invite.token, &output, &mut on_progress).await
        };
        match result {
            Ok(_) => return TransferResult::Complete(file_name),
            Err(error) if can_retry_transfer(&error) && attempts < MAX_TRANSFER_ATTEMPTS => {
                let mut next_attempt = attempts.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                loop {
                    sleep(reconnect_backoff(next_attempt)).await;
                    match connect_direct(&invite.sender_id, &invite.token).await {
                        Ok(reconnected) => {
                            transport = DataTransport::Iroh(reconnected);
                            attempts = next_attempt;
                            break;
                        }
                        Err(error) if next_attempt < MAX_TRANSFER_ATTEMPTS => {
                            eprintln!("direct receiver reconnect failed: {error:#}");
                            next_attempt =
                                next_attempt.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                        }
                        Err(error) => {
                            return TransferResult::Failed(format!(
                                "failed to reconnect: {error:#}"
                            ));
                        }
                    }
                }
            }
            Err(error) => return TransferResult::Failed(error.to_string()),
        }
    }
}

fn progress_reporter(
    id: u64,
    direction: Direction,
    events: mpsc::UnboundedSender<Event>,
) -> impl FnMut(u64, u64) + Send {
    let mut last_report = None;
    move |total, done| {
        if done == total
            || last_report.is_none_or(|last: Instant| last.elapsed() >= Duration::from_millis(100))
        {
            emit(
                &events,
                Event::Progress {
                    id,
                    direction,
                    done,
                    total,
                },
            );
            last_report = Some(Instant::now());
        }
    }
}

fn display_file_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "received-file".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cancel_acknowledgement_waits_for_the_active_worker() {
        let (cancel, receiver) = watch::channel(false);
        let state = Arc::new(Mutex::new(HostState {
            active: Some(ActiveTransfer {
                id: 7,
                direction: Direction::Send,
                invite: None,
                file_name: None,
                cancel,
            }),
        }));
        let (events, mut received) = mpsc::unbounded_channel();
        handle_request(
            Request::Cancel { id: 8 },
            Arc::clone(&state),
            events.clone(),
        )
        .await;
        assert!(*receiver.borrow());
        assert!(matches!(
            received.try_recv(),
            Err(mpsc::error::TryRecvError::Empty)
        ));

        state.lock().await.active = None;
        handle_request(Request::Cancel { id: 9 }, state, events).await;
        assert!(matches!(
            received.try_recv(),
            Ok(Event::Error { id: 9, .. })
        ));
    }
}
