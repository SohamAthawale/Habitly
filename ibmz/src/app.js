import { createHash, timingSafeEqual } from 'node:crypto';
import { COMMANDS, eventEmbed, GLOSSARY, handleCommand, lookupGlossary, resourceEmbed } from './commands.js';
import { TOPICS } from './db.js';
import { CATEGORY_NAME, CHANNEL_PLAN, createSignatureVerifier } from './discord.js';

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

const MEMBER_COLUMNS = ['id', 'name', 'student_number', 'email', 'created_at', 'updated_at'];
const STUDENT_NUMBER = /^s?(\d{7})$/i;
const RMIT_STUDENT_EMAIL = /^[^\s@]+@student\.rmit\.edu\.au$/;
// the join form is public, so each IP gets a few sign-ups per window
const JOIN_LIMIT = { max: 5, windowMs: 10 * 60 * 1000 };

// What the website reports to POST /api/track (the get-started funnel and its buttons)
export const TRACK_EVENTS = ['flow_view', 'step_learn', 'step_xplore', 'step_club', 'referral_click', 'name_copy',
  'join', 'discord_click', 'qr_open'];
const TRACK_SOURCE = /^[a-z0-9_-]{1,20}$/;
const TRACK_LIMIT = { max: 120, windowMs: 10 * 60 * 1000 };

// per-IP sliding window, kept in memory (a restart forgets it, which is fine)
function rateLimiter({ max, windowMs }, message) {
  const hits = new Map();
  return (req) => {
    // nginx sets X-Real-IP; the container is only reachable through it
    const ip = req.headers['x-real-ip'] || req.socket.remoteAddress || '';
    const now = Date.now();
    const recent = (hits.get(ip) ?? []).filter((t) => now - t < windowMs);
    if (recent.length >= max) throw new HttpError(429, message);
    recent.push(now);
    if (hits.size > 10_000) hits.clear();
    hits.set(ip, recent);
  };
}

function csvCell(value) {
  const s = String(value ?? '');
  // quote everything; a leading = + - @ is neutralised so spreadsheets don't run it as a formula
  return `"${(/^[=+\-@]/.test(s) ? `'${s}` : s).replace(/"/g, '""')}"`;
}

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

    members: db.prepare(`SELECT ${MEMBER_COLUMNS.join(', ')} FROM members ORDER BY id DESC`),
    upsertMember: db.prepare(`
      INSERT INTO members (student_number, name, email) VALUES (:studentNumber, :name, :email)
      ON CONFLICT(student_number) DO UPDATE SET
        name = excluded.name, email = excluded.email, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`),
    deleteMember: db.prepare('DELETE FROM members WHERE id = ?'),

    insertHit: db.prepare('INSERT INTO hits (event, source) VALUES (?, ?)'),
    hitTotals: db.prepare('SELECT event, COUNT(*) AS n FROM hits WHERE created_at >= ? GROUP BY event'),
    hitSources: db.prepare(
      'SELECT event, source, COUNT(*) AS n FROM hits WHERE created_at >= ? GROUP BY event, source ORDER BY n DESC'),
    hitDays: db.prepare(`
      SELECT substr(created_at, 1, 10) AS day, event, COUNT(*) AS n FROM hits
      WHERE created_at >= ? GROUP BY day, event ORDER BY day`),
    memberCounts: db.prepare('SELECT COUNT(*) AS total, COALESCE(SUM(created_at >= ?), 0) AS recent FROM members'),
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

  const limitJoins = rateLimiter(JOIN_LIMIT, 'too many sign-ups from this address, try again later');
  const limitTracking = rateLimiter(TRACK_LIMIT, 'too many events');

  // /api/stats takes the admin key or the (weaker, read-only) stats password,
  // so failed attempts are capped per IP to stop guessing.
  const statsHashes = [config.adminApiKey, config.statsPassword].filter(Boolean).map(sha256);
  const statsFailures = new Map();
  const STATS_FAILURE_LIMIT = { max: 10, windowMs: 10 * 60 * 1000 };
  function requireStatsAccess(req) {
    const ip = req.headers['x-real-ip'] || req.socket.remoteAddress || '';
    const now = Date.now();
    const recent = (statsFailures.get(ip) ?? []).filter((t) => now - t < STATS_FAILURE_LIMIT.windowMs);
    if (recent.length >= STATS_FAILURE_LIMIT.max) throw new HttpError(429, 'too many wrong passwords, try again later');
    const header = req.headers.authorization ?? '';
    const given = header.startsWith('Bearer ') ? sha256(header.slice(7)) : null;
    if (given && statsHashes.some((h) => timingSafeEqual(given, h))) return;
    recent.push(now);
    if (statsFailures.size > 10_000) statsFailures.clear();
    statsFailures.set(ip, recent);
    throw new HttpError(401, 'invalid stats password');
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
        'GET /api/community',
        'POST /api/members',
        'POST /api/track',
        'POST /discord/interactions',
      ],
    }]],

    ['GET', /^\/health$/, () => [200, { status: 'ok' }]],

    ['GET', /^\/api\/topics$/, () => [200, { topics: TOPICS }]],

    // What the website needs to show the Discord join flow: invite, channels, slash commands
    ['GET', /^\/api\/community$/, () => [200, {
      name: CATEGORY_NAME,
      invite: config.discord.inviteUrl || null,
      channels: CHANNEL_PLAN.map(({ name, topic }) => ({ name, topic })),
      commands: COMMANDS.map(({ name, description }) => ({ name, description })),
    }]],

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

    // Website join form (RMIT students). Always answers the same way, so it can't be used to test who is a member.
    ['POST', /^\/api\/members$/, async (req) => {
      limitJoins(req);
      const body = parseJson(await readBody(req));
      const joined = [201, { status: 'joined', discord: config.discord.inviteUrl || null }];
      if (text(body, 'website', { max: 200, required: false })) return joined; // honeypot: bots fill it in
      if (body.consent !== true) throw new HttpError(400, 'consent is required');
      const number = STUDENT_NUMBER.exec(text(body, 'student_number', { max: 20 }));
      if (!number) throw new HttpError(400, 'student_number must look like s1234567');
      const email = text(body, 'email', { max: 254 }).toLowerCase();
      if (!RMIT_STUDENT_EMAIL.test(email)) throw new HttpError(400, 'email must be your RMIT student email (@student.rmit.edu.au)');
      q.upsertMember.run({ studentNumber: `s${number[1]}`, name: text(body, 'name', { max: 100 }), email });
      return joined;
    }],

    // ?format=csv downloads a spreadsheet-ready file
    ['GET', /^\/api\/members$/, (req, _params, url) => {
      requireAdmin(req);
      const members = q.members.all();
      if (url.searchParams.get('format') !== 'csv') return [200, { members }];
      const rows = [MEMBER_COLUMNS, ...members.map((m) => MEMBER_COLUMNS.map((c) => m[c]))];
      return [200, rows.map((r) => r.map(csvCell).join(',')).join('\r\n') + '\r\n', {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="members.csv"',
      }];
    }],

    ['DELETE', /^\/api\/members\/(\d+)$/, (req, [id]) => {
      requireAdmin(req);
      if (q.deleteMember.run(Number(id)).changes === 0) throw new HttpError(404, 'member not found');
      return [204];
    }],

    // Anonymous funnel counts from the website. The page sends them with sendBeacon
    // (text/plain), which parseJson accepts. Nothing identifying is stored.
    ['POST', /^\/api\/track$/, async (req) => {
      limitTracking(req);
      const body = parseJson(await readBody(req));
      if (!TRACK_EVENTS.includes(body.event)) throw new HttpError(400, `event must be one of: ${TRACK_EVENTS.join(', ')}`);
      const source = typeof body.source === 'string' && TRACK_SOURCE.test(body.source) ? body.source : '';
      q.insertHit.run(body.event, source);
      return [204];
    }],

    ['GET', /^\/api\/stats$/, (req, _params, url) => {
      requireStatsAccess(req);
      const days = Math.min(Math.max(Number(url.searchParams.get('days')) || 30, 1), 365);
      const since = new Date(Date.now() - days * 86_400_000).toISOString();
      const totals = Object.fromEntries(TRACK_EVENTS.map((e) => [e, 0]));
      for (const { event, n } of q.hitTotals.all(since)) totals[event] = n;
      return [200, {
        days,
        since,
        totals,
        bySource: q.hitSources.all(since),
        byDay: q.hitDays.all(since),
        members: q.memberCounts.get(since),
      }];
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

  // a string body is sent as-is with the handler's own headers (CSV); anything else as JSON
  function send(res, status, body, headers = {}) {
    const raw = typeof body === 'string';
    res.writeHead(status, {
      'Access-Control-Allow-Origin': config.corsOrigin,
      'Access-Control-Allow-Headers': 'Authorization, Content-Type',
      'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
      ...(body === undefined || raw ? {} : { 'Content-Type': 'application/json; charset=utf-8' }),
      ...headers,
    });
    res.end(body === undefined || raw ? body : JSON.stringify(body));
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
        const [status, body, headers] = await handler(req, match.slice(1), url);
        return send(res, status, body, headers);
      }
      throw methodMismatch ? new HttpError(405, 'method not allowed') : new HttpError(404, 'not found');
    } catch (err) {
      if (err instanceof HttpError) return send(res, err.status, { error: err.message });
      console.error(err);
      return send(res, 500, { error: 'internal server error' });
    }
  };
}
