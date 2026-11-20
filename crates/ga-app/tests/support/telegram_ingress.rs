use std::{error::Error, time::Duration};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    channel_ingress::telegram_poll::{TelegramPollError, TelegramPollReceipt},
    runtime_lease::RuntimeLease,
    telegram_cursor::TelegramCursorError,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::Value;
use tokio::time::{sleep, timeout};

pub(super) async fn receive(
    runtime: &mut RuntimeLease,
    binding: TelegramBinding<'_>,
    wire: &Value,
    received: StoredInstant,
    time_known: bool,
) -> Result<TelegramPollReceipt, Box<dyn Error>> {
    Ok(timeout(Duration::from_secs(3), async {
        loop {
            match binding
                .accept_polled_update(runtime, wire, received, time_known)
                .await
            {
                Err(
                    TelegramPollError::Busy | TelegramPollError::Cursor(TelegramCursorError::Busy),
                ) => sleep(Duration::from_millis(5)).await,
                result => return result,
            }
        }
    })
    .await??)
}
