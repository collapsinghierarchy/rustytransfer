use std::fmt;

/// A transport protocol or lifecycle failure with a stable, inspectable category.
#[derive(Debug)]
pub enum TransportError {
    WebSocketClosed,
    InvalidWebSocketMessage(&'static str),
    InvalidWebSocketFrame(String),
    IrohEndpointNotOnline(String),
    RendezvousCodeUnavailable,
    InvalidRendezvousInput(&'static str),
}

impl fmt::Display for TransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::WebSocketClosed => formatter.write_str("websocket closed unexpectedly"),
            Self::InvalidWebSocketMessage(kind) => {
                write!(formatter, "unexpected websocket {kind} message")
            }
            Self::InvalidWebSocketFrame(reason) => {
                write!(formatter, "invalid websocket signaling frame: {reason}")
            }
            Self::IrohEndpointNotOnline(reason) => {
                write!(formatter, "Iroh endpoint did not become online: {reason}")
            }
            Self::RendezvousCodeUnavailable => {
                formatter.write_str("rendezvous code is used/expired/unknown (HTTP 410 Gone)")
            }
            Self::InvalidRendezvousInput(reason) => formatter.write_str(reason),
        }
    }
}

impl std::error::Error for TransportError {}
