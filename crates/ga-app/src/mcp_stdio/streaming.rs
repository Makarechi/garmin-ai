use std::io::{self, Write};

use rmcp::{
    ErrorData,
    service::{RxJsonRpcMessage, TxJsonRpcMessage},
    transport::{Transport, async_rw::JsonRpcMessageCodecError},
};
use tokio::{
    io::{AsyncRead, AsyncWrite, AsyncWriteExt},
    sync::{mpsc, oneshot},
    task::JoinHandle,
};
use tokio_util::bytes::BytesMut;

use super::protocol::{NativeServer, Response};

mod input;

const CHUNK_BYTES: usize = 16 * 1024;

enum Output {
    Message(
        Box<TxJsonRpcMessage<NativeServer>>,
        oneshot::Sender<io::Result<()>>,
    ),
}

enum ProtocolError {
    Queue,
    Writing(oneshot::Receiver<io::Result<()>>),
}

pub(super) struct StreamingStdio {
    input: mpsc::Receiver<BytesMut>,
    output: mpsc::Sender<Output>,
    reader: JoinHandle<io::Result<()>>,
    writer: JoinHandle<io::Result<()>>,
    protocol_error: Option<ProtocolError>,
}

impl StreamingStdio {
    pub(super) fn new<R, W>(input: R, output: W) -> Self
    where
        R: AsyncRead + Send + Unpin + 'static,
        W: AsyncWrite + Send + Unpin + 'static,
    {
        let (frames, receive_frames) = mpsc::channel(1);
        let reader = tokio::spawn(input::copy_frames(input, frames));
        let (send, receive) = mpsc::channel(1);
        let writer = tokio::spawn(write_messages(output, receive));
        Self {
            input: receive_frames,
            output: send,
            reader,
            writer,
            protocol_error: None,
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
        loop {
            if let Some(error) = self.protocol_error.as_mut() {
                if matches!(error, ProtocolError::Queue) {
                    let permit = self.output.reserve().await.ok()?;
                    let response = TxJsonRpcMessage::<NativeServer>::error(
                        ErrorData::invalid_request("Invalid request", None),
                        None,
                    );
                    let (done, written) = oneshot::channel();
                    permit.send(Output::Message(Box::new(response), done));
                    *error = ProtocolError::Writing(written);
                }
                if let ProtocolError::Writing(written) = error {
                    written.await.ok()?.ok()?;
                }
                self.protocol_error = None;
            }
            match input::decode(self.input.recv().await?) {
                Ok(Some(message)) => return Some(message),
                Err(JsonRpcMessageCodecError::Serde(error))
                    if matches!(
                        error.classify(),
                        serde_json::error::Category::Data | serde_json::error::Category::Io
                    ) =>
                {
                    self.protocol_error = Some(ProtocolError::Queue);
                }
                Ok(None) | Err(_) => {}
            }
        }
    }

    async fn close(&mut self) -> io::Result<()> {
        self.reader.abort();
        self.writer.abort();
        Ok(())
    }
}

async fn write_messages<W: AsyncWrite + Send + Unpin>(
    mut output: W,
    mut messages: mpsc::Receiver<Output>,
) -> io::Result<()> {
    while let Some(message) = messages.recv().await {
        match message {
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
