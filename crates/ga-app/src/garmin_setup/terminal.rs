use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
};

use rustix::termios::{OptionalActions, Termios, tcgetattr, tcsetattr};
use zeroize::Zeroizing;

pub(super) struct Terminal(File);

impl Terminal {
    pub(super) fn open() -> io::Result<Self> {
        OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/tty")
            .map(Self)
            .map_err(|_| io::Error::other("Garmin sign-in requires a local terminal"))
    }

    pub(super) fn input(
        &mut self,
        prompt: &str,
        hidden: bool,
        maximum: usize,
    ) -> io::Result<Zeroizing<String>> {
        // Disable echo before writing the prompt so pasted secrets cannot appear.
        let settings = if hidden {
            Some(Hidden::acquire(&self.0)?)
        } else {
            None
        };
        let mut file = &self.0;
        file.write_all(prompt.as_bytes())?;
        file.flush()?;
        let result = read_line(&mut file, maximum);
        drop(settings);
        if hidden {
            file.write_all(b"\n")?;
        }
        result
    }
}

struct Hidden<'a> {
    file: &'a File,
    original: Termios,
}

impl<'a> Hidden<'a> {
    fn acquire(file: &'a File) -> io::Result<Self> {
        let original = tcgetattr(file)?;
        let mut raw = original.clone();
        raw.make_raw();
        tcsetattr(file, OptionalActions::Flush, &raw)?;
        Ok(Self { file, original })
    }
}

impl Drop for Hidden<'_> {
    fn drop(&mut self) {
        let _ = tcsetattr(self.file, OptionalActions::Flush, &self.original);
    }
}

fn read_line(reader: &mut impl Read, maximum: usize) -> io::Result<Zeroizing<String>> {
    let mut bytes = Zeroizing::new(Vec::with_capacity(maximum));
    loop {
        let mut input = Zeroizing::new([0_u8]);
        reader.read_exact(input.as_mut_slice())?;
        match input[0] {
            b'\r' | b'\n' => break,
            3 | 4 => return Err(io::Error::other("Garmin sign-in cancelled")),
            8 | 127 if !bytes.is_empty() => {
                let mut last = bytes.len() - 1;
                while last > 0 && bytes[last] & 0xc0 == 0x80 {
                    last -= 1;
                }
                bytes[last..].fill(0);
                bytes.truncate(last);
            }
            0..=31 | 127 => {}
            _ if bytes.len() == maximum => {
                return Err(io::Error::other("Garmin input is too long"));
            }
            byte => bytes.push(byte),
        }
    }
    let value = std::str::from_utf8(&bytes)
        .map_err(|_| io::Error::other("Garmin sign-in input must be UTF-8"))?;
    Ok(Zeroizing::new(value.to_owned()))
}
