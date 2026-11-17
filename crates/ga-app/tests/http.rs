use std::{
    io::{self, BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    process::{Child, Command, Stdio},
    time::Duration,
};

struct Server {
    process: Child,
    address: SocketAddr,
}

impl Server {
    fn start() -> Result<Self, Box<dyn std::error::Error>> {
        let mut process = Command::new(env!("CARGO_BIN_EXE_garmin-ai-rust"))
            .env_clear()
            .args(["serve", "--port", "0"])
            .stdout(Stdio::piped())
            .spawn()?;
        let address = (|| -> Result<SocketAddr, Box<dyn std::error::Error>> {
            let stdout = process
                .stdout
                .take()
                .ok_or_else(|| io::Error::other("Missing stdout"))?;
            let mut line = String::new();
            BufReader::new(stdout).read_line(&mut line)?;
            let address = line
                .trim()
                .strip_prefix("Listening on http://")
                .ok_or_else(|| io::Error::other("Missing liveness listener address"))?;
            Ok(address.parse()?)
        })();
        match address {
            Ok(address) => Ok(Self { process, address }),
            Err(error) => {
                let _ = process.kill();
                let _ = process.wait();
                Err(error)
            }
        }
    }

    fn request(&self, method: &str, path: &str, additional_headers: &str) -> io::Result<String> {
        let mut stream = TcpStream::connect_timeout(&self.address, Duration::from_secs(3))?;
        stream.set_read_timeout(Some(Duration::from_secs(3)))?;
        write!(
            stream,
            "{method} {path} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: 0\r\n{additional_headers}\r\n",
            self.address
        )?;
        let mut response = String::new();
        stream.read_to_string(&mut response)?;
        Ok(response)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}

fn parts(raw: &str) -> io::Result<(&str, &str)> {
    raw.split_once("\r\n\r\n")
        .ok_or_else(|| io::Error::other("Expected an HTTP response"))
}

#[test]
fn liveness_has_the_python_response_and_cache_policy() -> Result<(), Box<dyn std::error::Error>> {
    let server = Server::start()?;
    assert!(server.address.ip().is_loopback());

    let raw = server.request("GET", "/health/live", "")?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 200 "));
    assert!(headers.contains("content-type: application/json"));
    assert!(!headers.contains("cache-control:"));
    assert_eq!(body, r#"{"status":"alive"}"#);

    let raw = server.request("GET", "/health/live", "Authorization: Bearer synthetic\r\n")?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 200 "));
    assert!(headers.contains("cache-control: no-store"));
    assert_eq!(body, r#"{"status":"alive"}"#);
    Ok(())
}

#[test]
fn unsupported_methods_and_unknown_paths_have_python_error_shapes()
-> Result<(), Box<dyn std::error::Error>> {
    let server = Server::start()?;
    for method in ["HEAD", "POST", "OPTIONS"] {
        let raw = server.request(method, "/health/live", "")?;
        let (headers, body) = parts(&raw)?;
        assert!(headers.starts_with("HTTP/1.1 405 "));
        assert!(headers.contains("allow: GET"));
        assert!(headers.contains("content-type: application/json"));
        if method == "HEAD" {
            assert!(body.is_empty());
            assert!(headers.contains("content-length: 31"));
        } else {
            assert_eq!(body, r#"{"detail":"Method Not Allowed"}"#);
        }
    }

    let raw = server.request("GET", "/missing", "")?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 404 "));
    assert_eq!(body, r#"{"detail":"Not Found"}"#);
    Ok(())
}

#[test]
fn readiness_reports_unavailable_without_a_database() -> Result<(), Box<dyn std::error::Error>> {
    let server = Server::start()?;
    let raw = server.request(
        "GET",
        "/health/ready",
        "Authorization: Bearer synthetic\r\n",
    )?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 503 "));
    assert!(headers.contains("cache-control: no-store"));
    assert_eq!(body, r#"{"detail":"Database unavailable or not migrated"}"#);
    Ok(())
}
