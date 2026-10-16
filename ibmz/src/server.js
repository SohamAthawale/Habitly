import { createServer } from 'node:http';
import { createApp } from './app.js';
import { loadConfig } from './config.js';
import { openDatabase } from './db.js';
import { createDiscordClient, readChannelState } from './discord.js';

const config = loadConfig();
const db = openDatabase(config.dbPath);

const discord = config.discord.botToken ? createDiscordClient({ botToken: config.discord.botToken }) : null;

// state is re-read each time so running `npm run discord:setup` needs no restart
async function announce(channelKey, message) {
  const channelId = readChannelState(config.discord.statePath)?.channels?.[channelKey];
  if (!discord || !channelId) return;
  await discord.sendMessage(channelId, message);
}

const server = createServer(createApp(config, db, { announce }));

server.listen(config.port, config.host, () => {
  console.log(`IBM Z hub listening on http://${config.host}:${config.port}${config.basePath}`);
  if (!config.adminApiKey) console.warn('ADMIN_API_KEY is not set: POST/DELETE endpoints are disabled');
  if (!discord) console.warn('DISCORD_BOT_TOKEN is not set: Discord announcements are disabled');
});

function shutdown() {
  server.close(() => {
    db.close();
    process.exit(0);
  });
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
