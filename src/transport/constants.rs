pub static BASE_API_URL: &str = "https://nt.whitenoise.systems";
pub static API_URL_CODE_REQ: &str = "https://nt.whitenoise.systems/rendezvous/code";
pub static API_URL_CODE_REDEEM: &str = "https://nt.whitenoise.systems/rendezvous/redeem";


const FRAG_MAGIC_HDR: &[u8; 4] = b"RTFH";
const FRAG_MAGIC_DAT: &[u8; 4] = b"RTFD";

// Conservative. If you still see the error, try 8 * 1024.
const CHUNK_PAYLOAD: usize = 16 * 1024;