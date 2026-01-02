use rand_core::{OsRng, RngCore};

use aes_gcm::{
    aead::{Aead, AeadCore, KeyInit, Payload},
    Aes256Gcm, Key, Nonce
};
use aes_gcm::aead::generic_array::GenericArray;

pub struct DemState {
    rng: OsRng,
}

pub struct SealResult {
    pub nonce: [u8; 12],      // AES-GCM standard nonce size
    pub ciphertext: Vec<u8>,  
}

impl DemState {
    pub fn new() -> Self {
        Self { rng: OsRng }
    }

    pub fn seal(&mut self, key: &[u8; 32], plaintext: &[u8], aad: &[u8]) -> Result<SealResult, aes_gcm::Error> {
        let aes_key = Key::<Aes256Gcm>::from_slice(key);
        let cipher = Aes256Gcm::new(&aes_key);
        let nonce = Aes256Gcm::generate_nonce(&mut self.rng);
        let ciphertext = cipher.encrypt(&nonce, plaintext)?;
        let nonce: [u8; 12] = nonce.as_slice().try_into().unwrap();
        Ok(SealResult { nonce, ciphertext })
    }

    pub fn open(&self, key: &[u8; 32], seal: SealResult) -> Result<Vec<u8>, aes_gcm::Error> {
        let aes_key = Key::<Aes256Gcm>::from_slice(key);
        let cipher = Aes256Gcm::new(&aes_key);
        cipher.decrypt(Nonce::from_slice(&seal.nonce), seal.ciphertext.as_ref())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dem_roundtrip_recovers_plaintext() {
        let mut dem = DemState::new();

        let key = [7u8; 32];
        let aad = b"context";
        let pt = b"hello aes-gcm";

        let seal = dem.seal(&key, pt, aad).expect("seal failed");
        let opened = dem.open(&key, seal).expect("open failed");

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
        let res = dem.open(&key_bad, seal);

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

        let res = dem.open(&key, seal);
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
}
