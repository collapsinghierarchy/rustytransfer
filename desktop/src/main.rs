#![cfg_attr(
    all(target_os = "windows", not(debug_assertions)),
    windows_subsystem = "windows"
)]

mod field;
mod gpu;
mod model;
#[cfg(test)]
mod tests;

use field::{BG, Field, Lens, Selection};
use iced::{
    Color, Element, Event, Fill, Point, Rectangle, Size, Subscription, Task, Theme,
    keyboard::{self, Key, key::Named},
    mouse,
    widget::{button, canvas, checkbox, column, container, row, shader, stack, text},
    window,
};
use model::{Scene, TransferState};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    time::{Duration, Instant},
};

fn main() -> iced::Result {
    let run_started = std::time::SystemTime::now();
    let options = match Options::parse(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    };
    let smoke_output = options.smoke.clone();
    let app = match App::new(options) {
        Ok(app) => app,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    };
    let result = iced::application(move || app.clone(), App::update, App::view)
        .title("Rustytransfer · Native field MVP")
        .theme(Theme::custom(
            "Rustytransfer",
            iced::theme::Palette {
                background: BG,
                text: INK,
                primary: field::BLUE,
                success: field::GREEN,
                danger: field::FAILED,
                warning: field::RELAY,
            },
        ))
        .style(|_, _| iced::theme::Style {
            background_color: BG,
            text_color: Color::from_rgb8(220, 232, 245),
        })
        .window(window::Settings {
            size: Size::new(1280.0, 840.0),
            min_size: Some(Size::new(800.0, 650.0)),
            ..Default::default()
        })
        .subscription(App::subscription)
        .run();
    if let Some(dir) = smoke_output {
        let completed = [
            "overview.png",
            "lens.png",
            "dragged-lens.png",
            "frame-cadence.json",
        ]
        .iter()
        .all(|name| {
            std::fs::metadata(dir.join(name))
                .and_then(|m| m.modified())
                .is_ok_and(|time| time >= run_started)
        });
        if !completed {
            eprintln!(
                "Smoke test did not complete; the window closed before all evidence was written."
            );
            std::process::exit(1);
        }
    }
    result
}

#[derive(Default)]
struct Options {
    scenario: usize,
    replay: Option<PathBuf>,
    smoke: Option<PathBuf>,
}
impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut options = Self::default();
        let mut args = args;
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--scenario" => {
                    options.scenario = match args.next().as_deref() {
                        Some("direct") => 0,
                        Some("relay") => 1,
                        Some("resume") => 2,
                        _ => return Err("--scenario requires direct, relay, or resume".into()),
                    }
                }
                "--replay" => {
                    options.replay = Some(
                        args.next()
                            .map(PathBuf::from)
                            .ok_or("--replay requires an NDJSON file")?,
                    )
                }
                "--smoke-test" => {
                    options.smoke = Some(
                        args.next()
                            .map(PathBuf::from)
                            .ok_or("--smoke-test requires an output directory")?,
                    )
                }
                "--help" | "-h" => {
                    println!(
                        "Rustytransfer native visual MVP (event replay; does not transfer files)\n\
                        cargo run --manifest-path desktop/Cargo.toml --release --locked -- [--scenario direct|relay|resume] [--replay FILE] [--smoke-test DIR]"
                    );
                    std::process::exit(0);
                }
                _ => return Err(format!("unknown option: {arg}")),
            }
        }
        Ok(options)
    }
}
const FIXTURES: [&str; 3] = [
    include_str!("../fixtures/direct.ndjson"),
    include_str!("../fixtures/relay.ndjson"),
    include_str!("../fixtures/resume.ndjson"),
];
const MUTED: Color = Color::from_rgb8(117, 140, 165);
const INK: Color = Color::from_rgb8(228, 237, 249);

#[derive(Clone)]
struct App {
    scene: Scene,
    events: Vec<model::Event>,
    index: usize,
    scenario: usize,
    custom: bool,
    playing: bool,
    last_event: Instant,
    start: Instant,
    last_frame: Option<Instant>,
    intervals: Vec<f64>,
    field: Field,
    overrides: BTreeMap<String, Point>,
    pointer: Option<Point>,
    selection: Option<Selection>,
    lens: Option<Lens>,
    drag: Option<Drag>,
    reduced_motion: bool,
    diagnostics: bool,
    error: Option<String>,
    smoke: Option<PathBuf>,
    smoke_stage: u8,
    drag_revision: Option<u64>,
    motion_rebuilds: u64,
    drag_resizes: u64,
}
#[derive(Clone)]
enum Drag {
    Lens {
        offset: Point,
    },
    Node {
        id: String,
        offset: Point,
        origin: Point,
        moved: bool,
    },
}
#[derive(Debug, Clone)]
enum Message {
    Tick(Instant),
    Resize(Size),
    Move(Option<Point>),
    Down(Point),
    Up,
    Scenario(usize),
    Play,
    Restart,
    Step,
    Inspect,
    Reduced(bool),
    Diagnostics,
    Keyboard(Key, keyboard::Modifiers),
    Captured(&'static str, window::Screenshot),
    Saved(Result<(), String>),
}
impl App {
    fn new(options: Options) -> Result<Self, String> {
        let input = match &options.replay {
            Some(path) => {
                std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?
            }
            None => FIXTURES[options.scenario].into(),
        };
        let events = model::parse_ndjson(&input)?;
        if events.is_empty() {
            return Err("event stream is empty".into());
        }
        let mut app = Self {
            scene: Scene::default(),
            events,
            index: 0,
            scenario: options.scenario,
            custom: options.replay.is_some(),
            playing: false,
            last_event: Instant::now(),
            start: Instant::now(),
            last_frame: None,
            intervals: Vec::new(),
            field: Field::default(),
            overrides: BTreeMap::new(),
            pointer: None,
            selection: None,
            lens: None,
            drag: None,
            reduced_motion: false,
            diagnostics: options.smoke.is_some(),
            error: None,
            smoke: options.smoke,
            smoke_stage: 0,
            drag_revision: None,
            motion_rebuilds: 0,
            drag_resizes: 0,
        };
        // Open on the first observed transferring event; all data is from the replay.
        while app.index < app.events.len() {
            app.step();
            if app
                .scene
                .transfers
                .values()
                .any(|t| t.state == TransferState::Transferring)
            {
                break;
            }
        }
        Ok(app)
    }
    fn rebuild(&mut self) {
        if self.field.size.width > 0.0 {
            self.field
                .rebuild(&self.scene, self.field.size, &self.overrides);
        }
    }
    fn step(&mut self) {
        if let Some(event) = self.events.get(self.index).cloned() {
            if let Err(error) = self.scene.apply(event) {
                self.error = Some(error);
                self.playing = false;
                return;
            }
            self.index += 1;
            self.rebuild();
            if !self.selection_exists() {
                self.selection = None;
                self.lens = None;
                self.drag = None;
            }
        }
        if self.index == self.events.len() {
            self.playing = false;
        }
    }
    fn selection_exists(&self) -> bool {
        match &self.selection {
            None => true,
            Some(Selection::Node(id)) => {
                self.scene.nodes.contains_key(id)
                    || self.scene.source.as_ref().is_some_and(|s| &s.id == id)
            }
            Some(Selection::Route(id)) => self.scene.routes.contains_key(id),
        }
    }
    fn select(&mut self, selection: Selection) {
        self.selection = Some(selection);
        self.lens = Some(Lens::new(
            Point::new(self.field.size.width * 0.52, self.field.size.height * 0.50),
            self.field.size,
        ));
        self.pointer = None;
    }
    fn inspect(&mut self) {
        if let Some(route) = self
            .scene
            .routes
            .values()
            .find(|r| r.kind == model::RouteKind::Payload)
        {
            self.select(Selection::Route(route.id.clone()));
        } else if let Some(source) = &self.scene.source {
            self.select(Selection::Node(source.id.clone()));
        }
    }
    fn motion_pointer(&self) -> Option<Point> {
        self.pointer.filter(|_| !self.reduced_motion)
    }
    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::Resize(size) => {
                if self.smoke_stage == 4 {
                    self.drag_resizes += 1;
                }
                self.field.rebuild(&self.scene, size, &self.overrides);
                if let Some(lens) = self.lens {
                    self.lens = Some(Lens::new(clamp_lens(lens.center, size), size));
                }
            }
            Message::Move(point) => {
                self.pointer = point;
                if let Some(p) = point {
                    match self.drag.clone() {
                        Some(Drag::Lens { offset }) => {
                            if let Some(lens) = &mut self.lens {
                                lens.center = clamp_lens(
                                    Point::new(p.x - offset.x, p.y - offset.y),
                                    self.field.size,
                                );
                            }
                        }
                        Some(Drag::Node {
                            id,
                            offset,
                            origin,
                            moved,
                        }) => {
                            let next = Point::new(p.x - offset.x, p.y - offset.y);
                            self.overrides.insert(
                                id.clone(),
                                Point::new(
                                    next.x.clamp(30.0, self.field.size.width - 30.0),
                                    next.y.clamp(30.0, self.field.size.height - 30.0),
                                ),
                            );
                            self.drag = Some(Drag::Node {
                                id,
                                offset,
                                origin,
                                moved: moved || p.distance(origin) > 4.0,
                            });
                            self.rebuild();
                        }
                        None => {}
                    }
                }
            }
            Message::Down(p) => {
                if let Some(lens) = self.lens.filter(|l| p.distance(l.center) < l.core) {
                    self.drag = Some(Drag::Lens {
                        offset: Point::new(p.x - lens.center.x, p.y - lens.center.y),
                    });
                } else if let Some(selection) = self.field.hit(p, self.motion_pointer(), self.lens)
                {
                    if let Selection::Node(id) = &selection {
                        if self.scene.source.as_ref().is_none_or(|s| &s.id != id) {
                            if let Some(raw) = self.field.nodes.get(id) {
                                self.drag = Some(Drag::Node {
                                    id: id.clone(),
                                    offset: Point::new(p.x - raw.x, p.y - raw.y),
                                    origin: p,
                                    moved: false,
                                });
                            }
                        } else {
                            self.select(selection);
                        }
                    } else {
                        self.select(selection);
                    }
                } else {
                    self.selection = None;
                    self.lens = None;
                }
            }
            Message::Up => {
                if let Some(Drag::Node {
                    id, moved: false, ..
                }) = self.drag.take()
                {
                    self.select(Selection::Node(id));
                }
            }
            Message::Scenario(index) => {
                self.events = match model::parse_ndjson(FIXTURES[index]) {
                    Ok(events) => events,
                    Err(e) => {
                        self.error = Some(e);
                        return Task::none();
                    }
                };
                self.scenario = index;
                self.custom = false;
                self.scene = Scene::default();
                self.index = 0;
                self.selection = None;
                self.lens = None;
                self.drag = None;
                self.overrides.clear();
                self.error = None;
                while self.index < self.events.len() {
                    self.step();
                    if self
                        .scene
                        .transfers
                        .values()
                        .any(|t| t.state == TransferState::Transferring)
                    {
                        break;
                    }
                }
                self.playing = false;
            }
            Message::Play => {
                if self.index == self.events.len() {
                    return self.update(Message::Restart);
                }
                self.playing = !self.playing;
                self.last_event = Instant::now();
            }
            Message::Restart => {
                self.scene = Scene::default();
                self.index = 0;
                self.selection = None;
                self.lens = None;
                self.drag = None;
                self.overrides.clear();
                self.error = None;
                self.step();
                self.playing = true;
                self.last_event = Instant::now();
            }
            Message::Step => {
                self.playing = false;
                self.step();
            }
            Message::Inspect => self.inspect(),
            Message::Reduced(value) => {
                self.reduced_motion = value;
                self.pointer = None;
            }
            Message::Diagnostics => self.diagnostics = !self.diagnostics,
            Message::Keyboard(key, modifiers) => match key.as_ref() {
                Key::Named(Named::Escape) => {
                    self.lens = None;
                    self.selection = None;
                    self.drag = None;
                }
                Key::Named(Named::Space) => return self.update(Message::Play),
                Key::Named(Named::F12) => return self.update(Message::Diagnostics),
                Key::Character("n" | "N") => {
                    let selections: Vec<_> = self
                        .scene
                        .routes
                        .keys()
                        .map(|id| Selection::Route(id.clone()))
                        .chain(
                            self.field
                                .nodes
                                .keys()
                                .map(|id| Selection::Node(id.clone())),
                        )
                        .collect();
                    let index = self
                        .selection
                        .as_ref()
                        .and_then(|s| selections.iter().position(|p| p == s));
                    if !selections.is_empty() {
                        self.select(
                            selections[index.map_or(0, |i| (i + 1) % selections.len())].clone(),
                        );
                    }
                }
                Key::Named(
                    Named::ArrowLeft | Named::ArrowRight | Named::ArrowUp | Named::ArrowDown,
                ) => {
                    if let Some(lens) = &mut self.lens {
                        let distance = if modifiers.shift() { 42.0 } else { 18.0 };
                        match key.as_ref() {
                            Key::Named(Named::ArrowLeft) => lens.center.x -= distance,
                            Key::Named(Named::ArrowRight) => lens.center.x += distance,
                            Key::Named(Named::ArrowUp) => lens.center.y -= distance,
                            _ => lens.center.y += distance,
                        }
                        lens.center = clamp_lens(lens.center, self.field.size);
                    }
                }
                Key::Character("r" | "R") => return self.update(Message::Restart),
                Key::Character("i" | "I") => return self.update(Message::Inspect),
                Key::Character("m" | "M") => {
                    return self.update(Message::Reduced(!self.reduced_motion));
                }
                _ => {}
            },
            Message::Tick(now) => {
                if let Some(previous) = self.last_frame
                    && self.intervals.len() < 20_000
                {
                    self.intervals
                        .push(now.duration_since(previous).as_secs_f64() * 1000.0);
                }
                self.last_frame = Some(now);
                if self.playing && now.duration_since(self.last_event) > Duration::from_millis(1400)
                {
                    self.step();
                    self.last_event = now;
                }
                if self.smoke.is_some() {
                    return self.smoke_tick(now);
                }
            }
            Message::Captured(name, screenshot) => {
                if let Some(dir) = &self.smoke {
                    let result = save_screenshot(dir.join(name), screenshot);
                    return Task::done(Message::Saved(result));
                }
            }
            Message::Saved(result) => {
                if let Err(e) = result {
                    eprintln!("Smoke capture failed: {e}");
                    self.error = Some(e);
                }
            }
        }
        Task::none()
    }
    fn smoke_tick(&mut self, now: Instant) -> Task<Message> {
        let seconds = now.duration_since(self.start).as_secs_f32();
        match self.smoke_stage {
            0 if seconds >= 2.0 => {
                self.smoke_stage = 1;
                return capture("overview.png");
            }
            1 if seconds >= 3.0 => {
                self.inspect();
                self.smoke_stage = 2;
            }
            2 if seconds >= 4.0 => {
                self.smoke_stage = 3;
                return capture("lens.png");
            }
            3 if seconds >= 5.0 => {
                if let Some(lens) = self.lens {
                    let _ = self.update(Message::Down(lens.center));
                }
                self.drag_revision = Some(self.field.revision);
                self.intervals.clear();
                self.smoke_stage = 4;
            }
            4 if seconds < 12.0 => {
                let p = Point::new(
                    self.field.size.width * (0.50 + (seconds * 1.5).sin() * 0.10),
                    self.field.size.height * (0.50 + (seconds * 1.1).cos() * 0.08),
                );
                if !matches!(self.drag, Some(Drag::Lens { .. }))
                    && let Some(lens) = self.lens
                {
                    let _ = self.update(Message::Down(lens.center));
                }
                let revision = self.field.revision;
                let _ = self.update(Message::Move(Some(p)));
                if self.field.revision != revision {
                    self.motion_rebuilds += 1;
                }
            }
            4 => {
                let _ = self.update(Message::Up);
                self.smoke_stage = 5;
                return capture("dragged-lens.png");
            }
            5 if seconds >= 13.0 => {
                let result = self.write_metrics();
                if let Err(e) = result {
                    eprintln!("Smoke test failed: {e}");
                    std::process::exit(1);
                }
                self.smoke_stage = 6;
                return window::oldest().and_then(window::close);
            }
            _ => {}
        }
        Task::none()
    }
    fn write_metrics(&self) -> Result<(), String> {
        if let Some(error) = &self.error {
            return Err(error.clone());
        }
        if self.motion_rebuilds != 0 {
            return Err("lens drag rebuilt static geometry".into());
        }
        let mut samples = self.intervals.clone();
        samples.sort_by(f64::total_cmp);
        let p95 = samples
            .get(samples.len().saturating_sub(1) * 95 / 100)
            .copied()
            .unwrap_or(0.0);
        let report = serde_json::json!({ "measurement": "window frame callback intervals during scripted lens motion (not GPU execution time)",
            "frames": samples.len(), "mean_ms": samples.iter().sum::<f64>() / samples.len().max(1) as f64, "p95_ms": p95,
            "max_ms": samples.last(), "geometry_rebuilds_during_lens_drag": 0, "dots": self.field.dots.len(),
            "resize_rebuilds_during_measurement": self.drag_resizes, "geometry_revision_at_drag_start": self.drag_revision,
            "geometry_revision_at_drag_end": self.field.revision,
            "logical_field_size": [self.field.size.width, self.field.size.height], "intervals_ms": self.intervals,
            "limitations": "WSLg/compositor/adapter can affect cadence; this is not a transfer benchmark or a performance gate" });
        let dir = self.smoke.as_ref().ok_or("missing smoke directory")?;
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        std::fs::write(
            dir.join("frame-cadence.json"),
            serde_json::to_vec_pretty(&report).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        println!(
            "Native motion smoke test: {} frames, p95 {:.2} ms, 0 geometry rebuilds; {}",
            samples.len(),
            p95,
            dir.display()
        );
        Ok(())
    }
    fn subscription(&self) -> Subscription<Message> {
        let keyboard = iced::event::listen().filter_map(|event| match event {
            Event::Keyboard(keyboard::Event::KeyPressed { key, modifiers, .. }) => {
                Some(Message::Keyboard(key, modifiers))
            }
            Event::Window(window::Event::Unfocused) => Some(Message::Up),
            _ => None,
        });
        // Demand-driven when paused/reduced; only active packet/replay/smoke motion ticks.
        let animated = self.smoke.is_some()
            || self.playing
            || (!self.reduced_motion
                && self
                    .scene
                    .transfers
                    .values()
                    .any(|t| t.state == TransferState::Transferring));
        if animated {
            Subscription::batch([keyboard, window::frames().map(Message::Tick)])
        } else {
            keyboard
        }
    }
    fn view(&self) -> Element<'_, Message> {
        let header = row![
            column![
                text("[ : ]  rustytransfer").size(24).color(INK),
                text("YOUR FILES. DIRECT.").size(10).color(MUTED)
            ]
            .spacing(5),
            iced::widget::space().width(Fill),
            button("Direct").on_press(Message::Scenario(0)).style(
                if self.scenario == 0 && !self.custom {
                    button::primary
                } else {
                    quiet_button
                }
            ),
            button("Relay").on_press(Message::Scenario(1)).style(
                if self.scenario == 1 && !self.custom {
                    button::primary
                } else {
                    quiet_button
                }
            ),
            button("Failure / resume")
                .on_press(Message::Scenario(2))
                .style(if self.scenario == 2 && !self.custom {
                    button::primary
                } else {
                    quiet_button
                }),
        ]
        .spacing(10)
        .align_y(iced::Center);
        let controls = row![
            text(if self.custom {
                "EVENT REPLAY · CUSTOM"
            } else {
                "DEMO · EVENT REPLAY"
            })
            .size(11)
            .color(field::RELAY),
            text("Click a route. Move the field.").size(12).color(MUTED),
            iced::widget::space().width(Fill),
            button(if self.playing { "Pause" } else { "Play" })
                .on_press(Message::Play)
                .style(quiet_button),
            button("Step").on_press(Message::Step).style(quiet_button),
            button("Restart")
                .on_press(Message::Restart)
                .style(quiet_button),
            button("Inspect payload")
                .on_press(Message::Inspect)
                .style(quiet_button),
        ]
        .spacing(10)
        .align_y(iced::Center);
        let field_view = stack![
            shader(gpu::View {
                field: &self.field,
                pointer: self.pointer,
                lens: self.lens,
                time: self.start.elapsed().as_secs_f32(),
                reduced_motion: self.reduced_motion
            })
            .width(Fill)
            .height(Fill),
            canvas(self).width(Fill).height(Fill),
        ];
        let transfer = self.scene.transfers.values().next();
        let status = if let Some(error) = &self.error {
            error.clone()
        } else if let Some(t) = transfer {
            format!(
                "{}  ·  {}  ·  {} / {}",
                t.file_name,
                transfer_state(t.state),
                bytes(t.bytes_sent),
                bytes(t.bytes_total)
            )
        } else {
            "Waiting for a transfer event".into()
        };
        let footer = row![
            text(status).size(13).color(if self.error.is_some() {
                field::FAILED
            } else {
                INK
            }),
            iced::widget::space().width(Fill),
            checkbox(self.reduced_motion)
                .label("Reduced motion")
                .on_toggle(Message::Reduced)
                .size(14),
        ]
        .spacing(15)
        .align_y(iced::Center);
        container(column![header, controls, field_view, footer,
            text("Visual MVP · No files are sent   /   Drag peers or the lens · Esc closes · Arrow keys move the lens").size(11).color(MUTED)]
            .spacing(14)).padding(24).into()
    }
}
fn clamp_lens(p: Point, size: Size) -> Point {
    let lens = Lens::new(p, size);
    let margin = lens.core * 0.74;
    Point::new(
        p.x.clamp(margin, size.width.max(margin * 2.0) - margin),
        p.y.clamp(margin, size.height.max(margin * 2.0) - margin),
    )
}
fn quiet_button(_: &Theme, status: button::Status) -> button::Style {
    let highlighted = matches!(status, button::Status::Hovered | button::Status::Pressed);
    button::Style {
        background: Some(
            Color::from_rgb8(
                if highlighted { 21 } else { 12 },
                if highlighted { 38 } else { 23 },
                if highlighted { 59 } else { 38 },
            )
            .into(),
        ),
        text_color: INK,
        border: iced::Border {
            color: Color::from_rgb8(32, 48, 68),
            width: 1.0,
            radius: 5.0.into(),
        },
        ..Default::default()
    }
}
fn bytes(value: u64) -> String {
    if value >= 1_000_000_000 {
        format!("{:.2} GB", value as f64 / 1e9)
    } else if value >= 1_000_000 {
        format!("{:.1} MB", value as f64 / 1e6)
    } else if value >= 1000 {
        format!("{:.1} kB", value as f64 / 1000.0)
    } else {
        format!("{value} B")
    }
}
fn transfer_state(state: TransferState) -> &'static str {
    match state {
        TransferState::Staged => "Staged",
        TransferState::Connecting => "Connecting",
        TransferState::Transferring => "Transferring",
        TransferState::Paused => "Paused",
        TransferState::Verifying => "Verifying",
        TransferState::Complete => "Complete",
        TransferState::Failed => "Failed",
    }
}
fn capture(name: &'static str) -> Task<Message> {
    window::oldest().and_then(move |id| {
        window::screenshot(id).map(move |screenshot| Message::Captured(name, screenshot))
    })
}
fn save_screenshot(path: PathBuf, screenshot: window::Screenshot) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let file = std::fs::File::create(path).map_err(|e| e.to_string())?;
    let mut encoder = png::Encoder::new(
        std::io::BufWriter::new(file),
        screenshot.size.width,
        screenshot.size.height,
    );
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    encoder
        .write_header()
        .map_err(|e| e.to_string())?
        .write_image_data(&screenshot.rgba)
        .map_err(|e| e.to_string())
}

impl canvas::Program<Message> for App {
    type State = ();
    fn update(
        &self,
        _: &mut (),
        event: &Event,
        bounds: Rectangle,
        cursor: mouse::Cursor,
    ) -> Option<canvas::Action<Message>> {
        let position = cursor
            .position()
            .map(|p| Point::new(p.x - bounds.x, p.y - bounds.y));
        let message = match event {
            Event::Window(window::Event::RedrawRequested(_))
                if self.field.size != bounds.size() =>
            {
                Message::Resize(bounds.size())
            }
            Event::Mouse(mouse::Event::CursorMoved { .. }) => Message::Move(position.filter(|p| {
                Rectangle::with_size(bounds.size()).contains(*p) || self.drag.is_some()
            })),
            Event::Mouse(mouse::Event::CursorLeft) => Message::Move(None),
            Event::Mouse(mouse::Event::ButtonPressed(mouse::Button::Left)) => {
                Message::Down(cursor.position_in(bounds)?)
            }
            Event::Mouse(mouse::Event::ButtonReleased(mouse::Button::Left))
                if self.drag.is_some() =>
            {
                Message::Up
            }
            _ => return None,
        };
        Some(canvas::Action::publish(message).and_capture())
    }
    fn mouse_interaction(
        &self,
        _: &(),
        bounds: Rectangle,
        cursor: mouse::Cursor,
    ) -> mouse::Interaction {
        if self.drag.is_some() {
            mouse::Interaction::Grabbing
        } else if cursor.position_in(bounds).is_some_and(|p| {
            self.lens.is_some_and(|l| p.distance(l.center) < l.core)
                || self
                    .field
                    .hit(p, self.motion_pointer(), self.lens)
                    .is_some()
        }) {
            mouse::Interaction::Grab
        } else {
            mouse::Interaction::default()
        }
    }
    fn draw(
        &self,
        _: &(),
        renderer: &iced::Renderer,
        _: &Theme,
        bounds: Rectangle,
        _: mouse::Cursor,
    ) -> Vec<canvas::Geometry> {
        let mut frame = canvas::Frame::new(renderer, bounds.size());
        for (id, raw) in &self.field.nodes {
            let node = self
                .scene
                .nodes
                .get(id)
                .or_else(|| self.scene.source.as_ref().filter(|s| &s.id == id));
            let Some(node) = node else {
                continue;
            };
            let p = field::warp(*raw, self.motion_pointer(), self.lens);
            let color = field::node_color(node.kind);
            let source = node.kind == model::NodeKind::LocalSource;
            let radius = if source { 16.0 } else { 7.0 };
            let selected = self.selection == Some(Selection::Node(id.clone()));
            frame.stroke(
                &canvas::Path::circle(p, radius + if selected { 12.0 } else { 7.0 }),
                canvas::Stroke::default()
                    .with_color(Color { a: 0.25, ..color })
                    .with_width(1.0),
            );
            frame.fill(&canvas::Path::circle(p, radius), color);
            if source {
                label(
                    &mut frame,
                    Point::new(p.x, p.y - 7.0),
                    "[ : ]",
                    12.0,
                    BG,
                    true,
                );
            }
            label(
                &mut frame,
                Point::new(p.x, p.y + radius + 18.0),
                &node.label,
                if source { 15.0 } else { 12.0 },
                color,
                true,
            );
            let detail = match node.kind {
                model::NodeKind::LocalSource => "LOCAL SOURCE",
                model::NodeKind::Peer => "RECEIVER",
                model::NodeKind::Relay => "RELAY",
                model::NodeKind::Signaling => "SIGNALING",
                _ => "LOCAL NETWORK",
            };
            label(
                &mut frame,
                Point::new(p.x, p.y + radius + 36.0),
                detail,
                9.0,
                MUTED,
                true,
            );
        }
        if let (Some(lens), Some(selection)) = (self.lens, &self.selection) {
            let c = lens.center;
            let width = lens.core * 1.40;
            let left = c.x - width * 0.5;
            let right = c.x + width * 0.5;
            let (title, subtitle, color, facts) = self.inspector(selection);
            // The information core moves rigidly; no text participates in deformation.
            label(
                &mut frame,
                Point::new(c.x, c.y - 121.0),
                "CONTEXT / DRAG TO EXPLORE",
                9.0,
                MUTED,
                true,
            );
            label(
                &mut frame,
                Point::new(c.x, c.y - 84.0),
                &title,
                26.0,
                color,
                true,
            );
            label(
                &mut frame,
                Point::new(c.x, c.y - 46.0),
                &subtitle,
                11.0,
                MUTED,
                true,
            );
            frame.stroke(
                &canvas::Path::line(Point::new(left, c.y - 18.0), Point::new(right, c.y - 18.0)),
                canvas::Stroke::default().with_color(Color::from_rgb8(31, 48, 67)),
            );
            for (i, (key, value)) in facts.iter().enumerate() {
                let y = c.y + 2.0 + i as f32 * 31.0;
                label(&mut frame, Point::new(left, y), key, 10.0, MUTED, false);
                label_right(&mut frame, Point::new(right, y), value, 12.0, INK);
            }
            label(
                &mut frame,
                Point::new(c.x, c.y + 158.0),
                "DEMO DATA   ·   ESC TO CLOSE",
                9.0,
                MUTED,
                true,
            );
        } else {
            label(
                &mut frame,
                Point::new(bounds.width * 0.5, bounds.height * 0.92),
                "A field of connections. An undisturbed space for context.",
                12.0,
                MUTED,
                true,
            );
        }
        if self.diagnostics {
            let recent: Vec<_> = self.intervals.iter().rev().take(120).copied().collect();
            let mean = recent.iter().sum::<f64>() / recent.len().max(1) as f64;
            label(
                &mut frame,
                Point::new(12.0, 12.0),
                &format!(
                    "Frame callbacks {:.1} ms  /  {} dots  /  geometry revision {}",
                    mean,
                    self.field.dots.len(),
                    self.field.revision
                ),
                10.0,
                MUTED,
                false,
            );
        }
        vec![frame.into_geometry()]
    }
}
impl App {
    fn inspector(&self, selection: &Selection) -> (String, String, Color, Vec<(String, String)>) {
        match selection {
            Selection::Node(id) => {
                let node = self
                    .scene
                    .nodes
                    .get(id)
                    .or_else(|| self.scene.source.as_ref().filter(|s| &s.id == id));
                if let Some(n) = node {
                    (
                        n.label.clone(),
                        "Endpoint / recorded event".into(),
                        field::node_color(n.kind),
                        vec![
                            ("REACHABILITY".into(), format!("{:?}", n.reachability)),
                            (
                                "TRANSPORT".into(),
                                n.transport.clone().unwrap_or("Unknown".into()),
                            ),
                            ("IDENTITY".into(), n.id.clone()),
                            ("NETWORK / TRUST".into(), "Not supplied".into()),
                        ],
                    )
                } else {
                    ("Unavailable".into(), String::new(), MUTED, Vec::new())
                }
            }
            Selection::Route(id) => {
                if let Some(r) = self.scene.routes.get(id) {
                    let transfer = r
                        .transfer_id
                        .as_ref()
                        .and_then(|id| self.scene.transfers.get(id));
                    let label = |id: &String| {
                        self.scene
                            .nodes
                            .get(id)
                            .or_else(|| self.scene.source.as_ref().filter(|s| &s.id == id))
                            .map_or(id.clone(), |n| n.label.clone())
                    };
                    let title = if r.kind == model::RouteKind::Signaling {
                        "Signaling route"
                    } else if r.topology == model::RouteTopology::Relay {
                        "Relayed payload"
                    } else {
                        match r.topology {
                            model::RouteTopology::Direct => "Direct payload",
                            model::RouteTopology::Local => "Local payload",
                            model::RouteTopology::Indirect => "Indirect payload",
                            model::RouteTopology::Relay => "Relayed payload",
                        }
                    };
                    (
                        title.into(),
                        format!("{}  →  {}", label(&r.from), label(&r.to)),
                        field::route_color(r.kind, r.topology, r.state),
                        vec![
                            ("ROUTE".into(), format!("{:?} / {:?}", r.topology, r.state)),
                            (
                                "TRANSPORT".into(),
                                r.transport.clone().unwrap_or("Unknown".into()),
                            ),
                            (
                                "TRANSFER".into(),
                                transfer.map_or("Not supplied".into(), |t| {
                                    transfer_state(t.state).into()
                                }),
                            ),
                            (
                                "PROGRESS".into(),
                                transfer.map_or("Not supplied".into(), |t| {
                                    format!("{} / {}", bytes(t.bytes_sent), bytes(t.bytes_total))
                                }),
                            ),
                        ],
                    )
                } else {
                    ("Unavailable".into(), String::new(), MUTED, Vec::new())
                }
            }
        }
    }
}
fn label(
    frame: &mut canvas::Frame,
    position: Point,
    value: &str,
    size: f32,
    color: Color,
    center: bool,
) {
    frame.fill_text(canvas::Text {
        content: value.into(),
        position,
        color,
        size: size.into(),
        align_x: if center {
            iced::alignment::Horizontal::Center.into()
        } else {
            iced::alignment::Horizontal::Left.into()
        },
        ..Default::default()
    });
}
fn label_right(frame: &mut canvas::Frame, position: Point, value: &str, size: f32, color: Color) {
    frame.fill_text(canvas::Text {
        content: value.into(),
        position,
        color,
        size: size.into(),
        align_x: iced::alignment::Horizontal::Right.into(),
        ..Default::default()
    });
}
