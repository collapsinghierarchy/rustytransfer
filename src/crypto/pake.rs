use wasm_bindgen::prelude::*;
use spake2::{Ed25519Group, Identity, Password, Spake2};

#[derive(Debug)]
#[wasm_bindgen]
pub struct PakeState {
    inner: Option<Spake2<Ed25519Group>>,
    outbound: Vec<u8>,
}

#[wasm_bindgen]
impl PakeState {
    #[wasm_bindgen(js_name = startSender)]
    pub fn start_sender(pw: &[u8]) -> PakeState {
        let (s1, outbound_msg) = Spake2::<Ed25519Group>::start_b(
            &Password::new(pw),
            &Identity::new(b"smt_receiver"),
            &Identity::new(b"smt_sender"),
        );
        PakeState { inner: Some(s1), outbound: outbound_msg }
    }

    #[wasm_bindgen(js_name = startReceiver)]
    pub fn start_receiver(pw: &[u8]) -> PakeState {
        let (s1, outbound_msg) = Spake2::<Ed25519Group>::start_a(
            &Password::new(pw),
            &Identity::new(b"smt_receiver"),
            &Identity::new(b"smt_sender"),
        );
        PakeState { inner: Some(s1), outbound: outbound_msg }
    }

    #[wasm_bindgen(js_name = outboundMsg)]
    pub fn outbound_msg(&self) -> Vec<u8> {
        self.outbound.clone()
    }

    #[wasm_bindgen(js_name = takeOutboundMsg)]
    pub fn take_outbound_msg(&mut self) -> Vec<u8> {
        // This returns Vec<u8>, which wasm-bindgen turns into Uint8Array for JS
        std::mem::take(&mut self.outbound)
    }

    #[wasm_bindgen]
    pub fn finish(&mut self, inbound_msg: &[u8]) -> Result<Vec<u8>, JsValue> {
        let s = self.inner.take()
            .ok_or_else(|| JsValue::from_str("PakeState already finished"))?;
        
        // s.finish() returns a Result<Vec<u8>, Error>
        let key = s.finish(inbound_msg)
            .map_err(|e| JsValue::from_str(&format!("{e:?}")))?;
            
        // No need for .as_ref(). Just return the Vec<u8>.
        Ok(key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_password_same_key() {
        let pw = b"correct horse battery staple";

        let mut sender_state = PakeState::start_sender(pw);
        let mut receiver_state = PakeState::start_receiver(pw);

        // 1. take_outbound_msg() returns Vec<u8>
        // 2. finish() takes &[u8], so we pass it by reference (&)
        // 3. We .expect() because finish returns a Result
        let sender_key = sender_state.finish(&receiver_state.take_outbound_msg())
            .expect("Sender failed to finish");
        let receiver_key = receiver_state.finish(&sender_state.take_outbound_msg())
            .expect("Receiver failed to finish");

        println!("sender key:   {:02x?}", sender_key);
        println!("receiver key: {:02x?}", receiver_key);

        assert_eq!(sender_key, receiver_key, "Keys should match for same password");
    }
}



