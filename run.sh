#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv "${VENV_DIR}"
fi

if ! "${VENV_DIR}/bin/python" -c 'import flask' 2>/dev/null; then
    "${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"
fi

cd "${SCRIPT_DIR}"
exec "${VENV_DIR}/bin/python" app.py
