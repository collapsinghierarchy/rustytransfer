//! Native controls belong to the selected item's lens, outside the warped field.
use crate::*;
use iced::widget::{Row, column, pick_list};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Demo {
    Direct,
    Relay,
    Resume,
}
impl std::fmt::Display for Demo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Direct => "Direct",
            Self::Relay => "Relay",
            Self::Resume => "Failure / resume",
        })
    }
}
impl App {
    pub(super) fn related_route(&self, id: &str, kind: Option<model::RouteKind>) -> Option<String> {
        self.scene
            .routes
            .values()
            .find(|route| {
                kind.is_none_or(|kind| route.kind == kind)
                    && (route.from == id || route.to == id || route.via.iter().any(|via| via == id))
                    && self.field.routes.iter().any(|g| g.id == route.id)
            })
            .map(|r| r.id.clone())
    }
    pub(super) fn item_actions(&self) -> Vec<(&'static str, Message)> {
        let mut actions = Vec::new();
        match &self.selection {
            Some(Selection::Node(id)) => {
                let source = self.scene.source.as_ref().is_some_and(|s| &s.id == id);
                if source {
                    actions.push((
                        if self.playing {
                            "Pause demo"
                        } else {
                            "Play demo"
                        },
                        Message::Play,
                    ));
                    actions.push(("Step", Message::Step));
                    actions.push(("Replay", Message::Restart));
                } else {
                    if let Some(route) = self.related_route(id, Some(model::RouteKind::Payload)) {
                        actions
                            .push(("Inspect transfer", Message::Select(Selection::Route(route))));
                    }
                    if let Some(route) = self.related_route(id, Some(model::RouteKind::Signaling)) {
                        actions.push(("Inspect control", Message::Select(Selection::Route(route))));
                    }
                }
                if self.overrides.contains_key(id) {
                    actions.push(("Reset position", Message::ResetPosition(id.clone())));
                }
                if !source {
                    actions.push(("Return to field", Message::CloseLens));
                }
            }
            Some(Selection::Route(id)) => {
                if let Some(route) = self.scene.routes.get(id) {
                    if route.kind == model::RouteKind::Payload && route.transfer_id.is_some() {
                        actions.push((
                            if self.playing {
                                "Pause replay"
                            } else {
                                "Play replay"
                            },
                            Message::Play,
                        ));
                        actions.push(("Step event", Message::Step));
                        actions.push(("Replay transfer", Message::Restart));
                    } else {
                        actions.push((
                            "Inspect source",
                            Message::Select(Selection::Node(route.from.clone())),
                        ));
                        actions.push((
                            "Inspect target",
                            Message::Select(Selection::Node(route.to.clone())),
                        ));
                    }
                }
            }
            None => {}
        }
        actions
    }
    pub(super) fn lens_actions(&self) -> Element<'_, Message> {
        let Some(lens) = self.lens else {
            return iced::widget::space().into();
        };
        let source_selected = matches!(&self.selection, Some(Selection::Node(id)) if self.scene.source.as_ref().is_some_and(|s| &s.id == id));
        let actions = self.item_actions();
        let mut content = column![].spacing(10).align_x(iced::Center);
        for chunk in actions.chunks(if source_selected { 4 } else { 3 }) {
            let buttons = chunk.iter().map(|(title, message)| {
                button(text(*title).size(12))
                    .padding([7, 9])
                    .on_press(message.clone())
                    .style(quiet_button)
                    .into()
            });
            content = content.push(Row::with_children(buttons).spacing(8).align_y(iced::Center));
        }
        if source_selected {
            content = content.push(
                row![
                    text("Demo").size(11).color(MUTED),
                    pick_list(
                        [Demo::Direct, Demo::Relay, Demo::Resume],
                        if self.custom {
                            None
                        } else {
                            Some([Demo::Direct, Demo::Relay, Demo::Resume][self.scenario])
                        },
                        |demo| Message::Scenario(match demo {
                            Demo::Direct => 0,
                            Demo::Relay => 1,
                            Demo::Resume => 2,
                        })
                    )
                    .placeholder("Choose scenario")
                    .text_size(12)
                    .padding([5, 8])
                ]
                .spacing(10)
                .align_y(iced::Center),
            );
        }
        let width = 370.0;
        container(container(content).width(width))
            .padding(iced::Padding {
                left: (lens.center.x - width * 0.5).max(0.0),
                top: lens.center.y + 108.0,
                right: 0.0,
                bottom: 0.0,
            })
            .width(Fill)
            .height(Fill)
            .into()
    }
}
