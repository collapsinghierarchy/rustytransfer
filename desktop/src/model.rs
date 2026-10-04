use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum NodeKind {
    LocalSource,
    Peer,
    Signaling,
    Relay,
    Router,
    Service,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Reachability {
    Unknown,
    Nearby,
    Reachable,
    Offline,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RouteKind {
    Signaling,
    Payload,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RouteTopology {
    Direct,
    Indirect,
    Relay,
    Local,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RouteState {
    Resolving,
    Connected,
    Failed,
    Closed,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TransferState {
    Staged,
    Connecting,
    Transferring,
    Paused,
    Verifying,
    Complete,
    Failed,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PairingState {
    Started,
    AwaitingConfirmation,
    Verified,
    Expired,
    Failed,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct Node {
    pub id: String,
    pub label: String,
    pub kind: NodeKind,
    pub reachability: Reachability,
    #[serde(default)]
    pub transport: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct Route {
    pub id: String,
    #[serde(default)]
    pub transfer_id: Option<String>,
    pub kind: RouteKind,
    pub topology: RouteTopology,
    pub from: String,
    pub via: Vec<String>,
    pub to: String,
    pub state: RouteState,
    #[serde(default)]
    pub transport: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct Transfer {
    pub id: String,
    pub source_id: String,
    pub target_id: String,
    pub file_name: String,
    pub bytes_total: u64,
    pub bytes_sent: u64,
    pub state: TransferState,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct Pairing {
    pub id: String,
    #[serde(default)]
    pub peer_id: Option<String>,
    pub state: PairingState,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    Snapshot {
        source: Node,
        peers: Vec<Node>,
        infrastructure: Vec<Node>,
        routes: Vec<Route>,
        transfers: Vec<Transfer>,
    },
    PeerUpsert {
        peer: Node,
    },
    PeerRemoved {
        peer_id: String,
    },
    InfrastructureUpsert {
        node: Node,
    },
    RouteUpsert {
        route: Route,
    },
    RouteRemoved {
        route_id: String,
    },
    TransferCreated {
        transfer: Transfer,
    },
    TransferProgress {
        transfer: Transfer,
    },
    TransferState {
        transfer: Transfer,
    },
    PairingState {
        pairing: Pairing,
    },
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Scene {
    pub source: Option<Node>,
    pub nodes: BTreeMap<String, Node>,
    pub routes: BTreeMap<String, Route>,
    pub transfers: BTreeMap<String, Transfer>,
    pub pairings: BTreeMap<String, Pairing>,
}

impl Scene {
    pub fn apply(&mut self, event: Event) -> Result<(), String> {
        match event {
            Event::Snapshot {
                source,
                peers,
                infrastructure,
                routes,
                transfers,
            } => {
                for transfer in &transfers {
                    validate_transfer(transfer)?;
                }
                self.source = Some(source);
                self.nodes.clear();
                self.routes.clear();
                self.transfers.clear();
                self.pairings.clear();
                self.nodes.extend(
                    peers
                        .into_iter()
                        .chain(infrastructure)
                        .map(|n| (n.id.clone(), n)),
                );
                self.routes
                    .extend(routes.into_iter().map(|r| (r.id.clone(), r)));
                self.transfers
                    .extend(transfers.into_iter().map(|t| (t.id.clone(), t)));
            }
            Event::PeerUpsert { peer } => {
                self.nodes.insert(peer.id.clone(), peer);
            }
            Event::PeerRemoved { peer_id } => {
                self.nodes.remove(&peer_id);
            }
            Event::InfrastructureUpsert { node } => {
                self.nodes.insert(node.id.clone(), node);
            }
            Event::RouteUpsert { route } => {
                self.routes.insert(route.id.clone(), route);
            }
            Event::RouteRemoved { route_id } => {
                self.routes.remove(&route_id);
            }
            Event::TransferCreated { transfer }
            | Event::TransferProgress { transfer }
            | Event::TransferState { transfer } => {
                validate_transfer(&transfer)?;
                self.transfers.insert(transfer.id.clone(), transfer);
            }
            Event::PairingState { pairing } => {
                self.pairings.insert(pairing.id.clone(), pairing);
            }
        }
        Ok(())
    }
}

fn validate_transfer(transfer: &Transfer) -> Result<(), String> {
    if transfer.bytes_sent > transfer.bytes_total {
        Err(format!(
            "transfer {} has bytes_sent ({}) greater than bytes_total ({})",
            transfer.id, transfer.bytes_sent, transfer.bytes_total
        ))
    } else {
        Ok(())
    }
}

pub fn parse_ndjson(input: &str) -> Result<Vec<Event>, String> {
    input
        .lines()
        .enumerate()
        .filter(|(_, line)| !line.trim().is_empty())
        .map(|(index, line)| {
            let event: Event =
                serde_json::from_str(line).map_err(|e| format!("line {}: {}", index + 1, e))?;
            if let Event::Snapshot { transfers, .. } = &event {
                for transfer in transfers {
                    validate_transfer(transfer)
                        .map_err(|e| format!("line {}: {}", index + 1, e))?;
                }
            }
            if let Event::TransferCreated { transfer }
            | Event::TransferProgress { transfer }
            | Event::TransferState { transfer } = &event
            {
                validate_transfer(transfer).map_err(|e| format!("line {}: {}", index + 1, e))?;
            }
            Ok(event)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixtures_parse_and_replay() {
        for fixture in [
            include_str!("../fixtures/direct.ndjson"),
            include_str!("../fixtures/relay.ndjson"),
            include_str!("../fixtures/resume.ndjson"),
        ] {
            let events = parse_ndjson(fixture).expect("fixture should parse");
            let mut scene = Scene::default();
            for event in events {
                scene.apply(event).expect("fixture should replay");
            }
            assert!(scene.source.is_some());
            assert!(!scene.transfers.is_empty());
        }
    }

    #[test]
    fn snapshot_clears_stale_data() {
        let mut scene = Scene::default();
        scene
            .apply(parse_ndjson(include_str!("../fixtures/direct.ndjson")).unwrap()[1].clone())
            .unwrap();
        let snapshot = parse_ndjson(include_str!("../fixtures/direct.ndjson"))
            .unwrap()
            .remove(0);
        scene.apply(snapshot).unwrap();
        assert!(scene.nodes.is_empty() && scene.routes.is_empty() && scene.transfers.is_empty());
    }

    #[test]
    fn malformed_unknown_and_invalid_progress_are_reported_with_line() {
        assert!(parse_ndjson("{bad}").unwrap_err().contains("line 1"));
        assert!(
            parse_ndjson(r#"{"type":"future_event"}"#)
                .unwrap_err()
                .contains("line 1")
        );
        let invalid = r#"{"type":"transfer_progress","transfer":{"id":"t","source_id":"s","target_id":"p","file_name":"f","bytes_total":2,"bytes_sent":3,"state":"transferring"}}"#;
        assert!(parse_ndjson(invalid).unwrap_err().contains("line 1"));
    }

    fn transfer(id: &str, sent: u64, total: u64) -> Transfer {
        Transfer {
            id: id.into(),
            source_id: "s".into(),
            target_id: "p".into(),
            file_name: "f".into(),
            bytes_total: total,
            bytes_sent: sent,
            state: TransferState::Transferring,
            error: None,
        }
    }

    #[test]
    fn pairing_state_is_retained_and_snapshot_clears_it() {
        let mut scene = Scene::default();
        let pairing = Pairing {
            id: "pair".into(),
            peer_id: None,
            state: PairingState::Started,
        };
        scene
            .apply(Event::PairingState {
                pairing: pairing.clone(),
            })
            .unwrap();
        assert_eq!(scene.pairings.get("pair"), Some(&pairing));
        scene
            .apply(
                parse_ndjson(include_str!("../fixtures/direct.ndjson"))
                    .unwrap()
                    .remove(0),
            )
            .unwrap();
        assert!(scene.pairings.is_empty());
    }

    #[test]
    fn invalid_transfer_is_atomic_and_zero_byte_transfer_is_valid() {
        let mut scene = Scene::default();
        let initial = transfer("t", 0, 0);
        scene
            .apply(Event::TransferCreated {
                transfer: initial.clone(),
            })
            .unwrap();
        scene
            .apply(Event::TransferProgress {
                transfer: transfer("t", 1, 0),
            })
            .unwrap_err();
        assert_eq!(scene.transfers.get("t"), Some(&initial));
    }

    #[test]
    fn peer_and_route_removals_clear_entries() {
        let mut scene = Scene::default();
        let events = parse_ndjson(include_str!("../fixtures/direct.ndjson")).unwrap();
        scene.apply(events[1].clone()).unwrap();
        scene
            .apply(Event::PeerRemoved {
                peer_id: "phone".into(),
            })
            .unwrap();
        assert!(!scene.nodes.contains_key("phone"));
        let route = Route {
            id: "r".into(),
            transfer_id: None,
            kind: RouteKind::Payload,
            topology: RouteTopology::Direct,
            from: "s".into(),
            via: vec![],
            to: "p".into(),
            state: RouteState::Connected,
            transport: None,
        };
        scene.apply(Event::RouteUpsert { route }).unwrap();
        scene
            .apply(Event::RouteRemoved {
                route_id: "r".into(),
            })
            .unwrap();
        assert!(!scene.routes.contains_key("r"));
    }
}
