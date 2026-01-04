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
use crate::crypto::mac::*;
use crate::crypto::kem::*;
use crate::crypto::dem::*;
use ml_kem::{KemCore, EncodedSizeUser, MlKem768, Encoded};
use ml_kem::array::typenum::Unsigned;

const TAG_LEN: usize = 32;          
const PK_LEN: usize =
    <<<MlKem768 as KemCore>::EncapsulationKey as EncodedSizeUser>::EncodedSize as Unsigned>::USIZE;

fn parse_auth_kem_msg(input: &[u8])
    -> Result<([u8; TAG_LEN], Encoded<<MlKem768 as KemCore>::EncapsulationKey>), &'static str>
{
    if input.len() != TAG_LEN + PK_LEN {
        return Err("bad AUTH_KEM message length");
    }

    let (tag_bytes, pk_bytes) = input.split_at(TAG_LEN);

    let tag: [u8; TAG_LEN] = tag_bytes.try_into().map_err(|_| "bad tag len")?;
    let pk_enc: Encoded<<MlKem768 as KemCore>::EncapsulationKey> =
        pk_bytes.try_into().map_err(|_| "bad pk len")?;

    Ok((tag, pk_enc))
}

pub struct SenderFsm {
    pub state: State,
    pub file_data: Vec<u8>,
}

impl SenderFsm {
    pub fn new(pw: Vec<u8>) -> Self {
        SenderFsm {
            state: State::Init { role: Role::Sender, pw: Some(pw) },
            file_data: Vec::new(),
        }
    }

    pub fn step (&mut self, tag: &str, input: Option<Vec<u8>>) -> Result<Option<Vec<u8>>, StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let (next_state, outbox) = match (current, tag, input) {
            (State::Init {role: Role::Sender, pw: Some(pw)}, "PAKE_START", input) => {
                let pake_pw = pw.as_slice();
                let pake_sender_msg = input.ok_or_else(|| StepError::InvalidTransition("Expected input for PakeStart".into()))?;
                let mut pake_receiver_state = PakeState::start_receiver(pake_pw);
                let receiver_key = pake_receiver_state.finish(&pake_sender_msg)
            .expect("Receiver failed to finish");
                (State::Pake {role: Role::Sender, pake_state: pake_receiver_state, shared_key: Some(receiver_key)}, None)
            }
            (State::Pake {role: Role::Sender, pake_state: pake_receiver_state, shared_key}, "RECEIVED_AUTH_KEM", input) => {
                let input_bytes: &[u8] = input.as_deref().ok_or( StepError::InvalidTransition("Input is malformed".into()))?;
                let (tag, pk_enc) = parse_auth_kem_msg(input_bytes)?;
                let shared_key_bytes: &[u8] = shared_key.as_deref().ok_or_else(|| StepError::InvalidTransition("Missing shared_key".into()))?;
                let mac = MacState::new(shared_key_bytes);
                let vfy_flag = mac.verify(&pk_enc, &tag);
                if !vfy_flag {
                    return Err(StepError::InvalidTransition("MAC verification failed".into()));
                }
                let mut kem = KemState::new();
                kem.set_public_key_bytes(&pk_enc);
                let encaps_result = kem.encapsulate();

                let mut dem = DemState::new();
                let seal = dem.seal(encaps_result.shared_secret.as_ref(), &self.file_data, &[]).expect("seal failed");

                let tag = mac.tag(encaps_result.ciphertext.as_ref());
                
                let mut outbox = Vec::new();
                outbox.extend_from_slice(tag.as_ref());
                outbox.extend_from_slice(encaps_result.ciphertext.as_ref());
                outbox.extend_from_slice(seal.nonce.as_ref());
                outbox.extend_from_slice(seal.ciphertext.as_ref());

                (State::KemAuth { role: Role::Sender, mac: Some(mac), kem: Some(kem) }, Some(outbox))
             }
              (State::KemAuth { role: Role::Sender, mac: Some(mac), kem: Some(kem) }, "FIN", input) => {
                let bytes: &[u8] = input
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("FIN expected input bytes".into()))?;

                if bytes != b"FIN" {
                    return Err(StepError::InvalidTransition("FIN payload must be b\"FIN\"".into()));
                }
                (State::Success("Finished!".to_string()), None)
              }
              (state,tag, input) => {
                (State::Failed(format!("invalid transition: from {:?} with ({:?},{:?})", state, tag, input)), None)
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
    }

    #[test]
    fn sender_pakestart_requires_input() {
        let mut fsm = SenderFsm::new(b"Password".to_vec());
        let err = fsm.step("PakeStart", None).unwrap_err();
        println!("got expected error: {:?}", err);
    }
}
