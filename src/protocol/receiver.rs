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

pub struct ReceiverFsm {
    pub state: State,
}

#[derive(Debug)]
enum Message {
    PakeInit {
        pw: Password,
        rendezvous: RendezvousInfo  
    },

    KemPkTag {
        pk_kem: KemPublicKey,
        tag: MacTag
    },

    KemCtDem {
        ct_kem: KemCiphertext,
        dem: DemData
    }
}

impl ReceiverFsm {
    pub fn new() -> Self {
        ReceiverFsm {
            state: State::Init { role: Role::Receiver },
        }
    }

    pub fn step(&mut self, input: Option<Message>) -> Result<Option<Message>, StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let (next_state, outgoing) = match (current, input) {
            (State::Init {role: Role::Receiver}, Some(Message::PakeInit { pw, rendezvous })) => {
                //Received the PW and rendezvous info --> initiate PAKE with sender at rendezvous
                todo!()
            }
            (state, msg) => {
                (State::Failed(format!("invalid transition: {:?} with {:?}", state, msg)), None)
            }
        };

        self.state = next_state;
        Ok(outgoing)
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn receiver_starts_in_init_state() {
        let fsm = ReceiverFsm::new();

        match fsm.state {
            State::Init { role: Role::Receiver } => {
                // Test passes
            }
            other => panic!("unexpected initial state: {:?}", other),
        }
    }
}