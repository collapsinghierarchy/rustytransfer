use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{self, Read, Write};

pub(crate) const MAX_FRAME_SIZE: usize = 64 * 1024;

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Request {
    StartSend { id: u64 },
    StartReceive { id: u64, invite: String },
    Cancel { id: u64 },
    Status { id: u64 },
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Direction {
    Send,
    Receive,
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(crate) enum Event {
    Invite {
        id: u64,
        invite: String,
        file_name: String,
        file_size: u64,
    },
    Progress {
        id: u64,
        direction: Direction,
        done: u64,
        total: u64,
    },
    Complete {
        id: u64,
        direction: Direction,
        file_name: String,
    },
    Error {
        id: u64,
        message: String,
    },
    Cancelled {
        id: u64,
    },
    Status {
        id: u64,
        state: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        direction: Option<Direction>,
        #[serde(skip_serializing_if = "Option::is_none")]
        invite: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        file_name: Option<String>,
    },
}

#[derive(Debug)]
pub(crate) struct InvalidRequest {
    pub(crate) id: u64,
    pub(crate) message: String,
}

pub(crate) fn decode_request(frame: &[u8]) -> Result<Request, InvalidRequest> {
    let value: Value = serde_json::from_slice(frame).map_err(|error| InvalidRequest {
        id: 0,
        message: format!("invalid JSON request: {error}"),
    })?;
    let id = value.get("id").and_then(Value::as_u64).unwrap_or(0);
    serde_json::from_value(value).map_err(|error| InvalidRequest {
        id,
        message: format!("invalid request: {error}"),
    })
}

pub(crate) fn read_frame<R: Read>(reader: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut prefix = [0_u8; 4];
    loop {
        match reader.read(&mut prefix[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    reader.read_exact(&mut prefix[1..])?;
    let frame_len = usize::try_from(u32::from_ne_bytes(prefix)).map_err(io::Error::other)?;
    if frame_len > MAX_FRAME_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("native message exceeds {MAX_FRAME_SIZE} bytes"),
        ));
    }
    let mut frame = vec![0_u8; frame_len];
    reader.read_exact(&mut frame)?;
    Ok(Some(frame))
}

pub(crate) fn write_frame<W: Write, T: Serialize>(writer: &mut W, value: &T) -> io::Result<()> {
    let frame = serde_json::to_vec(value).map_err(io::Error::other)?;
    if frame.len() > MAX_FRAME_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("native message exceeds {MAX_FRAME_SIZE} bytes"),
        ));
    }
    let frame_len = u32::try_from(frame.len()).map_err(io::Error::other)?;
    writer.write_all(&frame_len.to_ne_bytes())?;
    writer.write_all(&frame)?;
    writer.flush()
}

#[cfg(test)]
mod tests {
    use super::{
        Direction, Event, MAX_FRAME_SIZE, Request, decode_request, read_frame, write_frame,
    };
    use serde_json::json;
    use std::io::{Cursor, ErrorKind};

    #[test]
    fn native_frame_round_trips_with_native_endian_prefix() {
        let event = Event::Progress {
            id: 17,
            direction: Direction::Send,
            done: 12,
            total: 42,
        };
        let mut encoded = Vec::new();
        write_frame(&mut encoded, &event).expect("frame should write");

        let expected_len = u32::from_ne_bytes(
            encoded[..4]
                .try_into()
                .expect("frame prefix has four bytes"),
        );
        assert_eq!(expected_len as usize, encoded.len() - 4);
        let decoded = read_frame(&mut Cursor::new(encoded)).expect("frame should read");
        assert_eq!(
            decoded,
            Some(
                br#"{"type":"progress","id":17,"direction":"send","done":12,"total":42}"#.to_vec()
            )
        );
    }

    #[test]
    fn request_decode_preserves_id_for_invalid_commands() {
        assert!(matches!(
            decode_request(br#"{"id":4,"type":"status"}"#),
            Ok(Request::Status { id: 4 })
        ));
        let error = decode_request(br#"{"id":23,"type":"unknown"}"#)
            .expect_err("unknown command should fail");
        assert_eq!(error.id, 23);
        assert!(error.message.contains("invalid request"));

        let invalid_json = decode_request(b"{").expect_err("malformed JSON should fail");
        assert_eq!(invalid_json.id, 0);
    }

    #[test]
    fn reader_rejects_oversized_and_truncated_frames() {
        let oversized = u32::try_from(MAX_FRAME_SIZE + 1)
            .expect("test frame size fits u32")
            .to_ne_bytes();
        let error =
            read_frame(&mut Cursor::new(oversized)).expect_err("oversized frame should fail");
        assert_eq!(error.kind(), ErrorKind::InvalidData);

        let truncated = 8_u32
            .to_ne_bytes()
            .into_iter()
            .chain([b'{'])
            .collect::<Vec<_>>();
        let error = read_frame(&mut Cursor::new(truncated)).expect_err("short frame should fail");
        assert_eq!(error.kind(), ErrorKind::UnexpectedEof);

        assert_eq!(
            decode_request(br#"{"id":2,"type":"start_send","unexpected":true}"#)
                .expect_err("unknown fields should fail")
                .id,
            2
        );
    }

    #[test]
    fn status_event_omits_empty_optional_fields() {
        let event = Event::Status {
            id: 8,
            state: "idle".to_owned(),
            direction: None,
            invite: None,
            file_name: None,
        };
        assert_eq!(
            serde_json::to_value(event).expect("event should serialize"),
            json!({ "type": "status", "id": 8, "state": "idle" })
        );
    }
}
