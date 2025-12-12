use spake2::{Ed25519Group, Identity, Password, Spake2};


/// Local PAKE state (one side of the protocol)
#[derive(Debug)]
pub struct PakeState(Spake2<Ed25519Group>);

impl PakeState {

    pub fn start_receiver(pw: &[u8]) -> (Self, Vec<u8>) {
        let pw = Password::new(pw);
        let (s1, outbound_msg) = Spake2::<Ed25519Group>::start_a(
            &pw,
            &Identity::new(b"smt_receiver"),
            &Identity::new(b"smt_sender"));

            (PakeState(s1), outbound_msg)
    }

    pub fn start_sender(pw: &[u8]) -> (Self, Vec<u8>) {
        let pw = Password::new(pw);
        let (s1, outbound_msg) = Spake2::<Ed25519Group>::start_b(
            &pw,
            &Identity::new(b"smt_receiver"),
            &Identity::new(b"smt_sender"));

            (PakeState(s1), outbound_msg)
    }

    pub fn finish(self,inbound_msg: &[u8]) -> Result<Vec<u8>, spake2::Error> {
        let key = self.0.finish(inbound_msg)?;
        Ok(key.to_vec())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_password_same_key() {
        let pw = b"correct horse battery staple";

        // sender starts with role A
        let (sender_state, sender_msg) = PakeState::start_sender(pw);

        // receiver starts with role B
        let (receiver_state, receiver_msg) = PakeState::start_receiver(pw);

        // simulate “send/receive” by just passing the messages
        let sender_key = sender_state.finish(&receiver_msg);
        let receiver_key = receiver_state.finish(&sender_msg);

        println!("sender key:   {:02x?}", sender_key);
        println!("receiver key: {:02x?}", receiver_key);

        assert_eq!(sender_key, receiver_key);
    }

    #[test]
    fn different_passwords_do_not_agree() {
        let pw1 = b"password one";
        let pw2 = b"password two";

        let (s1, m1) = PakeState::start_sender(pw1);
        let (s2, m2) = PakeState::start_receiver(pw2);

        let k1 = s1.finish(&m2);
        let k2 = s2.finish(&m1);

        println!("k1: {:?}", k1);
        println!("k2: {:?}", k2);

        // Either at least one side errors, or both succeed but keys differ.
        if let (Ok(k1), Ok(k2)) = (k1, k2) {
            assert_ne!(k1, k2, "different passwords must not yield same key");
        }
    }
}



