use std::{
    cell::RefCell,
    future::Future,
    time::{Duration, Instant},
};

use serde::Serialize;

const MAX_STAGES: usize = 64;

tokio::task_local! {
    static REQUEST: RefCell<RequestLatency>;
}

#[derive(Serialize)]
pub struct RequestLatency {
    #[serde(skip)]
    start: Instant,
    processing_ms: u64,
    stages: Vec<RecordedStage>,
    omitted_stages: usize,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    PostgresAnalysisRead,
    PostgresSleepRead,
    PostgresSleepQuery,
    GeminiHttp,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Ok,
    Error,
    QueryCanceled,
    Timeout,
    Cancelled,
}

#[derive(Serialize)]
struct RecordedStage {
    stage: Stage,
    #[serde(skip_serializing_if = "Option::is_none")]
    attempt: Option<u8>,
    offset_ms: u64,
    duration_ms: u64,
    outcome: Outcome,
}

/// Keep independent records for requests sharing the same worker runtime.
pub async fn collect<F: Future>(process: F) -> (F::Output, RequestLatency) {
    REQUEST
        .scope(
            RefCell::new(RequestLatency {
                start: Instant::now(),
                processing_ms: 0,
                stages: Vec::new(),
                omitted_stages: 0,
            }),
            async {
                let result = process.await;
                let record = REQUEST.with(|record| {
                    let mut record = record.borrow_mut();
                    let processing_ms = millis(record.start.elapsed());
                    let mut stages = std::mem::take(&mut record.stages);
                    stages.sort_by_key(|stage| stage.offset_ms);
                    RequestLatency {
                        start: record.start,
                        processing_ms,
                        stages,
                        omitted_stages: record.omitted_stages,
                    }
                });
                (result, record)
            },
        )
        .await
}

/// Measure one operation inside a request. A dropped timer is recorded as cancelled.
pub struct Timer {
    stage: Stage,
    attempt: Option<u8>,
    start: Option<Instant>,
    outcome: Outcome,
}

impl Timer {
    #[must_use]
    pub fn start(stage: Stage, attempt: Option<u8>) -> Self {
        Self {
            stage,
            attempt,
            start: REQUEST.try_with(|_| Instant::now()).ok(),
            outcome: Outcome::Cancelled,
        }
    }

    pub fn finish<T, E>(
        mut self,
        result: Result<T, E>,
        on_error: impl FnOnce(&E) -> Outcome,
    ) -> Result<T, E> {
        if self.start.is_some() {
            self.outcome = result.as_ref().map_or_else(on_error, |_| Outcome::Ok);
        }
        result
    }
}

impl Drop for Timer {
    fn drop(&mut self) {
        let Some(start) = self.start else { return };
        let duration_ms = millis(start.elapsed());
        let _ = REQUEST.try_with(|record| {
            let mut record = record.borrow_mut();
            if record.stages.len() == MAX_STAGES {
                record.omitted_stages = record.omitted_stages.saturating_add(1);
                return;
            }
            let offset_ms = millis(start.saturating_duration_since(record.start));
            record.stages.push(RecordedStage {
                stage: self.stage,
                attempt: self.attempt,
                offset_ms,
                duration_ms,
                outcome: self.outcome,
            });
        });
    }
}

fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}
