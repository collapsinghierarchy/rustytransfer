use super::*;
use iced::{Point, Size};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

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
fn peer_and_source_drags_rebuild_geometry_and_move_both_nodes() {
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
    let _ = app.update(Message::Down(source));
    let _ = app.update(Message::Move(Some(Point::new(
        source.x + 60.0,
        source.y + 45.0,
    ))));
    let source_expected = Point::new(source.x + 60.0, source.y + 45.0);
    assert_eq!(app.overrides.get("source"), Some(&source_expected));
    assert_eq!(app.field.nodes["source"], source_expected);
    let _ = app.update(Message::Up);
    assert!(app.drag.is_none());
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

fn payload_route_id(app: &App) -> String {
    app.scene
        .routes
        .values()
        .find(|route| route.kind == model::RouteKind::Payload)
        .expect("direct fixture has a payload route")
        .id
        .clone()
}

fn route_hit_point(app: &App, id: &str) -> Point {
    let route = app
        .field
        .routes
        .iter()
        .find(|route| route.id == id)
        .expect("route is in field geometry");
    route
        .points
        .iter()
        .copied()
        .find(|point| {
            app.field.hit(*point, None, None, None) == Some(Selection::Route(id.to_owned()))
        })
        .expect("route geometry has an unambiguous hit point")
}

fn action_labels(app: &App) -> Vec<&'static str> {
    app.item_actions()
        .into_iter()
        .map(|(label, _)| label)
        .collect()
}

#[test]
fn node_click_selects_on_release_after_small_jitter() {
    let mut app = app();
    let peer = app.field.nodes["phone"];
    let _ = app.update(Message::Down(peer));
    assert!(app.selection.is_none() && app.lens.is_none());

    let _ = app.update(Message::Move(Some(Point::new(peer.x + 3.0, peer.y + 2.0))));
    assert!(app.selection.is_none() && app.lens.is_none());
    let _ = app.update(Message::Up);
    assert_eq!(app.selection, Some(Selection::Node("phone".into())));
    assert!(app.lens.is_some());
    assert!(app.drag.is_none());
    assert!(!app.overrides.contains_key("phone"));
}

#[test]
fn dragging_node_past_threshold_then_back_does_not_select_it() {
    let mut app = app();
    let peer = app.field.nodes["phone"];
    let _ = app.update(Message::Down(peer));
    let _ = app.update(Message::Move(Some(Point::new(
        peer.x + 18.0,
        peer.y + 14.0,
    ))));
    assert!(app.overrides.contains_key("phone"));
    let _ = app.update(Message::Move(Some(peer)));
    let _ = app.update(Message::Up);
    assert_eq!(app.overrides.get("phone"), Some(&peer));
    assert!(app.selection.is_none() && app.lens.is_none());
    assert!(app.drag.is_none());
}

#[test]
fn cancelling_focus_gesture_never_turns_press_into_click() {
    let mut app = app();
    let peer = app.field.nodes["phone"];
    let _ = app.update(Message::Down(peer));
    assert!(app.drag.is_some());
    let _ = app.update(Message::CancelGesture);
    let _ = app.update(Message::Up);
    assert!(app.drag.is_none());
    assert!(app.selection.is_none() && app.lens.is_none());
}

#[test]
fn route_selects_only_on_release_and_drag_does_not_open_lens() {
    let mut app = app();
    let route_id = payload_route_id(&app);
    let point = route_hit_point(&app, &route_id);
    let _ = app.update(Message::Down(point));
    assert!(app.selection.is_none() && app.lens.is_none());
    let _ = app.update(Message::Up);
    assert_eq!(app.selection, Some(Selection::Route(route_id.clone())));
    assert!(app.lens.is_some());

    let mut dragged = self::app();
    let route_id = payload_route_id(&dragged);
    let point = route_hit_point(&dragged, &route_id);
    let _ = dragged.update(Message::Down(point));
    let _ = dragged.update(Message::Move(Some(Point::new(
        point.x + 24.0,
        point.y + 12.0,
    ))));
    let _ = dragged.update(Message::Up);
    assert!(dragged.selection.is_none() && dragged.lens.is_none());
    assert!(dragged.drag.is_none());
}

#[test]
fn route_hover_is_ephemeral_and_pointer_clear_removes_it() {
    let mut app = app();
    let route_id = payload_route_id(&app);
    let route = app
        .field
        .routes
        .iter()
        .find(|route| route.id == route_id)
        .unwrap();
    let candidates = route.points.clone();
    let mut hovered_route = false;
    'points: for point in candidates {
        for (dx, dy) in [
            (0.0, 0.0),
            (12.0, 0.0),
            (-12.0, 0.0),
            (0.0, 12.0),
            (0.0, -12.0),
        ] {
            let _ = app.update(Message::Move(Some(Point::new(point.x + dx, point.y + dy))));
            if app.hovered == Some(Selection::Route(route_id.clone())) {
                hovered_route = true;
                break 'points;
            }
        }
    }
    assert!(
        hovered_route,
        "moving over rendered route geometry highlights it"
    );
    assert!(app.selection.is_none());
    let _ = app.update(Message::Move(None));
    assert!(app.pointer.is_none());
    assert!(app.hovered.is_none());
}

#[test]
fn selected_node_capture_tracks_lens_without_rebuilding_field() {
    let mut app = app();
    app.select(Selection::Node("phone".into()));
    app.capture
        .as_mut()
        .expect("node selection starts capture")
        .began = Instant::now() - Duration::from_secs(1);
    let original = app.field.nodes["phone"];
    let dots = Arc::clone(&app.field.dots);
    let revision = app.field.revision;
    let initial_attachment = app.attachment().expect("selected node attaches").delta;

    let lens_start = app.lens.unwrap();
    let _ = app.update(Message::Down(lens_start.center));
    let _ = app.update(Message::Move(Some(Point::new(
        lens_start.center.x + 42.0,
        lens_start.center.y + 26.0,
    ))));
    let lens_end = app.lens.unwrap();
    let captured = app
        .captured_position()
        .expect("captured node has a position");
    assert_eq!(
        captured,
        Point::new(lens_end.center.x, lens_end.center.y - 145.0)
    );
    let attachment = app.attachment().expect("captured node attaches to routes");
    assert_ne!(attachment.delta, initial_attachment);
    assert_eq!(app.field.nodes["phone"], original);
    assert_eq!(app.field.revision, revision);
    assert!(Arc::ptr_eq(&dots, &app.field.dots));

    let route_id = payload_route_id(&app);
    app.select(Selection::Route(route_id));
    assert!(app.capture.is_none());
    assert!(app.attachment().is_none());
}

#[test]
fn filters_hide_geometry_without_deleting_replay_state_and_restore_it() {
    let mut app = app();
    let original_route_count = app.field.routes.len();
    let scene_route_count = app.scene.routes.len();
    assert!(app.field.routes.iter().any(|route| route.id == "signal-t1"));

    let _ = app.update(Message::Visible(FilterKind::Control, false));
    assert!(!app.field.routes.iter().any(|route| route.id == "signal-t1"));
    assert_eq!(app.field.routes.len(), original_route_count - 1);
    assert_eq!(app.scene.routes.len(), scene_route_count);
    assert!(app.scene.routes.contains_key("signal-t1"));
    let _ = app.update(Message::Visible(FilterKind::Control, true));
    assert!(app.field.routes.iter().any(|route| route.id == "signal-t1"));
    assert_eq!(app.field.routes.len(), original_route_count);

    app.select(Selection::Node("phone".into()));
    assert!(app.lens.is_some());
    let _ = app.update(Message::Visible(FilterKind::Peers, false));
    assert!(app.scene.nodes.contains_key("phone"));
    assert!(!app.field.nodes.contains_key("phone"));
    assert!(app.selection.is_none() && app.lens.is_none());
    let _ = app.update(Message::Visible(FilterKind::Peers, true));
    assert!(app.field.nodes.contains_key("phone"));
    assert_eq!(app.scene.routes.len(), scene_route_count);
}

#[test]
fn local_peer_placement_is_explicit_and_unknown_nearby_stays_remote() {
    let mut app = app();
    let source = app.field.nodes["source"];
    app.scene.nodes.get_mut("phone").unwrap().network_scope = model::NetworkScope::Local;
    app.rebuild();
    let local_peer = app.field.nodes["phone"];
    assert_eq!(local_peer.x, source.x);
    assert!(local_peer.y > source.y);

    app.scene.nodes.get_mut("phone").unwrap().network_scope = model::NetworkScope::Unknown;
    app.rebuild();
    let unknown_peer = app.field.nodes["phone"];
    assert_eq!(
        app.scene.nodes["phone"].reachability,
        model::Reachability::Nearby
    );
    assert!(unknown_peer.x > source.x);
}

#[test]
fn item_actions_depend_on_selected_item_kind() {
    let mut app = app();
    app.select(Selection::Node("source".into()));
    let actions = action_labels(&app);
    assert!(actions.contains(&"Play demo"));
    assert!(actions.contains(&"Step"));
    assert!(actions.contains(&"Replay"));

    app.select(Selection::Node("phone".into()));
    let actions = action_labels(&app);
    assert!(actions.contains(&"Inspect transfer"));
    assert!(actions.contains(&"Inspect control"));
    assert!(!actions.iter().any(|label| label.contains("Play")));

    let payload = payload_route_id(&app);
    app.select(Selection::Route(payload));
    let actions = action_labels(&app);
    assert!(actions.contains(&"Play replay"));
    assert!(actions.contains(&"Step event"));
    assert!(actions.contains(&"Replay transfer"));
    assert!(app.capture.is_none() && app.attachment().is_none());

    app.select(Selection::Route("signal-t1".into()));
    let actions = action_labels(&app);
    assert!(actions.contains(&"Inspect source"));
    assert!(actions.contains(&"Inspect target"));
    assert!(!actions.iter().any(|label| label.contains("Replay")));
}
