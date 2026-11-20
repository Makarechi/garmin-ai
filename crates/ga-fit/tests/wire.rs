use std::{error::Error, fs::File, io::BufReader};

use ga_fit::{ByteOrder, FitError, FitInventory, MAX_FIT_BYTES, WireInventory, visit_fit_wire};

// Synthetic records independently encoded and decoded by fitdecode 0.11.0.
const BIG_ENDIAN: &[u8] = b"\x0e\x20\x34\x08\x20\x00\x00\x00.FIT\xb8\x77\x60\x00\x01\xff\x01\x03\xfd\x04\x86\xde\x02\x84\x63\x03\x0d\x01\x63\x03\x09\x00\x45\x24\x32\x7e\x12\x34\x00\xff\x70\x01\x03\x05\x06\x15";
const COMPRESSED: &[u8] = b"\x0e\x20\x34\x08\x28\x00\x00\x00.FIT\xb9\xd1\x40\x00\x00\x14\x00\x02\xfd\x04\x86\x03\x01\x02\x00\x7e\x32\x24\x45\x3e\x41\x00\x00\x14\x00\x01\x03\x01\x02\xa3\x3f\x40\x00\x00\x14\x00\x01\x03\x01\x01\x00\xfe\x4a\xd5";
const RESET: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x41\x00\x00\x14\x00\x01\x03\x01\x02\xa3\x3f\x8c\xcf";
const OVERFLOW: &[u8] = b"\x0e\x20\x34\x08\x19\x00\x00\x00.FIT\x7b\x09\x40\x00\x00\x14\x00\x01\xfd\x04\x86\x00\xfe\xff\xff\xff\x41\x00\x00\x14\x00\x01\x03\x01\x02\xa1\x3f\xae\xc1";

#[test]
fn keeps_vendor_bytes_and_distinguishes_native_and_developer_fields() -> Result<(), FitError> {
    let actual = visit_fit_wire(
        BIG_ENDIAN,
        MAX_FIT_BYTES,
        || false,
        |message| {
            assert_eq!(message.frame_index, 0);
            assert_eq!(message.message_index, 0);
            assert_eq!(message.byte_offset, 33);
            assert_eq!(message.global_number, 0xff01);
            assert_eq!(message.local_number, 0);
            assert_eq!(message.byte_order, ByteOrder::Big);
            assert_eq!(message.compressed_timestamp, None);
            let fields: Vec<_> = message
                .fields
                .iter()
                .map(|field| {
                    (
                        field.number,
                        field.developer_data_index,
                        field.base_type,
                        field.bytes.as_slice(),
                    )
                })
                .collect();
            assert_eq!(
                fields,
                [
                    (253, None, Some(0x86), &[0x45, 0x24, 0x32, 0x7e][..]),
                    (222, None, Some(0x84), &[0x12, 0x34]),
                    (99, None, Some(0x0d), &[0, 255, 112]),
                    (99, Some(9), None, &[1, 3, 5]),
                ]
            );
            Ok(())
        },
    )?;
    assert_eq!(
        actual,
        WireInventory {
            member: FitInventory {
                frames: 1,
                bytes: BIG_ENDIAN.len() as u64,
            },
            messages: 1,
        }
    );
    Ok(())
}

#[test]
fn reads_redefined_messages_and_resets_timestamp_each_frame() -> Result<(), FitError> {
    let raw = [COMPRESSED, RESET, OVERFLOW].concat();
    let mut actual = Vec::new();
    let inventory = visit_fit_wire(
        raw.as_slice(),
        MAX_FIT_BYTES,
        || false,
        |message| {
            let field = message.fields.last().ok_or(FitError::InvalidDefinition)?;
            actual.push((
                message.frame_index,
                message.message_index,
                message.local_number,
                message.compressed_timestamp,
                field.base_type,
                field.bytes[0],
            ));
            Ok(())
        },
    )?;
    assert_eq!(inventory.member.frames, 3);
    assert_eq!(inventory.messages, 6);
    assert_eq!(
        actual,
        [
            (0, 0, 0, None, Some(2), 62),
            (0, 1, 1, Some(1_160_000_131), Some(2), 63),
            (0, 2, 0, None, Some(1), 254),
            (1, 3, 1, Some(3), Some(2), 63),
            (2, 4, 0, None, Some(0x86), 254),
            (2, 5, 1, Some(4_294_967_297), Some(2), 63),
        ]
    );
    Ok(())
}

#[test]
fn checks_frame_bounds_before_following_checksum_or_definition() {
    let mut invalid = RESET.to_vec();
    invalid[14] = 0x01;
    assert_eq!(
        visit_fit_wire(invalid.as_slice(), MAX_FIT_BYTES, || false, |_| Ok(())),
        Err(FitError::MissingDefinition)
    );
    let mut truncated = RESET.to_vec();
    truncated[18] = 0;
    truncated[17] = 0;
    truncated[19] = 255;
    assert_eq!(
        visit_fit_wire(truncated.as_slice(), MAX_FIT_BYTES, || false, |_| Ok(())),
        Err(FitError::InvalidRecord)
    );
    assert_eq!(
        visit_fit_wire(RESET, RESET.len() as u64 - 1, || false, |_| Ok(())),
        Err(FitError::LimitExceeded)
    );
}

#[test]
fn late_checksum_failure_invalidates_already_visited_candidates() {
    let mut corrupt = [COMPRESSED, RESET].concat();
    if let Some(footer) = corrupt.last_mut() {
        *footer ^= 1;
    }
    let mut candidates = 0;
    assert_eq!(
        visit_fit_wire(
            corrupt.as_slice(),
            MAX_FIT_BYTES,
            || false,
            |_| {
                candidates += 1;
                Ok(())
            },
        ),
        Err(FitError::FileChecksum)
    );
    assert_eq!(candidates, 4);
}

#[test]
#[ignore = "requires the generated synthetic R14 100 MiB FIT fixture"]
fn reads_every_wire_field_at_the_export_ceiling() -> Result<(), Box<dyn Error>> {
    let source = File::open(std::env::var_os("GA_TEST_MAX_FIT").ok_or("GA_TEST_MAX_FIT missing")?)?;
    let size = source.metadata()?.len();
    assert_eq!(size, MAX_FIT_BYTES);
    let expected: Vec<_> = (0..255).collect::<Vec<u8>>();
    let mut fields = 0;
    let mut short = 0;
    let actual = visit_fit_wire(
        BufReader::with_capacity(64 * 1024, source),
        MAX_FIT_BYTES,
        || false,
        |message| {
            assert_eq!(message.message_index, fields);
            assert_eq!(message.global_number, 20);
            assert_eq!(message.fields.len(), 1);
            let field = &message.fields[0];
            assert_eq!(field.number, 99);
            assert_eq!(field.base_type, Some(0x0d));
            assert_eq!(field.bytes, expected[..field.bytes.len()]);
            fields += 1;
            if field.bytes.len() != 255 {
                short += 1;
                assert_eq!(fields, 409_600);
            }
            Ok(())
        },
    )?;
    assert_eq!(short, 1);
    assert_eq!(fields, 409_600);
    assert_eq!(
        actual,
        WireInventory {
            member: FitInventory {
                frames: 1,
                bytes: size
            },
            messages: fields,
        }
    );
    Ok(())
}

#[test]
fn preserves_wire_and_projection_across_short_reader_chunks() -> Result<(), FitError> {
    use ga_fit::visit_fit_projection;
    use std::io::{self, Read};

    struct Chunked<'a> {
        bytes: &'a [u8],
        limit: usize,
    }

    impl Read for Chunked<'_> {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            let count = buffer.len().min(self.limit);
            self.bytes.read(&mut buffer[..count])
        }
    }

    let raw = [BIG_ENDIAN, COMPRESSED, RESET, OVERFLOW].concat();
    let observe = |limit| -> Result<_, FitError> {
        let reader = || Chunked {
            bytes: raw.as_slice(),
            limit,
        };
        let mut wire = Vec::new();
        let wire_inventory = visit_fit_wire(
            reader(),
            raw.len() as u64,
            || false,
            |message| {
                let fields: Vec<_> = message
                    .fields
                    .iter()
                    .map(|field| {
                        (
                            field.number,
                            field.developer_data_index,
                            field.base_type,
                            field.bytes.clone(),
                        )
                    })
                    .collect();
                wire.push((
                    message.frame_index,
                    message.message_index,
                    message.byte_offset,
                    message.global_number,
                    message.local_number,
                    message.byte_order,
                    message.developer_data_flag,
                    message.compressed_timestamp,
                    fields,
                ));
                Ok(())
            },
        )?;
        let mut projected = Vec::new();
        let projected_inventory = visit_fit_projection(
            reader(),
            raw.len() as u64,
            || false,
            |message| {
                projected.push((message.frame_index, message.message_index, message.payload));
                Ok(())
            },
        )?;
        Ok((wire_inventory, wire, projected_inventory, projected))
    };
    let normal = observe(raw.len())?;
    assert_eq!(normal.0.member.frames, 4);
    assert_eq!(normal.0.messages, 7);
    assert_eq!(normal.2.member.frames, 4);
    assert_eq!(normal.3.len(), 7);
    for max_chunk in 1..=32 {
        assert_eq!(observe(max_chunk)?, normal, "maximum chunk {max_chunk}");
    }
    Ok(())
}
