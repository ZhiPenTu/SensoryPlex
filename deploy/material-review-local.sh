#!/bin/sh
set -eu
cd /Users/tuzhipeng/.codex/worktrees/material-review/SensoryPlex
docker compose --env-file .data/material-review/preview.env -f .data/material-review/compose.yml "$@"
