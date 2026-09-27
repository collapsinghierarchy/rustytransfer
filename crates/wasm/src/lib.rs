use rustytransfer_crypto::pake::PakeState as CryptoPakeState;
use rustytransfer_protocol::{PAKE_RECEIVER_ID, PAKE_SENDER_ID};
use wasm_bindgen::prelude::*;

/// JavaScript adapter for the platform-independent PAKE implementation.
#[wasm_bindgen]
pub struct PakeState {
    inner: CryptoPakeState,
}

#[wasm_bindgen]
impl PakeState {
    #[wasm_bindgen(js_name = startSender)]
    pub fn start_sender(password: &[u8]) -> PakeState {
        Self {
            inner: CryptoPakeState::start_sender(password, PAKE_RECEIVER_ID, PAKE_SENDER_ID),
        }
    }

    #[wasm_bindgen(js_name = startReceiver)]
    pub fn start_receiver(password: &[u8]) -> PakeState {
        Self {
            inner: CryptoPakeState::start_receiver(password, PAKE_RECEIVER_ID, PAKE_SENDER_ID),
        }
    }

    #[wasm_bindgen(js_name = outboundMsg)]
    pub fn outbound_msg(&self) -> Vec<u8> {
        self.inner.outbound_msg()
    }

    #[wasm_bindgen(js_name = takeOutboundMsg)]
    pub fn take_outbound_msg(&mut self) -> Vec<u8> {
        self.inner.take_outbound_msg()
    }

    /// Finish the exchange without exposing the peer message in the JS error.
    ///
    /// # Errors
    ///
    /// Returns a redacted error if PAKE fails.
    pub fn finish(&mut self, inbound_msg: &[u8]) -> Result<Vec<u8>, JsValue> {
        self.inner
            .finish(inbound_msg)
            .map_err(|_source| JsValue::from_str("PAKE exchange failed"))
    }
}
