#!/usr/bin/env bash
# Install pr-watch globally: command on PATH + user config.
set -euo pipefail

PR_WATCH_HOME="$(cd "$(dirname "$0")" && pwd)"
INSTALL_BIN="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/pr-watch"

mkdir -p "${INSTALL_BIN}" "${CONFIG_DIR}"

ln -sf "${PR_WATCH_HOME}/pr-watch" "${INSTALL_BIN}/pr-watch"
ln -sf "${PR_WATCH_HOME}/pr-watch" "${INSTALL_BIN}/prwatch"
chmod +x "${PR_WATCH_HOME}/pr-watch" "${PR_WATCH_HOME}/pr_watch.py" "${PR_WATCH_HOME}/install.sh"

if [[ ! -f "${CONFIG_DIR}/repos.json" ]]; then
  cp "${PR_WATCH_HOME}/repos.json" "${CONFIG_DIR}/repos.json"
  echo "Created ${CONFIG_DIR}/repos.json"
else
  echo "Keeping existing ${CONFIG_DIR}/repos.json"
fi

case ":${PATH}:" in
  *":${INSTALL_BIN}:"*) ;;
  *)
    echo ""
    echo "Add ~/.local/bin to your PATH (add to ~/.zshrc):"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac

echo ""
echo "Installed. Usage from any folder:"
echo '  pr-watch "https://github.com/ackotech/AckoFlutter/pull/123"'
echo '  prwatch "https://github.com/ackotech/AckoFlutter/pull/123"'
echo ""
echo "Edit repo rules:"
echo "  ${CONFIG_DIR}/repos.json"
