use hmac::{Hmac, Mac};
use sha3::{Digest, Sha3_256};

// Clippy baseline: the alias names make the MAC key size explicit in the public API.
pub type MacKey = [u8; 32];
// Clippy baseline: the alias names make the MAC tag size explicit in the public API.
pub type MacTag = [u8; 32];

type HmacSha3_256 = Hmac<Sha3_256>;

pub struct MacState {
    key: MacKey,
}

impl MacState {
    #[must_use]
    pub fn new(pake_key: &[u8]) -> Self {
        Self {
            key: derive_mac_key(pake_key),
        }
    }

    /// Computes an HMAC-SHA3-256 tag.
    ///
    /// # Panics
    ///
    /// Panics only if the HMAC implementation rejects the fixed 32-byte key;
    /// HMAC-SHA3-256 accepts keys of every length, so this cannot occur with
    /// the current implementation.
    // Clippy baseline: HMAC-SHA3-256 accepts keys of every length, including this fixed key.
    #[must_use]
    pub fn tag(&self, data: &[u8]) -> MacTag {
        let mut mac =
            HmacSha3_256::new_from_slice(&self.key).expect("HMAC accepts keys of any size");
        mac.update(data);
        let out = mac.finalize().into_bytes(); // 32 bytes for Sha3_256
        let mut tag = [0u8; 32];
        tag.copy_from_slice(&out);
        tag
    }

    /// Verifies an HMAC-SHA3-256 tag in constant time.
    ///
    /// # Panics
    ///
    /// Panics only if the HMAC implementation rejects the fixed 32-byte key;
    /// HMAC-SHA3-256 accepts keys of every length, so this cannot occur with
    /// the current implementation.
    // Clippy baseline: HMAC-SHA3-256 accepts keys of every length, including this fixed key.
    #[must_use]
    pub fn verify(&self, data: &[u8], tag: &[u8]) -> bool {
        let mut mac =
            HmacSha3_256::new_from_slice(&self.key).expect("HMAC accepts keys of any size");
        mac.update(data);
        mac.verify_slice(tag).is_ok()
    }
}

fn derive_mac_key(pake_key: &[u8]) -> MacKey {
    let d = Sha3_256::digest(pake_key);
    let mut key = [0u8; 32];
    key.copy_from_slice(&d);
    key
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tag_then_verify_succeeds() {
        let key: MacKey = [0x11; 32];
        let mac = MacState::new(&key);

        let msg = b"hello";
        let tag = mac.tag(msg);

        assert!(mac.verify(msg, &tag));
    }

    #[test]
    fn tampered_message_fails() {
        let key: MacKey = [0x22; 32];
        let mac = MacState::new(&key);

        let msg = b"hello";
        let tag = mac.tag(msg);

        let tampered = b"hell0";
        assert!(!mac.verify(tampered, &tag));
    }

    #[test]
    fn wrong_key_fails() {
        let key1: MacKey = [0x33; 32];
        let key2: MacKey = [0x44; 32];

        let mac1 = MacState::new(&key1);
        let mac2 = MacState::new(&key2);

        let msg = b"same message";
        let tag = mac1.tag(msg);

        assert!(!mac2.verify(msg, &tag));
    }

    #[test]
    fn tag_is_deterministic_for_same_key_and_msg() {
        let key: MacKey = [0x55; 32];
        let mac = MacState::new(&key);

        let msg = b"determinism";
        let t1 = mac.tag(msg);
        let t2 = mac.tag(msg);

        assert_eq!(t1, t2);
    }
}
