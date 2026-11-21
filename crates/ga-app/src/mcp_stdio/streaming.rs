use std::io::{self, Write};

use rmcp::{
    service::{RxJsonRpcMessage, TxJsonRpcMessage},
    transport::{Transport, async_rw::AsyncRwTransport},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader, DuplexStream},
    sync::{mpsc, oneshot},
    task::JoinHandle,
};

use super::protocol::{NativeServer, Response};

mod input;

const CHUNK_BYTES: usize = 16 * 1024;

enum Output {
    Message(
        Box<TxJsonRpcMessage<NativeServer>>,
        oneshot::Sender<io::Result<()>>,
    ),
    CodecError(Vec<u8>),
}

pub(super) struct StreamingStdio {
    input: AsyncRwTransport<NativeServer, DuplexStream, DuplexStream>,
    output: mpsc::Sender<Output>,
    reader: JoinHandle<io::Result<()>>,
    writer: JoinHandle<io::Result<()>>,
    errors: JoinHandle<()>,
}

impl StreamingStdio {
    pub(super) fn new<R, W>(input: R, output: W) -> Self
    where
        R: AsyncRead + Send + Unpin + 'static,
        W: AsyncWrite + Send + Unpin + 'static,
    {
        let (input_writer, normalized_input) = tokio::io::duplex(CHUNK_BYTES);
        let reader = tokio::spawn(input::copy_frames(input, input_writer));
        let (codec_writer, codec_reader) = tokio::io::duplex(1024);
        let (send, receive) = mpsc::channel(1);
        let writer = tokio::spawn(write_messages(output, receive));
        let error_output = send.clone();
        let errors = tokio::spawn(async move {
            let mut reader = BufReader::new(codec_reader);
            loop {
                // The SDK emits fixed, redacted protocol errors on this private pipe.
                let mut line = Vec::new();
                match reader.read_until(b'\n', &mut line).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        if error_output.send(Output::CodecError(line)).await.is_err() {
                            break;
                        }
                    }
                }
            }
        });
        Self {
            input: AsyncRwTransport::new(normalized_input, codec_writer)
                .with_max_line_length(input::MAX_NORMALIZED_BYTES),
            output: send,
            reader,
            writer,
            errors,
        }
    }
}

impl Transport<NativeServer> for StreamingStdio {
    type Error = io::Error;

    fn send(
        &mut self,
        message: TxJsonRpcMessage<NativeServer>,
    ) -> impl Future<Output = io::Result<()>> + Send + 'static {
        let output = self.output.clone();
        async move {
            let (done, finished) = oneshot::channel();
            output
                .send(Output::Message(Box::new(message), done))
                .await
                .map_err(|_| closed())?;
            finished.await.map_err(|_| closed())?
        }
    }

    async fn receive(&mut self) -> Option<RxJsonRpcMessage<NativeServer>> {
        self.input.receive().await
    }

    async fn close(&mut self) -> io::Result<()> {
        let result = self.input.close().await;
        self.reader.abort();
        self.errors.abort();
        self.writer.abort();
        result
    }
}

async fn write_messages<W: AsyncWrite + Send + Unpin>(
    mut output: W,
    mut messages: mpsc::Receiver<Output>,
) -> io::Result<()> {
    while let Some(message) = messages.recv().await {
        match message {
            Output::CodecError(bytes) => output.write_all(&bytes).await?,
            Output::Message(message, done) => {
                let (send, mut chunks) = mpsc::channel(2);
                let encoder = tokio::task::spawn_blocking(move || {
                    let mut writer = ChunkWriter {
                        send,
                        buffer: Vec::with_capacity(CHUNK_BYTES),
                    };
                    if let rmcp::model::JsonRpcMessage::Response(response) = &*message
                        && let Response::Tool(result) = &response.result
                    {
                        writer.write_all(b"{\"jsonrpc\":")?;
                        serde_json::to_writer(&mut writer, &response.jsonrpc)
                            .map_err(io::Error::other)?;
                        writer.write_all(b",\"id\":")?;
                        serde_json::to_writer(&mut writer, &response.id)
                            .map_err(io::Error::other)?;
                        writer.write_all(b",\"result\":")?;
                        result.write_json(&mut writer)?;
                        writer.write_all(b"}")?;
                    } else {
                        serde_json::to_writer(&mut writer, &message).map_err(io::Error::other)?;
                    }
                    writer.write_all(b"\n")?;
                    writer.flush()
                });
                let mut result = Ok(());
                while let Some(chunk) = chunks.recv().await {
                    if let Err(error) = output.write_all(&chunk).await {
                        result = Err(error);
                        break;
                    }
                }
                drop(chunks);
                let encoded = encoder
                    .await
                    .map_err(|_| io::Error::other("MCP encoder stopped"))?;
                if result.is_ok() {
                    result = encoded;
                }
                let failed = result.is_err();
                let _ = done.send(result);
                if failed {
                    return Err(closed());
                }
            }
        }
        output.flush().await?;
    }
    Ok(())
}

struct ChunkWriter {
    send: mpsc::Sender<Vec<u8>>,
    buffer: Vec<u8>,
}

impl Write for ChunkWriter {
    fn write(&mut self, mut bytes: &[u8]) -> io::Result<usize> {
        let length = bytes.len();
        while !bytes.is_empty() {
            let count = bytes.len().min(CHUNK_BYTES - self.buffer.len());
            self.buffer.extend_from_slice(&bytes[..count]);
            bytes = &bytes[count..];
            if self.buffer.len() == CHUNK_BYTES {
                self.flush()?;
            }
        }
        Ok(length)
    }

    fn flush(&mut self) -> io::Result<()> {
        if !self.buffer.is_empty() {
            let bytes = std::mem::replace(&mut self.buffer, Vec::with_capacity(CHUNK_BYTES));
            self.send.blocking_send(bytes).map_err(|_| closed())?;
        }
        Ok(())
    }
}

fn closed() -> io::Error {
    io::Error::new(io::ErrorKind::BrokenPipe, "MCP output closed")
}
