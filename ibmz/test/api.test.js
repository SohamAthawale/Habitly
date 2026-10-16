import assert from 'node:assert/strict';
import { generateKeyPairSync, sign } from 'node:crypto';
import { createServer } from 'node:http';
import { after, before, describe, test } from 'node:test';
import { createApp } from '../src/app.js';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/db.js';
import { CATEGORY_NAME, CHANNEL_PLAN, ensureChannels } from '../src/discord.js';

const ADMIN_KEY = 'test-admin-key';
const { publicKey, privateKey } = generateKeyPairSync('ed25519');
// raw 32-byte key is the tail of the SPKI DER encoding
const publicKeyHex = publicKey.export({ format: 'der', type: 'spki' }).subarray(-32).toString('hex');

let server;
let base;
const announced = [];

before(async () => {
  const config = loadConfig({
    BASE_PATH: '/ibmz',
    DB_PATH: ':memory:',
    ADMIN_API_KEY: ADMIN_KEY,
    DISCORD_PUBLIC_KEY: publicKeyHex,
  });
  const app = createApp(config, openDatabase(':memory:'), {
    announce: async (channelKey, message) => announced.push({ channelKey, message }),
  });
  server = createServer(app);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}/ibmz`;
});

after(() => server.close());

async function api(method, path, { body, admin = false, headers = {} } = {}) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: {
      ...(body !== undefined && { 'Content-Type': 'application/json' }),
      ...(admin && { Authorization: `Bearer ${ADMIN_KEY}` }),
      ...headers,
    },
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
  });
  return { status: res.status, body: res.status === 204 ? null : await res.json() };
}

function signedInteraction(payload, { tamper = false } = {}) {
  const body = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = sign(null, Buffer.from(timestamp + body), privateKey).toString('hex');
  return api('POST', '/discord/interactions', {
    body: tamper ? body.replace('}', ',"x":1}') : body,
    headers: { 'X-Signature-Ed25519': signature, 'X-Signature-Timestamp': timestamp },
  });
}

describe('routing', () => {
  test('serves under the /ibmz base path', async () => {
    assert.deepEqual((await api('GET', '/health')).body, { status: 'ok' });
    assert.equal((await api('GET', '/')).body.name, 'IBM Z & Mainframes hub');
  });

  test('also works when the proxy strips the prefix', async () => {
    const res = await fetch(`${base.replace('/ibmz', '')}/health`);
    assert.equal(res.status, 200);
  });

  test('404 and 405', async () => {
    assert.equal((await api('GET', '/nope')).status, 404);
    assert.equal((await api('PUT', '/api/resources')).status, 405);
  });
});

describe('resources', () => {
  test('seeded resources are listed and filterable', async () => {
    const all = await api('GET', '/api/resources');
    assert.ok(all.body.resources.length >= 5);
    const cobol = await api('GET', '/api/resources?topic=cobol');
    assert.ok(cobol.body.resources.every((r) => r.topic === 'cobol'));
    assert.equal((await api('GET', '/api/resources?topic=bogus')).status, 400);
    const search = await api('GET', '/api/resources?q=Zowe');
    assert.equal(search.body.resources[0].title, 'Zowe');
  });

  test('writes need the admin key', async () => {
    const body = { title: 'JCL primer', url: 'https://example.com/jcl', topic: 'jcl' };
    assert.equal((await api('POST', '/api/resources', { body })).status, 401);
    assert.equal((await api('POST', '/api/resources', {
      body, headers: { Authorization: 'Bearer wrong' },
    })).status, 401);
  });

  test('create announces to Discord, then delete', async () => {
    announced.length = 0;
    const created = await api('POST', '/api/resources', {
      admin: true,
      body: { title: 'JCL primer', url: 'https://example.com/jcl', topic: 'jcl', description: 'Basics' },
    });
    assert.equal(created.status, 201);
    assert.equal(created.body.resource.topic, 'jcl');
    assert.equal(announced[0].channelKey, 'resources');
    assert.equal(announced[0].message.embeds[0].url, 'https://example.com/jcl');

    const id = created.body.resource.id;
    assert.equal((await api('GET', `/api/resources/${id}`)).status, 200);
    assert.equal((await api('DELETE', `/api/resources/${id}`, { admin: true })).status, 204);
    assert.equal((await api('GET', `/api/resources/${id}`)).status, 404);
  });

  test('validates input', async () => {
    const bad = [
      { title: 'x', url: 'javascript:alert(1)', topic: 'jcl' },
      { title: 'x', url: 'https://a.b', topic: 'nope' },
      { url: 'https://a.b', topic: 'jcl' },
    ];
    for (const body of bad) {
      assert.equal((await api('POST', '/api/resources', { admin: true, body })).status, 400);
    }
    assert.equal((await api('POST', '/api/resources', { admin: true, body: '[1]' })).status, 400);
  });
});

describe('events', () => {
  test('only upcoming events are listed by default', async () => {
    const future = new Date(Date.now() + 7 * 86_400_000).toISOString();
    await api('POST', '/api/events', { admin: true, body: { title: 'Past meetup', starts_at: '2020-01-01T10:00:00Z' } });
    const created = await api('POST', '/api/events', {
      admin: true, body: { title: 'z/OS meetup', starts_at: future, location: 'Online' },
    });
    assert.equal(created.status, 201);
    assert.equal(announced.at(-1).channelKey, 'events');

    const upcoming = await api('GET', '/api/events');
    assert.deepEqual(upcoming.body.events.map((e) => e.title), ['z/OS meetup']);
    assert.equal((await api('GET', '/api/events?all=1')).body.events.length, 2);
    assert.equal((await api('POST', '/api/events', {
      admin: true, body: { title: 'x', starts_at: 'not a date' },
    })).status, 400);
  });
});

describe('glossary', () => {
  test('looks up terms case-insensitively', async () => {
    assert.match((await api('GET', '/api/glossary?term=JCL')).body.definition, /Job Control Language/);
    assert.equal((await api('GET', '/api/glossary?term=zos')).body.term, 'z/os');
    assert.equal((await api('GET', '/api/glossary?term=xyz')).status, 404);
  });
});

describe('discord interactions', () => {
  test('rejects unsigned or tampered requests', async () => {
    assert.equal((await api('POST', '/discord/interactions', { body: { type: 1 } })).status, 401);
    assert.equal((await signedInteraction({ type: 1 }, { tamper: true })).status, 401);
  });

  test('answers PING', async () => {
    assert.deepEqual((await signedInteraction({ type: 1 })).body, { type: 1 });
  });

  test('slash commands', async () => {
    const glossary = await signedInteraction({
      type: 2, data: { name: 'glossary', options: [{ name: 'term', value: 'cics' }] },
    });
    assert.equal(glossary.body.type, 4);
    assert.match(glossary.body.data.embeds[0].description, /transaction server/);

    const resources = await signedInteraction({
      type: 2, data: { name: 'resources', options: [{ name: 'topic', value: 'zowe' }] },
    });
    assert.equal(resources.body.data.embeds[0].title, 'Zowe');

    const events = await signedInteraction({ type: 2, data: { name: 'events' } });
    assert.equal(events.body.data.embeds[0].title, 'z/OS meetup');
  });
});

describe('discord channel setup', () => {
  function fakeDiscord(existing) {
    let nextId = 100;
    const channels = [...existing];
    return {
      channels,
      listChannels: async () => channels,
      createChannel: async (_guild, ch) => {
        const created = { id: String(nextId++), parent_id: null, ...ch };
        channels.push(created);
        return created;
      },
    };
  }

  test('creates the category and every channel, then is idempotent', async () => {
    const client = fakeDiscord([{ id: '1', type: 0, name: 'general', parent_id: null }]);
    const first = await ensureChannels(client, 'guild');
    assert.equal(first.created.length, CHANNEL_PLAN.length + 1);
    assert.equal(client.channels.find((c) => c.type === 4).name, CATEGORY_NAME);
    assert.deepEqual(Object.keys(first.state.channels), CHANNEL_PLAN.map((c) => c.key));

    const second = await ensureChannels(client, 'guild');
    assert.deepEqual(second.created, []);
    assert.deepEqual(second.state, first.state);
  });
});
