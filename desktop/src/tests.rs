use super::*;
use iced::{Point, Size};
use std::sync::Arc;

fn app() -> App {
    let mut app = App::new(Options::default()).expect("bundled direct replay loads");
    let _ = app.update(Message::Resize(Size::new(1200.0, 700.0)));
    app
}

#[test]
fn dragging_lens_moves_only_the_lens_and_reuses_field_geometry() {
    let mut app = app();
    let _ = app.update(Message::Move(Some(Point::new(5.0, 8.0))));
    let _ = app.update(Message::Inspect);
    let start = app.lens.expect("inspect opens a lens");
    let dots = Arc::clone(&app.field.dots);
    let revision = app.field.revision;

    let _ = app.update(Message::Down(start.center));
    for point in [
        Point::new(start.center.x + 15.0, start.center.y + 10.0),
        Point::new(start.center.x + 45.0, start.center.y + 30.0),
        Point::new(start.center.x + 80.0, start.center.y + 55.0),
    ] {
        let _ = app.update(Message::Move(Some(point)));
    }
    let moved = app.lens.expect("lens remains open during drag");
    assert_ne!(moved.center, start.center);
    assert_eq!(app.field.revision, revision);
    assert!(Arc::ptr_eq(&dots, &app.field.dots));

    let _ = app.update(Message::Up);
    assert!(app.drag.is_none());
    assert_eq!(app.lens.expect("lens remains open").center, moved.center);
    assert_eq!(app.field.revision, revision);
    assert!(Arc::ptr_eq(&dots, &app.field.dots));
}

#[test]
fn peer_drag_rebuilds_geometry_while_source_stays_pinned() {
    let mut app = app();
    let phone = app.field.nodes["phone"];
    let revision = app.field.revision;
    let _ = app.update(Message::Down(phone));
    let _ = app.update(Message::Move(Some(Point::new(
        phone.x + 50.0,
        phone.y + 40.0,
    ))));

    let expected = Point::new(phone.x + 50.0, phone.y + 40.0);
    assert_eq!(app.overrides.get("phone"), Some(&expected));
    assert!(app.field.revision > revision);
    assert_eq!(app.field.nodes["phone"], expected);
    let _ = app.update(Message::Up);
    assert!(app.drag.is_none());

    let _ = app.update(Message::Move(None));
    let source = app.field.nodes["source"];
    let pinned_revision = app.field.revision;
    let _ = app.update(Message::Down(source));
    assert!(
        app.drag.is_none(),
        "source endpoints cannot start a node drag"
    );
    let _ = app.update(Message::Move(Some(Point::new(
        source.x + 60.0,
        source.y + 45.0,
    ))));
    let _ = app.update(Message::Up);
    assert!(!app.overrides.contains_key("source"));
    assert_eq!(app.field.nodes["source"], source);
    assert_eq!(app.field.revision, pinned_revision);
}

#[test]
fn restart_and_scenario_switch_reset_interaction_and_follow_replay_state() {
    let mut app = app();
    let _ = app.update(Message::Inspect);
    app.overrides
        .insert("phone".into(), Point::new(400.0, 300.0));
    let _ = app.update(Message::Play);
    assert!(app.playing);
    assert!(app.selection.is_some() && app.lens.is_some());

    let _ = app.update(Message::Restart);
    assert!(app.playing);
    assert!(app.selection.is_none() && app.lens.is_none() && app.drag.is_none());
    assert!(app.overrides.is_empty());
    assert!(app.scene.source.is_some());
    assert!(
        app.scene.transfers.is_empty(),
        "restart has applied only the initial snapshot"
    );
    assert_eq!(app.index, 1);

    let _ = app.update(Message::Scenario(1));
    assert_eq!(app.scenario, 1);
    assert!(app.scene.nodes.contains_key("tablet"));
    assert!(app.scene.nodes.contains_key("relay"));
    assert_eq!(app.scene.transfers["t1"].target_id, "tablet");
    assert!(!app.playing);

    let _ = app.update(Message::Scenario(2));
    assert_eq!(app.scenario, 2);
    assert!(app.scene.nodes.contains_key("phone"));
    assert!(!app.scene.nodes.contains_key("relay"));
    assert_eq!(app.scene.transfers["t1"].target_id, "phone");
    assert!(app.selection.is_none() && app.lens.is_none() && app.overrides.is_empty());
}

#[test]
fn reduced_motion_clears_pointer_but_keeps_static_lens_and_inspection() {
    let mut app = app();
    let _ = app.update(Message::Inspect);
    let _ = app.update(Message::Move(Some(Point::new(350.0, 260.0))));
    let lens_before = app.lens.expect("inspect opens a lens");
    let selection = app.selection.clone().expect("inspect selects a route");
    assert!(app.motion_pointer().is_some());

    let _ = app.update(Message::Reduced(true));
    let lens_after = app.lens.expect("reduced motion retains lens");
    assert!(app.pointer.is_none());
    assert!(app.motion_pointer().is_none());
    assert_eq!(lens_after.center, lens_before.center);
    assert_eq!(lens_after.core, lens_before.core);
    let (title, _, _, facts) = app.inspector(&selection);
    assert!(!title.is_empty());
    assert!(facts.iter().any(|(name, _)| name == "ROUTE"));
}

#[test]
fn command_line_options_reject_missing_or_invalid_values() {
    for args in [
        vec!["--scenario"],
        vec!["--scenario", "local"],
        vec!["--replay"],
        vec!["--smoke-test"],
        vec!["--unknown"],
    ] {
        assert!(Options::parse(args.into_iter().map(str::to_owned)).is_err());
    }

    let options = Options::parse(
        [
            "--scenario",
            "relay",
            "--replay",
            "events.ndjson",
            "--smoke-test",
            "out",
        ]
        .into_iter()
        .map(str::to_owned),
    )
    .expect("valid options parse");
    assert_eq!(options.scenario, 1);
    assert_eq!(options.replay, Some(PathBuf::from("events.ndjson")));
    assert_eq!(options.smoke, Some(PathBuf::from("out")));
}
