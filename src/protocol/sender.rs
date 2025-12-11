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

pub struct SenderFsm {
    pub state: State,
}

#[derive(Debug)]
enum Message {
    PakeStart {
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

impl SenderFsm {
    pub fn new() -> Self {
        SenderFsm {
            state: State::Init { role: Role::Sender },
        }
    }

    pub fn step(&mut self, input: Option<Message>) -> Result<Option<Message>, StepError> {
           let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let (next_state, outgoing) = match (current, input) {
            (State::Init {role: Role::Sender}, Some(Message::PakeStart { pw, rendezvous })) => {
                //Generated pw and rendezvouz info -> waiting for receiver to connect and init the PAKE.
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
