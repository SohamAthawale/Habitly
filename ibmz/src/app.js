import { createHash, timingSafeEqual } from 'node:crypto';
import { eventEmbed, GLOSSARY, handleCommand, lookupGlossary, resourceEmbed } from './commands.js';
import { TOPICS } from './db.js';
import { createSignatureVerifier } from './discord.js';

const MAX_BODY_BYTES = 64 * 1024;

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new HttpError(413, 'request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function parseJson(raw) {
  try {
    const body = JSON.parse(raw.toString('utf8') || '{}');
    if (typeof body === 'object' && body !== null && !Array.isArray(body)) return body;
  } catch {
    // fall through
  }
  throw new HttpError(400, 'body must be a JSON object');
}

function text(body, field, { max, required = true }) {
  const value = typeof body[field] === 'string' ? body[field].trim() : '';
  if (required && !value) throw new HttpError(400, `${field} is required`);
  if (value.length > max) throw new HttpError(400, `${field} must be at most ${max} characters`);
  return value;
}

function httpUrl(body, field, { required = true } = {}) {
  const value = text(body, field, { max: 500, required });
  if (!value) return value;
  try {
    if (['http:', 'https:'].includes(new URL(value).protocol)) return value;
  } catch {
    // fall through
  }
  throw new HttpError(400, `${field} must be an http(s) URL`);
}

const sha256 = (value) => createHash('sha256').update(value).digest();

// announce(channelKey, message) posts to Discord; failures are logged, never surfaced to the API caller
export function createApp(config, db, { announce = async () => {} } = {}) {
  const q = {
    resources: db.prepare(`
      SELECT * FROM resources
      WHERE (:topic IS NULL OR topic = :topic)
        AND (:search IS NULL OR title LIKE :search OR description LIKE :search)
      ORDER BY id DESC LIMIT :limit`),
    resource: db.prepare('SELECT * FROM resources WHERE id = ?'),
    insertResource: db.prepare(
      'INSERT INTO resources (title, url, topic, description) VALUES (?, ?, ?, ?) RETURNING *'),
    deleteResource: db.prepare('DELETE FROM resources WHERE id = ?'),

    upcomingEvents: db.prepare('SELECT * FROM events WHERE starts_at >= ? ORDER BY starts_at LIMIT ?'),
    allEvents: db.prepare('SELECT * FROM events ORDER BY starts_at DESC LIMIT ?'),
    event: db.prepare('SELECT * FROM events WHERE id = ?'),
    insertEvent: db.prepare(
      'INSERT INTO events (title, starts_at, url, location, description) VALUES (?, ?, ?, ?, ?) RETURNING *'),
    deleteEvent: db.prepare('DELETE FROM events WHERE id = ?'),
  };

  const store = {
    listResources: ({ topic, search, limit = 50 }) => q.resources.all({
      topic: topic ?? null,
      search: search ? `%${search}%` : null,
      limit,
    }),
    upcomingEvents: (limit) => q.upcomingEvents.all(new Date().toISOString(), limit),
  };

  const verifyDiscordSignature = createSignatureVerifier(config.discord.publicKey);
  const adminKeyHash = config.adminApiKey ? sha256(config.adminApiKey) : null;

  function requireAdmin(req) {
    if (!adminKeyHash) throw new HttpError(503, 'writes are disabled: ADMIN_API_KEY is not set');
    const header = req.headers.authorization ?? '';
    const given = header.startsWith('Bearer ') ? sha256(header.slice(7)) : null;
    if (!given || !timingSafeEqual(given, adminKeyHash)) throw new HttpError(401, 'invalid admin key');
  }

  function notify(channelKey, message) {
    announce(channelKey, message).catch((err) => console.error(`Discord announce failed: ${err.message}`));
  }

  const routes = [
    ['GET', /^\/$/, () => [200, {
      name: 'IBM Z & Mainframes hub',
      discord: config.discord.inviteUrl || null,
      endpoints: [
        'GET /health',
        'GET /api/topics',
        'GET /api/resources?topic=&q=',
        'GET /api/events?all=1',
        'GET /api/glossary?term=',
        'POST /discord/interactions',
      ],
    }]],

    ['GET', /^\/health$/, () => [200, { status: 'ok' }]],

    ['GET', /^\/api\/topics$/, () => [200, { topics: TOPICS }]],

    ['GET', /^\/api\/resources$/, (req, _params, url) => {
      const topic = url.searchParams.get('topic');
      if (topic && !TOPICS[topic]) throw new HttpError(400, 'unknown topic');
      const search = url.searchParams.get('q')?.trim() || undefined;
      return [200, { resources: store.listResources({ topic, search }) }];
    }],

    ['GET', /^\/api\/resources\/(\d+)$/, (req, [id]) => {
      const resource = q.resource.get(Number(id));
      if (!resource) throw new HttpError(404, 'resource not found');
      return [200, { resource }];
    }],

    ['POST', /^\/api\/resources$/, async (req) => {
      requireAdmin(req);
      const body = parseJson(await readBody(req));
      const topic = text(body, 'topic', { max: 20 });
      if (!TOPICS[topic]) throw new HttpError(400, `topic must be one of: ${Object.keys(TOPICS).join(', ')}`);
      const resource = q.insertResource.get(
        text(body, 'title', { max: 200 }),
        httpUrl(body, 'url'),
        topic,
        text(body, 'description', { max: 1000, required: false }),
      );
      notify('resources', { content: 'New resource added to the hub:', embeds: [resourceEmbed(resource)] });
      return [201, { resource }];
    }],

    ['DELETE', /^\/api\/resources\/(\d+)$/, (req, [id]) => {
      requireAdmin(req);
      if (q.deleteResource.run(Number(id)).changes === 0) throw new HttpError(404, 'resource not found');
      return [204];
    }],

    ['GET', /^\/api\/events$/, (req, _params, url) => {
      const all = url.searchParams.get('all') === '1';
      return [200, { events: all ? q.allEvents.all(200) : store.upcomingEvents(50) }];
    }],

    ['POST', /^\/api\/events$/, async (req) => {
      requireAdmin(req);
      const body = parseJson(await readBody(req));
      const startsAt = Date.parse(text(body, 'starts_at', { max: 40 }));
      if (Number.isNaN(startsAt)) throw new HttpError(400, 'starts_at must be an ISO 8601 date-time');
      const event = q.insertEvent.get(
        text(body, 'title', { max: 200 }),
        new Date(startsAt).toISOString(),
        httpUrl(body, 'url', { required: false }),
        text(body, 'location', { max: 200, required: false }),
        text(body, 'description', { max: 1000, required: false }),
      );
      notify('events', { content: 'New event:', embeds: [eventEmbed(event)] });
      return [201, { event }];
    }],

    ['DELETE', /^\/api\/events\/(\d+)$/, (req, [id]) => {
      requireAdmin(req);
      if (q.deleteEvent.run(Number(id)).changes === 0) throw new HttpError(404, 'event not found');
      return [204];
    }],

    ['GET', /^\/api\/glossary$/, (req, _params, url) => {
      const term = url.searchParams.get('term');
      if (!term) return [200, { glossary: GLOSSARY }];
      const entry = lookupGlossary(term);
      if (!entry) throw new HttpError(404, 'term not found');
      return [200, entry];
    }],

    // Discord "Interactions Endpoint URL": slash commands arrive here as signed HTTP requests
    ['POST', /^\/discord\/interactions$/, async (req) => {
      if (!verifyDiscordSignature) throw new HttpError(503, 'DISCORD_PUBLIC_KEY is not set');
      const raw = await readBody(req);
      const valid = verifyDiscordSignature(
        req.headers['x-signature-ed25519'], req.headers['x-signature-timestamp'], raw);
      if (!valid) throw new HttpError(401, 'invalid request signature');

      const interaction = parseJson(raw);
      if (interaction.type === 1) return [200, { type: 1 }]; // PING
      if (interaction.type === 2) return [200, handleCommand(interaction, store, config)];
      throw new HttpError(400, 'unsupported interaction type');
    }],
  ];

  function send(res, status, body) {
    res.writeHead(status, {
      'Access-Control-Allow-Origin': config.corsOrigin,
      'Access-Control-Allow-Headers': 'Authorization, Content-Type',
      'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json; charset=utf-8' }),
    });
    res.end(body === undefined ? undefined : JSON.stringify(body));
  }

  return async function handle(req, res) {
    try {
      const url = new URL(req.url, 'http://localhost');
      let path = url.pathname;

      // works whether the reverse proxy keeps the /ibmz prefix or strips it
      if (config.basePath && (path === config.basePath || path.startsWith(`${config.basePath}/`))) {
        path = path.slice(config.basePath.length);
      }
      path = path.replace(/\/+$/, '') || '/';

      if (req.method === 'OPTIONS') return send(res, 204);

      let methodMismatch = false;
      for (const [method, pattern, handler] of routes) {
        const match = pattern.exec(path);
        if (!match) continue;
        if (method !== req.method) {
          methodMismatch = true;
          continue;
        }
        const [status, body] = await handler(req, match.slice(1), url);
        return send(res, status, body);
      }
      throw methodMismatch ? new HttpError(405, 'method not allowed') : new HttpError(404, 'not found');
    } catch (err) {
      if (err instanceof HttpError) return send(res, err.status, { error: err.message });
      console.error(err);
      return send(res, 500, { error: 'internal server error' });
    }
  };
}
