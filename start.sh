#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${SCRIPT_DIR}/.venv"
INIT_MARKER="${SCRIPT_DIR}/.gcp_free_initialized"

if [[ ! -f "$INIT_MARKER" ]]; then
  if ! command -v gcloud >/dev/null 2>&1; then
    echo "[错误] 未找到 gcloud，请先安装 Google Cloud SDK。" >&2
    exit 1
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装 Python 3。" >&2
    exit 1
  fi

  echo "[初始化] 正在启用所需的 GCP API..."
  gcloud services enable cloudresourcemanager.googleapis.com
  gcloud services enable compute.googleapis.com
  gcloud services enable cloudbilling.googleapis.com

  if [[ -d "$VENV_DIR" && ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "[初始化] 检测到 venv 不完整，正在重新创建..."
    python3 -m venv --clear "$VENV_DIR"
  elif [[ ! -d "$VENV_DIR" ]]; then
    echo "[初始化] 正在创建 venv..."
    python3 -m venv "$VENV_DIR"
  fi

  if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "[错误] venv 创建失败，请检查 python3-venv 是否已安装。" >&2
    exit 1
  fi

  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install google-cloud-compute google-cloud-resource-manager

  touch "$INIT_MARKER"
else
  if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "[错误] 未找到 venv 激活脚本：$VENV_DIR/bin/activate" >&2
    echo "[错误] 请删除 $INIT_MARKER 以重新初始化。" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
fi

exec python gcp.py
