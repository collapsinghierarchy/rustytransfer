use indicatif::{ProgressBar, ProgressStyle};
use std::time::Duration;

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
