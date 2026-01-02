use rustytransfer::crypto::dem::DemState;
use rustytransfer::crypto::kem::KemState;
use rustytransfer::crypto::mac::MacState;
use rustytransfer::crypto::pake::PakeState;

fn ss_to_aes_key(ss: &ml_kem::SharedKey<ml_kem::MlKem768>) -> [u8; 32] {
    // Use .as_slice() if available; otherwise .as_ref()
    ss.as_slice().try_into().expect("ML-KEM shared secret is 32 bytes")
    // If you don't have as_slice():
    // ss.as_ref().try_into().expect("ML-KEM shared secret is 32 bytes")
}

#[test]
fn pake_mac_kem_dem_roundtrip() {
    let pw = b"Password";

    // ---- PAKE roundtrip
    let mut sender = PakeState::start_sender(pw);
    let mut receiver = PakeState::start_receiver(pw);

    let sender_key = sender.finish(&receiver.take_outbound_msg()).expect("sender finish");
    let receiver_key = receiver.finish(&sender.take_outbound_msg()).expect("receiver finish");
    assert_eq!(sender_key, receiver_key);

    // ---- derive MAC keys (same on both sides)
    let sender_mac = MacState::new(&sender_key);
    let receiver_mac = MacState::new(&receiver_key);

    // ---- receiver: KEM keypair + MAC(pk)
    let mut kem_receiver = KemState::new();
    kem_receiver.generate_keypair();

    let pk = kem_receiver.public_key_bytes();
    let tag_pk = receiver_mac.tag(pk.as_slice());
    assert!(sender_mac.verify(pk.as_slice(), &tag_pk));

    // ---- sender: set pk, encapsulate + MAC(ct)
    let mut kem_sender = KemState::new();
    kem_sender.set_public_key_bytes(&pk);
    let enc = kem_sender.encapsulate();

    let ct_bytes = enc.ciphertext.as_slice(); // or .as_ref()
    let tag_ct = sender_mac.tag(ct_bytes);
    assert!(receiver_mac.verify(ct_bytes, &tag_ct));

    // ---- receiver: decapsulate, compare shared secrets
    let ss_recv = kem_receiver.decapsulate(&enc.ciphertext);
    assert_eq!(enc.shared_secret, ss_recv);

    // ---- DEM using KEM shared secret as AES-256-GCM key
    let dem_key = ss_to_aes_key(&ss_recv);

    let mut dem_sender = DemState::new();
    let dem_receiver = DemState::new();

    let aad = b"file-transfer-v1"; // optional context
    let pt = b"hello from DEM";

    let seal = dem_sender.seal(&dem_key, pt, aad).expect("dem seal");
    let opened = dem_receiver.open(&dem_key, seal).expect("dem open");

    assert_eq!(opened, pt);

    // ---- Tamper tests (optional but good)
    // 1) tamper KEM ct -> MAC fails (already in your snippet)
    let mut ct_tampered = ct_bytes.to_vec();
    ct_tampered[0] ^= 1;
    assert!(!receiver_mac.verify(&ct_tampered, &tag_ct));
}
