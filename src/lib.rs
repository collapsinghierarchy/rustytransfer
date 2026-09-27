//! Compatibility facade for the Rustytransfer workspace.

pub use rustytransfer_crypto as crypto;
pub use rustytransfer_protocol as protocol;

#[cfg(not(target_arch = "wasm32"))]
pub use rustytransfer_native::{constants, rendezvous, transport};
#[cfg(not(target_arch = "wasm32"))]
pub use rustytransfer_transfer::{self as transfer, error};

#[cfg(not(target_arch = "wasm32"))]
#[doc(hidden)]
pub mod cli;
#[cfg(not(target_arch = "wasm32"))]
#[path = "cli/ui/mod.rs"]
pub mod cli_ui;
