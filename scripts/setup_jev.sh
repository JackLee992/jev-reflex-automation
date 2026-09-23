#!/usr/bin/env bash

set +x
trap - DEBUG RETURN
set -euo pipefail

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: bash scripts/setup_jev.sh

Interactively installs the repository's jev-engineering Codex skill and stores
a TypeSafe API key in ~/.config/typesafe/api_key. The key is entered with echo
disabled, is never passed on the command line, and is written with mode 600.
EOF
  exit 0
fi

[[ $# -eq 0 ]] || die "unknown argument: $1 (use --help)"
[[ -t 0 ]] || die "interactive terminal required; run this script directly and paste the key at the prompt"
[[ -n "${HOME:-}" ]] || die "HOME is not set"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
skill_source="$repo_root/skills/jev-engineering"
[[ -f "$skill_source/SKILL.md" ]] || die "jev-engineering skill not found at $skill_source"

codex_home="${CODEX_HOME:-$HOME/.codex}"
codex_skills_dir="$codex_home/skills"
skill_target="$codex_skills_dir/jev-engineering"

if [[ -L "$skill_target" ]]; then
  existing_target="$(readlink "$skill_target")"
  [[ "$existing_target" == "$skill_source" ]] || die \
    "$skill_target already links to $existing_target; remove or relocate it before installing this copy"
elif [[ -e "$skill_target" ]]; then
  die "$skill_target already exists and will not be overwritten"
fi

config_dir="$HOME/.config/typesafe"
key_file="$config_dir/api_key"
[[ ! -L "$key_file" ]] || die "$key_file is a symbolic link and will not be overwritten"
if [[ -e "$key_file" && ! -f "$key_file" ]]; then
  die "$key_file exists but is not a regular file"
fi
if [[ -f "$key_file" ]]; then
  printf 'An existing API key will be replaced atomically after validation.\n' >&2
fi

printf 'Paste TypeSafe API key (input hidden): ' >&2
if ! IFS= read -r -s api_key; then
  printf '\n' >&2
  die "no API key was read"
fi
printf '\n' >&2

if [[ ! "$api_key" =~ ^apikey_[A-Za-z0-9_=-]{32,}$ ]]; then
  unset api_key
  die "unexpected key format; expected apikey_ followed by at least 32 letters, digits, '_', '-' or '='"
fi

umask 077
mkdir -p "$config_dir" "$codex_skills_dir"
chmod 700 "$config_dir"

tmp_key="$(mktemp "$config_dir/.api_key.tmp.XXXXXX")"
cleanup() {
  if [[ -n "${tmp_key:-}" && -e "$tmp_key" ]]; then
    rm -f "$tmp_key"
  fi
  unset api_key
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

printf '%s\n' "$api_key" > "$tmp_key"
unset api_key
chmod 600 "$tmp_key"
mv -f "$tmp_key" "$key_file"
tmp_key=""
chmod 600 "$key_file"

if [[ ! -e "$skill_target" && ! -L "$skill_target" ]]; then
  ln -s "$skill_source" "$skill_target"
fi

printf '\nJEV is ready.\n'
printf '  API key: %s (mode 600)\n' "$key_file"
printf '  Codex skill: %s -> %s\n' "$skill_target" "$skill_source"
printf '  Defaults: https://api.typesafe.ai/v1/systemone, model jev-1.13.0\n'
printf '\nIn Codex, try: 使用 $jev-engineering 分析日志并给出下一条只读诊断。\n'
