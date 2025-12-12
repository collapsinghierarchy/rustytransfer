#[derive(Debug)]
pub enum State {
    Init {role: Role},
    Pake {role: Role, pw: Password},
    KemAuth {role: Role, kem_pk: KemPublicKey, mac_key: MacKey},
    Smt {role: Role, dem_key: DemKey, file: FileData},

    Success(String),
    Failed(String)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Sender,
    Receiver
}

#[derive(Debug)]
pub struct PakeKey;         // you can later make this a newtype around [u8; N]
#[derive(Debug)]
pub struct Password;        // later: actual password representation
#[derive(Debug)]
pub struct KemPublicKey;
#[derive(Debug)]
pub struct MacKey;
#[derive(Debug)]
pub struct DemKey;
#[derive(Debug)]
pub struct FileData;

// For sender/receiver messages:
#[derive(Debug)]
pub struct RendezvousInfo;
#[derive(Debug)]
pub struct MacTag;
#[derive(Debug)]
pub struct KemCiphertext;
#[derive(Debug)]
pub struct DemData;

#[derive(Debug)]
pub enum StepError {
    InvalidTransition(String),
    // later: CryptoError, MacError, etc.
}