use std::{num::NonZeroU64, sync::Arc, time::Duration};

use ga_archive::GarminTokenFile;
use reqwest::{Response, StatusCode, Url};
use tokio::{io::AsyncWrite, sync::Mutex};

use super::{
    Hosts, endpoint,
    query::LegacyRead,
    session::{LegacySession, Native, RestorePolicy},
};
use crate::{GarminError, Schedule, TransportConfig, read_json_body, stream_download, successful};

#[derive(Clone)]
pub struct LegacyReader {
    inner: Arc<Inner>,
}

struct Inner {
    native: Native,
    store: GarminTokenFile,
    expected_fingerprint: String,
    pacing: Duration,
    state: Mutex<State>,
}

struct State {
    session: LegacySession,
    schedule: Schedule,
}

impl LegacyReader {
    pub async fn restore(
        store: GarminTokenFile,
        expected_fingerprint: &str,
        config: TransportConfig,
    ) -> Result<Self, GarminError> {
        Self::with_hosts(store, expected_fingerprint, config, Hosts::production()?).await
    }

    pub(super) async fn with_hosts(
        store: GarminTokenFile,
        expected_fingerprint: &str,
        config: TransportConfig,
        hosts: Hosts,
    ) -> Result<Self, GarminError> {
        let native = Native::new(hosts, config)?;
        let session = native
            .restore(&store, expected_fingerprint, RestorePolicy::IfNeeded)
            .await?;
        Ok(Self {
            inner: Arc::new(Inner {
                native,
                store,
                expected_fingerprint: expected_fingerprint.to_owned(),
                pacing: config.pacing,
                state: Mutex::new(State {
                    session,
                    schedule: Schedule::new(),
                }),
            }),
        })
    }

    /// Return the original JSON bytes from an explicitly supported read.
    pub async fn get_json(
        &self,
        resource: LegacyRead,
        max_bytes: u64,
    ) -> Result<Option<Vec<u8>>, GarminError> {
        let response = self
            .read("application/json", |base, name| resource.url(base, name))
            .await?;
        read_json_body(response, max_bytes).await
    }

    /// Stream the original export to caller-owned staging.
    pub async fn download_activity(
        &self,
        id: NonZeroU64,
        max_bytes: u64,
        destination: &mut (impl AsyncWrite + Unpin),
    ) -> Result<u64, GarminError> {
        let response = self
            .read("*/*", |base, _| activity_export(base, id))
            .await?;
        stream_download(response, max_bytes, destination).await
    }

    async fn read(
        &self,
        accept: &str,
        url: impl Fn(&Url, Option<&str>) -> Result<Url, GarminError>,
    ) -> Result<Response, GarminError> {
        let mut state = self.inner.state.lock().await;
        state.schedule.check()?;
        let mut rotated = state.session.expires_soon();
        if rotated {
            self.refresh(&mut state).await?;
        }
        loop {
            let client = &self.inner.native.http;
            let base = &self.inner.native.hosts.api;
            let url = url(base, state.session.display_name())?;
            let request = state.session.authorized_read(client, url, accept)?;
            let response = state.schedule.send(self.inner.pacing, request).await?;
            if response.status() == StatusCode::UNAUTHORIZED && !rotated {
                self.refresh(&mut state).await?;
                rotated = true;
                continue;
            }
            return successful(response);
        }
    }

    async fn refresh(&self, state: &mut State) -> Result<(), GarminError> {
        let result = self
            .inner
            .native
            .restore(
                &self.inner.store,
                &self.inner.expected_fingerprint,
                RestorePolicy::Refresh,
            )
            .await;
        if let Err(GarminError::RateLimited { retry_after }) = result {
            state.schedule.cooldown(retry_after);
        }
        state.session = result?;
        Ok(())
    }
}

pub(super) fn activity_export(base: &Url, id: NonZeroU64) -> Result<Url, GarminError> {
    endpoint(base, &format!("download-service/files/activity/{id}"))
}
