/*
# Init

- Wait for PAKE-init.

# PAKE

-Run PAKE until finished.
- End result: shared K_pake, then derive K_mac.

# SMT-init

- Generate ML-KEM keypair (pk_kem, sk_kem) using enc_rust.
- Compute tag = MAC(K_mac, transcript || role=Receiver || pk_kem).
- Send (pk_kem, tag) to Sender.
- Go to “waiting for file/ct” state.

# SMT-recv (“Finished” phase from Receiver side)

- Receive ct_kem and DEM (file ciphertext + MAC).
- Decapsulate: ss = decaps(sk_kem, ct_kem).
- Derive session key K from ss (+ maybe K_pake etc.).
- Verify DEM MAC with K.
- If OK, decrypt file → done.
*/
use crate::protocol::fsm::*;
use crate::crypto::pake::*;

pub struct ReceiverFsm {
    pub state: State,
}

impl ReceiverFsm {
    pub fn new(pw: Vec<u8>) -> Self {
        ReceiverFsm {
            state: State::Init { role: Role::Receiver, pw: Some(pw) },
        }
    }

    pub fn step (&mut self, tag: &str, input: Option<Vec<u8>>) -> Result<Vec<u8>, StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let (next_state, outbox) = match (current, tag, input) {
            (State::Init {role: Role::Receiver, pw: Some(pw)}, "PakeStart", input) => {
                let pake_pw = pw.as_slice();
                let mut pake_sender_state = PakeState::start_sender(pake_pw);
                let outbound_msg = pake_sender_state.take_outbound_msg();
                (State::Pake {role: Role::Receiver, pake_state: pake_sender_state}, outbound_msg)
            }
            (State::Pake {role: Role::Receiver, mut pake_state}, "Receiver_Answer", input) => {
                //Pre-Condition: PAKE started
                let pake_receiver_msg = input.ok_or_else(|| StepError::InvalidTransition("Expected input for PakeStart".into()))?;
                let sender_key = pake_state.finish(&pake_receiver_msg).expect("Sender failed to finish");
                //TODO: go into KEM-AUTH state
                
                // For this: 
                // - generate KEM  
                // - compute MAC tag over transcript + pk_kem
                // - transition into the new state and return the network message
                (State::Pake {role: Role::Receiver, pake_state}, sender_key)
            }
            (state,tag, input) => {
                (State::Failed(format!("invalid transition: from {:?} with ({:?},{:?})", state, tag, input)), Vec::new())
            }
        };

        self.state = next_state;
        Ok(outbox)
        
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn receiver_transitions_into_pake() {
        let mut fsm = ReceiverFsm::new(b"Password".to_vec());

        let out = fsm.step("PakeStart", None).expect("step failed");
        if let State::Pake { role: Role::Receiver, .. } = &fsm.state { } else { panic!("wrong state"); }
        assert!(!out.is_empty());
    }

     #[test]
    fn receiver_derives_sender_key_via_pake_sender_state() {
        // Single owner of the password Vec (no clone)
        let pw = b"Password".to_vec();

        // Build the mock peer FIRST (borrows pw only during the call)
        let mut peer_receiver = PakeState::start_receiver(pw.as_slice());

        // Move pw into the FSM (pw no longer accessible after this)
        let mut fsm = ReceiverFsm::new(pw);

        // Step 1: ReceiverFsm (application role Receiver) starts PAKE as "sender"
        let sender_msg = fsm
            .step("PakeStart", None)
            .expect("PakeStart step failed");
        assert!(!sender_msg.is_empty(), "expected non-empty sender PAKE msg");

        // Mock peer processes sender_msg:
        // - derives its key
        // - prepares a reply message back to the sender side
        let peer_key = peer_receiver
            .finish(&sender_msg)
            .expect("peer receiver failed to finish");

        // Most PAKE APIs store an outbound reply after finish; adjust if your API differs
        let receiver_reply_msg = peer_receiver.take_outbound_msg();
        assert!(
            !receiver_reply_msg.is_empty(),
            "expected non-empty receiver reply msg"
        );

        // Step 2: feed receiver's reply back into the FSM to derive sender_key
        let sender_key = fsm
            .step("Receivers_Answer", Some(receiver_reply_msg))
            .expect("Receivers_Answer step failed");

        // Basic sanity checks
        assert!(!sender_key.is_empty(), "expected non-empty sender_key");
        assert!(
            (16..=64).contains(&sender_key.len()),
            "unexpected sender_key length: {}",
            sender_key.len()
        );

        // Optional (strong) check: both sides derived the same key
        assert_eq!(sender_key, peer_key, "PAKE keys mismatch");
    }
}