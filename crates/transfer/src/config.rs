use super::*;

/// Transfer settings shared by the CLI and local transfer tests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransferConfig {
    pub chunk_size: usize,
}

impl Default for TransferConfig {
    fn default() -> Self {
        Self {
            chunk_size: 8 * 1024,
        }
    }
}

impl TransferConfig {
    pub(super) fn validate(self, file_len: u64) -> std::result::Result<u32, TransferError> {
        let context = TransferContext::new();
        if self.chunk_size == 0 || self.chunk_size > MAX_CHUNK_SIZE {
            return Err(context.error(TransferErrorKind::Config(
                "chunk_size must be between 1 and 1048576 bytes",
            )));
        }
        let chunk_size = u32::try_from(self.chunk_size).map_err(|_source| {
            context.error(TransferErrorKind::Config(
                "chunk_size exceeds the protocol limit",
            ))
        })?;
        let max_file_len = u64::from(chunk_size)
            .checked_mul(u64::from(u32::MAX))
            .ok_or_else(|| context.error(TransferErrorKind::Config("chunk limit overflow")))?;
        if file_len > max_file_len {
            return Err(context.error(TransferErrorKind::Config(
                "file requires more chunks than the nonce counter allows",
            )));
        }
        Ok(chunk_size)
    }
}
