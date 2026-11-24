use std::{
    ffi::OsString,
    io::{self, Read},
    os::unix::ffi::OsStringExt,
};

use tar::Header;

const BLOCK: usize = 512;
const MAX_METADATA_BYTES: u64 = 64 * 1024;
const MAX_ARCHIVE_BYTES: u64 = (1_u64 << 36) - 32;
const TAR_RECORD: usize = 10240;

pub(super) struct Member {
    pub path: OsString,
    pub bytes: u64,
}

#[derive(Default)]
struct Metadata {
    path: Option<Vec<u8>>,
    bytes: Option<u64>,
}

impl Member {
    pub fn next(stream: &mut impl Read) -> io::Result<Option<Self>> {
        let mut block = [0; BLOCK];
        let mut pax = None;
        let mut long_name = None;
        loop {
            stream.read_exact(&mut block)?;
            if block.iter().all(|byte| *byte == 0) {
                if pax.is_some() || long_name.is_some() {
                    return Err(invalid());
                }
                finish(stream)?;
                return Ok(None);
            }
            let header = Header::from_byte_slice(&block);
            let expected = header.cksum()?;
            let mut checked = header.clone();
            checked.set_cksum();
            if checked.cksum()? != expected {
                return Err(invalid());
            }
            let size = header.entry_size()?;
            let kind = header.entry_type();
            if kind.is_pax_local_extensions() && pax.is_none() {
                pax = Some(metadata(&read_metadata(stream, size)?)?);
                continue;
            }
            if kind.is_gnu_longname() && long_name.is_none() {
                let mut raw = read_metadata(stream, size)?;
                if raw.last() == Some(&0) {
                    raw.pop();
                }
                long_name = Some(raw);
                continue;
            }
            if !kind.is_file() {
                return Err(invalid());
            }
            let pax = pax.unwrap_or_default();
            let bytes = pax.bytes.unwrap_or(size);
            let name = match (pax.path, long_name) {
                (Some(_), Some(_)) => return Err(invalid()),
                (Some(name), None) | (None, Some(name)) => name,
                (None, None) => header.path_bytes().into_owned(),
            };
            if name.is_empty() || name.contains(&0) || bytes > MAX_ARCHIVE_BYTES {
                return Err(invalid());
            }
            return Ok(Some(Self {
                path: OsString::from_vec(name),
                bytes,
            }));
        }
    }
}

fn metadata(mut records: &[u8]) -> io::Result<Metadata> {
    let mut result = Metadata::default();
    while !records.is_empty() {
        let space = records
            .iter()
            .position(|byte| *byte == b' ')
            .ok_or_else(invalid)?;
        let length = std::str::from_utf8(&records[..space])
            .ok()
            .and_then(|digits| digits.parse::<usize>().ok())
            .filter(|&length| (space + 3..=records.len()).contains(&length))
            .ok_or_else(invalid)?;
        let line = records.get(space + 1..length).ok_or_else(invalid)?;
        let record = line.strip_suffix(b"\n").ok_or_else(invalid)?;
        let equal = record
            .iter()
            .position(|byte| *byte == b'=')
            .ok_or_else(invalid)?;
        let value = &record[equal + 1..];
        match &record[..equal] {
            b"path" if result.path.is_none() => result.path = Some(value.to_vec()),
            b"size" if result.bytes.is_none() => {
                result.bytes = Some(
                    std::str::from_utf8(value)
                        .ok()
                        .and_then(|text| text.parse().ok())
                        .ok_or_else(invalid)?,
                );
            }
            b"path" | b"size" | b"linkpath" => return Err(invalid()),
            key if key.starts_with(b"GNU.sparse") => return Err(invalid()),
            _ => {}
        }
        records = &records[length..];
    }
    Ok(result)
}

fn read_metadata(stream: &mut impl Read, size: u64) -> io::Result<Vec<u8>> {
    if size > MAX_METADATA_BYTES {
        return Err(invalid());
    }
    let length = usize::try_from(size).map_err(|_| invalid())?;
    let mut metadata = vec![0; length];
    stream.read_exact(&mut metadata)?;
    skip_padding(stream, size)?;
    Ok(metadata)
}

pub(super) fn skip_padding(stream: &mut impl Read, size: u64) -> io::Result<()> {
    let remainder = usize::try_from(size % BLOCK as u64).map_err(|_| invalid())?;
    let padded = (BLOCK - remainder) % BLOCK;
    stream.read_exact(&mut [0; BLOCK][..padded])
}

fn finish(stream: &mut impl Read) -> io::Result<()> {
    let mut block = [0; BLOCK];
    stream.read_exact(&mut block)?;
    if block.iter().any(|byte| *byte != 0) {
        return Err(invalid());
    }
    let mut trailing = [0; 4096];
    let mut padding = 0;
    loop {
        let read = stream.read(&mut trailing)?;
        padding += read;
        if padding > TAR_RECORD || trailing[..read].iter().any(|byte| *byte != 0) {
            return Err(invalid());
        }
        if read == 0 {
            return Ok(());
        }
    }
}

pub(super) fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "Invalid backup archive")
}
