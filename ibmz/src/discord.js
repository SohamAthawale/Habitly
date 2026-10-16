import { createPublicKey, verify } from 'node:crypto';
import { readFileSync } from 'node:fs';

const API = 'https://discord.com/api/v10';
const CHANNEL_TYPE_TEXT = 0;
const CHANNEL_TYPE_CATEGORY = 4;

// Manage Channels + View Channel + Send Messages + Embed Links
export const BOT_PERMISSIONS = String((1 << 4) | (1 << 10) | (1 << 11) | (1 << 14));

export const CATEGORY_NAME = 'IBM Z & Mainframes';

// `key` is what the backend uses to look a channel up; `name` is what Discord shows
export const CHANNEL_PLAN = [
  { key: 'announcements', name: 'announcements', topic: 'News about the hub and the IBM Z ecosystem' },
  { key: 'general', name: 'general-mainframe', topic: 'Anything mainframe: hardware, history, war stories' },
  { key: 'zos', name: 'zos-jcl-tso', topic: 'z/OS, JCL, JES2, TSO/ISPF, datasets and batch' },
  { key: 'languages', name: 'cobol-rexx-hlasm', topic: 'COBOL, PL/I, REXX and HLASM programming' },
  { key: 'data', name: 'db2-cics-ims', topic: 'Db2 for z/OS, CICS, IMS and transaction processing' },
  { key: 'linuxone', name: 'linuxone', topic: 'Linux on IBM Z, LinuxONE and s390x' },
  { key: 'devops', name: 'zowe-devops', topic: 'Zowe, VS Code, Git, CI/CD and modern tooling on z/OS' },
  { key: 'learning', name: 'learning-and-careers', topic: 'IBM Z Xplore, courses, certs and mainframe jobs' },
  { key: 'help', name: 'help-desk', topic: 'Stuck? Ask here - include the JCL/abend code/messages' },
  { key: 'resources', name: 'resources', topic: 'New resources added to the hub are posted here automatically' },
  { key: 'events', name: 'events', topic: 'Upcoming mainframe events, posted automatically by the hub' },
];

export function inviteUrl(applicationId) {
  const params = new URLSearchParams({
    client_id: applicationId,
    scope: 'bot applications.commands',
    permissions: BOT_PERMISSIONS,
  });
  return `https://discord.com/oauth2/authorize?${params}`;
}

export function createDiscordClient({ botToken, fetchImpl = fetch }) {
  async function call(method, path, body) {
    const res = await fetchImpl(`${API}${path}`, {
      method,
      headers: {
        Authorization: `Bot ${botToken}`,
        'Content-Type': 'application/json',
        'User-Agent': 'DiscordBot (https://sohamathawale.duckdns.org/ibmz, 1.0.0)',
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
      const err = new Error(`Discord ${method} ${path} failed: ${res.status} ${await res.text()}`);
      err.status = res.status;
      throw err;
    }
    return res.status === 204 ? null : res.json();
  }

  return {
    listChannels: (guildId) => call('GET', `/guilds/${guildId}/channels`),
    createChannel: (guildId, channel) => call('POST', `/guilds/${guildId}/channels`, channel),
    sendMessage: (channelId, message) => call('POST', `/channels/${channelId}/messages`, message),
    registerGuildCommands: (applicationId, guildId, commands) =>
      call('PUT', `/applications/${applicationId}/guilds/${guildId}/commands`, commands),
  };
}

// Creates the category and any missing channels; safe to run repeatedly.
export async function ensureChannels(client, guildId) {
  const existing = await client.listChannels(guildId);
  const sameName = (a, b) => a.toLowerCase() === b.toLowerCase();

  let category = existing.find((c) => c.type === CHANNEL_TYPE_CATEGORY && sameName(c.name, CATEGORY_NAME));
  const created = [];
  if (!category) {
    category = await client.createChannel(guildId, { name: CATEGORY_NAME, type: CHANNEL_TYPE_CATEGORY });
    created.push(CATEGORY_NAME);
  }

  const channels = {};
  for (const plan of CHANNEL_PLAN) {
    let channel = existing.find((c) =>
      c.type === CHANNEL_TYPE_TEXT && c.parent_id === category.id && sameName(c.name, plan.name));
    if (!channel) {
      channel = await client.createChannel(guildId, {
        name: plan.name,
        type: CHANNEL_TYPE_TEXT,
        topic: plan.topic,
        parent_id: category.id,
      });
      created.push(`#${plan.name}`);
    }
    channels[plan.key] = channel.id;
  }

  return { state: { guildId, categoryId: category.id, channels }, created };
}

export function readChannelState(statePath) {
  try {
    return JSON.parse(readFileSync(statePath, 'utf8'));
  } catch {
    return null;
  }
}

// Discord gives the public key as 32 raw bytes in hex; wrap it in SPKI DER for node:crypto
const ED25519_SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');

export function createSignatureVerifier(publicKeyHex) {
  if (!/^[0-9a-f]{64}$/i.test(publicKeyHex ?? '')) return null;
  const key = createPublicKey({
    key: Buffer.concat([ED25519_SPKI_PREFIX, Buffer.from(publicKeyHex, 'hex')]),
    format: 'der',
    type: 'spki',
  });

  return (signatureHex, timestamp, rawBody) => {
    if (!/^[0-9a-f]{128}$/i.test(signatureHex ?? '') || !timestamp) return false;
    return verify(null, Buffer.concat([Buffer.from(timestamp), rawBody]), key, Buffer.from(signatureHex, 'hex'));
  };
}
