pub mod fsm;
pub mod receiver;
pub mod sender;
pub mod share_code;

/// Version-one SPAKE2 identity bytes. Changing these breaks the wire protocol.
pub const PAKE_RECEIVER_ID: &[u8] = b"smt_receiver";
pub const PAKE_SENDER_ID: &[u8] = b"smt_sender";
