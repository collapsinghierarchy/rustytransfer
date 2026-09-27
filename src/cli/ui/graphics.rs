use indicatif::{ProgressBar, ProgressStyle};
use std::time::Duration;

use anyhow::{Context, Result};
use std::ops::{Deref, DerefMut};
use std::path::PathBuf;

/// Marker for an intentional user cancellation in an interactive prompt.
#[derive(Debug, Clone, Copy)]
pub struct UserCancelled;

impl std::fmt::Display for UserCancelled {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("cancelled by user")
    }
}

impl std::error::Error for UserCancelled {}

/// Progress UI that always clears itself when its scope ends.
pub struct ProgressGuard(ProgressBar);

impl ProgressGuard {
    fn new(progress: ProgressBar) -> Self {
        Self(progress)
    }

    pub fn finish_and_clear(&self) {
        self.0.finish_and_clear();
    }
}

impl Deref for ProgressGuard {
    type Target = ProgressBar;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl DerefMut for ProgressGuard {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}

impl Drop for ProgressGuard {
    fn drop(&mut self) {
        self.0.finish_and_clear();
    }
}

/// Open the terminal file picker and return the selected file.
///
/// # Errors
///
/// Returns an error if terminal setup, input handling, rendering, or file
/// explorer navigation fails, or if the user cancels the picker.
pub fn pick_file_tui(start_dir: Option<PathBuf>) -> Result<PathBuf> {
    use std::io::{Write, stdout};

    use crossterm::{
        cursor,
        event::{self, KeyCode, KeyEventKind},
        execute,
        terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
    };

    use ratatui::{
        Terminal,
        backend::CrosstermBackend,
        layout::{Constraint, Direction, Layout},
        widgets::{Block, Borders, Paragraph},
    };

    use ratatui_explorer::{FileExplorer, Theme};

    struct TermGuard;
    impl Drop for TermGuard {
        fn drop(&mut self) {
            drop(disable_raw_mode());
            let mut out = stdout();
            drop(execute!(out, LeaveAlternateScreen, cursor::Show));
        }
    }

    enable_raw_mode().context("enable_raw_mode failed")?;
    let mut out = stdout();
    execute!(out, EnterAlternateScreen, cursor::Hide).context("enter alt screen failed")?;
    out.flush().context("flush terminal setup failed")?;
    let _guard = TermGuard;

    let backend = CrosstermBackend::new(stdout());
    let mut terminal = Terminal::new(backend).context("create terminal failed")?;

    let theme = Theme::default().add_default_title();
    let mut explorer = FileExplorer::with_theme(theme).context("FileExplorer init failed")?;

    if let Some(dir) = start_dir {
        explorer.set_cwd(dir).context("set start dir failed")?;
    }

    loop {
        terminal
            .draw(|f| {
                let chunks = Layout::default()
                    .direction(Direction::Vertical)
                    .constraints([Constraint::Min(1), Constraint::Length(2)])
                    .split(f.area());
                let w = explorer.widget();
                if let Some(area) = chunks.first() {
                    f.render_widget(&w, *area);
                }

                let help = Paragraph::new(
                    "↑↓ move  h/←/Backspace parent  l/→/Enter open dir  Enter on file selects  q/Esc cancel",
                )
                .block(Block::default().borders(Borders::TOP));
                if let Some(area) = chunks.get(1) {
                    f.render_widget(help, *area);
                }
            })
            .context("draw failed")?;
        let ev: crossterm::event::Event = event::read().context("read event failed")?;

        if let crossterm::event::Event::Key(k) = &ev
            && k.kind == KeyEventKind::Press
        {
            match k.code {
                KeyCode::Esc | KeyCode::Char('q') => return Err(UserCancelled.into()),
                KeyCode::Enter => {
                    let cur = explorer.current();
                    if cur.is_file() {
                        return Ok(cur.path().clone());
                    }
                    // if it's a dir, explorer.handle will open it (Enter bound)
                }
                _ => {}
            }
        }

        explorer.handle(&ev).context("explorer handle failed")?;
    }
}

/// Create a spinner with the application's standard presentation.
#[must_use]
pub fn spinner(msg: &str) -> ProgressGuard {
    let pb = ProgressBar::new_spinner();
    pb.set_style(
        ProgressStyle::with_template("{spinner} {msg}")
            .unwrap_or_else(|_| ProgressStyle::default_spinner())
            .tick_strings(&["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]),
    );
    pb.set_message(msg.to_string());
    pb.enable_steady_tick(Duration::from_millis(90));
    ProgressGuard::new(pb)
}

/// Create a byte progress bar with the application's standard presentation.
#[must_use]
pub fn bytes_bar(total: u64, msg: &str) -> ProgressGuard {
    let pb = ProgressBar::new(total);
    pb.set_style(
        ProgressStyle::with_template("{msg} [{bar:40}] {bytes}/{total_bytes} ({eta})")
            .unwrap_or_else(|_| ProgressStyle::default_bar()),
    );
    pb.set_message(msg.to_string());
    ProgressGuard::new(pb)
}
