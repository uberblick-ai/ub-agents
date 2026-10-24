# A launcher image: ub-agents, git, gh, mise, Claude Code and Codex.
# Agent CLIs and mise live in the agent's home so runtime updates can replace them.
FROM python:3.14-slim-trixie

ARG UB_AGENTS_REF=v0.3.5

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg jq less nodejs npm procps tini \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/ub-agents \
 && /opt/ub-agents/bin/pip install --no-cache-dir \
      "ub-agents @ git+https://github.com/uberblick-ai/ub-agents@${UB_AGENTS_REF}" \
 && ln -s /opt/ub-agents/bin/ub-agents /usr/local/bin/ub-agents

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
 && mkdir -p /work && chown agent:agent /work
USER agent
ENV HOME=/home/agent \
    PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:$PATH \
    NPM_CONFIG_PREFIX=/home/agent/.local \
    MISE_TRUSTED_CONFIG_PATHS=/work \
    MISE_YES=1
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && npm install -g @openai/codex \
 && curl -fsSL https://mise.run | sh

COPY --chmod=755 entrypoint.sh /usr/local/bin/ub-agents-entrypoint
WORKDIR /work
ENTRYPOINT ["tini", "--", "ub-agents-entrypoint"]
CMD ["launch"]
