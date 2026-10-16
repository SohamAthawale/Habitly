# IBM Z & Mainframes hub

Backend for **https://sohamathawale.duckdns.org/ibmz**, plus a Discord bot that sets up an
IBM Z / mainframe community on your Discord server.

- **REST API**: curated resources by topic (z/OS, JCL, COBOL, CICS, Db2, LinuxONE, Zowe and more),
  upcoming events, and a mainframe glossary.
- **Discord**: one command creates an `IBM Z & Mainframes` category with channels, and registers the
  `/resources`, `/events`, `/glossary` and `/hub` slash commands. New resources and events are
  posted to `#resources` and `#events` automatically.
- **No npm dependencies**: it uses Node's built-in `http`, `sqlite` and `crypto` modules, so nothing
  native has to compile on IBM Z (s390x). Requires **Node.js 22.13 or newer**.

## Run locally

```bash
cd ibmz
cp .env.example .env      # set ADMIN_API_KEY at least
npm start                 # http://127.0.0.1:3000/ibmz/
npm test
```

## API

All paths are under `/ibmz`. Reads are public. Writes need `Authorization: Bearer <ADMIN_API_KEY>`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/` | Index with the list of endpoints and the Discord invite link |
| GET | `/health` | `{ "status": "ok" }` |
| GET | `/api/topics` | Topic keys and their labels |
| GET | `/api/resources?topic=cobol&q=course` | Both filters are optional |
| GET | `/api/resources/:id` | |
| POST | `/api/resources` | `{ title, url, topic, description? }`, then announced in `#resources` |
| DELETE | `/api/resources/:id` | |
| GET | `/api/events` | Upcoming events only. Add `?all=1` to include past ones |
| POST | `/api/events` | `{ title, starts_at (ISO 8601), url?, location?, description? }`, then announced in `#events` |
| DELETE | `/api/events/:id` | |
| GET | `/api/glossary?term=jcl` | Leave out `term` to get the whole glossary |
| GET | `/api/community` | Discord invite (`DISCORD_INVITE_URL`), channel list and slash commands, for the website |
| POST | `/api/members` | Website join form, RMIT students only: `{ name, student_number: "s1234567", email: "...@student.rmit.edu.au", consent: true }`. Public, 5 per IP per 10 min |
| GET | `/api/members` | Admin. Add `?format=csv` for a spreadsheet |
| DELETE | `/api/members/:id` | Admin |
| POST | `/api/track` | Anonymous website usage counts `{ event, source? }` (no cookies, IPs or ids stored). Public, 120 per IP per 10 min |
| GET | `/api/stats?days=30` | `Bearer <STATS_PASSWORD>` (read-only) or the admin key. Event totals, by source, by day (UTC), and member counts. 10 wrong tries per IP per 10 min |
| POST | `/discord/interactions` | Discord slash commands (signature-verified) |

```bash
curl -X POST https://sohamathawale.duckdns.org/ibmz/api/events \
  -H "Authorization: Bearer $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"title":"COBOL study night","starts_at":"2026-11-05T18:00:00Z","location":"Discord #cobol-rexx-hlasm"}'
```

## Set up Discord

1. Create the application at https://discord.com/developers/applications, then **New Application**.
   - **General Information**: copy the **Application ID** into `DISCORD_APPLICATION_ID` and the
     **Public Key** into `DISCORD_PUBLIC_KEY`.
   - **Bot**: click **Reset Token** and copy it into `DISCORD_BOT_TOKEN`.
2. Get your server id. In Discord, turn on **Settings > Advanced > Developer Mode**, then right-click
   your server and choose **Copy Server ID**. Put it in `DISCORD_GUILD_ID`.
3. Invite the bot. Open this URL with your application id filled in (it grants Manage Channels,
   View Channel, Send Messages and Embed Links):
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=19472
   ```
4. Create the channels and register the commands:
   ```bash
   npm run discord:setup
   ```
   This creates the following under **IBM Z & Mainframes**: `#announcements`, `#general-mainframe`,
   `#zos-jcl-tso`, `#cobol-rexx-hlasm`, `#db2-cics-ims`, `#linuxone`, `#zowe-devops`,
   `#learning-and-careers`, `#help-desk`, `#resources` and `#events`. You can run it again safely,
   because it only creates what's missing. Channel ids are saved to `data/discord.json`.
5. Turn on the slash commands. Once the backend is live on the server, go to **General Information >
   Interactions Endpoint URL** in the Developer Portal, set it to
   `https://sohamathawale.duckdns.org/ibmz/discord/interactions` and save. Discord pings the URL to
   verify it, so the backend must already be running.

To change the channels, edit `CHANNEL_PLAN` in `src/discord.js` and run `npm run discord:setup` again.

## Deploy on the server

```bash
# as root on the server behind sohamathawale.duckdns.org
useradd --system --home /opt/Habitly --shell /usr/sbin/nologin ibmz
git clone https://github.com/SohamAthawale/Habitly.git /opt/Habitly
cd /opt/Habitly/ibmz
cp .env.example .env && chmod 600 .env && nano .env     # fill in the values
mkdir -p data && chown -R ibmz:ibmz data .env

sudo -u ibmz node --env-file=.env scripts/discord-setup.js   # step 4 above

cp deploy/ibmz-hub.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now ibmz-hub
curl http://127.0.0.1:3000/ibmz/health
```

Then add `deploy/nginx-ibmz.conf` to the HTTPS `server` block for the domain, run
`nginx -t && systemctl reload nginx`, and check `https://sohamathawale.duckdns.org/ibmz/health`.
If the domain doesn't have HTTPS yet, run `certbot --nginx -d sohamathawale.duckdns.org` first,
because Discord only accepts `https://` interaction URLs.

On IBM Z / LinuxONE (s390x), install Node from the official `linux-s390x` build at
https://nodejs.org/en/download or from your distro's Node 22+ package. If `node` isn't at
`/usr/bin/node`, update `ExecStart` in `deploy/ibmz-hub.service`.

To update later: `cd /opt/Habitly && git pull && systemctl restart ibmz-hub`.
