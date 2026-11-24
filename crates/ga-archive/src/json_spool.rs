use std::{
    fmt,
    fs::File,
    io::{self, BufWriter, Cursor, Read, Write},
    sync::Arc,
};

const MEMORY_BYTES: usize = 128 * 1024;
const BUFFER_BYTES: usize = 64 * 1024;

/// Retain small responses in memory and spill larger ones into a private, unnamed file.
pub struct JsonSpool(SpoolStorage);

enum SpoolStorage {
    Memory(Vec<u8>),
    File(BufWriter<File>),
}

impl Default for JsonSpool {
    fn default() -> Self {
        Self(SpoolStorage::Memory(Vec::new()))
    }
}

impl Write for JsonSpool {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if let SpoolStorage::Memory(buffer) = &self.0
            && buffer.len().saturating_add(bytes.len()) > MEMORY_BYTES
        {
            let mut file = BufWriter::with_capacity(BUFFER_BYTES, tempfile::tempfile()?);
            file.write_all(buffer)?;
            self.0 = SpoolStorage::File(file);
        }
        match &mut self.0 {
            SpoolStorage::Memory(buffer) => {
                buffer.extend_from_slice(bytes);
                Ok(bytes.len())
            }
            SpoolStorage::File(file) => file.write(bytes),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match &mut self.0 {
            SpoolStorage::Memory(_) => Ok(()),
            SpoolStorage::File(file) => file.flush(),
        }
    }
}

impl JsonSpool {
    /// Finish a response after its caller has written the complete JSON document.
    pub fn finish(self) -> io::Result<JsonDocument> {
        match self.0 {
            SpoolStorage::Memory(bytes) => Ok(JsonDocument(DocumentStorage::Memory(bytes.into()))),
            SpoolStorage::File(file) => {
                let file = file
                    .into_inner()
                    .map_err(std::io::IntoInnerError::into_error)?;
                file.sync_data()?;
                let bytes = file.metadata()?.len();
                Ok(JsonDocument(DocumentStorage::File {
                    file: Arc::new(file),
                    bytes,
                }))
            }
        }
    }
}

#[derive(Clone)]
pub struct JsonDocument(DocumentStorage);

#[derive(Clone)]
enum DocumentStorage {
    Memory(Arc<[u8]>),
    File { file: Arc<File>, bytes: u64 },
}

impl fmt::Debug for JsonDocument {
    fn fmt(&self, out: &mut fmt::Formatter<'_>) -> fmt::Result {
        out.debug_struct("JsonDocument")
            .field("bytes", &self.byte_len())
            .finish()
    }
}

impl JsonDocument {
    #[must_use]
    pub fn byte_len(&self) -> u64 {
        match &self.0 {
            DocumentStorage::Memory(bytes) => bytes.len() as u64,
            DocumentStorage::File { bytes, .. } => *bytes,
        }
    }

    /// Create an independent reader whose position is unaffected by other readers.
    #[must_use]
    pub fn reader(&self) -> JsonReader {
        let storage = match &self.0 {
            DocumentStorage::Memory(bytes) => ReaderStorage::Memory(Cursor::new(bytes.clone())),
            DocumentStorage::File { file, .. } => ReaderStorage::File {
                file: file.clone(),
                offset: 0,
            },
        };
        JsonReader(storage)
    }
}

pub struct JsonReader(ReaderStorage);

enum ReaderStorage {
    Memory(Cursor<Arc<[u8]>>),
    File { file: Arc<File>, offset: u64 },
}

impl Read for JsonReader {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        match &mut self.0 {
            ReaderStorage::Memory(reader) => reader.read(bytes),
            ReaderStorage::File { file, offset } => {
                #[cfg(unix)]
                let count = std::os::unix::fs::FileExt::read_at(&**file, bytes, *offset)?;
                #[cfg(windows)]
                let count = std::os::windows::fs::FileExt::seek_read(&**file, bytes, *offset)?;
                *offset += count as u64;
                Ok(count)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readers_retain_exact_bytes_after_spilling_and_cloning() -> io::Result<()> {
        let bytes = vec![b'x'; MEMORY_BYTES * 3 + 17];
        let mut spool = JsonSpool::default();
        spool.write_all(&bytes)?;
        let document = spool.finish()?;
        assert_eq!(document.byte_len(), bytes.len() as u64);
        assert!(matches!(document.0, DocumentStorage::File { .. }));
        let cloned = document.clone();
        let mut first = document.reader();
        let mut second = cloned.reader();
        drop(document);
        drop(cloned);
        let mut prefix = [0; 31];
        first.read_exact(&mut prefix)?;
        assert_eq!(prefix, [b'x'; 31]);
        let mut entire = Vec::new();
        second.read_to_end(&mut entire)?;
        assert_eq!(entire, bytes);
        let mut tail = Vec::new();
        first.read_to_end(&mut tail)?;
        assert_eq!(tail, bytes[31..]);
        Ok(())
    }
}
