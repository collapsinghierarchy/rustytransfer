use anyhow::{anyhow, Context, Result};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio_tungstenite::{
    connect_async,
    tungstenite::protocol::Message as WsMsg,
    MaybeTlsStream,
    WebSocketStream,
};
use futures_util::stream::{SplitSink, SplitStream};

use crate::transport::frames::Frame;

pub type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;
pub type WsWrite = SplitSink<WsStream, WsMsg>;
pub type WsRead = SplitStream<WsStream>;

pub struct WsRoomTransport {
    pub app_id: String,
    pub side: String,
}

impl WsRoomTransport {
    pub fn new(app_id: String, side: String) -> Self {
        Self { app_id, side }
    }

    // helper that builds the wss URL for a given side
    pub fn ws_url(&self) -> String {
        format!("wss://nt.whitenoise.systems/ws?appID={}&side={}", &self.app_id, &self.side)
    }

    pub async fn connect_room(&self) -> Result<(WsWrite, WsRead)> {
        let url = self.ws_url();
        let (ws, _resp) = connect_async(&url)
            .await
            .with_context(|| format!("failed to connect websocket: {url}"))?;
        Ok(ws.split())
    }

    pub async fn send_frame(write: &mut WsWrite, frame: &Frame) -> Result<()> {
        let text = serde_json::to_string(frame)?;
        write.send(WsMsg::Text(text.into())).await?;
        Ok(())
    }

    pub async fn recv_frame(read: &mut WsRead) -> Result<Frame> {
        loop {
            let msg = read
                .next()
                .await
                .ok_or_else(|| anyhow!("websocket closed unexpectedly"))??;

            let Ok(body) = msg.into_text() else { continue };

            if let Ok(frame) = serde_json::from_str::<Frame>(&body) {
                return Ok(frame);
            }
        }
    }

}

#[cfg(test)]
mod tests {
    use super::*;
    use anyhow::{anyhow, Result};
    use tokio::time::{timeout, Duration};
    use uuid::Uuid;

    // Reasonable timeout for remote backend IO.
    const T: Duration = Duration::from_secs(8);

    async fn wait_for_offer(read: &mut WsRead) -> Result<Frame> {
        // Room_Full may appear first; ignore it and keep waiting.
        for _ in 0..10 {
            let frame = timeout(T, WsRoomTransport::recv_frame(read))
                .await
                .map_err(|_| anyhow!("timeout waiting for offer"))??;

            match frame {
                Frame::Room_Full => continue,
                Frame::Offer { .. } => return Ok(frame),
                Frame::Answer { .. } => continue, // ignore unexpected
            }
        }
        Err(anyhow!("did not receive Offer within retries"))
    }

    async fn wait_for_answer(read: &mut WsRead) -> Result<Frame> {
        for _ in 0..10 {
            let frame = timeout(T, WsRoomTransport::recv_frame(read))
                .await
                .map_err(|_| anyhow!("timeout waiting for answer"))??;

            match frame {
                Frame::Room_Full => continue,
                Frame::Answer { .. } => return Ok(frame),
                Frame::Offer { .. } => continue, // ignore unexpected
            }
        }
        Err(anyhow!("did not receive Answer within retries"))
    }

    #[tokio::test]
    async fn ws_url_formats_expected_query() {
        let t = WsRoomTransport::new("abc123".to_string(), "A".to_string());
        let url = t.ws_url();

        assert!(url.contains("wss://nt.whitenoise.systems/ws?"));
        assert!(url.contains("appID=abc123"));
        assert!(url.contains("side=A"));
    }

    #[tokio::test]
    async fn remote_backend_relays_offer_a_to_b() -> Result<()> {
        // New room for every test run.
        let app_id = Uuid::new_v4().to_string();
        println!("Using appID: {app_id}");

        // Connect both sides to the real backend.
        let a = WsRoomTransport::new(app_id.clone(), "A".to_string());
        let b = WsRoomTransport::new(app_id.clone(), "B".to_string());

        let (mut write_a, _read_a) = a.connect_room().await?;
        let (_write_b, mut read_b) = b.connect_room().await?;

        // A sends an Offer
        let sent = Frame::Offer {
            sdp: "test-sdp".to_string(),
            candidates: vec![],
        };
        WsRoomTransport::send_frame(&mut write_a, &sent).await?;

        // B must receive it
        let got = wait_for_offer(&mut read_b).await?;
        match got {
            Frame::Offer { sdp, .. } => assert_eq!(sdp, "test-sdp"),
            other => panic!("expected Offer, got: {other:?}"),
        }

        Ok(())
    }

    #[tokio::test]
    async fn remote_backend_relays_offer_and_answer_bidirectionally() -> Result<()> {
        let app_id = Uuid::new_v4().to_string();
        println!("Using appID: {app_id}");

        let a = WsRoomTransport::new(app_id.clone(), "A".to_string());
        let b = WsRoomTransport::new(app_id.clone(), "B".to_string());

        let (mut write_a, mut read_a) = a.connect_room().await?;
        let (mut write_b, mut read_b) = b.connect_room().await?;

        // A -> B : Offer
        WsRoomTransport::send_frame(
            &mut write_a,
            &Frame::Offer {
                sdp: "offer-from-a".to_string(),
                candidates: vec![],
            },
        )
        .await?;

        let got_offer = wait_for_offer(&mut read_b).await?;
        match got_offer {
            Frame::Offer { sdp, .. } => assert_eq!(sdp, "offer-from-a"),
            other => panic!("expected Offer at B, got: {other:?}"),
        }

        // B -> A : Answer
        WsRoomTransport::send_frame(
            &mut write_b,
            &Frame::Answer {
                sdp: "answer-from-b".to_string(),
                candidates: vec![],
            },
        )
        .await?;

        let got_answer = wait_for_answer(&mut read_a).await?;
        match got_answer {
            Frame::Answer { sdp, .. } => assert_eq!(sdp, "answer-from-b"),
            other => panic!("expected Answer at A, got: {other:?}"),
        }

        Ok(())
    }

    #[tokio::test]
    async fn remote_backend_allows_multiple_frames_in_same_room() -> Result<()> {
        let app_id = Uuid::new_v4().to_string();
        println!("Using appID: {app_id}");

        let a = WsRoomTransport::new(app_id.clone(), "A".to_string());
        let b = WsRoomTransport::new(app_id.clone(), "B".to_string());

        let (mut write_a, _read_a) = a.connect_room().await?;
        let (_write_b, mut read_b) = b.connect_room().await?;

        // Send two offers back-to-back
        WsRoomTransport::send_frame(
            &mut write_a,
            &Frame::Offer {
                sdp: "offer-1".to_string(),
                candidates: vec![],
            },
        )
        .await?;
        WsRoomTransport::send_frame(
            &mut write_a,
            &Frame::Offer {
                sdp: "offer-2".to_string(),
                candidates: vec![],
            },
        )
        .await?;

        let got1 = wait_for_offer(&mut read_b).await?;
        let sdp1 = match got1 {
            Frame::Offer { sdp, .. } => sdp,
            other => panic!("expected Offer, got: {other:?}"),
        };

        let got2 = wait_for_offer(&mut read_b).await?;
        let sdp2 = match got2 {
            Frame::Offer { sdp, .. } => sdp,
            other => panic!("expected Offer, got: {other:?}"),
        };

        assert_eq!(sdp1, "offer-1");
        assert_eq!(sdp2, "offer-2");

        Ok(())
    }
}
