const http = require('http');

const bot = '/bot100:synthetic-voice';
const audio = 'OggS';
const server = http.createServer((request, response) => {
  if (request.method === 'POST' && request.url === '/bot100:synthetic-rate-limit/getFile') {
    response.writeHead(429, { 'Content-Type': 'application/json', 'Retry-After': '13' });
    return response.end(JSON.stringify({ ok: false, parameters: { retry_after: 13 } }));
  }
  if (request.method === 'POST' && request.url === bot + '/getFile') {
    response.setHeader('Content-Type', 'application/json');
    return response.end(JSON.stringify({
      ok: true, result: { file_path: 'voice/synthetic.oga', file_size: audio.length },
    }));
  }
  if (request.method === 'GET' && request.url === '/file' + bot + '/voice/synthetic.oga') {
    response.writeHead(200, { 'Content-Type': 'audio/ogg' });
    return response.end(audio);
  }
  response.writeHead(501);
  response.end();
});
server.listen(process.env.PORT || 4500);
