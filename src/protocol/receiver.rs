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
pub enum Event {
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

    pub fn step(&mut self, input: Option<Event>) -> Option<StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let next_state = match (current, input) {
            (State::Init {role: Role::Receiver}, Some(Event::PakeInit { pw: _, rendezvous: _ })) => {
                //Pre-Condition: Received pw and rendezvouz info
                // connect to sender and init the PAKE
                //Post-Condition: PAKE started 
                // transition to Pake state, or Failed on error
                State::Pake {role: Role::Receiver, pw: Password}
            }
            (State::Pake {role: Role::Receiver, pw: _}, Some(Event::KemPkTag { pk_kem: _, tag :_})) => {
                //Pre-Condition: Pake started
                // Do the PAKE
                //Post-Condition: PAKE finished -> derived K_mac, generated and sent (pk_kem, tag) to sender
                //transition to KemAuth state, or Failed on error
                State::KemAuth {role: Role::Receiver, kem_pk: KemPublicKey, mac_key: MacKey}
            }
            // KemAuth -> Smt-recv
            // Smt-recv -> Success/Failed
            (state, msg) => {
                State::Failed(format!("invalid transition: {:?} with {:?}", state, msg))
            }
        };

        self.state = next_state;
        None
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn receiver_starts_in_init_state() {
        let fsm = ReceiverFsm::new();
        assert!(matches!(fsm.state, State::Init { role: Role::Receiver }));
    }

    #[test]
    fn receiver_transitions_into_pake() {
        let mut fsm = ReceiverFsm::new();
        let pw = Password;
        let rendezvous = RendezvousInfo;
        let pake_init = Event::PakeInit {
            pw,
            rendezvous
        };
        fsm.step(Some(pake_init));
        assert!(matches!(fsm.state, State::Pake { role: Role::Receiver, pw: Password }));
    }
}