use crate::crypto::error::CryptoError;
use ml_kem::kem::{Decapsulate, Encapsulate};
use ml_kem::{Ciphertext, Encoded, EncodedSizeUser, KemCore, MlKem768, SharedKey};
use rand_core::OsRng;

pub struct KemState {
    rng: OsRng,
    dk: Option<<MlKem768 as KemCore>::DecapsulationKey>,
    ek: Option<<MlKem768 as KemCore>::EncapsulationKey>,
}

pub struct EncapsulationResult {
    pub ciphertext: Ciphertext<MlKem768>,
    pub shared_secret: SharedKey<MlKem768>,
}

impl Default for KemState {
    fn default() -> Self {
        Self::new()
    }
}

impl KemState {
    #[must_use]
    pub fn new() -> Self {
        Self {
            rng: OsRng,
            dk: None,
            ek: None,
        }
    }

    pub fn generate_keypair(&mut self) {
        let (dk, ek) = MlKem768::generate(&mut self.rng);
        self.dk = Some(dk);
        self.ek = Some(ek);
    }

    /// Encapsulates a fresh shared key with the generated public key.
    ///
    /// Encapsulates a shared key using the current public key.
    pub fn encapsulate(&mut self) -> Result<EncapsulationResult, CryptoError> {
        let ek = self
            .ek
            .as_ref()
            .ok_or(CryptoError::MissingEncapsulationKey)?;
        let (ct, ss) = ek
            .encapsulate(&mut self.rng)
            .map_err(|_source| CryptoError::EncapsulationFailed)?;
        Ok(EncapsulationResult {
            ciphertext: ct,
            shared_secret: ss,
        })
    }

    /// Decapsulates a ciphertext with the generated private key.
    ///
    /// Decapsulates a shared key using the current private key.
    pub fn decapsulate(
        &self,
        ciphertext: &Ciphertext<MlKem768>,
    ) -> Result<SharedKey<MlKem768>, CryptoError> {
        let dk = self
            .dk
            .as_ref()
            .ok_or(CryptoError::MissingDecapsulationKey)?;
        dk.decapsulate(ciphertext)
            .map_err(|_source| CryptoError::DecapsulationFailed)
    }

    /// Returns the encoded public key.
    ///
    /// Returns the encoded public key when one is available.
    pub fn public_key_bytes(
        &self,
    ) -> Result<Encoded<<MlKem768 as KemCore>::EncapsulationKey>, CryptoError> {
        self.ek
            .as_ref()
            .map(|key| key.as_bytes())
            .ok_or(CryptoError::MissingEncapsulationKey)
    }

    pub fn set_public_key_bytes(
        &mut self,
        pk: &Encoded<<MlKem768 as KemCore>::EncapsulationKey>,
    ) -> Result<(), CryptoError> {
        let ek = <MlKem768 as KemCore>::EncapsulationKey::from_bytes(pk);
        self.dk = None;
        self.ek = Some(ek);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_keypair_sets_keys() {
        let mut kem = KemState::new();
        assert!(kem.dk.is_none());
        assert!(kem.ek.is_none());

        kem.generate_keypair();
        assert!(kem.dk.is_some());
        assert!(kem.ek.is_some());
    }

    #[test]
    fn kem_roundtrip_shared_secret_matches() {
        let mut kem = KemState::new();
        kem.generate_keypair();

        let res = kem.encapsulate().expect("encapsulation succeeds");
        let ss2 = kem
            .decapsulate(&res.ciphertext)
            .expect("decapsulation succeeds");

        assert_eq!(res.shared_secret, ss2);

        // If it doesn't, compare bytes instead (uncomment if needed):
        // assert_eq!(res.shared_secret.as_slice(), ss2.as_slice());
    }

    #[test]
    fn encapsulate_fails_without_keypair() {
        let mut kem = KemState::new();
        assert!(matches!(
            kem.encapsulate(),
            Err(CryptoError::MissingEncapsulationKey)
        ));
    }

    #[test]
    fn decapsulate_fails_without_keypair() {
        let mut source = KemState::new();
        source.generate_keypair();
        let ciphertext = source
            .encapsulate()
            .expect("encapsulation succeeds")
            .ciphertext;

        let kem = KemState::new();
        assert!(matches!(
            kem.decapsulate(&ciphertext),
            Err(CryptoError::MissingDecapsulationKey)
        ));
    }
}
