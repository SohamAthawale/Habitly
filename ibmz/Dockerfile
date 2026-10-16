# IBM Z & Mainframes hub API. No npm dependencies, so there is no install step.
# Build and run with docker-compose.yml (it mounts ./data and reads .env).
FROM node:22-alpine

WORKDIR /app
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000

COPY package.json ./
COPY src ./src
COPY scripts ./scripts

# data/ holds the sqlite db and discord.json; compose bind-mounts it over this.
RUN mkdir -p /app/data && chown node:node /app/data

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/ibmz/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]

CMD ["node", "--disable-warning=ExperimentalWarning", "src/server.js"]
