FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.159.3
ARG UB_AGENTS_COMMIT
LABEL org.opencontainers.image.revision=$UB_AGENTS_COMMIT
LABEL ub-agent.spike=issue-10
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv git gh procps ca-certificates curl iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force
COPY pyproject.toml README.md LICENSE /opt/ub-agents/
COPY src /opt/ub-agents/src
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /opt/ub-agents
COPY .agents/implementer.md /opt/spike/base-role.md
COPY spikes/docker-runner /opt/spike
RUN mkdir -p /work && touch /work/.spike-volume \
    && chown -R 1000:1000 /work
ENV PATH="/opt/venv/bin:${PATH}" PYTHONUNBUFFERED=1 HOME=/run/spike-auth/home
USER 1000:1000
WORKDIR /work
ENTRYPOINT ["python3", "/opt/spike/entrypoint.py"]
