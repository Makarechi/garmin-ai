use std::error::Error;
use std::fs::{self, File, FileTimes, Permissions};
use std::io;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, SystemTime};

use flate2::{Compression, write::GzEncoder};
use rustix::fs::{FlockOperation, flock};
use serde_json::{Value, json};

type TestResult = Result<(), Box<dyn Error>>;

const DOCKER: &str = r#"#!/bin/sh
set -eu
record=$CLEANUP_FIXTURE
[ "$DOCKER_HOST" = unix:///var/run/docker.sock ]
[ "${DOCKER_CONTEXT+x}" != x ]
case "$1:$2" in
    container:ls)
        while read -r id; do
            if [ -e "$record/removed-$id" ]; then continue; fi
            if [ "$#" -gt 4 ] && [ -e "$record/foreign-$id" ]; then continue; fi
            printf '%s\n' "$id"
        done < "$record/containers"
        ;;
    container:inspect)
        shift 4
        for id do
            cat "$record/container-$id"
        done
        ;;
    container:rm)
        printf '%s\n' "container rm $3" >> "$record/deletions"
        touch "$record/removed-$3"
        ;;
    image:ls)
        if [ -e "$record/fail-inventory" ]; then
            echo 'synthetic private Docker response' >&2
            exit 1
        fi
        cat "$record/images"
        ;;
    image:inspect)
        cat "$record/$5"
        ;;
    image:rm)
        printf '%s\n' "$*" >> "$record/deletions"
        ;;
    *)
        exit 1
        ;;
esac
"#;

fn id(digit: char) -> String {
    digit.to_string().repeat(64)
}

fn image(digit: char) -> String {
    format!("sha256:{}", id(digit))
}

struct Host {
    _temporary: tempfile::TempDir,
    root: PathBuf,
    uploads: PathBuf,
}

impl Host {
    fn new() -> Result<Self, Box<dyn Error>> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().canonicalize()?;
        let uploads = root.join("uploads");
        fs::create_dir(&uploads)?;
        let docker = root.join("docker");
        fs::write(&docker, DOCKER)?;
        fs::set_permissions(&docker, Permissions::from_mode(0o700))?;
        let mut host = Self {
            _temporary: temporary,
            root,
            uploads,
        };
        host.release("healthy", 'a')?;
        host.container('3', "migrate", 'c', "exited", None)?;
        host.container('4', "db", 'd', "running", Some("healthy"))?;
        host.images(&[
            ('a', "garmin-ai", "current"),
            ('b', "garmin-ai", "rollback"),
            ('c', "garmin-ai", "old"),
            ('c', "garmin-ai", "old-alias"),
            ('d', "timescale/db", "latest"),
        ])?;
        Ok(host)
    }

    fn container(
        &mut self,
        container: char,
        service: &str,
        tag: char,
        status: &str,
        health: Option<&str>,
    ) -> TestResult {
        let id = id(container);
        let path = self.root.join(format!("container-{id}"));
        if !path.exists() {
            use io::Write as _;
            let mut listed = File::options()
                .create(true)
                .append(true)
                .open(self.root.join("containers"))?;
            writeln!(listed, "{id}")?;
        }
        let row = json!({
            "id": id, "service": service, "image": image(tag),
            "status": status, "health": health, "exit_code": 0,
        });
        fs::write(path, format!("{row}\n"))?;
        Ok(())
    }

    fn release(&mut self, health: &str, tag: char) -> TestResult {
        self.container('1', "api", tag, "running", Some(health))?;
        self.container('2', "worker", tag, "running", Some(health))
    }

    fn images(&self, entries: &[(char, &str, &str)]) -> TestResult {
        let mut output = String::new();
        for (digit, repo, tag) in entries {
            let digest = image(*digit);
            let row = json!({"id": digest, "repository": repo, "tag": tag});
            output.push_str(&row.to_string());
            output.push('\n');
            let reference = format!("{repo}:{tag}");
            if !reference.contains('/') {
                fs::write(self.root.join(reference), format!("{digest}\n"))?;
            }
        }
        fs::write(self.root.join("images"), output)?;
        Ok(())
    }

    fn state(&self) -> PathBuf {
        self.root.join("state/state.json")
    }

    fn with_legacy_state(&self) -> TestResult {
        fs::create_dir_all(self.root.join("state"))?;
        fs::write(
            self.state(),
            format!(
                "{}\n",
                json!({
                    "archive_dirs": [self.uploads], "current": image('a'),
                    "rollback": image('b'), "version": 1,
                })
            ),
        )?;
        Ok(())
    }

    fn invocation(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_garmin-ai-release-cleanup"));
        command
            .env("PATH", format!("{}:/usr/bin:/bin", self.root.display()))
            .env("CLEANUP_FIXTURE", &self.root)
            .env_remove("DOCKER_HOST")
            .env("DOCKER_CONTEXT", "foreign-fixture")
            .args(["--state-dir"])
            .arg(self.root.join("state"))
            .args(["--lock-file"])
            .arg(self.root.join("lock"));
        command
    }

    fn run(&self, args: &[&str]) -> Result<(Output, Value), Box<dyn Error>> {
        let output = self.invocation().args(args).output()?;
        let report = serde_json::from_slice(&output.stdout)?;
        Ok((output, report))
    }
}

fn archive(path: &Path, entry: &str, tags: &[&str], old: bool) -> TestResult {
    let file = File::create(path)?;
    let mut tar = tar::Builder::new(GzEncoder::new(file, Compression::fast()));
    let manifest = serde_json::to_vec(&json!([
        {"Config": "synthetic", "Layers": [], "RepoTags": tags}
    ]))?;
    let mut header = tar::Header::new_ustar();
    header.set_size(manifest.len() as u64);
    header.set_mode(0o600);
    header.set_cksum();
    tar.append_data(&mut header, entry, manifest.as_slice())?;
    let file = tar.into_inner()?.finish()?;
    if old {
        let modified = SystemTime::now() - Duration::from_secs(2 * 86400);
        file.set_times(FileTimes::new().set_modified(modified))?;
    }
    Ok(())
}

#[test]
fn public_command_previews_records_and_cleans_only_obsolete_release_files() -> TestResult {
    let host = Host::new()?;
    let obsolete = host.uploads.join("garmin-ai-old.tar.gz");
    archive(&obsolete, "manifest.json", &["garmin-ai:old"], true)?;
    let preserved = [
        (
            "garmin-ai-new.tar.gz",
            "manifest.json",
            "garmin-ai:new",
            false,
        ),
        (
            "garmin-ai-db.tar.gz",
            "manifest.json",
            "timescale/db:latest",
            true,
        ),
        (
            "garmin-ai-private.tar.gz",
            "personal.json",
            "garmin-ai:old",
            true,
        ),
    ];
    for (name, entry, tag, old) in preserved {
        archive(&host.uploads.join(name), entry, &[tag], old)?;
    }
    let private = host.uploads.join("backups");
    fs::create_dir(&private)?;
    let private_archive = private.join("garmin-ai-private.tar.gz");
    archive(&private_archive, "manifest.json", &["garmin-ai:old"], true)?;
    let symlink_path = host.uploads.join("garmin-ai-symlink.tar.gz");
    symlink(&private_archive, &symlink_path)?;
    let hardlink_path = host.uploads.join("garmin-ai-hardlink.tar.gz");
    fs::hard_link(&private_archive, &hardlink_path)?;
    let corrupt = host.uploads.join("garmin-ai-corrupt.tar.gz");
    fs::write(&corrupt, b"synthetic corrupt file")?;
    File::options().write(true).open(&corrupt)?.set_times(
        FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(2 * 86400)),
    )?;

    let uploads = host.uploads.to_str().ok_or("fixture path must be UTF-8")?;
    let arguments = [
        "--initialize-rollback",
        "garmin-ai:rollback",
        "--archive-dir",
        uploads,
    ];
    let (output, preview) = host.run(&arguments)?;
    assert!(output.status.success(), "{preview}");
    assert!(!host.state().exists());
    assert_eq!(
        preview["images"],
        json!([["garmin-ai:old", "garmin-ai:old-alias"]])
    );
    assert_eq!(preview["archives"], json!([obsolete]));
    assert!(obsolete.exists());

    let (output, record) = host.run(&[&arguments[..], &["--apply", "--record-only"]].concat())?;
    assert!(output.status.success(), "{record}");
    assert_eq!(record["archives"], json!([]));
    let state: Value = serde_json::from_slice(&fs::read(host.state())?)?;
    assert_eq!(state["rollback"], image('b'));
    assert_eq!(
        fs::metadata(host.state())?.permissions().mode() & 0o777,
        0o600
    );
    assert!(!host.root.join("deletions").exists());

    let (output, report) = host.run(&["--apply"])?;
    assert!(output.status.success(), "{report}");
    assert_eq!(report["archives"], json!([obsolete]));
    assert!(!obsolete.exists());
    for (name, _, _, _) in preserved {
        assert!(host.uploads.join(name).exists());
    }
    for path in [private_archive, symlink_path, hardlink_path, corrupt] {
        assert!(path.exists());
    }
    let actions = fs::read_to_string(host.root.join("deletions"))?;
    assert_eq!(
        actions,
        format!(
            "container rm {}\nimage rm --no-prune garmin-ai:old garmin-ai:old-alias\n",
            id('3'),
        ),
    );
    Ok(())
}

#[test]
fn legacy_retention_only_advances_after_healthy_rollout() -> TestResult {
    let mut host = Host::new()?;
    host.with_legacy_state()?;
    let original = fs::read(host.state())?;

    host.release("unhealthy", 'a')?;
    let (output, report) = host.run(&["--apply", "--record-only"])?;
    assert!(output.status.success(), "{report}");
    assert_eq!(fs::read(host.state())?, original);

    host.release("unhealthy", 'c')?;
    let (output, report) = host.run(&["--apply", "--record-only"])?;
    assert_eq!(output.status.code(), Some(1), "{report}");
    assert_eq!(fs::read(host.state())?, original);

    host.release("healthy", 'c')?;
    let (output, report) = host.run(&["--apply", "--record-only"])?;
    assert!(output.status.success(), "{report}");
    let current: Value = serde_json::from_slice(&fs::read(host.state())?)?;
    assert_eq!(current["current"], image('c'));
    assert_eq!(current["rollback"], image('a'));
    assert!(!host.root.join("deletions").exists());
    Ok(())
}

#[test]
fn unrelated_alias_and_foreign_container_keep_images_and_migration() -> TestResult {
    let mut host = Host::new()?;
    host.with_legacy_state()?;
    host.container('5', "api", 'c', "running", Some("healthy"))?;
    fs::write(host.root.join(format!("foreign-{}", id('5'))), [])?;
    host.images(&[
        ('a', "garmin-ai", "current"),
        ('b', "garmin-ai", "rollback"),
        ('c', "garmin-ai", "old"),
        ('e', "garmin-ai", "shared"),
        ('e', "another-repository", "alias"),
    ])?;
    let (output, report) = host.run(&["--apply"])?;
    assert!(output.status.success(), "{report}");
    assert_eq!(report["images"], json!([]));
    assert!(!host.root.join("deletions").exists());
    Ok(())
}

#[test]
fn remote_docker_endpoint_cannot_drive_local_retention() -> TestResult {
    let host = Host::new()?;
    host.with_legacy_state()?;
    let output = host
        .invocation()
        .env("DOCKER_HOST", "ssh://synthetic.invalid")
        .arg("--apply")
        .output()?;
    let report: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(output.status.code(), Some(1), "{report}");
    assert_eq!(
        report["error"],
        "Release cleanup requires a local Docker socket"
    );
    assert!(!host.root.join("deletions").exists());
    Ok(())
}

#[test]
fn held_lock_invalid_inventory_and_replaced_tag_prevent_deletion() -> TestResult {
    let host = Host::new()?;
    host.with_legacy_state()?;
    let lock = File::create(host.root.join("lock"))?;
    flock(&lock, FlockOperation::LockExclusive)?;
    let (output, report) = host.run(&["--apply"])?;
    assert!(output.status.success(), "{report}");
    assert_eq!(report["skipped"], "deployment or cleanup lock is held");
    drop(lock);

    let fault = host.root.join("fail-inventory");
    fs::write(&fault, [])?;
    let (output, report) = host.run(&["--apply"])?;
    assert_eq!(output.status.code(), Some(1), "{report}");
    assert_eq!(report["error"], "Docker command failed: image ls");
    assert!(output.stderr.is_empty());
    assert!(!host.root.join("deletions").exists());

    fs::remove_file(fault)?;
    fs::write(host.root.join("garmin-ai:old"), format!("{}\n", image('d')))?;
    let (output, report) = host.run(&["--apply"])?;
    assert_eq!(output.status.code(), Some(1), "{report}");
    assert_eq!(report["error"], "Image tag changed during cleanup");
    let actions = fs::read_to_string(host.root.join("deletions"))?;
    assert_eq!(actions, format!("container rm {}\n", id('3')));
    Ok(())
}
