//#[cfg(not(target_arch = "wasm32"))]
//mod transport;
pub mod crypto;
pub mod protocol;
pub mod transport;
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn hello_in_rust() -> String {
    "Hello from Rust!".into()
}

