use aes_gcm::KeyInit;
use hmac::Hmac;
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
const NONCE_PREFIX_LEN: usize = 8;

fn parse_smt_header(
    input: &[u8],
) -> Result<(
    &[u8],                    // tag
    Ciphertext<MlKem768>,     // kem_ct
    [u8; NONCE_PREFIX_LEN],   // nonce_prefix
    u64,                      // file_len
    u32,                      // chunk_size
), StepError> {
    let need = TAG_LEN + KEM_CT_LEN + NONCE_PREFIX_LEN + 8 + 4;
    if input.len() < need {
        return Err(StepError::InvalidTransition(format!(
            "SMT header too short: got {}, need {}",
            input.len(),
            need
        )));
    }

    let (tag_bytes, rest) = input.split_at(TAG_LEN);
    let (kem_ct_bytes, rest) = rest.split_at(KEM_CT_LEN);
    let (prefix_bytes, rest) = rest.split_at(NONCE_PREFIX_LEN);
    let (file_len_bytes, rest) = rest.split_at(8);
    let (chunk_size_bytes, _rest) = rest.split_at(4);

    let kem_ct: Ciphertext<MlKem768> = kem_ct_bytes
        .try_into()
        .map_err(|_| StepError::InvalidTransition("Bad KEM ciphertext length".into()))?;

    let prefix: [u8; NONCE_PREFIX_LEN] = prefix_bytes
        .try_into()
        .map_err(|_| StepError::InvalidTransition("Bad nonce_prefix length".into()))?;

    let file_len = u64::from_be_bytes(
        file_len_bytes
            .try_into()
            .map_err(|_| StepError::InvalidTransition("Bad file_len".into()))?,
    );

    let chunk_size = u32::from_be_bytes(
        chunk_size_bytes
            .try_into()
            .map_err(|_| StepError::InvalidTransition("Bad chunk_size".into()))?,
    );

    Ok((tag_bytes, kem_ct, prefix, file_len, chunk_size))
}

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

    file_len: u64,
    chunk_size: u32,
    bytes_recv: u64,
    dem_stream: Option<DemStreamOpener>,
}

impl ReceiverFsm {
    pub fn new(pw: Vec<u8>) -> Self {
        ReceiverFsm {
            state: State::Init { role: Role::Receiver, pw: Some(pw) },
            file_len: 0,
            chunk_size: 0,
            bytes_recv: 0,
            dem_stream: None,
        }
    }

    pub fn file_len(&self) -> u64 { self.file_len }
    pub fn bytes_received(&self) -> u64 { self.bytes_recv }
    pub fn chunk_size(&self) -> u32 { self.chunk_size }
    pub fn is_complete(&self) -> bool { self.file_len > 0 && self.bytes_recv == self.file_len }

    pub fn step (&mut self, tag: &str, input: Option<Vec<u8>>) -> Result<Option<Vec<u8>>, StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("stepped from invalid state".into()));

        let (next_state, outbox) = match (current, tag, input) {
            (State::Init {role: Role::Receiver, pw: Some(pw)}, "PAKE_START", _input) => {
                let pake_pw = pw.as_slice();
                let mut pake_sender_state = PakeState::start_sender(pake_pw);
                let outbound_msg = pake_sender_state.take_outbound_msg();
                (State::Pake {role: Role::Receiver, pake_state: pake_sender_state, shared_key: None}, Some(outbound_msg))
            }
            (State::Pake {role: Role::Receiver, mut pake_state, shared_key: _}, "PAKE_ANSWER", input) => {
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
            // Receive SMT HEADER (tag || kem_ct || prefix || file_len || chunk_size), verify, decap, init stream opener
            (State::KemAuth { role: Role::Receiver, mac: Some(mac), kem: Some(kem) }, "SMT", input) => {
                let input_bytes: &[u8] = input
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("Missing SMT header input".into()))?;

                let (tag_bytes, kem_ct, prefix, file_len, chunk_size) = parse_smt_header(input_bytes)?;

                // Verify MAC over (kem_ct || prefix || file_len || chunk_size)
                let mut mac_input = Vec::new();
                mac_input.extend_from_slice(kem_ct.as_ref());
                mac_input.extend_from_slice(&prefix);
                mac_input.extend_from_slice(&file_len.to_be_bytes());
                mac_input.extend_from_slice(&chunk_size.to_be_bytes());

                let vfy_flag = mac.verify(mac_input.as_ref(), tag_bytes);
                if !vfy_flag {
                    return Err(StepError::InvalidTransition("MAC verification failed".into()));
                }

                let dem_key = kem.decapsulate(&kem_ct);

                self.file_len = file_len;
                self.chunk_size = chunk_size;
                self.bytes_recv = 0;
                self.dem_stream = Some(DemStreamOpener::new(dem_key.as_ref(), prefix));

                (State::Smt { role: Role::Receiver }, None)
            }
             // Receive & decrypt one SMT chunk
            (State::Smt { role: Role::Receiver }, "SMT", Some(ct)) => {
                let opener = self.dem_stream
                    .as_mut()
                    .ok_or_else(|| StepError::InvalidTransition("missing dem_stream".into()))?;

                let pt = opener.open_chunk(ct.as_slice())?;

                if pt.len() > self.chunk_size as usize {
                    return Err(StepError::InvalidTransition("plaintext chunk larger than chunk_size".into()));
                }

                if self.bytes_recv + pt.len() as u64 > self.file_len {
                    return Err(StepError::InvalidTransition("received beyond file_len".into()));
                }

                self.bytes_recv += pt.len() as u64;

                (State::Smt { role: Role::Receiver }, Some(pt))
            }
             // Finalize SMT (caller passes None after writing file_len bytes)
            (State::Smt { role: Role::Receiver }, "SMT", None) => {
                if self.bytes_recv != self.file_len {
                    return Err(StepError::InvalidTransition(
                        "SMT finalize called before full file received".into(),
                    ));
                }

                (State::Success("Finished!".to_string()), Some(b"FIN".to_vec()))
            }
            (state,tag, input) => {
                (State::Failed(format!("invalid transition: from {:?} with ({:?},{:?})", state, tag, input)), None)
            }
        };

        self.state = next_state;
        Ok(outbox)
        
    }
}