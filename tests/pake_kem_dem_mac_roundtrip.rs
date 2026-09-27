use anyhow::{Result, anyhow};
use rustytransfer::crypto::dem::DemState;
use rustytransfer::crypto::kem::KemState;
use rustytransfer::crypto::mac::MacState;
use rustytransfer::crypto::pake::PakeState;
use rustytransfer::protocol::{PAKE_RECEIVER_ID, PAKE_SENDER_ID};

fn ss_to_aes_key(ss: &ml_kem::SharedKey<ml_kem::MlKem768>) -> [u8; 32] {
    let mut key = [0_u8; 32];
    key.copy_from_slice(ss.as_slice());
    key
}

#[test]
fn pake_mac_kem_dem_roundtrip() -> Result<()> {
    let pw = b"Password";

    // ---- PAKE roundtrip
    let mut sender = PakeState::start_sender(pw, PAKE_RECEIVER_ID, PAKE_SENDER_ID);
    let mut receiver = PakeState::start_receiver(pw, PAKE_RECEIVER_ID, PAKE_SENDER_ID);

    let sender_key = sender
        .finish(&receiver.take_outbound_msg())
        .map_err(|error| anyhow!("sender PAKE finish failed: {error:?}"))?;
    let receiver_key = receiver
        .finish(&sender.take_outbound_msg())
        .map_err(|error| anyhow!("receiver PAKE finish failed: {error:?}"))?;
    assert_eq!(sender_key, receiver_key);

    // ---- derive MAC keys (same on both sides)
    let sender_mac = MacState::new(&sender_key);
    let receiver_mac = MacState::new(&receiver_key);

    // ---- receiver: KEM keypair + MAC(pk)
    let mut kem_receiver = KemState::new();
    kem_receiver.generate_keypair();

    let pk = kem_receiver.public_key_bytes()?;
    let tag_pk = receiver_mac.tag(pk.as_slice());
    assert!(sender_mac.verify(pk.as_slice(), &tag_pk));

    // ---- sender: set pk, encapsulate + MAC(ct)
    let mut kem_sender = KemState::new();
    kem_sender.set_public_key_bytes(&pk)?;
    let enc = kem_sender.encapsulate()?;

    let ct_bytes = enc.ciphertext.as_slice(); // or .as_ref()
    let tag_ct = sender_mac.tag(ct_bytes);
    assert!(receiver_mac.verify(ct_bytes, &tag_ct));

    // ---- receiver: decapsulate, compare shared secrets
    let ss_recv = kem_receiver.decapsulate(&enc.ciphertext)?;
    assert_eq!(enc.shared_secret, ss_recv);

    // ---- DEM using KEM shared secret as AES-256-GCM key
    let dem_key = ss_to_aes_key(&ss_recv);

    let mut dem_sender = DemState::new();
    let dem_receiver = DemState::new();

    let aad = b"file-transfer-v1"; // optional context
    let pt = b"hello from DEM";

    let seal = dem_sender
        .seal(&dem_key, pt, aad)
        .map_err(|error| anyhow!("DEM seal failed: {error:?}"))?;
    let opened = dem_receiver
        .open(&dem_key, seal, aad)
        .map_err(|error| anyhow!("DEM open failed: {error:?}"))?;

    assert_eq!(opened, pt);

    // ---- Tamper tests (optional but good)
    // 1) tamper KEM ct -> MAC fails (already in your snippet)
    let mut ct_tampered = ct_bytes.to_vec();
    *ct_tampered
        .first_mut()
        .ok_or_else(|| anyhow!("ML-KEM ciphertext was empty"))? ^= 1;
    assert!(!receiver_mac.verify(&ct_tampered, &tag_ct));
    Ok(())
}
