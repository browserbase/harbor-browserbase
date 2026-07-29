# Harbor builds with this environment/ directory as its context, so the Stagehand
# monorepo is cloned rather than copied from outside that context. For a local-source
# build, stage the source into environment/ or build an image out-of-band and set
# [environment].docker_image in task.toml.
FROM node:22-slim

ARG STAGEHAND_REPO=https://github.com/browserbase/stagehand.git
ARG STAGEHAND_REF=

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        g++ \
        git \
        make \
        procps \
        python3 \
    && rm -rf /var/lib/apt/lists/* \
    && if [ -n "$STAGEHAND_REF" ]; then \
         git clone --depth 1 --branch "$STAGEHAND_REF" "$STAGEHAND_REPO" /opt/stagehand; \
       else \
         git clone --depth 1 "$STAGEHAND_REPO" /opt/stagehand; \
       fi

# TEMPORARY: pending the upstream PR from the Stagehand branch
# fix/benchharness-config-overrides. Delete patches/ and these two steps once
# it lands on public main. This file must stay byte-identical across all ten
# tasks/*/environment/ directories or Harbor will build ten separate images
# instead of sharing one.
COPY patches/ /tmp/stagehand-patches/

RUN cd /opt/stagehand \
    && git apply --check /tmp/stagehand-patches/benchharness-config-overrides.patch \
    && git apply /tmp/stagehand-patches/benchharness-config-overrides.patch \
    && grep -q 'experimental: !' packages/evals/framework/benchHarness.ts \
    && grep -q 'browserbaseSessionID: process.env.BROWSERBASE_SESSION_ID' packages/evals/framework/benchHarness.ts

WORKDIR /opt/stagehand

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

RUN corepack enable \
    && corepack prepare pnpm@9.15.0 --activate \
    && pnpm install --frozen-lockfile \
    && pnpm run build \
    && printf '%s\n' '#!/bin/sh' \
         'exec node /opt/stagehand/packages/evals/dist/cli/cli.js "$@"' \
         > /usr/local/bin/evals \
    && chmod +x /usr/local/bin/evals \
    && mkdir -p /logs/agent \
    && chmod 0777 /logs/agent

CMD ["evals", "--help"]
