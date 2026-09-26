use crate::crypto::dem::DemStreamOpener;
use crate::crypto::kem::KemState;
use crate::crypto::mac::MacState;
use crate::crypto::pake::PakeState;
use crate::protocol::fsm::{Role, State, StepError};
use ml_kem::array::typenum::Unsigned;
use ml_kem::{Ciphertext, KemCore, MlKem768};

const KEM_CT_LEN: usize = <<MlKem768 as KemCore>::CiphertextSize as Unsigned>::USIZE;
const TAG_LEN: usize = 32;
const NONCE_LEN: usize = 12;
const NONCE_PREFIX_LEN: usize = 8;
const MAX_CHUNK_SIZE: u32 = 1024 * 1024;

type SmtHeader<'a> = (
    &'a [u8],
    Ciphertext<MlKem768>,
    [u8; NONCE_PREFIX_LEN],
    u64,
    u32,
);
type SmtMessage<'a> = (&'a [u8], Ciphertext<MlKem768>, [u8; NONCE_LEN], &'a [u8]);

fn parse_smt_header(input: &[u8]) -> Result<SmtHeader<'_>, StepError> {
    let need = TAG_LEN + KEM_CT_LEN + NONCE_PREFIX_LEN + 8 + 4;
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
    let (chunk_size_bytes, _rest) = rest.split_at(4);

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

    Ok((tag_bytes, kem_ct, prefix, file_len, chunk_size))
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
}

impl ReceiverFsm {
    #[must_use]
    pub fn new(pw: Vec<u8>) -> Self {
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
    pub fn is_complete(&self) -> bool {
        self.header_received && self.bytes_recv == self.file_len
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
                let mut pake_sender_state = PakeState::start_sender(pake_pw);
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
                let sender_key = pake_state.finish_internal(&pake_receiver_msg)?;
                let mut kem = KemState::new();
                kem.generate_keypair();
                let mac = MacState::new(&sender_key);
                let pub_bytes = kem.public_key_bytes()?;
                let tag = mac.tag(&pub_bytes);
                let mut outbox = Vec::new();
                outbox.extend_from_slice(tag.as_ref());
                outbox.extend_from_slice(pub_bytes.as_ref());
                (
                    State::KemAuth {
                        role: Role::Receiver,
                        mac: Some(mac),
                        kem: Some(Box::new(kem)),
                    },
                    Some(outbox),
                )
            }
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

                let (tag_bytes, kem_ct, prefix, file_len, chunk_size) =
                    parse_smt_header(input_bytes)?;

                // Verify MAC over (kem_ct || prefix || file_len || chunk_size)
                let mut mac_input = Vec::new();
                mac_input.extend_from_slice(kem_ct.as_ref());
                mac_input.extend_from_slice(&prefix);
                mac_input.extend_from_slice(&file_len.to_be_bytes());
                mac_input.extend_from_slice(&chunk_size.to_be_bytes());

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
                if received > self.file_len {
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
                if self.bytes_recv != self.file_len {
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
