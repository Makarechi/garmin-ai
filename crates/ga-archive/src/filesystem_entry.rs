use std::{io, os::fd::AsFd, path::Path};

use rustix::{
    fs::{self, AtFlags, FileType},
    io::Errno,
    path::Arg,
};

use crate::DIRECTORY_READ;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) struct Identity {
    device: (u32, u32),
    inode: u64,
}

#[derive(PartialEq, Eq)]
pub(crate) struct Entry {
    pub identity: Identity,
    pub mount: u64,
    pub kind: FileType,
}

impl Entry {
    pub fn opened(directory: impl AsFd + Copy) -> io::Result<Self> {
        #[cfg(target_os = "linux")]
        return linux_entry(directory, "", AtFlags::EMPTY_PATH);

        #[cfg(not(target_os = "linux"))]
        Ok(Self::from_stat(fs::fstat(directory)?))
    }

    pub fn lookup(directory: impl AsFd + Copy, name: impl Arg + Copy) -> io::Result<Self> {
        #[cfg(target_os = "linux")]
        return linux_entry(directory, name, AtFlags::SYMLINK_NOFOLLOW);

        #[cfg(not(target_os = "linux"))]
        Ok(Self::from_stat(fs::statat(
            directory,
            name,
            AtFlags::SYMLINK_NOFOLLOW,
        )?))
    }

    #[cfg(not(target_os = "linux"))]
    fn from_stat(stat: fs::Stat) -> Self {
        Self {
            identity: Identity {
                device: (fs::major(stat.st_dev), fs::minor(stat.st_dev)),
                inode: stat.st_ino,
            },
            mount: stat.st_dev as u64,
            kind: FileType::from_raw_mode(stat.st_mode),
        }
    }
}

#[cfg(target_os = "linux")]
fn linux_entry(
    directory: impl AsFd + Copy,
    name: impl Arg + Copy,
    flags: AtFlags,
) -> io::Result<Entry> {
    use std::os::fd::AsRawFd;

    use rustix::fs::{Mode, OFlags, StatxFlags};

    let mask = StatxFlags::TYPE | StatxFlags::INO | StatxFlags::MNT_ID;
    match fs::statx(directory, name, flags, mask) {
        Ok(stat) if StatxFlags::from_bits_retain(stat.stx_mask).contains(mask) => {
            return Ok(Entry {
                identity: Identity {
                    device: (stat.stx_dev_major, stat.stx_dev_minor),
                    inode: stat.stx_ino,
                },
                mount: stat.stx_mnt_id,
                kind: FileType::from_raw_mode(stat.stx_mode.into()),
            });
        }
        Ok(_) | Err(Errno::NOSYS | Errno::PERM | Errno::OPNOTSUPP) => {}
        Err(error) => return Err(error.into()),
    }

    // Older kernels expose the mount ID through fdinfo even without STATX_MNT_ID.
    let file;
    let opened = if flags.contains(AtFlags::EMPTY_PATH) {
        directory.as_fd()
    } else {
        file = fs::openat(
            directory,
            name,
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        file.as_fd()
    };
    let info = std::fs::read_to_string(format!("/proc/self/fdinfo/{}", opened.as_raw_fd()))?;
    let mount = info
        .lines()
        .find_map(|line| line.strip_prefix("mnt_id:")?.trim().parse().ok())
        .ok_or_else(|| io::Error::other("Filesystem mount identity is unavailable"))?;
    let stat = fs::fstat(opened)?;
    Ok(Entry {
        identity: Identity {
            device: (fs::major(stat.st_dev), fs::minor(stat.st_dev)),
            inode: stat.st_ino,
        },
        mount,
        kind: FileType::from_raw_mode(stat.st_mode),
    })
}

/// Inspect a configured directory through any aliases without changing it.
pub(crate) fn directory_identity(path: &Path) -> io::Result<Option<Identity>> {
    use rustix::fs::{Mode, OFlags};

    match fs::open(
        path,
        DIRECTORY_READ.difference(OFlags::NOFOLLOW),
        Mode::empty(),
    ) {
        Ok(directory) => Ok(Some(Entry::opened(&directory)?.identity)),
        Err(Errno::NOENT) => Ok(None),
        Err(error) => Err(error.into()),
    }
}
