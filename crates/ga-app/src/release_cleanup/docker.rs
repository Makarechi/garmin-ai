use std::collections::BTreeMap;
use std::process::Stdio;
use std::time::Duration;

use serde::Deserialize;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::time::timeout;

use crate::{Failure, Result};

const OUTPUT_LIMIT: u64 = 8 * 1024 * 1024;
const CONTAINER_FORMAT: &str = concat!(
    r#"{"id":{{json .Id}},"image":{{json .Image}},"#,
    r#""service":{{json (index .Config.Labels "com.docker.compose.service")}},"#,
    r#""status":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},"#,
    r#""health":{{if index .State "Health"}}{{json .State.Health.Status}}{{else}}null{{end}}}"#,
);
const IMAGE_FORMAT: &str =
    r#"{"id":{{json .ID}},"repository":{{json .Repository}},"tag":{{json .Tag}}}"#;

#[derive(Deserialize)]
pub(crate) struct Container {
    pub id: String,
    pub image: String,
    service: Option<String>,
    status: String,
    exit_code: i64,
    health: Option<String>,
}

impl Container {
    fn serves(&self, name: &str) -> bool {
        self.service.as_deref() == Some(name)
    }

    pub fn successful_migration(&self) -> bool {
        self.serves("migrate") && self.status == "exited" && self.exit_code == 0
    }
}

#[derive(Deserialize)]
pub(crate) struct ImageRow {
    id: String,
    pub repository: String,
    pub tag: String,
}

pub(crate) async fn command(args: &[&str]) -> Result<String> {
    let operation = args[..2].join(" ");
    let failure = || Failure(format!("Docker command failed: {operation}"));
    let mut child = Command::new("docker")
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| failure())?;
    let stdout = child.stdout.take().ok_or_else(failure)?;
    let output = timeout(Duration::from_secs(600), async {
        let mut output = Vec::new();
        stdout
            .take(OUTPUT_LIMIT + 1)
            .read_to_end(&mut output)
            .await?;
        if output.len() as u64 > OUTPUT_LIMIT || !child.wait().await?.success() {
            return Err(failure());
        }
        String::from_utf8(output).map_err(|_| failure())
    })
    .await;
    match output {
        Ok(result) => result,
        Err(_) => Err(Failure(format!("Docker command timed out: {operation}"))),
    }
}

pub(crate) fn image_identity(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(hex_identity)
}

fn hex_identity(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

pub(crate) async fn containers(project_only: bool) -> Result<Vec<Container>> {
    let mut args = vec!["container", "ls", "-aq", "--no-trunc"];
    if project_only {
        args.extend(["--filter", "label=com.docker.compose.project=garmin-ai"]);
    }
    let listing = command(&args).await?;
    let ids: Vec<_> = listing.split_ascii_whitespace().collect();
    if !ids.iter().all(|id| hex_identity(id)) {
        return Err(Failure::message("Invalid Docker container identity"));
    }
    let mut containers = Vec::new();
    for ids in ids.chunks(128) {
        let mut args = vec!["container", "inspect", "--format", CONTAINER_FORMAT];
        args.extend_from_slice(ids);
        let output = command(&args).await?;
        let inspected: Vec<Container> = output
            .lines()
            .map(serde_json::from_str)
            .collect::<std::result::Result<_, _>>()?;
        if inspected.len() != ids.len()
            || !inspected
                .iter()
                .zip(ids)
                .all(|(container, id)| container.id == *id && image_identity(&container.image))
        {
            return Err(Failure::message("Docker container inventory changed"));
        }
        containers.extend(inspected);
    }
    Ok(containers)
}

pub(crate) async fn current(require_healthy: bool) -> Result<(String, Vec<Container>)> {
    let project = containers(true).await?;
    let mut apps = project
        .iter()
        .filter(|c| c.serves("api") || c.serves("worker"));
    if let (Some(first), Some(second), None) = (apps.next(), apps.next(), apps.next())
        && first.service != second.service
        && first.image == second.image
        && [first, second].iter().all(|c| {
            c.status == "running" && (!require_healthy || c.health.as_deref() == Some("healthy"))
        })
        && !project
            .iter()
            .any(|c| c.serves("migrate") && c.status != "exited")
    {
        return Ok((first.image.clone(), project));
    }
    Err(Failure::message(
        "Cleanup requires healthy api and worker on the same image, with no migration",
    ))
}

pub(crate) async fn images() -> Result<BTreeMap<String, Vec<ImageRow>>> {
    let mut images = BTreeMap::<String, Vec<ImageRow>>::new();
    for line in command(&["image", "ls", "--no-trunc", "--format", IMAGE_FORMAT])
        .await?
        .lines()
    {
        let row: ImageRow = serde_json::from_str(line)?;
        if !image_identity(&row.id) {
            return Err(Failure::message("Invalid Docker image identity"));
        }
        images.entry(row.id.clone()).or_default().push(row);
    }
    Ok(images)
}

pub(crate) async fn inspect_image(reference: &str) -> Result<String> {
    if reference.is_empty() || reference.starts_with('-') {
        return Err(Failure::message("Invalid rollback image reference"));
    }
    let value = command(&["image", "inspect", "--format", "{{.Id}}", reference]).await?;
    let id = value.trim_end();
    if !image_identity(id) {
        return Err(Failure::message("Invalid Docker image identity"));
    }
    Ok(id.to_owned())
}
