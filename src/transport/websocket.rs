#[cfg(test)]
mod ws_room_tests {
    use std::any::Any;

    use super::*;
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::{connect_async, tungstenite::protocol::Message as WsMsg};
    use uuid::Uuid;
    use url::Url;
    use crate::transport::signal::SignalFrame;

    // helper that builds the wss URL for a given side
    fn ws_url(app_id: &str, side: &str) -> String {
        format!("wss://whitenoise.systems/ws?appID={app_id}&side={side}")
    }

    #[tokio::test]
    async fn ws_room_offer_roundtrip() -> Result<(), anyhow::Error> {
        // 1) generate random UUID for the room
        let app_id = Uuid::new_v4().to_string();
        println!("Using appID: {app_id}");

        // 2) connect side A and side B
        let url_a = ws_url(&app_id, "A"); // String
        let (ws_a, _resp_a) = connect_async(&url_a).await?;
        let url_b = ws_url(&app_id, "B"); // String
        let (ws_b, _resp_b) = connect_async(&url_b).await?;

        // split into (sink, stream)
        let (mut write_a, _read_a) = ws_a.split();
        let (_write_b, mut read_b) = ws_b.split();

        // 3) build an "offer" signal
        let sig = SignalFrame::Offer {
            sdp: "test-sdp-from-rust".to_string(),
        };
        let text = serde_json::to_string(&sig)?;
        println!("[A] sending: {text}");

        // send from A
        write_a.send(WsMsg::Text(text.into())).await?;

        // 4) read on B
        if let Some(msg) = read_b.next().await {
            let msg = msg?;
            assert!(msg.is_text(), "expected text frame from server");
            let body = msg.into_text()?;
            println!("[B] received raw: {body}");

            let recv: SignalFrame = serde_json::from_str(&body)?;
            match recv {
                SignalFrame::Offer { sdp } => {
                    assert_eq!(sdp, "test-sdp-from-rust");
                    println!("[B] parsed offer SDP: {sdp}");
                }
            }
        } else {
            panic!("B did not receive any message");
        }

        Ok(())
    }
}