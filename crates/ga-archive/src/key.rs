use std::{fmt, str::FromStr};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ArchiveKind {
    Binary,
    Json,
    Fit,
    Zip,
}

impl ArchiveKind {
    fn extension(self) -> &'static str {
        match self {
            Self::Binary => "bin",
            Self::Json => "json",
            Self::Fit => "fit",
            Self::Zip => "zip",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArchiveKey {
    pub(crate) digest: [u8; 32],
    pub(crate) kind: ArchiveKind,
}

impl ArchiveKey {
    #[must_use]
    pub fn digest_hex(&self) -> String {
        hex::encode(self.digest)
    }

    pub(crate) fn path_parts(&self) -> (String, String) {
        let digest = self.digest_hex();
        let prefix = digest[..2].to_owned();
        (prefix, format!("{digest}.{}", self.kind.extension()))
    }
}

impl fmt::Display for ArchiveKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let (prefix, name) = self.path_parts();
        write!(formatter, "{prefix}/{name}")
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidArchiveKey;

impl fmt::Display for InvalidArchiveKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Invalid archive key")
    }
}

impl std::error::Error for InvalidArchiveKey {}

impl FromStr for ArchiveKey {
    type Err = InvalidArchiveKey;

    fn from_str(key: &str) -> Result<Self, Self::Err> {
        let (prefix, name) = key.split_once('/').ok_or(InvalidArchiveKey)?;
        let (digest, extension) = name.split_once('.').ok_or(InvalidArchiveKey)?;
        let kind = match extension {
            "bin" => ArchiveKind::Binary,
            "json" => ArchiveKind::Json,
            "fit" => ArchiveKind::Fit,
            "zip" => ArchiveKind::Zip,
            _ => return Err(InvalidArchiveKey),
        };
        if digest.len() != 64
            || !digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            || prefix != &digest[..2]
        {
            return Err(InvalidArchiveKey);
        }
        let mut bytes = [0; 32];
        hex::decode_to_slice(digest, &mut bytes).map_err(|_| InvalidArchiveKey)?;
        Ok(Self {
            digest: bytes,
            kind,
        })
    }
}
