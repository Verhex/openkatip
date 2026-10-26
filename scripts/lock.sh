#!/usr/bin/env bash
# Linux ve Windows için kesin sürümleri ve hash'leri üretir.
# Bağımlılık değişince çalıştırın; ardından testleri ve pip-audit'i çalıştırın.
set -euo pipefail
cd "$(dirname "$0")/.."
uv pip compile pyproject.toml \
  --generate-hashes \
  --python-version 3.12 \
  --python-platform x86_64-unknown-linux-gnu \
  --output-file requirements.lock

# Ortak paketlerin sürümleri aynı kalır; uvloop Windows çözümüne dahil edilmez.
constraints="$(mktemp)"
trap 'rm -f "$constraints"' EXIT
sed -nE 's/^([A-Za-z0-9_.-]+==[^[:space:]\\]+).*/\1/p' requirements.lock > "$constraints"
uv pip compile pyproject.toml \
  --generate-hashes \
  --python-version 3.12 \
  --python-platform x86_64-pc-windows-msvc \
  --constraint "$constraints" \
  --output-file requirements.windows.lock
