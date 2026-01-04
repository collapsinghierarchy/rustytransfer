use aes_gcm::KeyInit;
use hmac::Hmac;

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
use crate::crypto::kem::*;
use crate::crypto::dem::*;
use crate::crypto::mac::*;
use ml_kem::{Ciphertext, KemCore, MlKem768};
use ml_kem::array::typenum::Unsigned;

const KEM_CT_LEN: usize = <<MlKem768 as KemCore>::CiphertextSize as Unsigned>::USIZE;
const TAG_LEN: usize = 32;
const NONCE_LEN: usize = 12;

pub fn parse_smt_msg(
    input: &[u8],
    tag_len: usize,
) -> Result<(&[u8], Ciphertext<MlKem768>, [u8; NONCE_LEN], &[u8]), StepError> {
    let need = tag_len + KEM_CT_LEN + NONCE_LEN;
    if input.len() < need {
        return Err(StepError::InvalidTransition(format!(
            "SMT input too short: got {}, need at least {}",
            input.len(),
            need
        )));
    }

    let (tag_bytes, rest) = input.split_at(tag_len);
    let (kem_ct_bytes, rest) = rest.split_at(KEM_CT_LEN);
    let (nonce_bytes, dem_ct_bytes) = rest.split_at(NONCE_LEN);

    let kem_ct: Ciphertext<MlKem768> = kem_ct_bytes
        .try_into()
        .map_err(|_| StepError::InvalidTransition("Bad KEM ciphertext length".into()))?;

    let nonce: [u8; NONCE_LEN] = nonce_bytes
        .try_into()
        .map_err(|_| StepError::InvalidTransition("Bad nonce length".into()))?;

    Ok((tag_bytes, kem_ct, nonce, dem_ct_bytes))
}
pub struct ReceiverFsm {
    pub state: State,
}

impl ReceiverFsm {
    pub fn new(pw: Vec<u8>) -> Self {
        ReceiverFsm {
            state: State::Init { role: Role::Receiver, pw: Some(pw) },
        }
    }

    pub fn step (&mut self, tag: &str, input: Option<Vec<u8>>) -> Result<Option<Vec<u8>>, StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let (next_state, outbox) = match (current, tag, input) {
            (State::Init {role: Role::Receiver, pw: Some(pw)}, "PAKE_START", input) => {
                let pake_pw = pw.as_slice();
                let mut pake_sender_state = PakeState::start_sender(pake_pw);
                let outbound_msg = pake_sender_state.take_outbound_msg();
                (State::Pake {role: Role::Receiver, pake_state: pake_sender_state, shared_key: None}, Some(outbound_msg))
            }
            (State::Pake {role: Role::Receiver, mut pake_state, shared_key}, "PAKE_ANSWER", input) => {
                //Pre-Condition: PAKE started
                let pake_receiver_msg = input.ok_or_else(|| StepError::InvalidTransition("Expected input for PakeStart".into()))?;
                let sender_key = pake_state.finish(&pake_receiver_msg).expect("Sender failed to finish");
                let mut kem = KemState::new();
                kem.generate_keypair();
                let mac = MacState::new(&sender_key);
                let pub_bytes = kem.public_key_bytes();
                let tag = mac.tag(&pub_bytes);
                let mut outbox = Vec::new();
                outbox.extend_from_slice(tag.as_ref());
                outbox.extend_from_slice(pub_bytes.as_ref());
                (State::KemAuth {role: Role::Receiver, mac: Some(mac), kem: Some(kem)}, Some(outbox))
            }
            (State::KemAuth {role: Role::Receiver, mac: Some(mac), kem: Some(kem)}, "SMT", input) => {
                let input_bytes: &[u8] = input
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("Missing SMT input".into()))?;

                let (tag_bytes, kem_ct, nonce, dem_ct_bytes) = parse_smt_msg(input_bytes, 32)?;
                let vfy_flag = mac.verify(kem_ct.as_ref(), tag_bytes);
                if !vfy_flag {
                    return Err(StepError::InvalidTransition("MAC verification failed".into()));
                }

                let key = kem.decapsulate(&kem_ct);
                let dem = DemState::new();
                let seal = SealResult {
                    nonce: nonce.into(),
                    ciphertext: dem_ct_bytes.into(),
                };
                let pt = dem.open(key.as_ref(), seal)?;
                (State::Smt { role: Role::Receiver }, Some(pt))
            }
            (State::Smt {role: Role::Receiver}, "SMT", input) => {
                (State::Success("Finished!".to_string()), b"FIN".to_vec().into())
            }
            (state,tag, input) => {
                (State::Failed(format!("invalid transition: from {:?} with ({:?},{:?})", state, tag, input)), None)
            }
        };

        self.state = next_state;
        Ok(outbox)
        
    }
}