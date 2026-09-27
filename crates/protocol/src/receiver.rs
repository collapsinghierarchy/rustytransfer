use crate::fsm::{Role, State, StepError};
use crate::{PAKE_RECEIVER_ID, PAKE_SENDER_ID};
use ml_kem::array::typenum::Unsigned;
use ml_kem::{Ciphertext, KemCore, MlKem768};
use rustytransfer_crypto::dem::DemStreamOpener;
use rustytransfer_crypto::kem::KemState;
use rustytransfer_crypto::mac::MacState;
use rustytransfer_crypto::pake::PakeState;
use sha3::{Digest, Sha3_256};

const KEM_CT_LEN: usize = <<MlKem768 as KemCore>::CiphertextSize as Unsigned>::USIZE;
const TAG_LEN: usize = 32;
const NONCE_LEN: usize = 12;
const NONCE_PREFIX_LEN: usize = 8;
const MAX_CHUNK_SIZE: u32 = 1024 * 1024;
const PREFIX_DIGEST_LEN: usize = 32;

type SmtHeader<'a> = (
    &'a [u8],
    Ciphertext<MlKem768>,
    [u8; NONCE_PREFIX_LEN],
    u64,
    u32,
    (u64, [u8; PREFIX_DIGEST_LEN]),
);
type SmtMessage<'a> = (&'a [u8], Ciphertext<MlKem768>, [u8; NONCE_LEN], &'a [u8]);

fn parse_smt_header(input: &[u8]) -> Result<SmtHeader<'_>, StepError> {
    let need = TAG_LEN + KEM_CT_LEN + NONCE_PREFIX_LEN + 8 + 4 + 8 + PREFIX_DIGEST_LEN;
    if input.len() != need {
        return Err(StepError::InvalidTransition(format!(
            "invalid SMT header length: got {}, expected {}",
            input.len(),
            need
        )));
    }

    let (tag_bytes, rest) = input.split_at(TAG_LEN);
    let (kem_ct_bytes, rest) = rest.split_at(KEM_CT_LEN);
    let (prefix_bytes, rest) = rest.split_at(NONCE_PREFIX_LEN);
    let (file_len_bytes, rest) = rest.split_at(8);
    let (chunk_size_bytes, resume_bytes) = rest.split_at(4);

    let kem_ct: Ciphertext<MlKem768> = kem_ct_bytes.try_into().map_err(|error| {
        StepError::InvalidTransition(format!("Bad KEM ciphertext length: {error}"))
    })?;

    let prefix: [u8; NONCE_PREFIX_LEN] = prefix_bytes.try_into().map_err(|error| {
        StepError::InvalidTransition(format!("Bad nonce_prefix length: {error}"))
    })?;

    let file_len = u64::from_be_bytes(
        file_len_bytes
            .try_into()
            .map_err(|error| StepError::InvalidTransition(format!("Bad file_len: {error}")))?,
    );

    let chunk_size = u32::from_be_bytes(
        chunk_size_bytes
            .try_into()
            .map_err(|error| StepError::InvalidTransition(format!("Bad chunk_size: {error}")))?,
    );

    let (offset_bytes, digest_bytes) = resume_bytes.split_at(8);
    let offset =
        u64::from_be_bytes(offset_bytes.try_into().map_err(|error| {
            StepError::InvalidTransition(format!("Bad resume offset: {error}"))
        })?);
    let digest = digest_bytes
        .try_into()
        .map_err(|error| StepError::InvalidTransition(format!("Bad resume digest: {error}")))?;

    Ok((
        tag_bytes,
        kem_ct,
        prefix,
        file_len,
        chunk_size,
        (offset, digest),
    ))
}

/// Parses an SMT message into its authenticated fields.
///
/// # Errors
///
/// Returns an error when the message is shorter than its tag, KEM
/// ciphertext, and nonce fields, or when a fixed-size field cannot be
/// decoded.
pub fn parse_smt_msg(input: &[u8], tag_len: usize) -> Result<SmtMessage<'_>, StepError> {
    let Some(need) = tag_len
        .checked_add(KEM_CT_LEN)
        .and_then(|value| value.checked_add(NONCE_LEN))
    else {
        return Err(StepError::InvalidTransition(
            "SMT input length overflows usize".into(),
        ));
    };
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

    let kem_ct: Ciphertext<MlKem768> = kem_ct_bytes.try_into().map_err(|error| {
        StepError::InvalidTransition(format!("Bad KEM ciphertext length: {error}"))
    })?;

    let nonce: [u8; NONCE_LEN] = nonce_bytes
        .try_into()
        .map_err(|error| StepError::InvalidTransition(format!("Bad nonce length: {error}")))?;

    Ok((tag_bytes, kem_ct, nonce, dem_ct_bytes))
}
// Clippy baseline: the public name identifies the receiver protocol state machine.
pub struct ReceiverFsm {
    pub state: State,

    file_len: u64,
    chunk_size: u32,
    bytes_recv: u64,
    header_received: bool,
    dem_stream: Option<DemStreamOpener>,
    offered_offset: u64,
    offered_digest: [u8; PREFIX_DIGEST_LEN],
    resume_offset: u64,
    resume_digest: [u8; PREFIX_DIGEST_LEN],
}

impl ReceiverFsm {
    #[must_use]
    pub fn new(pw: Vec<u8>, offer: crate::sender::ResumeOffer) -> Self {
        ReceiverFsm {
            state: State::Init {
                role: Role::Receiver,
                pw: Some(pw),
            },
            file_len: 0,
            chunk_size: 0,
            bytes_recv: 0,
            header_received: false,
            dem_stream: None,
            offered_offset: offer.offset,
            offered_digest: offer.prefix_digest,
            resume_offset: 0,
            resume_digest: Sha3_256::digest([]).into(),
        }
    }

    /// Creates a receiver state machine that uses a shared direct-transfer
    /// token instead of the PAKE exchange.
    #[must_use]
    pub fn new_direct(token: &[u8; 16], offer: crate::sender::ResumeOffer) -> Self {
        ReceiverFsm {
            state: State::DirectAuth {
                role: Role::Receiver,
                shared_key: token.to_vec(),
            },
            file_len: 0,
            chunk_size: 0,
            bytes_recv: 0,
            header_received: false,
            dem_stream: None,
            offered_offset: offer.offset,
            offered_digest: offer.prefix_digest,
            resume_offset: 0,
            resume_digest: Sha3_256::digest([]).into(),
        }
    }

    #[must_use]
    pub fn file_len(&self) -> u64 {
        self.file_len
    }
    #[must_use]
    pub fn bytes_received(&self) -> u64 {
        self.bytes_recv
    }
    #[must_use]
    pub fn chunk_size(&self) -> u32 {
        self.chunk_size
    }

    #[must_use]
    pub fn resume_offset(&self) -> u64 {
        self.resume_offset
    }

    #[must_use]
    pub fn remaining_len(&self) -> u64 {
        self.file_len.saturating_sub(self.resume_offset)
    }

    #[must_use]
    pub fn resume_digest(&self) -> [u8; PREFIX_DIGEST_LEN] {
        self.resume_digest
    }
    #[must_use]
    pub fn is_complete(&self) -> bool {
        self.header_received && self.bytes_recv == self.remaining_len()
    }

    fn start_auth_kem(
        shared_key: &[u8],
        resume_offer: (u64, [u8; PREFIX_DIGEST_LEN]),
    ) -> Result<(State, Option<Vec<u8>>), StepError> {
        let mut kem = KemState::new();
        kem.generate_keypair();
        let mac = MacState::new(shared_key);
        let pub_bytes = kem.public_key_bytes()?;
        let mac_input_len = pub_bytes
            .len()
            .checked_add(8)
            .and_then(|length| length.checked_add(PREFIX_DIGEST_LEN))
            .ok_or_else(|| {
                StepError::InvalidTransition("resume authentication input length overflow".into())
            })?;
        let mut mac_input = Vec::with_capacity(mac_input_len);
        mac_input.extend_from_slice(pub_bytes.as_ref());
        mac_input.extend_from_slice(&resume_offer.0.to_be_bytes());
        mac_input.extend_from_slice(&resume_offer.1);
        let tag = mac.tag(&mac_input);
        let mut outbox = Vec::new();
        outbox.extend_from_slice(tag.as_ref());
        outbox.extend_from_slice(pub_bytes.as_ref());
        outbox.extend_from_slice(&resume_offer.0.to_be_bytes());
        outbox.extend_from_slice(&resume_offer.1);
        Ok((
            State::KemAuth {
                role: Role::Receiver,
                mac: Some(mac),
                kem: Some(Box::new(kem)),
            },
            Some(outbox),
        ))
    }

    /// Advances the receiver state machine by one protocol message.
    ///
    /// # Errors
    ///
    /// Returns an error when the message does not match the current state,
    /// authentication fails, decryption fails, or the file size limits are
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
                    role: Role::Receiver,
                    pw: Some(pw),
                },
                "PAKE_START",
                _input,
            ) => {
                let pake_pw = pw.as_slice();
                let mut pake_sender_state =
                    PakeState::start_sender(pake_pw, PAKE_RECEIVER_ID, PAKE_SENDER_ID);
                let outbound_msg = pake_sender_state.take_outbound_msg();
                (
                    State::Pake {
                        role: Role::Receiver,
                        pake_state: pake_sender_state,
                        shared_key: None,
                    },
                    Some(outbound_msg),
                )
            }
            (
                State::Pake {
                    role: Role::Receiver,
                    mut pake_state,
                    shared_key: _,
                },
                "PAKE_ANSWER",
                input,
            ) => {
                let pake_receiver_msg = input.ok_or_else(|| {
                    StepError::InvalidTransition("Expected input for PakeStart".into())
                })?;
                let sender_key = pake_state.finish(&pake_receiver_msg)?;
                Self::start_auth_kem(&sender_key, (self.offered_offset, self.offered_digest))?
            }
            (
                State::DirectAuth {
                    role: Role::Receiver,
                    shared_key,
                },
                "DIRECT_AUTH",
                None,
            ) => Self::start_auth_kem(&shared_key, (self.offered_offset, self.offered_digest))?,
            // Receive SMT HEADER (tag || kem_ct || prefix || file_len || chunk_size), verify, decap, init stream opener
            (
                State::KemAuth {
                    role: Role::Receiver,
                    mac: Some(mac),
                    kem: Some(kem),
                },
                "SMT",
                input,
            ) => {
                let input_bytes: &[u8] = input.as_deref().ok_or_else(|| {
                    StepError::InvalidTransition("Missing SMT header input".into())
                })?;

                let (tag_bytes, kem_ct, prefix, file_len, chunk_size, resume) =
                    parse_smt_header(input_bytes)?;

                // Verify MAC over (kem_ct || prefix || file_len || chunk_size)
                let mut mac_input = Vec::new();
                mac_input.extend_from_slice(kem_ct.as_ref());
                mac_input.extend_from_slice(&prefix);
                mac_input.extend_from_slice(&file_len.to_be_bytes());
                mac_input.extend_from_slice(&chunk_size.to_be_bytes());
                mac_input.extend_from_slice(&resume.0.to_be_bytes());
                mac_input.extend_from_slice(&resume.1);

                let vfy_flag = mac.verify(mac_input.as_ref(), tag_bytes);
                if !vfy_flag {
                    return Err(StepError::Authentication);
                }

                if chunk_size == 0 || chunk_size > MAX_CHUNK_SIZE {
                    return Err(StepError::InvalidTransition(
                        "invalid advertised chunk size".into(),
                    ));
                }
                if file_len
                    > u64::from(chunk_size)
                        .checked_mul(u64::from(u32::MAX))
                        .ok_or_else(|| {
                            StepError::InvalidTransition("chunk limit overflow".into())
                        })?
                {
                    return Err(StepError::InvalidTransition(
                        "advertised file exceeds the stream nonce limit".into(),
                    ));
                }
                let empty_digest: [u8; PREFIX_DIGEST_LEN] = Sha3_256::digest([]).into();
                let selected_offer = resume.0 == self.offered_offset
                    && resume.1 == self.offered_digest
                    && resume.0 <= file_len;
                let selected_reset = resume.0 == 0 && resume.1 == empty_digest;
                if !selected_offer && !selected_reset {
                    return Err(StepError::InvalidTransition(
                        "sender selected an invalid resume prefix".into(),
                    ));
                }
                self.resume_offset = resume.0;
                self.resume_digest = resume.1;
                let dem_key = kem.decapsulate(&kem_ct)?;

                self.file_len = file_len;
                self.chunk_size = chunk_size;
                self.bytes_recv = 0;
                self.header_received = true;
                self.dem_stream = Some(DemStreamOpener::new(dem_key.as_ref(), prefix));

                (
                    State::Smt {
                        role: Role::Receiver,
                    },
                    None,
                )
            }
            // Receive & decrypt one SMT chunk
            (
                State::Smt {
                    role: Role::Receiver,
                },
                "SMT",
                Some(mut ct),
            ) => {
                if ct.is_empty() {
                    return Err(StepError::InvalidTransition(
                        "SMT chunks must contain ciphertext".into(),
                    ));
                }
                let opener = self
                    .dem_stream
                    .as_mut()
                    .ok_or_else(|| StepError::InvalidTransition("missing dem_stream".into()))?;

                opener.open_chunk_in_place(&mut ct)?;

                let chunk_size = usize::try_from(self.chunk_size).map_err(|error| {
                    StepError::InvalidTransition(format!("invalid chunk size: {error}"))
                })?;
                if ct.len() > chunk_size {
                    return Err(StepError::InvalidTransition(
                        "plaintext chunk larger than chunk_size".into(),
                    ));
                }

                let chunk_len = u64::try_from(ct.len()).map_err(|error| {
                    StepError::InvalidTransition(format!(
                        "chunk length does not fit in u64: {error}"
                    ))
                })?;
                let received = self.bytes_recv.checked_add(chunk_len).ok_or_else(|| {
                    StepError::InvalidTransition("received byte count overflow".into())
                })?;
                if received > self.remaining_len() {
                    return Err(StepError::InvalidTransition(
                        "received beyond file_len".into(),
                    ));
                }

                self.bytes_recv = received;

                (
                    State::Smt {
                        role: Role::Receiver,
                    },
                    Some(ct),
                )
            }
            // Finalize SMT (caller passes None after writing file_len bytes)
            (
                State::Smt {
                    role: Role::Receiver,
                },
                "SMT",
                None,
            ) => {
                if self.bytes_recv != self.remaining_len() {
                    return Err(StepError::InvalidTransition(
                        "SMT finalize called before full file received".into(),
                    ));
                }

                (
                    State::Success("Finished!".to_string()),
                    Some(b"FIN".to_vec()),
                )
            }
            _ => {
                return Err(StepError::InvalidTransition(
                    "invalid receiver transition".into(),
                ));
            }
        };

        self.state = next_state;
        Ok(outbox)
    }
}
