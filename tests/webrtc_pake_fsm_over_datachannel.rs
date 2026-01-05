use anyhow::{bail, ensure, Context, Result};
use std::time::Duration;
use tokio::time::{sleep, timeout};
use uuid::Uuid;

use rustytransfer::protocol::fsm::{Role, State};
use rustytransfer::protocol::receiver::ReceiverFsm;
use rustytransfer::protocol::sender::SenderFsm;
use rustytransfer::transport::answerer::connect_answerer;
use rustytransfer::transport::offerer::connect_offerer;

fn must_some(label: &str, v: Option<Vec<u8>>) -> Vec<u8> {
    v.unwrap_or_else(|| panic!("{label}: expected Some(Vec<u8>), got None"))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn webrtc_runs_pake_fsm_roundtrip_and_keeps_channel_alive() -> Result<()> {

    let app_id = Uuid::new_v4().to_string();
    println!("Using appID: {app_id}");

    let pw = b"Password".to_vec();
    let file_data = b"hello from sender".to_vec();

        // ---- Offerer side (maps to protocol Sender) ----
    let app_id_a = app_id.clone();
    let pw_a = pw.clone();
    let file_data_a = file_data.clone();
    let offerer_fut = async move {
        let st = timeout(Duration::from_secs(90), connect_offerer(&app_id_a))
            .await
            .context("offerer connect timeout")??;

        let mut sender = SenderFsm::new(pw_a);
        sender.file_data = file_data_a;

        // 1) Wait for PAKE_START from receiver
        let pake_msg_1 = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("offerer recv PAKE_START timeout")??;

        // 2) Sender consumes PAKE_START (no immediate outbox)
        let out = sender
            .step("PAKE_START", Some(pake_msg_1))
            .expect("sender PAKE_START failed");
        ensure!(out.is_none(), "sender PAKE_START should not emit output");
        ensure!(matches!(sender.state, State::Pake { role: Role::Sender, .. }));

        // 3) Sender -> Receiver: PAKE_ANSWER
        let pake_msg_2 = match &mut sender.state {
            State::Pake {
                role: Role::Sender,
                pake_state,
                ..
            } => pake_state.take_outbound_msg(),
            other => bail!("sender not in Pake after PAKE_START, got: {other:?}"),
        };
        st.send_vec(pake_msg_2).await?;

        // 4) Wait for AUTH_KEM from receiver
        let auth_kem = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("offerer recv AUTH_KEM timeout")??;

        // 5) Sender consumes AUTH_KEM -> emits SMT payload
        let smt_payload = must_some(
            "sender RECEIVED_AUTH_KEM outbox (SMT payload)",
            sender
                .step("RECEIVED_AUTH_KEM", Some(auth_kem))
                .expect("sender RECEIVED_AUTH_KEM failed"),
        );
        ensure!(matches!(sender.state, State::KemAuth { role: Role::Sender, .. }));
        st.send_vec(smt_payload).await?;

        // 6) Wait for FIN
        let fin = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("offerer recv FIN timeout")??;
        ensure!(fin == b"FIN".to_vec());

        // 7) Sender consumes FIN -> success
        let sender_out = sender.step("FIN", Some(fin)).expect("sender FIN failed");
        ensure!(sender_out.is_none());
        ensure!(matches!(sender.state, State::Success(_)));

        // 8) Prove channel stays alive after FSM success: send POST and await ack
        st.send_vec(b"POST".to_vec()).await?;
        let ack = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("offerer recv ACK_POST timeout")??;
        ensure!(ack == b"ACK_POST".to_vec(), "unexpected ack: {ack:?}");

        Ok::<_, anyhow::Error>(())
    };

    // ---- Answerer side (maps to protocol Receiver) ----
    let app_id_b = app_id.clone();
    let pw_b = pw.clone();
    let file_data_b = file_data.clone();
    let answerer_fut = async move {
        sleep(Duration::from_secs(3)).await;
        let st = timeout(Duration::from_secs(90), connect_answerer(&app_id_b))
            .await
            .context("answerer connect timeout")??;

        let mut receiver = ReceiverFsm::new(pw_b);

        // 1) Receiver -> Sender: PAKE_START
        let pake_msg_1 = must_some(
            "receiver PAKE_START outbox",
            receiver
                .step("PAKE_START", None)
                .expect("receiver PAKE_START failed"),
        );
        st.send_vec(pake_msg_1).await?;

        // 2) Wait for PAKE_ANSWER from sender
        let pake_msg_2 = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("answerer recv PAKE_ANSWER timeout")??;

        // 3) Receiver consumes PAKE_ANSWER -> emits AUTH_KEM
        let auth_kem = must_some(
            "receiver PAKE_ANSWER outbox (AUTH_KEM)",
            receiver
                .step("PAKE_ANSWER", Some(pake_msg_2))
                .expect("receiver PAKE_ANSWER failed"),
        );
        st.send_vec(auth_kem).await?;

        // 4) Wait for SMT payload
        let smt_payload = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("answerer recv SMT payload timeout")??;

        // 5) Receiver consumes SMT -> outputs plaintext
        let recovered = must_some(
            "receiver SMT outbox (plaintext)",
            receiver
                .step("SMT", Some(smt_payload))
                .expect("receiver SMT failed"),
        );
        ensure!(recovered == file_data_b, "recovered file_data mismatch");

        // 6) Receiver sends FIN
        let fin = must_some(
            "receiver SMT (FIN outbox)",
            receiver.step("SMT", None).expect("receiver FIN step failed"),
        );
        ensure!(fin == b"FIN".to_vec());
        ensure!(matches!(receiver.state, State::Success(_)));

        st.send_vec(fin).await?;

        // 7) Prove channel stays alive after FSM success: wait for POST and ack it
        let post = timeout(Duration::from_secs(90), st.recv_vec())
            .await
            .context("answerer recv POST timeout")??;
        ensure!(post == b"POST".to_vec(), "unexpected post message: {post:?}");

        st.send_vec(b"ACK_POST".to_vec()).await?;

        Ok::<_, anyhow::Error>(())
    };

    let (answerer_res, offerer_res) = tokio::join!(answerer_fut, offerer_fut);
    answerer_res?;
    offerer_res?;
    Ok(())
}
