#!/bin/sh
# As the agent user: prepare logins and the checkout, then run `ub-agents <command>`.
set -eu

: "${UB_AGENTS_REPO:?Set UB_AGENTS_REPO to owner/name}"
: "${GH_TOKEN:?Set GH_TOKEN to the launcher account token}"

gh auth setup-git >/dev/null

if [ -z "$(git config --global user.name || true)" ]; then
  login=$(gh api user --jq .login)
  id=$(gh api user --jq .id)
  git config --global user.name "${GIT_USER_NAME:-$login}"
  git config --global user.email "${GIT_USER_EMAIL:-$id+$login@users.noreply.github.com}"
fi

# Codex: a ChatGPT login copied from ~/.codex/auth.json, or an API key.
if [ ! -s "$HOME/.codex/auth.json" ]; then
  if [ -n "${CODEX_AUTH_JSON:-}" ]; then
    mkdir -p "$HOME/.codex" && umask 077
    printf '%s\n' "$CODEX_AUTH_JSON" > "$HOME/.codex/auth.json"
  elif [ -n "${OPENAI_API_KEY:-}" ]; then
    printf '%s' "$OPENAI_API_KEY" | codex login --with-api-key >/dev/null
  fi
fi

# Claude reads CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY from the environment.
# Skip the first-run onboarding that headless runs can't answer.
[ -s "$HOME/.claude.json" ] || echo '{"hasCompletedOnboarding": true}' > "$HOME/.claude.json"

checkout="/work/$(basename "$UB_AGENTS_REPO")"
if [ ! -d "$checkout/.git" ]; then
  gh repo clone "$UB_AGENTS_REPO" "$checkout"
fi
cd "$checkout"

if [ -f mise.toml ] || [ -f .mise.toml ] || [ -f .tool-versions ]; then
  mise install
fi

exec ub-agents "$@"
