use crate::error::CryptoError;
use spake2::{Ed25519Group, Identity, Password, Spake2};

pub struct PakeState {
    inner: Option<Spake2<Ed25519Group>>,
    outbound: Vec<u8>,
}

impl PakeState {
    #[must_use]
    pub fn start_sender(pw: &[u8], receiver_id: &[u8], sender_id: &[u8]) -> PakeState {
        let (s1, outbound_msg) = Spake2::<Ed25519Group>::start_b(
            &Password::new(pw),
            &Identity::new(receiver_id),
            &Identity::new(sender_id),
        );
        PakeState {
            inner: Some(s1),
            outbound: outbound_msg,
        }
    }

    #[must_use]
    pub fn start_receiver(pw: &[u8], receiver_id: &[u8], sender_id: &[u8]) -> PakeState {
        let (s1, outbound_msg) = Spake2::<Ed25519Group>::start_a(
            &Password::new(pw),
            &Identity::new(receiver_id),
            &Identity::new(sender_id),
        );
        PakeState {
            inner: Some(s1),
            outbound: outbound_msg,
        }
    }

    #[must_use]
    pub fn outbound_msg(&self) -> Vec<u8> {
        self.outbound.clone()
    }

    pub fn take_outbound_msg(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.outbound)
    }

    /// Finishes the PAKE exchange with the peer's outbound message.
    ///
    /// # Errors
    ///
    /// Returns an error when this state has already been finished or when the
    /// peer message is invalid.
    pub fn finish(&mut self, inbound_msg: &[u8]) -> Result<Vec<u8>, CryptoError> {
        let state = self.inner.take().ok_or(CryptoError::PakeAlreadyFinished)?;
        state
            .finish(inbound_msg)
            .map_err(|_source| CryptoError::InvalidPakeMessage)
    }
}

#[cfg(test)]
// Clippy baseline: these tests retain explicit failure messages for PAKE setup.
mod tests {
    use super::*;

    #[test]
    fn same_password_same_key() {
        let pw = b"correct horse battery staple";

        let mut sender_state = PakeState::start_sender(pw, b"smt_receiver", b"smt_sender");
        let mut receiver_state = PakeState::start_receiver(pw, b"smt_receiver", b"smt_sender");

        // 1. take_outbound_msg() returns Vec<u8>
        // 2. finish() takes &[u8], so we pass it by reference (&)
        // 3. We .expect() because finish returns a Result
        let sender_key = sender_state
            .finish(&receiver_state.take_outbound_msg())
            .expect("Sender failed to finish");
        let receiver_key = receiver_state
            .finish(&sender_state.take_outbound_msg())
            .expect("Receiver failed to finish");

        assert_eq!(
            sender_key, receiver_key,
            "Keys should match for same password"
        );
    }
}
