// Creates the "IBM Z & Mainframes" category + channels and registers slash commands.
// Usage: npm run discord:setup   (reads DISCORD_* from .env)
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { COMMANDS } from '../src/commands.js';
import { loadConfig } from '../src/config.js';
import { createDiscordClient, ensureChannels, inviteUrl } from '../src/discord.js';

const { discord } = loadConfig();

const missing = ['botToken', 'applicationId', 'guildId'].filter((key) => !discord[key]);
if (missing.length) {
  console.error(`Missing config: ${missing.join(', ')} (set DISCORD_BOT_TOKEN, DISCORD_APPLICATION_ID, DISCORD_GUILD_ID in .env)`);
  process.exit(1);
}

const client = createDiscordClient({ botToken: discord.botToken });

try {
  const { state, created } = await ensureChannels(client, discord.guildId);
  mkdirSync(dirname(discord.statePath), { recursive: true });
  writeFileSync(discord.statePath, `${JSON.stringify(state, null, 2)}\n`);
  console.log(created.length ? `Created: ${created.join(', ')}` : 'All channels already exist.');
  console.log(`Saved channel ids to ${discord.statePath}`);

  const registered = await client.registerGuildCommands(discord.applicationId, discord.guildId, COMMANDS);
  console.log(`Registered slash commands: ${registered.map((c) => `/${c.name}`).join(' ')}`);
} catch (err) {
  console.error(err.message);
  if (err.status === 403 || err.status === 404) {
    console.error(`\nIs the bot in your server with Manage Channels permission? Invite it with:\n${inviteUrl(discord.applicationId)}`);
  }
  process.exit(1);
}
