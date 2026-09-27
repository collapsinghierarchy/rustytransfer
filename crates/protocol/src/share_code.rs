use std::fmt;

/// Invalid human-readable rendezvous credentials.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShareCodeError {
    InvalidLength,
    InvalidCode,
    InvalidPassword,
}

impl fmt::Display for ShareCodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidLength => "share code must be 9 chars total (NNNN-ABCDE)",
            Self::InvalidCode => "rendezvous code must be exactly 4 digits",
            Self::InvalidPassword => "password must be exactly 5 uppercase letters",
        })
    }
}

impl std::error::Error for ShareCodeError {}

/// Formats `NNNN-ABCDE`.
pub fn format_share_code(code4: &str, pw5: &str) -> Result<String, ShareCodeError> {
    let code4 = normalize_code4(code4)?;
    let pw5 = normalize_pw5(pw5)?;
    Ok(format!("{code4}-{pw5}"))
}

/// Parses `NNNN-ABCDE` (dash optional, spaces ignored).
pub fn parse_share_code(s: &str) -> Result<(String, String), ShareCodeError> {
    let cleaned = s.trim().replace([' ', '-'], "");
    if cleaned.len() != 9 {
        return Err(ShareCodeError::InvalidLength);
    }
    let (Some(digits), Some(letters)) = (cleaned.get(..4), cleaned.get(4..)) else {
        return Err(ShareCodeError::InvalidLength);
    };
    Ok((normalize_code4(digits)?, normalize_pw5(letters)?))
}

/// Validates the four-digit rendezvous code.
pub fn normalize_code4(code4: &str) -> Result<String, ShareCodeError> {
    let code = code4.trim();
    if code.len() != 4 || !code.chars().all(|character| character.is_ascii_digit()) {
        return Err(ShareCodeError::InvalidCode);
    }
    Ok(code.to_string())
}

fn normalize_pw5(pw5: &str) -> Result<String, ShareCodeError> {
    let password = pw5.trim();
    if password.len() != 5
        || !password
            .chars()
            .all(|character| character.is_ascii_uppercase())
    {
        return Err(ShareCodeError::InvalidPassword);
    }
    Ok(password.to_string())
}
