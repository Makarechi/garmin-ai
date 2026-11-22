use std::os::fd::OwnedFd;

use rustix::{
    fs::{self, AtFlags, FileType},
    path::Arg,
};

use super::ErasureFilesError;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) struct Identity {
    device: (u32, u32),
    inode: u64,
}

#[derive(PartialEq, Eq)]
pub(super) struct Entry {
    pub identity: Identity,
    pub mount: u64,
    pub kind: FileType,
}

impl Entry {
    pub fn opened(directory: &OwnedFd) -> Result<Self, ErasureFilesError> {
        #[cfg(target_os = "linux")]
        return linux_entry(directory, "", AtFlags::EMPTY_PATH);

        #[cfg(not(target_os = "linux"))]
        Ok(Self::from_stat(fs::fstat(directory)?))
    }

    pub fn lookup(directory: &OwnedFd, name: impl Arg + Copy) -> Result<Self, ErasureFilesError> {
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
    directory: &OwnedFd,
    name: impl Arg + Copy,
    flags: AtFlags,
) -> Result<Entry, ErasureFilesError> {
    use std::os::fd::AsRawFd;

    use rustix::{
        fs::{Mode, OFlags, StatxFlags},
        io::Errno,
    };

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
        directory
    } else {
        file = fs::openat(
            directory,
            name,
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        &file
    };
    let info = std::fs::read_to_string(format!("/proc/self/fdinfo/{}", opened.as_raw_fd()))
        .map_err(crate::ArchiveError::from)?;
    let mount = info
        .lines()
        .find_map(|line| line.strip_prefix("mnt_id:")?.trim().parse().ok())
        .ok_or(ErasureFilesError::UnsafePath)?;
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
