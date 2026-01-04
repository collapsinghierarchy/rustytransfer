// tests/fsm_roundtrip.rs
use rustytransfer::protocol::fsm::{State, StepError, Role};
use rustytransfer::protocol::receiver::ReceiverFsm;
use rustytransfer::protocol::sender::SenderFsm;

fn must_some(label: &str, v: Option<Vec<u8>>) -> Vec<u8> {
        v.unwrap_or_else(|| panic!("{label}: expected Some(Vec<u8>), got None"))
}

  #[test]
    fn roundtrip_sender_receiver_reaches_success_with_fin() {
        let pw = b"Password".to_vec();
        let file_data = b"hello from sender".to_vec();

        let mut receiver = ReceiverFsm::new(pw.clone());
        let mut sender = SenderFsm::new(pw.clone());
        sender.file_data = file_data.clone();

        // 1) Receiver -> Sender: PAKE_START (receiver sends first PAKE msg)
        let pake_msg_1 = must_some(
            "receiver PAKE_START outbox",
            receiver.step("PAKE_START", None).expect("receiver PAKE_START failed"),
        );
        assert!(matches!(receiver.state, State::Pake { role: Role::Receiver, .. }));

        // 2) Sender consumes PAKE_START message
        let out = sender
            .step("PAKE_START", Some(pake_msg_1))
            .expect("sender PAKE_START failed");
        assert!(out.is_none(), "sender PAKE_START should not emit output");
        assert!(matches!(sender.state, State::Pake { role: Role::Sender, .. }));

        // 3) Sender -> Receiver: PAKE_ANSWER (sender produces second PAKE msg from its PakeState)
        let pake_msg_2 = match &mut sender.state {
            State::Pake { role: Role::Sender, pake_state, .. } => pake_state.take_outbound_msg(),
            other => panic!("sender not in Pake after PAKE_START, got: {other:?}"),
        };

        // 4) Receiver consumes PAKE_ANSWER and sends AUTH_KEM (tag || pk)
        let auth_kem = must_some(
            "receiver PAKE_ANSWER outbox (AUTH_KEM)",
            receiver
                .step("PAKE_ANSWER", Some(pake_msg_2))
                .expect("receiver PAKE_ANSWER failed"),
        );
        assert!(matches!(receiver.state, State::KemAuth { role: Role::Receiver, .. }));

        // 5) Sender consumes AUTH_KEM and sends SMT payload:
        //    outbox = tag || ct_kem || nonce || dem_ciphertext
        let smt_payload = must_some(
            "sender RECEIVED_AUTH_KEM outbox (SMT payload)",
            sender
                .step("RECEIVED_AUTH_KEM", Some(auth_kem))
                .expect("sender RECEIVED_AUTH_KEM failed"),
        );
        assert!(matches!(sender.state, State::KemAuth { role: Role::Sender, .. }));

        // 6) Receiver consumes SMT payload and outputs plaintext; receiver enters Smt
        let recovered = must_some(
            "receiver SMT outbox (plaintext)",
            receiver.step("SMT", Some(smt_payload)).expect("receiver SMT failed"),
        );
        assert_eq!(recovered, file_data);
        assert!(matches!(receiver.state, State::Smt { role: Role::Receiver }));

        // 7) Receiver sends FIN (your receiver currently uses tag "SMT" again to emit FIN)
        let fin = must_some(
            "receiver SMT (FIN outbox)",
            receiver.step("SMT", None).expect("receiver FIN step failed"),
        );
        assert_eq!(fin, b"FIN".to_vec());
        assert!(matches!(receiver.state, State::Success(_)));

        // 8) Sender consumes FIN and transitions to Success
        let sender_out = sender.step("FIN", Some(fin)).expect("sender FIN failed");
        assert!(sender_out.is_none());
        assert!(matches!(sender.state, State::Success(_)));
    }
