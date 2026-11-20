mod archives;
mod docker;

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, DirBuilder, File};
use std::io::{BufReader, Read, Write};
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::SystemTime;

use clap::Parser;
use rustix::fs::{FlockOperation, Mode, OFlags};
use rustix::io::Errno;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

type Result<T> = std::result::Result<T, Failure>;

#[derive(Debug)]
struct Failure(String);

impl Failure {
    fn message(message: &str) -> Self {
        Self(message.to_owned())
    }
}

impl From<std::io::Error> for Failure {
    fn from(_: std::io::Error) -> Self {
        Self::message("Local I/O operation failed")
    }
}

impl From<Errno> for Failure {
    fn from(error: Errno) -> Self {
        std::io::Error::from(error).into()
    }
}

impl From<serde_json::Error> for Failure {
    fn from(_: serde_json::Error) -> Self {
        Self::message("Invalid retention state or Docker inventory")
    }
}

#[derive(Parser)]
#[command(about = "Keep the current and verified rollback images on the Garmin AI host")]
struct Options {
    /// Perform changes (the default is a read-only preview).
    #[arg(long)]
    apply: bool,

    /// Record image identities without removing images or archives.
    #[arg(long)]
    record_only: bool,

    /// Explicitly verified local image used to initialize rollback retention.
    #[arg(long)]
    initialize_rollback: Option<String>,

    /// Canonical upload directory. May be repeated.
    #[arg(long)]
    archive_dir: Vec<PathBuf>,

    #[arg(long, default_value = "/var/lib/garmin-ai-release-cleanup")]
    state_dir: PathBuf,

    #[arg(long, default_value = "/run/lock/garmin-ai-release-cleanup.lock")]
    lock_file: PathBuf,
}

#[derive(Deserialize, PartialEq, Serialize)]
struct Retention {
    archive_dirs: Vec<PathBuf>,
    current: String,
    rollback: String,
    version: u32,
}

#[derive(Serialize)]
struct Report<'a> {
    apply: bool,
    current: &'a str,
    rollback: &'a str,
    images: Vec<Vec<String>>,
    archives: Vec<PathBuf>,
    archive_bytes: u64,
}

fn read_state(directory: &Path) -> Result<Option<Retention>> {
    let file = match rustix::fs::open(
        directory.join("state.json"),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(file) => File::from(file),
        Err(Errno::NOENT) => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let info = file.metadata()?;
    if !info.is_file() || info.len() > 1024 * 1024 {
        return Err(Failure::message("Invalid retention state"));
    }
    let state: Retention = serde_json::from_reader(BufReader::new(file.take(1024 * 1024 + 1)))?;
    if state.version != 1 {
        return Err(Failure::message("Unsupported retention state"));
    }
    Ok(Some(state))
}

fn save_state(directory: &Path, state: &Retention) -> Result<()> {
    let mut stage = tempfile::NamedTempFile::new_in(directory)?;
    serde_json::to_writer(&mut stage, state)?;
    stage.write_all(b"\n")?;
    stage.as_file().sync_all()?;
    stage
        .persist(directory.join("state.json"))
        .map_err(|error| Failure::from(error.error))?;
    File::open(directory)?.sync_all()?;
    Ok(())
}

async fn retention(
    options: &Options,
    old: Option<&Retention>,
    current: String,
) -> Result<Retention> {
    let (rollback, stored_dirs) = match (old, options.initialize_rollback.as_deref()) {
        (None, Some(reference)) => (
            docker::inspect_image(reference).await?,
            vec![PathBuf::from("/opt/garmin-ai/uploads")],
        ),
        (None, None) => {
            return Err(Failure::message(
                "Initialize with an explicitly verified rollback image first",
            ));
        }
        (Some(_), Some(_)) => {
            return Err(Failure::message(
                "Rollback initialization is only allowed with no existing state",
            ));
        }
        (Some(old), None) => (
            if old.current == current {
                old.rollback.clone()
            } else {
                old.current.clone()
            },
            old.archive_dirs.clone(),
        ),
    };
    let archive_dirs = if options.archive_dir.is_empty() {
        stored_dirs
    } else {
        options.archive_dir.clone()
    };
    for directory in &archive_dirs {
        archives::open_upload(directory)?;
    }
    if rollback == current {
        return Err(Failure::message(
            "A distinct, verified rollback image is required",
        ));
    }
    Ok(Retention {
        archive_dirs,
        current,
        rollback,
        version: 1,
    })
}

fn retained_images(state: &Retention, images: &BTreeMap<String, Vec<docker::ImageRow>>) -> bool {
    [&state.current, &state.rollback].into_iter().all(|id| {
        docker::image_identity(id)
            && images
                .get(id)
                .is_some_and(|rows| rows.iter().any(|row| row.repository == "garmin-ai"))
    })
}

async fn verify_current(
    state: &Retention,
    require_healthy: bool,
) -> Result<Vec<docker::Container>> {
    let (current, containers) = docker::current(require_healthy).await?;
    if current != state.current {
        return Err(Failure::message("Deployment changed during cleanup"));
    }
    Ok(containers)
}

async fn recheck(state: &Retention) -> Result<Vec<docker::Container>> {
    let project = verify_current(state, false).await?;
    if !retained_images(state, &docker::images().await?) {
        return Err(Failure::message(
            "A retained image disappeared; refusing cleanup",
        ));
    }
    Ok(project)
}

fn removable_tags(rows: &[docker::ImageRow]) -> Option<Vec<String>> {
    let mut tags: Vec<_> = rows
        .iter()
        .map(|row| {
            (row.repository == "garmin-ai" && !row.tag.is_empty() && row.tag != "<none>")
                .then(|| format!("garmin-ai:{}", row.tag))
        })
        .collect::<Option<_>>()?;
    tags.sort_unstable();
    tags.dedup();
    Some(tags)
}

async fn cleanup<'a>(
    state: &'a Retention,
    options: &Options,
    project: &[docker::Container],
    images: BTreeMap<String, Vec<docker::ImageRow>>,
) -> Result<Report<'a>> {
    let mut obsolete = BTreeMap::new();
    for (image, rows) in images {
        if image != state.current
            && image != state.rollback
            && let Some(tags) = removable_tags(&rows)
        {
            obsolete.insert(image, tags);
        }
    }

    let migrations: BTreeSet<_> = project
        .iter()
        .filter(|item| item.successful_migration() && obsolete.contains_key(&item.image))
        .map(|item| item.id.as_str())
        .collect();
    for container in docker::containers(false).await? {
        if !migrations.contains(container.id.as_str()) {
            obsolete.remove(&container.image);
        }
    }
    let mut report = Report {
        apply: options.apply,
        current: &state.current,
        rollback: &state.rollback,
        images: obsolete.values().cloned().collect(),
        archives: Vec::new(),
        archive_bytes: 0,
    };
    if options.record_only {
        return Ok(report);
    }

    if options.apply {
        for migration in project.iter().filter(|item| {
            migrations.contains(item.id.as_str()) && obsolete.contains_key(&item.image)
        }) {
            let current = recheck(state).await?;
            if !current.iter().any(|item| {
                item.id == migration.id
                    && item.image == migration.image
                    && item.successful_migration()
            }) {
                return Err(Failure::message(
                    "Migration container changed during cleanup",
                ));
            }
            // Omitting -v preserves every volume attached to the one-shot container.
            docker::command(&["container", "rm", &migration.id]).await?;
        }
        for (id, tags) in &obsolete {
            recheck(state).await?;
            let current_tags = docker::images()
                .await?
                .get(id)
                .and_then(|rows| removable_tags(rows));
            if current_tags.as_ref() != Some(tags)
                || docker::containers(false)
                    .await?
                    .iter()
                    .any(|item| &item.image == id)
            {
                return Err(Failure::message("Image references changed during cleanup"));
            }
            for tag in tags {
                if docker::inspect_image(tag).await? != *id {
                    return Err(Failure::message("Image tag changed during cleanup"));
                }
            }
            let mut args = vec!["image", "rm", "--no-prune"];
            args.extend(tags.iter().map(String::as_str));
            docker::command(&args).await?;
        }
    }
    for root in &state.archive_dirs {
        let Some(directory) = archives::open_upload(root)? else {
            continue;
        };
        for candidate in archives::candidates(&directory, SystemTime::now())? {
            report.archives.push(root.join(&candidate.name));
            report.archive_bytes = report
                .archive_bytes
                .checked_add(candidate.bytes)
                .ok_or_else(|| Failure::message("Archive inventory size exceeded its limit"))?;
            if options.apply {
                recheck(state).await?;
                archives::remove(&directory, root, &candidate)?;
            }
        }
    }
    Ok(report)
}

async fn run(options: Options) -> Result<Value> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&options.state_dir)?;
    if !fs::symlink_metadata(&options.state_dir)?.is_dir() {
        return Err(Failure::message("Invalid retention state directory"));
    }
    let lock = rustix::fs::open(
        &options.lock_file,
        OFlags::WRONLY | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::RUSR | Mode::WUSR,
    )?;
    let lock = File::from(lock);
    if !lock.metadata()?.is_file() {
        return Err(Failure::message("Invalid deployment lock file"));
    }
    match rustix::fs::flock(&lock, FlockOperation::NonBlockingLockExclusive) {
        Ok(()) => {}
        Err(Errno::WOULDBLOCK) => {
            return Ok(json!({"skipped": "deployment or cleanup lock is held"}));
        }
        Err(error) => return Err(error.into()),
    }

    let old = read_state(&options.state_dir)?;
    let (current, _) = docker::current(false).await?;
    let new_deployment = old.as_ref().is_none_or(|old| old.current != current);
    let state = retention(&options, old.as_ref(), current).await?;
    let project = verify_current(&state, new_deployment).await?;
    let images = docker::images().await?;
    if !retained_images(&state, &images) {
        return Err(Failure::message(
            "Both retained application images must exist locally",
        ));
    }
    if options.apply && Some(&state) != old.as_ref() {
        save_state(&options.state_dir, &state)?;
    }
    let report = cleanup(&state, &options, &project, images).await?;
    Ok(serde_json::to_value(report)?)
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    match run(Options::parse()).await {
        Ok(report) => {
            println!("{report}");
            ExitCode::SUCCESS
        }
        Err(error) => {
            println!("{}", json!({"error": error.0}));
            ExitCode::FAILURE
        }
    }
}
