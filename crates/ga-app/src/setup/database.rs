use std::collections::BTreeSet;

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, percent_decode_str, utf8_percent_encode};
use reqwest::Url;

use super::{ValueMap, configured, random};

const HOST_KEY: &str = "GA_DATABASE_URL";
const CONTAINER_KEY: &str = "GA_CONTAINER_DATABASE_URL";
const PASSWORD_KEY: &str = "GA_POSTGRES_PASSWORD";
const HOST_ERROR: &str =
    "Host database endpoint must use postgresql+psycopg at 127.0.0.1 and GA_DB_PORT";
const CONTAINER_ERROR: &str = "Container database endpoint must use postgresql+psycopg at db:5432";
const PASSWORD_ENCODE: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'_')
    .remove(b'.')
    .remove(b'~');

pub(super) fn prepare(values: &mut ValueMap, port: u16) -> Result<(), &'static str> {
    let database = configured(values, HOST_KEY)
        .map(|value| Url::parse(value).map_err(|_| HOST_ERROR))
        .transpose()?;
    let entered = configured(values, PASSWORD_KEY);
    let password = match (&database, entered) {
        (Some(url), entered) => {
            let decoded =
                password_from(url).ok_or("Configured database URL requires a password")?;
            if entered.is_some_and(|entered| entered != decoded) {
                return Err(
                    "Database URL and GA_POSTGRES_PASSWORD disagree; existing values were not changed",
                );
            }
            decoded
        }
        (None, Some(password)) => password.to_owned(),
        (None, None) => URL_SAFE_NO_PAD.encode(random::<32>()?),
    };
    let database = match database {
        Some(url) => url,
        None => {
            let encoded = utf8_percent_encode(&password, PASSWORD_ENCODE);
            Url::parse(&format!(
                "postgresql+psycopg://garmin:{encoded}@127.0.0.1:{port}/garmin_ai"
            ))
            .map_err(|_| HOST_ERROR)?
        }
    };
    if !endpoint(&database, "127.0.0.1", port) {
        return Err(HOST_ERROR);
    }
    let container = match configured(values, CONTAINER_KEY) {
        Some(value) => Url::parse(value).map_err(|_| CONTAINER_ERROR)?,
        None => {
            let mut url = database.clone();
            url.set_host(Some("db")).map_err(|_| CONTAINER_ERROR)?;
            url.set_port(Some(5432)).map_err(|()| CONTAINER_ERROR)?;
            url
        }
    };
    if !endpoint(&container, "db", 5432) {
        return Err(CONTAINER_ERROR);
    }
    for url in [&database, &container] {
        if url.username() != "garmin" || url.path() != "/garmin_ai" {
            return Err("Compose requires database user garmin and database garmin_ai");
        }
        if password_from(url).as_deref() != Some(&password) {
            return Err(
                "Host and container database credentials disagree; existing values were not changed",
            );
        }
        validate_query(url)?;
    }
    if configured(values, HOST_KEY).is_none() {
        values.insert(HOST_KEY.into(), database.into());
    }
    if configured(values, CONTAINER_KEY).is_none() {
        values.insert(CONTAINER_KEY.into(), container.into());
    }
    values.insert(PASSWORD_KEY.into(), password);
    Ok(())
}

fn endpoint(url: &Url, host: &str, port: u16) -> bool {
    url.scheme() == "postgresql+psycopg"
        && url.host_str() == Some(host)
        && url.port() == Some(port)
        && url.fragment().is_none()
}

fn password_from(url: &Url) -> Option<String> {
    percent_decode_str(url.password().filter(|password| !password.is_empty())?)
        .decode_utf8()
        .ok()
        .map(|value| value.into_owned())
}

fn validate_query(url: &Url) -> Result<(), &'static str> {
    let mut seen = BTreeSet::new();
    for (option, value) in url.query_pairs() {
        match option.as_ref() {
            "sslmode" if matches!(value.as_ref(), "disable" | "allow" | "prefer") => {}
            "sslmode" => {
                return Err(
                    "Bundled database sslmode must allow non-TLS connections: disable, allow or prefer",
                );
            }
            "connect_timeout"
                if value.bytes().all(|byte| byte.is_ascii_digit())
                    && value.parse::<i32>().is_ok() => {}
            "connect_timeout" => {
                return Err("Database connect_timeout must be a nonnegative 32-bit integer");
            }
            "application_name" if value.len() <= 63 && !value.contains('\0') => {}
            "application_name" => {
                return Err(
                    "Database application_name must be at most 63 bytes and contain no NUL",
                );
            }
            _ => {
                return Err(
                    "Database URL query may only contain sslmode, connect_timeout and application_name",
                );
            }
        }
        if !seen.insert(option) {
            return Err("Database query options must occur only once");
        }
    }
    Ok(())
}
