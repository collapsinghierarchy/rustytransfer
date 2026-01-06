use indicatif::{ProgressBar, ProgressStyle};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use std::path::PathBuf;

pub fn pick_file_tui(start_dir: Option<PathBuf>) -> Result<PathBuf> {
    use std::io::{stdout, Write};

    use crossterm::{
        cursor,
        event::{self, KeyCode, KeyEventKind},
        execute,
        terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
    };

    use ratatui::{
        backend::CrosstermBackend,
        layout::{Constraint, Direction, Layout},
        widgets::{Block, Borders, Paragraph},
        Terminal,
    };

    use ratatui_explorer::{FileExplorer, Theme};

    struct TermGuard;
    impl Drop for TermGuard {
        fn drop(&mut self) {
            let _ = disable_raw_mode();
            let mut out = stdout();
            let _ = execute!(out, LeaveAlternateScreen, cursor::Show);
        }
    }

    enable_raw_mode().context("enable_raw_mode failed")?;
    let mut out = stdout();
    execute!(out, EnterAlternateScreen, cursor::Hide).context("enter alt screen failed")?;
    out.flush().ok();
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
                f.render_widget(&w, chunks[0]);

                let help = Paragraph::new(
                    "↑↓ move  h/←/Backspace parent  l/→/Enter open dir  Enter on file selects  q/Esc cancel",
                )
                .block(Block::default().borders(Borders::TOP));
                f.render_widget(help, chunks[1]);
            })
            .context("draw failed")?;
        let ev: crossterm::event::Event = event::read().context("read event failed")?;

        if let crossterm::event::Event::Key(k) = &ev {
            if k.kind == KeyEventKind::Press {
                match k.code {
                    KeyCode::Esc | KeyCode::Char('q') => bail!("file selection cancelled"),
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
        }

        explorer.handle(&ev).context("explorer handle failed")?;
    }
}



pub fn spinner(msg: &str) -> ProgressBar {
    let pb = ProgressBar::new_spinner();
    pb.set_style(
        ProgressStyle::with_template("{spinner} {msg}")
            .unwrap()
            .tick_strings(&["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]),
    );
    pb.set_message(msg.to_string());
    pb.enable_steady_tick(Duration::from_millis(90));
    pb
}

pub fn bytes_bar(total: u64, msg: &str) -> ProgressBar {
    let pb = ProgressBar::new(total);
    pb.set_style(
        ProgressStyle::with_template("{msg} [{bar:40}] {bytes}/{total_bytes} ({eta})")
            .unwrap(),
    );
    pb.set_message(msg.to_string());
    pb
}
