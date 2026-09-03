# Judged image (organizer ruling, 2026-09-03): our Dockerfile and runtime,
# built and run for linux/arm64 (Apple Silicon), no organizer image. Base on
# python:3.12-slim-bookworm (the harness orchestrator, harness/, runs on
# python3.12) and copy Node in from the official pinned image, byte-for-byte,
# rather than installing Node some other way -- the starter's Node/npm pin
# must hold exactly.
FROM node:22.19.0-bookworm-slim AS node

FROM python:3.12-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && groupadd -g 1000 node && useradd -u 1000 -g node -m -s /bin/bash node

WORKDIR /challenge
ENV npm_config_cache=/challenge/.npm-cache

COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

COPY app-template/package.json app-template/package-lock.json ./app-template/
RUN npm --prefix app-template ci --ignore-scripts

COPY . .
# No "RUN npm run check" here (organizer ruling, 2026-09-03): the judged build
# no longer gates on it -- it stays a CI check (.gitlab-ci.yml), not an image
# build step. "chmod +x" is belt-and-braces alongside the tracked executable
# bit on scripts/*.sh (verify with `git ls-files -s scripts/`).
RUN chmod +x scripts/*.sh \
    && mkdir -p output artifacts \
    && chown -R node:node /challenge

# Bake Pi config into the image: there is no organizer-controlled image for
# this track, so the earlier "harness code must never set this" rule now
# applies to PI_CODING_AGENT_DIR only as harness code -- the Dockerfile ENV
# and .env.example (local dev) may set it. .pi-agent/ ships only
# models.json and settings.json (.dockerignore allowlist); Pi-written state
# (auth.json, models-store.json, sessions/) never enters the build context.
ENV PI_CODING_AGENT_DIR=/challenge/.pi-agent \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 3000
USER node

ENTRYPOINT ["scripts/entrypoint.sh"]
