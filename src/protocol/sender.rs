use crate::crypto::dem::DemStreamSealer;
use crate::crypto::kem::KemState;
use crate::crypto::mac::MacState;
use crate::crypto::pake::PakeState;
use crate::protocol::fsm::{Role, State, StepError};
use ml_kem::array::typenum::Unsigned;
use ml_kem::{Encoded, EncodedSizeUser, KemCore, MlKem768};

const TAG_LEN: usize = 32;
const MAX_CHUNK_SIZE: u32 = 1024 * 1024;
const PK_LEN: usize =
    <<<MlKem768 as KemCore>::EncapsulationKey as EncodedSizeUser>::EncodedSize as Unsigned>::USIZE;
type EncodedPublicKey = Encoded<<MlKem768 as KemCore>::EncapsulationKey>;

fn parse_auth_kem_msg(input: &[u8]) -> Result<([u8; TAG_LEN], EncodedPublicKey), StepError> {
    if input.len() != TAG_LEN + PK_LEN {
        return Err(StepError::InvalidTransition(
            "bad AUTH_KEM message length".into(),
        ));
    }

    let (tag_bytes, pk_bytes) = input.split_at(TAG_LEN);

    let tag: [u8; TAG_LEN] = tag_bytes
        .try_into()
        .map_err(|error| StepError::InvalidTransition(format!("bad tag length: {error}")))?;
    let pk_enc: EncodedPublicKey = pk_bytes
        .try_into()
        .map_err(|error| StepError::InvalidTransition(format!("bad public key length: {error}")))?;

    Ok((tag, pk_enc))
}

// Clippy baseline: the public name identifies the sender protocol state machine.
pub struct SenderFsm {
    pub state: State,

    file_len: u64,
    chunk_size: u32,
    bytes_sent: u64,
    dem_stream: Option<DemStreamSealer>,
}

impl SenderFsm {
    #[must_use]
    pub fn new(pw: Vec<u8>, file_len: u64, chunk_size: u32) -> Self {
        SenderFsm {
            state: State::Init {
                role: Role::Sender,
                pw: Some(pw),
            },
            file_len,
            chunk_size,
            bytes_sent: 0,
            dem_stream: None,
        }
    }

    #[must_use]
    pub fn bytes_sent(&self) -> u64 {
        self.bytes_sent
    }

    /// Advances the sender state machine by one protocol message.
    ///
    /// # Errors
    ///
    /// Returns an error when the message does not match the current state,
    /// authentication fails, encryption fails, or the file size limits are
    /// violated.
    pub fn step(
        &mut self,
        tag: &str,
        input: Option<Vec<u8>>,
    ) -> Result<Option<Vec<u8>>, StepError> {
        let current = std::mem::replace(&mut self.state, State::Failed("protocol failed".into()));

        let (next_state, outbox) = match (current, tag, input) {
            (
                State::Init {
                    role: Role::Sender,
                    pw: Some(pw),
                },
                "PAKE_START",
                input,
            ) => {
                if self.chunk_size == 0
                    || self.chunk_size > MAX_CHUNK_SIZE
                    || self.file_len
                        > u64::from(self.chunk_size)
                            .checked_mul(u64::from(u32::MAX))
                            .ok_or_else(|| {
                                StepError::InvalidTransition("chunk limit overflow".into())
                            })?
                {
                    return Err(StepError::InvalidTransition(
                        "invalid file or chunk size".into(),
                    ));
                }
                let pake_pw = pw.as_slice();
                let pake_sender_msg = input.ok_or_else(|| {
                    StepError::InvalidTransition("Expected input for PakeStart".into())
                })?;
                let mut pake_receiver_state = PakeState::start_receiver(pake_pw);
                let receiver_key = pake_receiver_state.finish_internal(&pake_sender_msg)?;
                (
                    State::Pake {
                        role: Role::Sender,
                        pake_state: pake_receiver_state,
                        shared_key: Some(receiver_key),
                    },
                    None,
                )
            }
            (
                State::Pake {
                    role: Role::Sender,
                    pake_state: _,
                    shared_key,
                },
                "RECEIVED_AUTH_KEM",
                input,
            ) => {
                let input_bytes: &[u8] = input
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("Input is malformed".into()))?;
                let (tag, pk_enc) = parse_auth_kem_msg(input_bytes)?;
                let shared_key_bytes: &[u8] = shared_key
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("Missing shared_key".into()))?;
                let mac = MacState::new(shared_key_bytes);
                let vfy_flag = mac.verify(&pk_enc, &tag);
                if !vfy_flag {
                    return Err(StepError::Authentication);
                }
                let mut kem = KemState::new();
                kem.set_public_key_bytes(&pk_enc)?;
                let encaps_result = kem.encapsulate()?;

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

                (State::Smt { role: Role::Sender }, Some(outbox))
            }
            (State::Smt { role: Role::Sender }, "SMT", Some(mut pt)) => {
                // optional: enforce your chosen chunk_size
                if pt.is_empty() {
                    return Err(StepError::InvalidTransition(
                        "SMT chunks must contain plaintext".into(),
                    ));
                }
                let chunk_size = usize::try_from(self.chunk_size).map_err(|error| {
                    StepError::InvalidTransition(format!("invalid chunk size: {error}"))
                })?;
                if pt.len() > chunk_size {
                    return Err(StepError::InvalidTransition(
                        "chunk larger than chunk_size".into(),
                    ));
                }

                // required: don't send more plaintext than file_len
                let chunk_len = u64::try_from(pt.len()).map_err(|error| {
                    StepError::InvalidTransition(format!(
                        "chunk length does not fit in u64: {error}"
                    ))
                })?;
                let sent = self.bytes_sent.checked_add(chunk_len).ok_or_else(|| {
                    StepError::InvalidTransition("sent byte count overflow".into())
                })?;
                if sent > self.file_len {
                    return Err(StepError::InvalidTransition(
                        "sending beyond file_len".into(),
                    ));
                }

                let sealer = self
                    .dem_stream
                    .as_mut()
                    .ok_or_else(|| StepError::InvalidTransition("missing dem_stream".into()))?;

                sealer.seal_chunk_in_place(&mut pt)?;
                self.bytes_sent = sent;

                (State::Smt { role: Role::Sender }, Some(pt))
            }
            (State::Smt { role: Role::Sender }, "SMT", None) => {
                return Err(StepError::InvalidTransition(
                    "SMT chunk missing input".into(),
                ));
            }
            (State::Smt { role: Role::Sender }, "FIN", input) => {
                let bytes: &[u8] = input.as_deref().ok_or_else(|| {
                    StepError::InvalidTransition("FIN expected input bytes".into())
                })?;

                if bytes != b"FIN" {
                    return Err(StepError::InvalidTransition(
                        "FIN payload must be b\"FIN\"".into(),
                    ));
                }

                if self.bytes_sent != self.file_len {
                    return Err(StepError::InvalidTransition(
                        "FIN received before full file sent".into(),
                    ));
                }

                (State::Success("Finished!".to_string()), None)
            }
            _ => {
                return Err(StepError::InvalidTransition(
                    "invalid sender transition".into(),
                ));
            }
        };
        self.state = next_state;
        Ok(outbox)
    }
}
