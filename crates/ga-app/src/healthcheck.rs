use std::{
    io::{self, Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    time::{Duration, Instant},
};

use ga_db::Database;

use crate::configuration;

const DEADLINE: Duration = Duration::from_secs(1);
const READY_RESPONSE_DEADLINE: Duration = Duration::from_millis(4750);

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn worker() -> bool {
    let Ok([Some(url)]) = configuration::read(["GA_DATABASE_URL"]) else {
        return false;
    };
    match Database::connect_lazy(&url) {
        Ok(database) => database.native_worker_healthy().await.unwrap_or(false),
        Err(_) => false,
    }
}

pub(crate) fn probe(port: u16, ready: bool) -> io::Result<bool> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut connection = TcpStream::connect_timeout(&address, DEADLINE)?;
    connection.set_write_timeout(Some(DEADLINE))?;
    let (endpoint, response_timeout) = if ready {
        ("/health/ready", READY_RESPONSE_DEADLINE)
    } else {
        ("/health/live", DEADLINE)
    };
    write!(
        connection,
        "GET {endpoint} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n"
    )?;
    let deadline = Instant::now() + response_timeout;
    let mut status = [0; 128];
    let mut used = 0;
    let line_end = loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() || used == status.len() {
            return Ok(false);
        }
        connection.set_read_timeout(Some(remaining))?;
        let read = connection.read(&mut status[used..])?;
        if read == 0 {
            return Ok(false);
        }
        if let Some(index) = status[used..used + read]
            .iter()
            .position(|byte| *byte == b'\n')
        {
            break used + index + 1;
        }
        used += read;
    };
    let status = &status[..line_end];
    let line = std::str::from_utf8(status).unwrap_or_default();
    let mut fields = line.split_ascii_whitespace();
    Ok(status.ends_with(b"\r\n")
        && matches!(fields.next(), Some("HTTP/1.0" | "HTTP/1.1"))
        && fields.next() == Some("200"))
}
