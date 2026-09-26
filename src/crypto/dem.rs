use rand_core::{OsRng, RngCore};

use crate::crypto::error::CryptoError;

use aes_gcm::{
    Aes256Gcm, Key, Nonce,
    aead::{Aead, AeadCore, AeadInPlace, KeyInit, Payload},
};

pub const NONCE_PREFIX_LEN: usize = 8;

// Clippy baseline: `DemState` keeps the cryptographic role explicit at call sites.
pub struct DemState {
    rng: OsRng,
}

pub struct SealResult {
    pub nonce: [u8; 12], // AES-GCM standard nonce size
    pub ciphertext: Vec<u8>,
}

impl Default for DemState {
    fn default() -> Self {
        Self::new()
    }
}

impl DemState {
    #[must_use]
    pub fn new() -> Self {
        Self { rng: OsRng }
    }

    /// Encrypts a message with AES-256-GCM and authenticates `aad`.
    ///
    /// # Errors
    ///
    /// Returns an error if AES-GCM cannot encrypt the message.
    pub fn seal(
        &mut self,
        key: &[u8; 32],
        plaintext: &[u8],
        aad: &[u8],
    ) -> Result<SealResult, aes_gcm::Error> {
        let aes_key = Key::<Aes256Gcm>::from_slice(key);
        let cipher = Aes256Gcm::new(aes_key);
        let nonce = Aes256Gcm::generate_nonce(&mut self.rng);
        let ciphertext = cipher.encrypt(
            &nonce,
            Payload {
                msg: plaintext,
                aad,
            },
        )?;
        let nonce: [u8; 12] = nonce.into();
        Ok(SealResult { nonce, ciphertext })
    }

    /// Decrypts a message produced by [`Self::seal`].
    ///
    /// # Errors
    ///
    /// Returns an error if the key, ciphertext, nonce, or authenticated data
    /// is invalid.
    pub fn open(
        &self,
        key: &[u8; 32],
        seal: SealResult,
        aad: &[u8],
    ) -> Result<Vec<u8>, aes_gcm::Error> {
        let SealResult { nonce, ciphertext } = seal;
        let aes_key = Key::<Aes256Gcm>::from_slice(key);
        let cipher = Aes256Gcm::new(aes_key);
        cipher.decrypt(
            Nonce::from_slice(&nonce),
            Payload {
                msg: ciphertext.as_ref(),
                aad,
            },
        )
    }
}

fn make_nonce(prefix: [u8; NONCE_PREFIX_LEN], ctr: u32) -> [u8; 12] {
    let mut n = [0u8; 12];
    n[0..8].copy_from_slice(&prefix);
    n[8..12].copy_from_slice(&ctr.to_be_bytes());
    n
}

// Clippy baseline: the stream sealer name makes its cryptographic role explicit.
pub struct DemStreamSealer {
    cipher: Aes256Gcm,
    prefix: [u8; NONCE_PREFIX_LEN],
    ctr: u32,
}

impl DemStreamSealer {
    #[must_use]
    pub fn new(key: &[u8; 32]) -> Self {
        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(key));
        let mut prefix = [0u8; NONCE_PREFIX_LEN];
        OsRng.fill_bytes(&mut prefix);
        Self {
            cipher,
            prefix,
            ctr: 0,
        }
    }

    #[must_use]
    pub fn nonce_prefix(&self) -> [u8; NONCE_PREFIX_LEN] {
        self.prefix
    }

    /// Encrypts one stream chunk.
    ///
    /// # Errors
    ///
    /// Returns an error if AES-GCM cannot encrypt the chunk.
    pub fn seal_chunk(&mut self, plaintext: &[u8]) -> Result<Vec<u8>, CryptoError> {
        let nonce_bytes = make_nonce(self.prefix, self.ctr);
        self.ctr = self.ctr.checked_add(1).ok_or(CryptoError::NonceExhausted)?;
        self.cipher
            .encrypt(Nonce::from_slice(&nonce_bytes), plaintext)
            .map_err(CryptoError::from)
    }

    /// Encrypts one stream chunk in place, appending the authentication tag.
    ///
    /// # Errors
    ///
    /// Returns an error if AES-GCM cannot encrypt the chunk.
    pub fn seal_chunk_in_place(&mut self, plaintext: &mut Vec<u8>) -> Result<(), CryptoError> {
        let nonce_bytes = make_nonce(self.prefix, self.ctr);
        self.ctr = self.ctr.checked_add(1).ok_or(CryptoError::NonceExhausted)?;
        self.cipher
            .encrypt_in_place(Nonce::from_slice(&nonce_bytes), &[], plaintext)
            .map_err(CryptoError::from)
    }
}

// Clippy baseline: the stream opener name makes its cryptographic role explicit.
pub struct DemStreamOpener {
    cipher: Aes256Gcm,
    prefix: [u8; NONCE_PREFIX_LEN],
    ctr: u32,
}

impl DemStreamOpener {
    #[must_use]
    pub fn new(key: &[u8; 32], prefix: [u8; NONCE_PREFIX_LEN]) -> Self {
        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(key));
        Self {
            cipher,
            prefix,
            ctr: 0,
        }
    }

    /// Decrypts one stream chunk.
    ///
    /// # Errors
    ///
    /// Returns an error if authentication fails or the ciphertext is invalid.
    pub fn open_chunk(&mut self, ciphertext: &[u8]) -> Result<Vec<u8>, CryptoError> {
        let nonce_bytes = make_nonce(self.prefix, self.ctr);
        self.ctr = self.ctr.checked_add(1).ok_or(CryptoError::NonceExhausted)?;
        self.cipher
            .decrypt(Nonce::from_slice(&nonce_bytes), ciphertext)
            .map_err(CryptoError::from)
    }

    /// Decrypts one stream chunk in place and removes its authentication tag.
    ///
    /// # Errors
    ///
    /// Returns an error if authentication fails or the ciphertext is invalid.
    pub fn open_chunk_in_place(&mut self, ciphertext: &mut Vec<u8>) -> Result<(), CryptoError> {
        let nonce_bytes = make_nonce(self.prefix, self.ctr);
        self.ctr = self.ctr.checked_add(1).ok_or(CryptoError::NonceExhausted)?;
        self.cipher
            .decrypt_in_place(Nonce::from_slice(&nonce_bytes), &[], ciphertext)
            .map_err(CryptoError::from)
    }
}

#[cfg(test)]
// Clippy baseline: these tests use explicit assertions, expects, and fixed test-vector indexes.
mod tests {
    use super::*;

    #[test]
    fn dem_roundtrip_recovers_plaintext() {
        let mut dem = DemState::new();

        let key = [7u8; 32];
        let aad = b"context";
        let pt = b"hello aes-gcm";

        let seal = dem.seal(&key, pt, aad).expect("seal failed");
        let opened = dem.open(&key, seal, aad).expect("open failed");

        assert_eq!(opened, pt);
    }

    #[test]
    fn dem_wrong_key_fails() {
        let mut dem = DemState::new();

        let key_ok = [1u8; 32];
        let key_bad = [2u8; 32];
        let aad = b"context";
        let pt = b"secret";

        let seal = dem.seal(&key_ok, pt, aad).expect("seal failed");
        let res = dem.open(&key_bad, seal, aad);

        assert!(res.is_err());
    }

    #[test]
    fn dem_tampered_ciphertext_fails() {
        let mut dem = DemState::new();

        let key = [9u8; 32];
        let aad = b"context";
        let pt = b"secret";

        let mut seal = dem.seal(&key, pt, aad).expect("seal failed");

        // flip one bit in ciphertext (will almost certainly break auth)
        seal.ciphertext[0] ^= 0x01;

        let res = dem.open(&key, seal, aad);
        assert!(res.is_err());
    }

    #[test]
    fn dem_nonce_is_12_bytes() {
        let mut dem = DemState::new();

        let key = [0u8; 32];
        let aad = b"aad";
        let pt = b"x";

        let seal = dem.seal(&key, pt, aad).expect("seal failed");
        assert_eq!(seal.nonce.len(), 12);
    }

    #[test]
    fn dem_stream_in_place_roundtrip_and_tamper_check() {
        let key = [11_u8; 32];
        let mut sealer = DemStreamSealer::new(&key);
        let mut opener = DemStreamOpener::new(&key, sealer.nonce_prefix());
        let expected = b"in-place encrypted chunk";
        let mut chunk = Vec::with_capacity(expected.len() + 16);
        chunk.extend_from_slice(expected);

        sealer
            .seal_chunk_in_place(&mut chunk)
            .expect("in-place encryption failed");
        assert_eq!(chunk.len(), expected.len() + 16);
        opener
            .open_chunk_in_place(&mut chunk)
            .expect("in-place decryption failed");
        assert_eq!(chunk, expected);

        let mut tampered = Vec::with_capacity(expected.len() + 16);
        tampered.extend_from_slice(expected);
        sealer
            .seal_chunk_in_place(&mut tampered)
            .expect("second in-place encryption failed");
        *tampered.last_mut().expect("tag should be present") ^= 1;
        assert!(opener.open_chunk_in_place(&mut tampered).is_err());
    }
}
