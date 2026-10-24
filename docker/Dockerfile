# A launcher image: ub-agents, git, gh, mise, Claude Code, Codex, build tools,
# a Docker engine for projects whose checks use Docker, and Playwright's system
# libraries. Agent CLIs, mise and browsers live in the agent's home volume so
# runtime updates can replace them.
FROM debian:trixie-slim

ARG UB_AGENTS_REF=v0.3.5
ARG PLAYWRIGHT_VERSION=1.62.1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git gnupg jq less libatomic1 nodejs npm pkg-config \
      procps python3 python3-venv tini unzip xz-utils zip \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli.gpg \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && arch=$(dpkg --print-architecture) \
 && echo "deb [arch=$arch signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin \
 && npx -y "playwright@${PLAYWRIGHT_VERSION}" install-deps chromium webkit \
 && rm -rf /var/lib/apt/lists/* /root/.npm

RUN python3 -m venv /opt/ub-agents \
 && /opt/ub-agents/bin/pip install --no-cache-dir \
      "ub-agents @ git+https://github.com/uberblick-ai/ub-agents@${UB_AGENTS_REF}" \
 && ln -s /opt/ub-agents/bin/ub-agents /usr/local/bin/ub-agents

RUN useradd --create-home --uid 1000 --shell /bin/bash --groups docker agent \
 && mkdir -p /work && chown agent:agent /work
ENV HOME=/home/agent \
    PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:$PATH \
    NPM_CONFIG_PREFIX=/home/agent/.local \
    MISE_TRUSTED_CONFIG_PATHS=/work \
    MISE_YES=1
USER agent
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && npm install -g @openai/codex \
 && curl -fsSL https://mise.run | sh \
 && npx -y "playwright@${PLAYWRIGHT_VERSION}" install chromium webkit \
 && rm -rf /home/agent/.npm/_npx

# The entrypoint starts as root to run dockerd, then continues as `agent`.
USER root
COPY --chmod=755 entrypoint.sh /usr/local/bin/ub-agents-entrypoint
COPY --chmod=755 setup.sh /usr/local/bin/ub-agents-setup
WORKDIR /work
ENTRYPOINT ["tini", "--", "ub-agents-entrypoint"]
CMD ["launch"]
