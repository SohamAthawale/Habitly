// normalise "/ibmz", "ibmz/", "/ibmz/" -> "/ibmz"; "" or "/" -> ""
function normaliseBasePath(value) {
  const trimmed = (value ?? '').trim().replace(/^\/+|\/+$/g, '');
  return trimmed ? `/${trimmed}` : '';
}

export function loadConfig(env = process.env) {
  return {
    host: env.HOST ?? '127.0.0.1',
    port: Number(env.PORT ?? 3000),
    basePath: normaliseBasePath(env.BASE_PATH ?? '/ibmz'),
    publicUrl: env.PUBLIC_URL ?? 'https://sohamathawale.duckdns.org/ibmz',
    dbPath: env.DB_PATH ?? './data/ibmz.db',
    corsOrigin: env.CORS_ORIGIN ?? '*',
    // writes (POST/DELETE) are disabled until this is set
    adminApiKey: env.ADMIN_API_KEY ?? '',
    // read-only password for the anonymous stats page (GET /api/stats); the admin key also works there
    statsPassword: env.STATS_PASSWORD ?? '',

    discord: {
      botToken: env.DISCORD_BOT_TOKEN ?? '',
      applicationId: env.DISCORD_APPLICATION_ID ?? '',
      publicKey: env.DISCORD_PUBLIC_KEY ?? '',
      guildId: env.DISCORD_GUILD_ID ?? '',
      inviteUrl: env.DISCORD_INVITE_URL ?? '',
      // written by `npm run discord:setup`, maps channel keys to Discord ids
      statePath: env.DISCORD_STATE_PATH ?? './data/discord.json',
    },
  };
}
