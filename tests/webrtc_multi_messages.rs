use anyhow::{Context, Result};
use std::time::Duration;
use tokio::time::timeout;
use uuid::Uuid;

use rustytransfer::transport::answerer::connect_answerer;
use rustytransfer::transport::offerer::connect_offerer;

fn e2e_enabled() -> bool {
    // Optional gating so normal `cargo test` doesn't depend on the remote backend.
    // Remove this check if you want it always-on.
    std::env::var("RUSTYTRANSFER_E2E").as_deref() == Ok("1")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn webrtc_datachannel_multiple_messages_roundtrip() -> Result<()> {
    if !e2e_enabled() {
        eprintln!("skipping: set RUSTYTRANSFER_E2E=1 to run backend E2E tests");
        return Ok(());
    }

    let app_id = Uuid::new_v4().to_string();
    println!("Using appID: {app_id}");

    let msgs: Vec<Vec<u8>> = vec![
        b"one".to_vec(),
        b"two".to_vec(),
        b"three".to_vec(),
    ];

    let answerer_fut = async {
        let st = timeout(Duration::from_secs(90), connect_answerer(&app_id))
            .await
            .context("answerer connect timeout")??;

        let mut received: Vec<Vec<u8>> = Vec::new();

        loop {
            let msg = timeout(Duration::from_secs(90), st.recv_vec())
                .await
                .context("answerer recv timeout")??;

            if msg == b"FIN" {
                // optional final ack then break
                st.send_vec(b"ACK:FIN".to_vec()).await?;
                break;
            }

            // record + ack with payload included
            received.push(msg.clone());
            let mut ack = b"ACK:".to_vec();
            ack.extend_from_slice(&msg);
            st.send_vec(ack).await?;
        }

        Ok::<_, anyhow::Error>(received)
    };

    let offerer_fut = async {
        let st = timeout(Duration::from_secs(90), connect_offerer(&app_id))
            .await
            .context("offerer connect timeout")??;

        for m in &msgs {
            st.send_vec(m.clone()).await?;

            let ack = timeout(Duration::from_secs(90), st.recv_vec())
                .await
                .context("offerer recv timeout")??;

            let mut expected = b"ACK:".to_vec();
            expected.extend_from_slice(m);

            anyhow::ensure!(ack == expected, "unexpected ack: got {ack:?}, expected {expected:?}");
        }

        // tell receiver we're done (keeps same channel alive until now)
        st.send_vec(b"FIN".to_vec()).await?;

        // optional: wait for final ack
        let final_ack = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("offerer final ack timeout")??;
        anyhow::ensure!(final_ack == b"ACK:FIN".to_vec());

        Ok::<_, anyhow::Error>(())
    };

    // Run both sides concurrently in the same test task
    let (received_res, offerer_res) = tokio::join!(answerer_fut, offerer_fut);

    let received = received_res?;
    offerer_res?;

    assert_eq!(received, msgs);
    Ok(())
}
