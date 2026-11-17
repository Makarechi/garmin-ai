const http = require('node:http');

const key = 'synthetic-personal-key';
const tokenPath = '/personalapitoken-service/personal-api/personal-api-key/token';
const settingsPath = '/userprofile-service/userprofile/settings';
const settings = '{ "unknownField" : [1, "value"] }';
const activity = Buffer.from('PK\x03\x04synthetic activity contents');
let token;
let issued = 0;

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  const auth = request.headers.authorization;

  // @endpoint POST /personalapitoken-service/personal-api/personal-api-key/token
  if (request.method === 'POST' && url.pathname === tokenPath) {
    if (auth !== `ApiKey ${key}`) {
      response.writeHead(401).end();
      return;
    }
    const exp = Math.floor(Date.now() / 1000) + 900;
    issued += 1;
    const claims = Buffer.from(JSON.stringify({ exp, sub: issued })).toString('base64url');
    token = `eyJhbGciOiJSUzI1NiJ9.${claims}.synthetic`;
    response.writeHead(200, { 'Content-Type': 'text/plain' }).end(token);
    return;
  }

  if (!token || auth !== `Bearer ${token}`) {
    response.writeHead(401).end();
    return;
  }

  // @endpoint GET /userprofile-service/userprofile/settings
  if (request.method === 'GET' && url.pathname === settingsPath) {
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(settings);
    return;
  }

  // @endpoint GET /download-service/files/activity/{activityId}
  if (request.method === 'GET' && url.pathname === '/download-service/files/activity/42') {
    response.writeHead(200, { 'Content-Type': 'application/x-zip-compressed' }).end(activity);
    return;
  }

  if (request.method === 'GET' && url.pathname === '/activity-service/activity/403') {
    response.writeHead(403).end();
    return;
  }

  if (request.method === 'GET' && url.pathname === '/activity-service/activity/429') {
    response.writeHead(429, { 'Retry-After': '73' }).end();
    return;
  }

  response.writeHead(501).end();
});

server.listen(Number(process.env.PORT || 4501), '127.0.0.1');
