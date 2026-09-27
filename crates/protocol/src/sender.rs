use crate::fsm::{Role, State, StepError};
use crate::{PAKE_RECEIVER_ID, PAKE_SENDER_ID};
use ml_kem::array::typenum::Unsigned;
use ml_kem::{Encoded, EncodedSizeUser, KemCore, MlKem768};
use rustytransfer_crypto::dem::DemStreamSealer;
use rustytransfer_crypto::kem::KemState;
use rustytransfer_crypto::mac::MacState;
use rustytransfer_crypto::pake::PakeState;
use sha3::{Digest, Sha3_256};

const TAG_LEN: usize = 32;
const MAX_CHUNK_SIZE: u32 = 1024 * 1024;
const PK_LEN: usize =
    <<<MlKem768 as KemCore>::EncapsulationKey as EncodedSizeUser>::EncodedSize as Unsigned>::USIZE;
type EncodedPublicKey = Encoded<<MlKem768 as KemCore>::EncapsulationKey>;
const PREFIX_DIGEST_LEN: usize = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResumeOffer {
    pub offset: u64,
    pub prefix_digest: [u8; PREFIX_DIGEST_LEN],
}

fn empty_prefix_digest() -> [u8; PREFIX_DIGEST_LEN] {
    Sha3_256::digest([]).into()
}

fn parse_auth_kem_msg(
    input: &[u8],
) -> Result<([u8; TAG_LEN], EncodedPublicKey, ResumeOffer), StepError> {
    let expected_len = TAG_LEN + PK_LEN + 8 + PREFIX_DIGEST_LEN;
    if input.len() != expected_len {
        return Err(StepError::InvalidTransition(
            "bad AUTH_KEM message length".into(),
        ));
    }

    let (tag_bytes, pk_bytes) = input.split_at(TAG_LEN);
    let (pk_bytes, resume_bytes) = pk_bytes.split_at(PK_LEN);

    let tag: [u8; TAG_LEN] = tag_bytes
        .try_into()
        .map_err(|error| StepError::InvalidTransition(format!("bad tag length: {error}")))?;
    let pk_enc: EncodedPublicKey = pk_bytes
        .try_into()
        .map_err(|error| StepError::InvalidTransition(format!("bad public key length: {error}")))?;

    let (offset_bytes, digest_bytes) = resume_bytes.split_at(8);
    let offset =
        u64::from_be_bytes(offset_bytes.try_into().map_err(|error| {
            StepError::InvalidTransition(format!("bad resume offset: {error}"))
        })?);
    let prefix_digest = digest_bytes
        .try_into()
        .map_err(|error| StepError::InvalidTransition(format!("bad resume digest: {error}")))?;
    let resume_offer = ResumeOffer {
        offset,
        prefix_digest,
    };

    Ok((tag, pk_enc, resume_offer))
}

// Clippy baseline: the public name identifies the sender protocol state machine.
pub struct SenderFsm {
    pub state: State,

    file_len: u64,
    chunk_size: u32,
    bytes_sent: u64,
    dem_stream: Option<DemStreamSealer>,
    resume_offset: u64,
    resume_digest: [u8; PREFIX_DIGEST_LEN],
    source_prefix_digest: Option<[u8; PREFIX_DIGEST_LEN]>,
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
            resume_offset: 0,
            resume_digest: empty_prefix_digest(),
            source_prefix_digest: Some(empty_prefix_digest()),
        }
    }

    /// Creates a sender state machine that uses a shared direct-transfer token
    /// instead of the PAKE exchange.
    #[must_use]
    pub fn new_direct(token: &[u8; 16], file_len: u64, chunk_size: u32) -> Self {
        SenderFsm {
            state: State::DirectAuth {
                role: Role::Sender,
                shared_key: token.to_vec(),
            },
            file_len,
            chunk_size,
            bytes_sent: 0,
            dem_stream: None,
            resume_offset: 0,
            resume_digest: empty_prefix_digest(),
            source_prefix_digest: Some(empty_prefix_digest()),
        }
    }

    #[must_use]
    pub fn bytes_sent(&self) -> u64 {
        self.bytes_sent
    }

    #[must_use]
    pub fn resume_offset(&self) -> u64 {
        self.resume_offset
    }

    #[must_use]
    pub fn remaining_len(&self) -> u64 {
        self.file_len.saturating_sub(self.resume_offset)
    }

    /// Authenticates and parses a receiver's resume request before source I/O.
    /// The source prefix is checked by the caller, then `step` checks this
    /// request again and binds the selected offset and digest into the SMT MAC.
    pub fn authenticate_resume_offer(&self, input: &[u8]) -> Result<ResumeOffer, StepError> {
        let shared_key = match &self.state {
            State::Pake {
                role: Role::Sender,
                shared_key: Some(shared_key),
                ..
            } => shared_key.as_slice(),
            State::DirectAuth {
                role: Role::Sender,
                shared_key,
            } => shared_key.as_slice(),
            _ => {
                return Err(StepError::InvalidTransition(
                    "sender is not ready to authenticate a resume request".into(),
                ));
            }
        };
        let (tag, public_key, offer) = parse_auth_kem_msg(input)?;
        let mut mac_input = Vec::with_capacity(PK_LEN + 8 + PREFIX_DIGEST_LEN);
        mac_input.extend_from_slice(public_key.as_ref());
        mac_input.extend_from_slice(&offer.offset.to_be_bytes());
        mac_input.extend_from_slice(&offer.prefix_digest);
        if !rustytransfer_crypto::mac::MacState::new(shared_key).verify(&mac_input, &tag) {
            return Err(StepError::Authentication);
        }
        Ok(offer)
    }

    /// Supplies the source digest corresponding to an authenticated offer.
    pub fn set_source_prefix_digest(&mut self, digest: [u8; PREFIX_DIGEST_LEN]) {
        self.source_prefix_digest = Some(digest);
    }

    fn receive_auth_kem(
        &mut self,
        input: Option<Vec<u8>>,
        shared_key: &[u8],
    ) -> Result<(State, Option<Vec<u8>>), StepError> {
        if self.chunk_size == 0
            || self.chunk_size > MAX_CHUNK_SIZE
            || self.file_len
                > u64::from(self.chunk_size)
                    .checked_mul(u64::from(u32::MAX))
                    .ok_or_else(|| StepError::InvalidTransition("chunk limit overflow".into()))?
        {
            return Err(StepError::InvalidTransition(
                "invalid file or chunk size".into(),
            ));
        }

        let input_bytes: &[u8] = input
            .as_deref()
            .ok_or_else(|| StepError::InvalidTransition("Input is malformed".into()))?;
        let (tag, pk_enc, offer) = parse_auth_kem_msg(input_bytes)?;
        let mac = MacState::new(shared_key);
        let mut auth_input = Vec::with_capacity(PK_LEN + 8 + PREFIX_DIGEST_LEN);
        auth_input.extend_from_slice(pk_enc.as_ref());
        auth_input.extend_from_slice(&offer.offset.to_be_bytes());
        auth_input.extend_from_slice(&offer.prefix_digest);
        let source_digest = self.source_prefix_digest.ok_or_else(|| {
            StepError::InvalidTransition("source prefix digest was not supplied".into())
        })?;
        if offer.offset <= self.file_len && source_digest == offer.prefix_digest {
            self.resume_offset = offer.offset;
            self.resume_digest = offer.prefix_digest;
        } else {
            self.resume_offset = 0;
            self.resume_digest = empty_prefix_digest();
        }
        if !mac.verify(&auth_input, &tag) {
            return Err(StepError::Authentication);
        }
        let mut kem = KemState::new();
        kem.set_public_key_bytes(&pk_enc)?;
        let encaps_result = kem.encapsulate()?;

        let sealer = DemStreamSealer::new(encaps_result.shared_secret.as_ref());
        let prefix = sealer.nonce_prefix();
        self.dem_stream = Some(sealer);

        // Tag over the fresh KEM/DEM setup and selected transfer range.
        let mut mac_input = Vec::new();
        mac_input.extend_from_slice(encaps_result.ciphertext.as_ref());
        mac_input.extend_from_slice(&prefix);
        mac_input.extend_from_slice(&self.file_len.to_be_bytes());
        mac_input.extend_from_slice(&self.chunk_size.to_be_bytes());
        mac_input.extend_from_slice(&self.resume_offset.to_be_bytes());
        mac_input.extend_from_slice(&self.resume_digest);

        let tag = mac.tag(mac_input.as_ref());

        let mut outbox = Vec::new();
        outbox.extend_from_slice(tag.as_ref());
        outbox.extend_from_slice(encaps_result.ciphertext.as_ref());
        outbox.extend_from_slice(&prefix);
        outbox.extend_from_slice(&self.file_len.to_be_bytes());
        outbox.extend_from_slice(&self.chunk_size.to_be_bytes());
        outbox.extend_from_slice(&self.resume_offset.to_be_bytes());
        outbox.extend_from_slice(&self.resume_digest);

        Ok((State::Smt { role: Role::Sender }, Some(outbox)))
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
                let mut pake_receiver_state =
                    PakeState::start_receiver(pake_pw, PAKE_RECEIVER_ID, PAKE_SENDER_ID);
                let receiver_key = pake_receiver_state.finish(&pake_sender_msg)?;
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
                let shared_key_bytes: &[u8] = shared_key
                    .as_deref()
                    .ok_or_else(|| StepError::InvalidTransition("Missing shared_key".into()))?;
                self.receive_auth_kem(input, shared_key_bytes)?
            }
            (
                State::DirectAuth {
                    role: Role::Sender,
                    shared_key,
                },
                "RECEIVED_AUTH_KEM",
                input,
            ) => self.receive_auth_kem(input, &shared_key)?,
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
                if sent > self.remaining_len() {
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

                if self.bytes_sent != self.remaining_len() {
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
