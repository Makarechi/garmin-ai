use std::{
    io::{self, Read, Seek, SeekFrom},
    num::NonZeroUsize,
    path::Component,
    time::{Duration, Instant},
};

use zip::{CompressionMethod, ZipReadOptions, read::Config, unstable::read::ZipIterable};

use crate::{FitError, FitFrame, MAX_FIT_BYTES, verify_frames};

#[derive(Clone, Copy, Debug)]
pub struct ExportLimits {
    pub max_bytes: u64,
    pub max_zip_entries: NonZeroUsize,
    pub max_duration: Duration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityExport {
    Fit,
    Zip,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ExportFrame {
    pub fit_member_index: usize,
    pub frame: FitFrame,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ExportInventory {
    pub format: ActivityExport,
    pub original_bytes: u64,
    pub fit_members: usize,
    pub fit_bytes: u64,
    pub frames: u32,
}

/// Verify a seekable activity export without extracting paths or keeping members in memory.
/// Callback offsets belong to candidates until the complete export has passed validation.
pub fn verify_activity_export(
    source: impl Read + Seek,
    limits: ExportLimits,
    cancelled: impl Fn() -> bool,
    on_frame: impl FnMut(ExportFrame),
) -> Result<ExportInventory, FitError> {
    let budget = Budget {
        started: Instant::now(),
        max_duration: limits.max_duration,
        cancelled,
    };
    let source = Budgeted {
        source,
        budget: &budget,
    };
    let result = verify(source, limits, &budget, on_frame);
    budget.check()?;
    result
}

fn verify(
    mut source: impl Read + Seek,
    limits: ExportLimits,
    budget: &Budget<impl Fn() -> bool>,
    mut on_frame: impl FnMut(ExportFrame),
) -> Result<ExportInventory, FitError> {
    let original_bytes = source.seek(SeekFrom::End(0))?;
    let max_bytes = limits.max_bytes.min(MAX_FIT_BYTES);
    if original_bytes > max_bytes {
        return Err(FitError::LimitExceeded);
    }
    source.seek(SeekFrom::Start(0))?;
    let mut prefix = [0; 4];
    if original_bytes >= prefix.len() as u64 {
        source.read_exact(&mut prefix)?;
        source.seek(SeekFrom::Start(0))?;
    }
    match ZipIterable::try_new(&mut source, Config::default()) {
        Ok(zip) => verify_zip(
            zip.enforce_header_consistency(true),
            original_bytes,
            max_bytes,
            limits.max_zip_entries.get(),
            budget,
            on_frame,
        ),
        Err(_) if matches!(&prefix, b"PK\x03\x04" | b"PK\x05\x06" | b"PK\x07\x08") => {
            Err(FitError::InvalidZip)
        }
        Err(_) => {
            source.seek(SeekFrom::Start(0))?;
            let fit = verify_frames(
                source,
                max_bytes,
                || budget.check().is_err(),
                |frame| {
                    on_frame(ExportFrame {
                        fit_member_index: 0,
                        frame,
                    });
                },
            )?;
            Ok(ExportInventory {
                format: ActivityExport::Fit,
                original_bytes,
                fit_members: usize::from(fit.frames != 0),
                fit_bytes: fit.bytes,
                frames: fit.frames,
            })
        }
    }
}

fn verify_zip<R: Read + Seek>(
    mut zip: ZipIterable<R>,
    original_bytes: u64,
    max_bytes: u64,
    max_entries: usize,
    budget: &Budget<impl Fn() -> bool>,
    mut on_frame: impl FnMut(ExportFrame),
) -> Result<ExportInventory, FitError> {
    if zip.len() > max_entries {
        return Err(FitError::TooManyZipMembers);
    }
    let mut total_declared = 0_u64;
    for entry in zip.files().map_err(|_| FitError::InvalidZip)? {
        budget.check()?;
        let entry = entry.map_err(|_| FitError::InvalidZip)?;
        let name = entry.name().map_err(|_| FitError::InvalidZip)?;
        if entry.is_symlink() || !safe_name(&name) {
            return Err(FitError::UnsafeZipMember);
        }
        if !entry.is_dir() && is_fit_name(&name) {
            if !matches!(
                entry.compression(),
                CompressionMethod::Stored | CompressionMethod::Deflated
            ) {
                return Err(FitError::UnsupportedZipCompression);
            }
            total_declared = total_declared
                .checked_add(entry.size())
                .filter(|total| *total <= max_bytes)
                .ok_or(FitError::LimitExceeded)?;
        }
    }

    let mut inventory = ExportInventory {
        format: ActivityExport::Zip,
        original_bytes,
        fit_members: 0,
        fit_bytes: 0,
        frames: 0,
    };
    let entries = zip.files().map_err(|_| FitError::InvalidZip)?;
    while let Some(entry) = entries.next() {
        budget.check()?;
        let entry = entry.map_err(|_| FitError::InvalidZip)?;
        let name = entry.name().map_err(|_| FitError::InvalidZip)?;
        if entry.is_dir() || !is_fit_name(&name) {
            continue;
        }
        let declared = entry.size();
        let file = entry
            .with_reader(entries.reader_mut(), ZipReadOptions::default())
            .map_err(|_| FitError::InvalidZip)?;
        let fit = verify_frames(
            file,
            declared.min(max_bytes - inventory.fit_bytes),
            || budget.check().is_err(),
            |frame| {
                on_frame(ExportFrame {
                    fit_member_index: inventory.fit_members,
                    frame,
                });
            },
        )
        .map_err(|error| {
            if error == FitError::ReadFailed {
                FitError::InvalidZip
            } else {
                error
            }
        })?;
        if fit.bytes != declared {
            return Err(FitError::InvalidZip);
        }
        inventory.fit_bytes += fit.bytes;
        inventory.frames += fit.frames;
        inventory.fit_members += 1;
    }
    if inventory.fit_members == 0 {
        return Err(FitError::NoFitMembers);
    }
    Ok(inventory)
}

fn safe_name(name: &str) -> bool {
    use std::path::Path;
    !name.is_empty()
        && !name.contains(['\\', '\0'])
        && !name
            .split('/')
            .next()
            .is_some_and(|part| part.contains(':'))
        && Path::new(name)
            .components()
            .all(|part| matches!(part, Component::CurDir | Component::Normal(_)))
}

fn is_fit_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    bytes[bytes.len().saturating_sub(4)..].eq_ignore_ascii_case(b".fit")
}

struct Budget<F> {
    started: Instant,
    max_duration: Duration,
    cancelled: F,
}

impl<F: Fn() -> bool> Budget<F> {
    fn check(&self) -> Result<(), FitError> {
        if (self.cancelled)() {
            return Err(FitError::Cancelled);
        }
        if self.started.elapsed() >= self.max_duration {
            return Err(FitError::DeadlineExceeded);
        }
        Ok(())
    }

    fn check_io(&self) -> io::Result<()> {
        self.check().map_err(io::Error::other)
    }
}

struct Budgeted<'a, R, F> {
    source: R,
    budget: &'a Budget<F>,
}

impl<R: Read, F: Fn() -> bool> Read for Budgeted<'_, R, F> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        self.budget.check_io()?;
        self.source.read(output)
    }
}

impl<R: Seek, F: Fn() -> bool> Seek for Budgeted<'_, R, F> {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        self.budget.check_io()?;
        self.source.seek(position)
    }
}
