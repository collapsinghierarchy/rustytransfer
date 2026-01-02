use rustytransfer::protocol::receiver::ReceiverFsm;
use rustytransfer::protocol::sender::SenderFsm;
use rustytransfer::protocol::fsm::{Role, State};


  #[test]
    fn sender_and_receiver_derive_same_pake_key() {
        // Two endpoints must each have the password (no cloning from one Vec)
        let mut receiver_fsm = ReceiverFsm::new(b"Password".to_vec());
        let mut sender_fsm   = SenderFsm::new(b"Password".to_vec());

        // 1) Receiver starts PAKE-as-sender -> produces the first PAKE message
        let msg_from_receiver = receiver_fsm
            .step("PakeStart", None)
            .expect("receiver PakeStart failed");
        assert!(!msg_from_receiver.is_empty(), "receiver produced empty PAKE start msg");

        assert!(matches!(
            receiver_fsm.state,
            State::Pake { role: Role::Receiver, .. }
        ));

        // 2) Sender consumes that message -> derives receiver_key
        let receiver_key = sender_fsm
            .step("PakeStart", Some(msg_from_receiver))
            .expect("sender PakeStart failed");
        assert!(!receiver_key.is_empty(), "sender derived empty key");

        assert!(matches!(
            sender_fsm.state,
            State::Pake { role: Role::Sender, .. }
        ));

        // 3) Sender's PAKE receiver-side state should have a reply message queued.
        //    Pull it out and send back to ReceiverFsm.
        let msg_from_sender = match &mut sender_fsm.state {
            State::Pake { role: Role::Sender, pake_state } => {
                let m = pake_state.take_outbound_msg();
                m
                // If your API is Option<Vec<u8>>:
                // pake_state.take_outbound_msg().expect("no outbound msg from sender")
            }
            other => panic!("sender in unexpected state: {:?}", other),
        };
        assert!(!msg_from_sender.is_empty(), "sender produced empty reply msg");

        // 4) Receiver consumes reply -> derives sender_key
        let sender_key = receiver_fsm
            .step("Receivers_Answer", Some(msg_from_sender))
            .expect("receiver Receivers_Answer failed");
        assert!(!sender_key.is_empty(), "receiver derived empty key");

        // Final check: both keys must match
        assert_eq!(sender_key, receiver_key, "PAKE keys mismatch");

        // Optional sanity: also check key length
        assert_eq!(sender_key.len(), receiver_key.len());
        assert!((16..=64).contains(&sender_key.len()), "unexpected key length");
    }