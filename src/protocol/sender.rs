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

    file_len: u64,
    chunk_size: u32,
    bytes_sent: u64,
    dem_stream: Option<DemStreamSealer>
}

impl SenderFsm {
    pub fn new(pw: Vec<u8>, file_len: u64, chunk_size: u32) -> Self {
        SenderFsm {
            state: State::Init { role: Role::Sender, pw: Some(pw) },
            file_len: file_len,
            chunk_size: chunk_size,
            bytes_sent: 0,
            dem_stream: None
        }
    }

    pub fn bytes_sent(&self) -> u64 {
        self.bytes_sent
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

                let sealer = DemStreamSealer::new(encaps_result.shared_secret.as_ref());
                let prefix = sealer.nonce_prefix();
                self.dem_stream = Some(sealer);

                // tag over (kem_ct || prefix || file_len || chunk_size)
                let mut mac_input = Vec::new();
                mac_input.extend_from_slice(encaps_result.ciphertext.as_ref());
                mac_input.extend_from_slice(&prefix);
                mac_input.extend_from_slice(&self.file_len.to_be_bytes());
                mac_input.extend_from_slice(&self.chunk_size.to_be_bytes());

                let tag = mac.tag(mac_input.as_ref());

                let mut outbox = Vec::new();
                outbox.extend_from_slice(tag.as_ref());
                outbox.extend_from_slice(encaps_result.ciphertext.as_ref());
                outbox.extend_from_slice(&prefix);
                outbox.extend_from_slice(&self.file_len.to_be_bytes());
                outbox.extend_from_slice(&self.chunk_size.to_be_bytes());

                (State::Smt { role: Role::Sender}, Some(outbox))
             }
             (State::Smt { role: Role::Sender }, "SMT", input) => {
                let pt: &[u8] = input
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("SMT chunk missing input".into()))?;

                // optional: enforce your chosen chunk_size
                if pt.len() > self.chunk_size as usize {
                    return Err(StepError::InvalidTransition("chunk larger than chunk_size".into()));
                }

                // required: don't send more plaintext than file_len
                if self.bytes_sent + pt.len() as u64 > self.file_len {
                    return Err(StepError::InvalidTransition("sending beyond file_len".into()));
                }

                let sealer = self.dem_stream
                    .as_mut()
                    .ok_or_else(|| StepError::InvalidTransition("missing dem_stream".into()))?;

                let ct = sealer.seal_chunk(pt)?;
                self.bytes_sent += pt.len() as u64;

                (State::Smt { role: Role::Sender }, Some(ct))
            }
              (State::Smt { role: Role::Sender }, "FIN", input) => {
                let bytes: &[u8] = input
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("FIN expected input bytes".into()))?;

                if bytes != b"FIN" {
                    return Err(StepError::InvalidTransition("FIN payload must be b\"FIN\"".into()));
                }
                
                if self.bytes_sent != self.file_len {
                    return Err(StepError::InvalidTransition("FIN received before full file sent".into()));
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
