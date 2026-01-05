use anyhow::{anyhow, bail, ensure, Result};
use bytes::Bytes;
use just_webrtc::DataChannelExt;
use just_webrtc::platform::{Channel, PeerConnection};
use just_webrtc::types::{ICECandidate, SDPType};
use tokio::time::{timeout, Duration};

use crate::transport::frames::Frame;
use crate::transport::websocket::{WsRead, WsRoomTransport};

pub struct WebRtcState {
    pub pc: PeerConnection,
    pub ch: Channel,
}

// --- Fragmentation settings (tune if needed) ---
const FRAG_MAGIC: &[u8; 4] = b"RTF1";

// Payload bytes per datachannel message.
// If you still see "outbound packet larger than maximum message size", lower this (e.g. 8*1024).
const CHUNK_PAYLOAD: usize = 16 * 1024;

// Safety / robustness
const MAX_REASSEMBLE_BYTES: usize = 512 * 1024 * 1024; // 512MB cap
const REASSEMBLE_TIMEOUT: Duration = Duration::from_secs(180);

fn parse_u64_be(b: &[u8]) -> u64 {
    u64::from_be_bytes(b.try_into().unwrap())
}

impl WebRtcState {
    pub async fn send_vec(&self, data: Vec<u8>) -> Result<()> {
        // Small payloads: send as a single message.
        if data.len() <= CHUNK_PAYLOAD {
            let b = Bytes::from(data);
            self.ch.send(&b).await?;
            return Ok(());
        }

        // Large payloads: send a header then raw chunks.
        // Header = MAGIC(4) + total_len(u64 BE) => 12 bytes.
        let total_len = data.len() as u64;
        let mut hdr = Vec::with_capacity(12);
        hdr.extend_from_slice(FRAG_MAGIC);
        hdr.extend_from_slice(&total_len.to_be_bytes());

        let hdr_b = Bytes::from(hdr);
        self.ch.send(&hdr_b).await?;

        for chunk in data.chunks(CHUNK_PAYLOAD) {
            // NOTE: we must own the bytes to make a Bytes; to_vec() is fine here.
            let b = Bytes::from(chunk.to_vec());
            self.ch.send(&b).await?;
        }

        Ok(())
    }

    pub async fn recv_vec(&self) -> Result<Vec<u8>> {
        let first = self.ch.receive().await?;
        let first_bytes = first.to_vec();

        // Not a fragment header? return as-is.
        if first_bytes.len() != 12 || &first_bytes[0..4] != FRAG_MAGIC {
            return Ok(first_bytes);
        }

        let total_len = parse_u64_be(&first_bytes[4..12]) as usize;
        ensure!(total_len > 0, "invalid fragmented total_len=0");
        ensure!(
            total_len <= MAX_REASSEMBLE_BYTES,
            "refusing to reassemble {total_len} bytes (cap={MAX_REASSEMBLE_BYTES})"
        );

        // Reassemble until we hit total_len, with a timeout to avoid hanging forever.
        let assembled = timeout(REASSEMBLE_TIMEOUT, async {
            let mut out = Vec::with_capacity(total_len);

            while out.len() < total_len {
                let chunk = self.ch.receive().await?;
                let c = chunk.to_vec();

                // Under your stated protocol constraints this should never happen.
                if c.len() == 12 && &c[0..4] == FRAG_MAGIC {
                    bail!("unexpected fragment header during reassembly (concurrent recv or interleaving?)");
                }

                out.extend_from_slice(&c);
            }

            out.truncate(total_len);
            Ok::<_, anyhow::Error>(out)
        })
        .await
        .map_err(|_| anyhow!("timeout while reassembling fragmented payload"))??;

        Ok(assembled)
    }
}

pub async fn wait_for_sdp_frame(
    read: &mut WsRead,
    expected: SDPType,
) -> Result<(String, Vec<ICECandidate>)> {
    loop {
        match WsRoomTransport::recv_frame(read).await? {
            Frame::Room_Full => continue,

            Frame::Offer { sdp, candidates } if expected == SDPType::Offer => {
                return Ok((sdp, candidates));
            }

            Frame::Answer { sdp, candidates } if expected == SDPType::Answer => {
                return Ok((sdp, candidates));
            }

            _ => continue,
        }
    }
}
