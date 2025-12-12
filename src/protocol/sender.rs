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
pub enum Event {
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

    pub fn step(&mut self, input: Option<Event>) -> Option<StepError> {
           let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let next_state = match (current, input) {
            (State::Init {role: Role::Sender}, Some(Event::PakeStart { pw, rendezvous })) => {
                //Pre-Condition: Generated pw and rendezvouz info
                //waiting for receiver to connect and init the PAKE
                //Post-Condition: PAKE started
                // transition to Pake state, or Failed on error
                State::Pake {role: Role::Sender, pw}

            }
            (State::Pake {role: Role::Sender, pw}, Some(Event::KemPkTag { pk_kem, tag })) => {
                //Pre-Condition: Pake started
                // Do the PAKE
                //Post-Condition: PAKE finished -> derived K_mac
                //transition to KemAuth state, or Failed on error
                State::KemAuth {role: Role::Sender, kem_pk: pk_kem, mac_key: MacKey}
            }
            /*
            (State::KemAuth {role: Role::Sender}, Some(Event::KemCtDem { ct_kem, dem })) => {
                //Pre-Condition: received pk_kem and tag from receiver
                todo!() // Verify tag with K_mac. 
                //Post-Condition: ...
                // transition to Smt state, or Failed on error
            }
            (State::Smt {role: Role::Sender}, Some(Event::KemCtDem { ct_kem, dem })) => {
                //Pre-Condition: ...
                todo!() // ...
                //Post-Condition: ...
                // transition to Success/Failed state
            }
            */
            (state, msg) => {
                State::Failed(format!("invalid transition: {:?} with {:?}", state, msg))
            }
        };
        self.state = next_state;
        None
    }
}
