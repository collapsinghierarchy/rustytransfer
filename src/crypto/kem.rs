use rand_core::OsRng;
use ml_kem::{Ciphertext, Encoded, KemCore, MlKem768, SharedKey,EncodedSizeUser};
use ml_kem::kem::{Decapsulate, Encapsulate};

#[derive(Debug)]
pub struct KemState {
    rng: OsRng,
    dk: Option<<MlKem768 as KemCore>::DecapsulationKey>,
    ek: Option<<MlKem768 as KemCore>::EncapsulationKey>
}

pub struct EncapsulationResult {
    pub ciphertext: Ciphertext<MlKem768>,
    pub shared_secret: SharedKey<MlKem768>,
}

impl KemState {
    pub fn new() -> Self {
        Self { rng: OsRng, dk: None, ek: None }
    }

    pub fn generate_keypair(&mut self) {
        let (dk, ek) = MlKem768::generate(&mut self.rng);
        self.dk = Some(dk);
        self.ek = Some(ek);
    }

    pub fn encapsulate(&mut self) -> EncapsulationResult {
        let ek = self.ek.as_ref().expect("Encapsulation key not generated");
        let (ct, ss) = ek.encapsulate(&mut self.rng).unwrap();
        EncapsulationResult {
            ciphertext: ct,
            shared_secret: ss
        }
    }

    pub fn decapsulate(&self, ciphertext: &Ciphertext<MlKem768>) -> SharedKey<MlKem768> {
        let dk = self.dk.as_ref().expect("Decapsulation key not generated");
        dk.decapsulate(ciphertext).unwrap()
    }

    pub fn public_key_bytes(&self) -> Encoded<<MlKem768 as KemCore>::EncapsulationKey> {
        self.ek
            .as_ref()
            .expect("Encapsulation key not generated")
            .as_bytes()
    }

    pub fn set_public_key_bytes(&mut self, pk: &Encoded<<MlKem768 as KemCore>::EncapsulationKey>) {
        let ek = <MlKem768 as KemCore>::EncapsulationKey::from_bytes(pk);
        self.ek = Some(ek);
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

        let res = kem.encapsulate();
        let ss2 = kem.decapsulate(&res.ciphertext);

        assert_eq!(res.shared_secret, ss2);

        // If it doesn't, compare bytes instead (uncomment if needed):
        // assert_eq!(res.shared_secret.as_slice(), ss2.as_slice());
    }

    #[test]
    #[should_panic(expected = "Encapsulation key not generated")]
    fn encapsulate_panics_without_keypair() {
        let mut kem = KemState::new();
        let _ = kem.encapsulate();
    }

    #[test]
    #[should_panic(expected = "Decapsulation key not generated")]
    fn decapsulate_panics_without_keypair() {
        let kem = KemState::new();
        // ciphertext value doesn't matter because it should panic before use
        let dummy = unsafe { std::mem::MaybeUninit::<Ciphertext<MlKem768>>::uninit().assume_init() };
        let _ = kem.decapsulate(&dummy);
    }
}