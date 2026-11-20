const http = require('node:http');
const routes = require('../../../migration/contracts/garmin-legacy-requests.json');

const profile = { profileId: 12345, displayName: 'Synthetic Profile' };
const document = {
  synthetic_integer: 42,
  synthetic_float: 1.25,
  synthetic_scalar: 'do not disclose this invented value',
  variants: [true, false, null, {}, [], {}, { inner: [{ leaf: 'value' }] }],
};
const activities = [{ activityId: 42, synthetic_scalar: 'do not disclose this invented value' }];
routes.activities[1].start = '0';

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  const mode = request.headers['x-fixture-mode'];
  if (request.method !== 'GET') {
    response.writeHead(501).end();
    return;
  }
  if (!request.headers.authorization?.startsWith('Bearer e30.')) {
    response.writeHead(401).end();
    return;
  }
  if (url.pathname === '/userprofile-service/socialProfile') {
    const identity = mode === 'different-owner' ? { ...profile, profileId: 3467 } : profile;
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(identity));
    return;
  }
  const endpoint = Object.entries(routes).find(([, [path, parameters]]) =>
    path === url.pathname
    && [...url.searchParams].length === Object.keys(parameters).length
    && Object.entries(parameters).every(([key, value]) => url.searchParams.get(key) === value)
  )?.[0];
  if (!endpoint) {
    response.writeHead(501).end();
    return;
  }
  if (mode === 'rate-limited') {
    response.writeHead(429, { 'Retry-After': '900' }).end();
    return;
  }
  if (endpoint === 'activity_fit') {
    response.writeHead(200, { 'Content-Type': 'application/zip' })
      .end(Buffer.from('PK\x03\x04synthetic source-free fixture'));
    return;
  }
  const result = { sleep: [], hrv: null, devices: [], activities };
  const body = Object.hasOwn(result, endpoint) ? result[endpoint] : document;
  response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(body));
});

server.listen(Number(process.env.PORT || 4514), '127.0.0.1');
