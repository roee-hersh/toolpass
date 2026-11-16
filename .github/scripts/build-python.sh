#!/usr/bin/env bash
# Builds the agent-authorizer package (agent-authorizer-py) at a version into OUT.
#   .github/scripts/build-python.sh 1.2.3 dist
set -euo pipefail
v="$1"
out="$2"
echo "$v" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.]+)?$' || { echo "bad version: $v"; exit 1; }
sed -i "s/^__version__ = .*/__version__ = \"$v\"/" agent-authorizer-py/src/agent_authorizer/_version.py
grep -q "^__version__ = \"$v\"$" agent-authorizer-py/src/agent_authorizer/_version.py
python -m pip install --quiet build
python -m build agent-authorizer-py -o "$out"
ls -l "$out"
