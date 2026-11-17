use std::{
    cell::Cell,
    error::Error,
    fs::File,
    io::{self, Cursor, Write},
    num::NonZeroUsize,
    time::Duration,
};

use ga_fit::{
    ActivityExport, ExportInventory, ExportLimits, FitError, MAX_FIT_BYTES, verify_activity_export,
};
use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

const FIT: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xb0\xe5";

fn limits() -> ExportLimits {
    ExportLimits {
        max_bytes: MAX_FIT_BYTES,
        max_zip_entries: NonZeroUsize::new(8).unwrap_or(NonZeroUsize::MIN),
        max_duration: Duration::from_secs(5),
    }
}

fn zip(entries: &[(&str, &[u8])], method: CompressionMethod) -> Result<Vec<u8>, Box<dyn Error>> {
    let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
    for (name, bytes) in entries {
        writer.start_file(
            *name,
            SimpleFileOptions::default().compression_method(method),
        )?;
        writer.write_all(bytes)?;
    }
    Ok(writer.finish()?.into_inner())
}

fn verify(raw: &[u8], budget: ExportLimits) -> Result<ExportInventory, FitError> {
    verify_activity_export(Cursor::new(raw), budget, || false, |_| {})
}

fn signature(raw: &[u8], magic: &[u8; 4]) -> Result<usize, io::Error> {
    raw.windows(4)
        .position(|data| data == magic)
        .ok_or_else(|| io::Error::other("Missing synthetic ZIP signature"))
}

#[test]
fn preserves_fit_member_order_and_concatenated_sequences() -> Result<(), Box<dyn Error>> {
    let chained = [FIT, FIT].concat();
    let raw = zip(
        &[
            ("some-folder/a.fit", &chained),
            ("other.txt", b"synthetic ignored"),
            ("b.FiT", FIT),
        ],
        CompressionMethod::Deflated,
    )?;
    let mut indices = Vec::new();
    let result = verify_activity_export(
        Cursor::new(&raw),
        limits(),
        || false,
        |candidate| {
            indices.push((
                candidate.fit_member_index,
                candidate.frame.index,
                candidate.frame.offset,
            ));
        },
    )?;
    assert_eq!(
        result,
        ExportInventory {
            format: ActivityExport::Zip,
            original_bytes: raw.len() as u64,
            fit_members: 2,
            fit_bytes: (FIT.len() * 3) as u64,
            frames: 3
        }
    );
    assert_eq!(indices, [(0, 0, 0), (0, 1, FIT.len() as u64), (1, 0, 0)]);
    assert_eq!(verify(FIT, limits())?.format, ActivityExport::Fit);
    assert_eq!(verify(&[], limits())?.fit_members, 0);
    Ok(())
}

#[test]
fn rejects_paths_symlinks_and_counts_unselected_members_before_inflation()
-> Result<(), Box<dyn Error>> {
    for unsafe_name in [
        "../data.fit",
        "/data.fit",
        "x/../data.fit",
        "C:/data.fit",
        "x\\data.fit",
    ] {
        let raw = zip(&[(unsafe_name, FIT)], CompressionMethod::Stored)?;
        assert_eq!(
            verify(&raw, limits()),
            Err(FitError::UnsafeZipMember),
            "{unsafe_name}"
        );
    }
    let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
    writer.add_symlink("data.fit", "other.fit", SimpleFileOptions::default())?;
    let link = writer.finish()?.into_inner();
    assert_eq!(verify(&link, limits()), Err(FitError::UnsafeZipMember));

    let two = zip(
        &[("a.fit", FIT), ("not-fit.txt", b"ignored")],
        CompressionMethod::Stored,
    )?;
    let one = ExportLimits {
        max_zip_entries: NonZeroUsize::MIN,
        ..limits()
    };
    assert_eq!(verify(&two, one), Err(FitError::TooManyZipMembers));
    let zero = zip(&[("not-fit.txt", b"ignored")], CompressionMethod::Stored)?;
    assert_eq!(verify(&zero, limits()), Err(FitError::NoFitMembers));
    Ok(())
}

#[test]
fn requires_both_zip_and_fit_checksums_and_bounds_actual_member_bytes() -> Result<(), Box<dyn Error>>
{
    let archive = zip(&[("activity.fit", FIT)], CompressionMethod::Stored)?;
    let central = signature(&archive, b"PK\x01\x02")?;
    let mut crc = archive.clone();
    crc[14] ^= 1;
    crc[central + 16] ^= 1;
    assert_eq!(verify(&crc, limits()), Err(FitError::InvalidZip));

    let mut malformed = archive.clone();
    malformed[22] = 1;
    malformed[central + 24] = 1;
    assert!(verify(&malformed, limits()).is_err());
    assert_eq!(
        verify(&archive[..archive.len() - 10], limits()),
        Err(FitError::InvalidZip)
    );
    let mut payload = FIT.to_vec();
    let last = payload.len() - 1;
    payload[last] ^= 1;
    let bad = zip(
        &[("valid.fit", FIT), ("late.fit", &payload)],
        CompressionMethod::Deflated,
    )?;
    assert_eq!(verify(&bad, limits()), Err(FitError::FileChecksum));

    let raw_limit = ExportLimits {
        max_bytes: archive.len() as u64 - 1,
        ..limits()
    };
    assert_eq!(verify(&archive, raw_limit), Err(FitError::LimitExceeded));
    let repeated = FIT.repeat(1000);
    let compressed = zip(
        &[("compressible.fit", &repeated)],
        CompressionMethod::Deflated,
    )?;
    let expanded_limit = ExportLimits {
        max_bytes: 4096,
        ..limits()
    };
    assert!(compressed.len() < 4096);
    assert_eq!(
        verify(&compressed, expanded_limit),
        Err(FitError::LimitExceeded)
    );
    Ok(())
}

#[test]
fn cancellation_and_deadline_stop_further_reads() -> Result<(), Box<dyn Error>> {
    let source = zip(&[("activity.fit", FIT)], CompressionMethod::Deflated)?;
    let cancelled = Cell::new(false);
    assert_eq!(
        verify_activity_export(
            Cursor::new(&source),
            limits(),
            || cancelled.get(),
            |_| cancelled.set(true)
        ),
        Err(FitError::Cancelled)
    );
    let expired = ExportLimits {
        max_duration: Duration::ZERO,
        ..limits()
    };
    assert_eq!(verify(&source, expired), Err(FitError::DeadlineExceeded));
    Ok(())
}

#[test]
#[ignore = "provide a synthetic ZIP with a 100 MiB FIT member"]
fn verifies_zip_with_an_exact_limit_synthetic_member() -> Result<(), Box<dyn Error>> {
    let path = std::env::var_os("GA_TEST_FIT_ZIP_CEILING")
        .ok_or_else(|| io::Error::other("Set GA_TEST_FIT_ZIP_CEILING to a synthetic ZIP file"))?;
    let result = verify_activity_export(File::open(path)?, limits(), || false, |_| {})?;
    assert_eq!(
        (
            result.format,
            result.fit_members,
            result.fit_bytes,
            result.frames
        ),
        (ActivityExport::Zip, 1, MAX_FIT_BYTES, 1)
    );
    Ok(())
}
