/*
# Init
- Actively send PAKE-init.

# PAKE
- Run PAKE until finished.
- End result: shared K_pake, derive K_mac.

# SMT-wait-pk
- Wait for (pk_kem, tag) from Receiver.
- Verify tag with K_mac.
- If MAC ok:
    - Encapsulate: (ct_kem, ss) = encap(pk_kem).
    - Derive session key K from ss (+ maybe K_pake).
    - Encrypt file under DEM with K.
    - Compute DEM MAC with K.
    - Send (ct_kem, DEM) to Receiver.
Done.
*/
use crate::protocol::fsm::*;
use crate::crypto::pake::{self, *};

pub struct SenderFsm {
    pub state: State,
}

impl SenderFsm {
    pub fn new(pw: Vec<u8>) -> Self {
        SenderFsm {
            state: State::Init { role: Role::Sender, pw: Some(pw) },
        }
    }
    
    pub fn step (&mut self, tag: &str, input: Option<Vec<u8>>) -> Result<Vec<u8>, StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let (next_state, outbox) = match (current, tag, input) {
            (State::Init {role: Role::Sender, pw: Some(pw)}, "PakeStart", input) => {
                let pake_pw = pw.as_slice();
                let pake_sender_msg = input.ok_or_else(|| StepError::InvalidTransition("Expected input for PakeStart".into()))?;
                let mut pake_receiver_state = PakeState::start_receiver(pake_pw);
                let receiver_key = pake_receiver_state.finish(&pake_sender_msg)
            .expect("Receiver failed to finish");
                //TODO: transition into the KEM-AUTH state
                // For this:
                // - wait for (pk_kem, tag) from Receiver
                // - verify tag with K_mac
                // - if MAC ok, encapsulate, derive session key, encrypt file, compute DEM MAC
                (State::Pake {role: Role::Sender, pake_state: pake_receiver_state}, receiver_key)
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
    fn sender_transitions_into_pake_and_key_len_ok() {
        let pw = b"Password".to_vec();

        let outbound_msg = {
            let mut pake_sender_state = PakeState::start_sender(pw.as_slice());
            pake_sender_state.take_outbound_msg()
        };

        let mut fsm = SenderFsm::new(pw);

        let receiver_key = fsm
            .step("PakeStart", Some(outbound_msg))
            .expect("step failed");

        match &fsm.state {
            State::Pake { role: Role::Sender, .. } => {}
            other => panic!("wrong state: {:?}", other),
        }

        const KEY_LEN: usize = 32; // <- set to your protocol’s key length
        assert_eq!(receiver_key.len(), KEY_LEN, "unexpected receiver key length");
    }

    #[test]
    fn sender_pakestart_requires_input() {
        let mut fsm = SenderFsm::new(b"Password".to_vec());
        let err = fsm.step("PakeStart", None).unwrap_err();
        println!("got expected error: {:?}", err);
    }
}
