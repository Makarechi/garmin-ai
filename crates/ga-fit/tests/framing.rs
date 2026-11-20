use std::{
    error::Error,
    fs::File,
    io::{self, Read},
};

use ga_fit::{FitError, FitFrame, FitInventory, MAX_FIT_BYTES, verify_frames};

// These synthetic one-record fixtures were independently encoded and decoded by fitdecode 0.11.0.
const SHORT: &[u8] =
    b"\x0c\x20\x34\x08\x0b\x00\x00\x00.FIT\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xcb\x2d";
const WITH_CRC: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xb0\xe5";
const ZERO_CRC: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\x00\x00\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\x38\x31";

fn verify(raw: &[u8]) -> Result<FitInventory, FitError> {
    verify_frames(raw, MAX_FIT_BYTES, || false, |_| {})
}

#[test]
fn preserves_chained_frame_offsets_and_resets_checksum_across_all_header_forms()
-> Result<(), FitError> {
    let raw = [SHORT, WITH_CRC, ZERO_CRC].concat();
    let mut frames = Vec::new();
    let result = verify_frames(
        raw.as_slice(),
        raw.len() as u64,
        || false,
        |frame| frames.push(frame),
    )?;
    assert_eq!(
        result,
        FitInventory {
            frames: 3,
            bytes: raw.len() as u64
        }
    );
    assert_eq!(
        frames,
        [
            FitFrame {
                index: 0,
                offset: 0,
                header_bytes: 12,
                data_bytes: 11,
                protocol: 0x20,
                profile: 2100
            },
            FitFrame {
                index: 1,
                offset: SHORT.len() as u64,
                header_bytes: 14,
                data_bytes: 11,
                protocol: 0x20,
                profile: 2100
            },
            FitFrame {
                index: 2,
                offset: (SHORT.len() + WITH_CRC.len()) as u64,
                header_bytes: 14,
                data_bytes: 11,
                protocol: 0x20,
                profile: 2100
            },
        ]
    );
    assert_eq!(verify(&[])?, FitInventory::default());
    Ok(())
}

#[test]
fn rejects_corrupt_crc_unsupported_header_missing_footer_and_trailing_data() {
    let mut incorrect = WITH_CRC.to_vec();
    incorrect[12] ^= 1;
    assert_eq!(verify(&incorrect), Err(FitError::HeaderChecksum));
    incorrect[0] = 16;
    assert_eq!(verify(&incorrect), Err(FitError::InvalidHeader));
    for end in [1, 10, 13, ZERO_CRC.len() - 2, ZERO_CRC.len() - 1] {
        assert_eq!(verify(&ZERO_CRC[..end]), Err(FitError::Truncated));
    }
    let mut data_only = ZERO_CRC.to_vec();
    data_only[25..].copy_from_slice(&WITH_CRC[25..]);
    assert_eq!(verify(&data_only), Err(FitError::FileChecksum));
    let trailing = [SHORT, b"bad bytes"].concat();
    assert_eq!(verify(&trailing), Err(FitError::InvalidHeader));
    let invalid_magic = [b"\x0c\x20\x34\x08\x0b\x00\x00\x00NOPE", &SHORT[12..]].concat();
    assert_eq!(verify(&invalid_magic), Err(FitError::InvalidHeader));
}

#[test]
fn late_failure_never_reports_the_failed_frame_as_verified() {
    let mut invalid = SHORT.to_vec();
    let last = invalid.len() - 1;
    invalid[last] ^= 1;
    let raw = [WITH_CRC, invalid.as_slice()].concat();
    let mut candidates = Vec::new();
    assert_eq!(
        verify_frames(
            raw.as_slice(),
            MAX_FIT_BYTES,
            || false,
            |frame| candidates.push(frame)
        ),
        Err(FitError::FileChecksum)
    );
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].offset, 0);
}

struct HeaderOnly<'a> {
    header: &'a [u8],
}

impl Read for HeaderOnly<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if self.header.is_empty() {
            return Err(io::Error::other("A body read was unexpected"));
        }
        self.header.read(output)
    }
}

#[test]
fn rejects_body_over_limit_and_cancels_before_reading_large_body() {
    let header = &WITH_CRC[..14];
    let source = HeaderOnly { header };
    assert_eq!(
        verify_frames(source, 24, || false, |_| {}),
        Err(FitError::LimitExceeded)
    );
    let mut remaining = 1;
    let cancelled = || {
        if remaining == 0 {
            return true;
        }
        remaining -= 1;
        false
    };
    let source = HeaderOnly { header };
    assert_eq!(
        verify_frames(source, MAX_FIT_BYTES, cancelled, |_| {}),
        Err(FitError::Cancelled)
    );
}

#[test]
#[ignore = "uses the synthetic exact-ceiling workload from the migration corpus"]
fn verifies_exact_limit_synthetic_fit_file() -> Result<(), Box<dyn Error>> {
    let path = std::env::var_os("GA_TEST_FIT_FRAMING_CEILING").ok_or_else(|| {
        io::Error::other("Set GA_TEST_FIT_FRAMING_CEILING to the generated 100 MiB synthetic file")
    })?;
    let source = File::open(&path)?;
    assert_eq!(source.metadata()?.len(), MAX_FIT_BYTES);
    assert_eq!(
        verify_frames(source, MAX_FIT_BYTES, || false, |_| {})?,
        FitInventory {
            frames: 1,
            bytes: MAX_FIT_BYTES
        }
    );
    Ok(())
}

#[test]
fn rejects_each_single_bit_change_to_a_chained_frame_body_or_footer() {
    for fixture in [SHORT, WITH_CRC, ZERO_CRC] {
        for byte in fixture[0] as usize..fixture.len() {
            for bit in 0..8 {
                let mut changed = fixture.to_vec();
                changed[byte] ^= 1 << bit;
                let raw = [WITH_CRC, changed.as_slice()].concat();
                let mut verified = Vec::new();
                assert_eq!(
                    verify_frames(
                        raw.as_slice(),
                        raw.len() as u64,
                        || false,
                        |frame| verified.push(frame.offset)
                    ),
                    Err(FitError::FileChecksum),
                    "header size {}, byte {byte}, bit {bit}",
                    fixture[0],
                );
                assert_eq!(verified, [0]);
            }
        }
    }
}
