mod terminal;

use std::{error::Error, path::PathBuf};

use ga_archive::{
    Archive, GarminTokenFile,
    storage_files::{StorageAccess, StorageFilesGuard, validate_backup_layout},
};
use ga_db::{
    Database,
    garmin_owner::{GarminSetupError, LocalGarminSetupMode},
};
use ga_garmin::{
    GarminError, TransportConfig,
    legacy::{LegacyLogin, LegacySession, LoginProgress},
};

use crate::configuration;
use terminal::Terminal;

type CommandResult = Result<(), Box<dyn Error>>;

pub(crate) fn login(confirm_existing_owner: bool) -> CommandResult {
    run(LocalGarminSetupMode::Login {
        confirm_existing_owner,
    })?;
    println!("Garmin login saved locally. Password is not stored by this application.");
    Ok(())
}

pub(crate) fn enroll() -> CommandResult {
    run(LocalGarminSetupMode::Enroll)?;
    println!("Existing-owner account binding verified. Other accounts cannot replace it.");
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn run(mode: LocalGarminSetupMode) -> CommandResult {
    let [url, data, tokens, locks, backups] = configuration::read([
        "GA_DATABASE_URL",
        "GA_DATA_DIR",
        "GA_TOKEN_DIR",
        "GA_LOCK_DIR",
        "GA_BACKUP_DIR",
    ])?;
    let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let token_dir = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let backups = PathBuf::from(backups.unwrap_or_else(|| "backups".into()));
    let locks = configuration::lock_directory(&data, &token_dir, locks.map(PathBuf::from))?;
    validate_backup_layout(&data, &token_dir, &locks, &backups)?;
    let access = match mode {
        LocalGarminSetupMode::Login { .. } => StorageAccess::Recovery,
        _ => StorageAccess::Active,
    };
    let _files = StorageFilesGuard::acquire(&locks, access)?;
    let retained = Archive::has_retained_entries(&data.join("raw"))?;
    let setup = match url.filter(|value| !value.is_empty()) {
        Some(url) => {
            let db =
                Database::connect_lazy(&url).map_err(|_| "Storage configuration is invalid")?;
            Some(db.acquire_local_garmin_setup(mode).await?)
        }
        None => match mode {
            LocalGarminSetupMode::Enroll | LocalGarminSetupMode::ImportProbe { .. } => {
                return Err("Local enrollment requires a database".into());
            }
            LocalGarminSetupMode::Login {
                confirm_existing_owner: false,
            } if retained => {
                return Err("Confirm ownership of retained raw storage locally".into());
            }
            LocalGarminSetupMode::Login { .. } | LocalGarminSetupMode::Probe => None,
        },
    };
    let client = TransportConfig::default();
    let candidate = if let LocalGarminSetupMode::Login { .. } = mode {
        authenticate(&mut Terminal::open()?, client).await?
    } else {
        let store = GarminTokenFile::open_existing(&token_dir)?
            .ok_or(GarminError::AuthenticationRequired)?;
        LegacySession::restore_for_local_enrollment(&store, client).await?
    };
    let fingerprint = candidate.fingerprint();
    let publish = || {
        let store =
            GarminTokenFile::open(&token_dir).map_err(|_| GarminSetupError::TokenPublication)?;
        candidate
            .save_for_owner(&store, &fingerprint)
            .map_err(|_| GarminSetupError::TokenPublication)
    };
    if let Some(mut setup) = setup {
        setup.publish(&fingerprint, retained, publish).await?;
    } else {
        publish()?;
    }
    Ok(())
}

async fn authenticate(
    terminal: &mut Terminal,
    config: TransportConfig,
) -> Result<LegacySession, Box<dyn Error>> {
    let email = terminal.input("Garmin email: ", false, 1024)?;
    let progress = {
        let password = terminal.input("Garmin password: ", true, 1024)?;
        LegacyLogin::new(config)?
            .start(email.trim(), &password)
            .await?
    };
    match progress {
        LoginProgress::Complete(session) => Ok(session),
        LoginProgress::Mfa(mut challenge) => {
            for _ in 0..3 {
                let code = terminal.input("Garmin MFA code: ", true, 20)?;
                match challenge.verify(code.trim()).await {
                    Ok(session) => return Ok(session),
                    Err(GarminError::InvalidMfaCode) => {}
                    Err(error) => return Err(error.into()),
                }
            }
            Err(GarminError::InvalidMfaCode.into())
        }
    }
}
