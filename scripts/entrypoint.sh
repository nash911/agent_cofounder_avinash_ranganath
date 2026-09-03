#!/usr/bin/env bash
# Image ENTRYPOINT. Resolves the model-gateway credential (never printing its
# value) and hands off to the documented run command.
#
# Credential precedence matches harness.credentials.resolve_api_key() (C3):
# first non-empty of BERGET_API_KEY, CHALLENGE_API_KEY, OPENAI_API_KEY. If
# BERGET_API_KEY was empty and one of the aliases supplied the value, this
# script exports it under BERGET_API_KEY so every downstream reader (Pi's
# .pi-agent/models.json "$BERGET_API_KEY" interpolation, harness/gateway.py)
# sees one consistent name.
set -euo pipefail

if [ -n "${BERGET_API_KEY:-}" ]; then
  echo "credentials: using BERGET_API_KEY" >&2
elif [ -n "${CHALLENGE_API_KEY:-}" ]; then
  export BERGET_API_KEY="$CHALLENGE_API_KEY"
  echo "credentials: using CHALLENGE_API_KEY" >&2
elif [ -n "${OPENAI_API_KEY:-}" ]; then
  export BERGET_API_KEY="$OPENAI_API_KEY"
  echo "credentials: using OPENAI_API_KEY" >&2
else
  echo "credentials: none set" >&2
fi

exec npm run challenge -- "$@"
