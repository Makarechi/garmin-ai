use std::{
    io::{self, Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    time::{Duration, Instant},
};

const DEADLINE: Duration = Duration::from_secs(1);

pub(crate) fn probe(port: u16, ready: bool) -> io::Result<bool> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut connection = TcpStream::connect_timeout(&address, DEADLINE)?;
    connection.set_write_timeout(Some(DEADLINE))?;
    let endpoint = if ready {
        "/health/ready"
    } else {
        "/health/live"
    };
    write!(
        connection,
        "GET {endpoint} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n"
    )?;
    let deadline = Instant::now() + DEADLINE;
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
