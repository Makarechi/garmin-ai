use std::{
    error::Error,
    fmt,
    io::{self, Read},
};

use crc::{CRC_16_ARC, Crc};

pub const MAX_FIT_BYTES: u64 = 100 * 1024 * 1024;
const FIT_CRC: Crc<u16> = Crc::<u16>::new(&CRC_16_ARC);
const READ_BUFFER_BYTES: usize = 32 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FitFrame {
    pub index: u32,
    pub offset: u64,
    pub header_bytes: u8,
    pub data_bytes: u32,
    pub protocol: u8,
    pub profile: u16,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FitInventory {
    pub frames: u32,
    pub bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FitError {
    Cancelled,
    LimitExceeded,
    InvalidHeader,
    HeaderChecksum,
    Truncated,
    FileChecksum,
    ReadFailed,
}

impl fmt::Display for FitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Cancelled => "FIT verification was cancelled",
            Self::LimitExceeded => "FIT input exceeds the byte limit",
            Self::InvalidHeader => "FIT input has an invalid header",
            Self::HeaderChecksum => "FIT header checksum mismatched",
            Self::Truncated => "FIT input ended unexpectedly",
            Self::FileChecksum => "FIT file checksum mismatched",
            Self::ReadFailed => "FIT input could not be read",
        })
    }
}

impl Error for FitError {}

impl From<io::Error> for FitError {
    fn from(error: io::Error) -> Self {
        if error.kind() == io::ErrorKind::UnexpectedEof {
            Self::Truncated
        } else {
            Self::ReadFailed
        }
    }
}

/// Verify a member containing zero or more complete FIT frames, using one bounded buffer.
/// The callback provides candidate offsets. A later failure invalidates the entire member.
pub fn verify_frames(
    mut source: impl Read,
    max_bytes: u64,
    mut cancelled: impl FnMut() -> bool,
    mut on_frame: impl FnMut(FitFrame),
) -> Result<FitInventory, FitError> {
    let mut inventory = FitInventory::default();
    let limit = max_bytes.min(MAX_FIT_BYTES);
    let mut buffer = [0; READ_BUFFER_BYTES];
    loop {
        check_cancelled(&mut cancelled)?;
        let mut header = [0; 14];
        if read_start(&mut source, &mut header[..1])? == 0 {
            return Ok(inventory);
        }
        let length = usize::from(header[0]);
        if length != 12 && length != 14 {
            return Err(FitError::InvalidHeader);
        }
        source.read_exact(&mut header[1..length])?;
        if &header[8..12] != b".FIT" {
            return Err(FitError::InvalidHeader);
        }
        let data_bytes = u32::from_le_bytes([header[4], header[5], header[6], header[7]]);
        let next = inventory.bytes + length as u64 + u64::from(data_bytes) + 2;
        if next > limit {
            return Err(FitError::LimitExceeded);
        }
        let header_checksum = u16::from_le_bytes([header[12], header[13]]);
        if header_checksum != 0 && FIT_CRC.checksum(&header[..12]) != header_checksum {
            return Err(FitError::HeaderChecksum);
        }
        let mut checksum = FIT_CRC.digest();
        checksum.update(&header[..length]);
        let mut remaining = data_bytes as usize;
        while remaining != 0 {
            check_cancelled(&mut cancelled)?;
            let size = remaining.min(buffer.len());
            let bytes = &mut buffer[..size];
            source.read_exact(bytes)?;
            checksum.update(bytes);
            remaining -= size;
        }
        check_cancelled(&mut cancelled)?;
        let mut footer = [0; 2];
        source.read_exact(&mut footer)?;
        if checksum.finalize() != u16::from_le_bytes(footer) {
            return Err(FitError::FileChecksum);
        }
        on_frame(FitFrame {
            index: inventory.frames,
            offset: inventory.bytes,
            header_bytes: header[0],
            data_bytes,
            protocol: header[1],
            profile: u16::from_le_bytes([header[2], header[3]]),
        });
        inventory.frames += 1;
        inventory.bytes = next;
    }
}

fn check_cancelled(cancelled: &mut impl FnMut() -> bool) -> Result<(), FitError> {
    if cancelled() {
        Err(FitError::Cancelled)
    } else {
        Ok(())
    }
}

fn read_start(source: &mut impl Read, byte: &mut [u8]) -> Result<usize, FitError> {
    loop {
        match source.read(byte) {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            result => return Ok(result?),
        }
    }
}
