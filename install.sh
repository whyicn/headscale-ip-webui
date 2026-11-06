#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Headscale IP WebUI - One-Line Installer & Auto-Configurator
# Default Language: English (set LANG_MODE=zh for Chinese)
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main/install.sh | bash
# ==============================================================================

REPO_URL="${REPO_URL:-https://github.com/whyicn/headscale-ip-webui.git}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/headscale-ip-webui}"
SERVICE_NAME="${SERVICE_NAME:-headscale-ip-webui}"
PORT="${PORT:-5000}"
LANG_MODE="${LANG_MODE:-en}"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

msg() {
  local en_text="$1"
  local zh_text="$2"
  if [[ "${LANG_MODE}" == zh* ]]; then
    echo "${zh_text}"
  else
    echo "${en_text} (${zh_text})"
  fi
}

echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}   Headscale IP WebUI - One-Line Installer / 一键安装脚本     ${NC}"
echo -e "${BLUE}============================================================${NC}"

# 1. Check Docker
if ! command -v docker >/dev/null 2>&1; then
  error "$(msg "Docker is not installed. Please install Docker first." "未检测到 Docker，请先安装 Docker。")"
fi

# 2. Auto-detect Headscale container
HEADSCALE_CONTAINER="${HEADSCALE_CONTAINER:-}"
if [[ -z "${HEADSCALE_CONTAINER}" ]]; then
  if docker ps --format '{{.Names}}' | grep -qx "headscale"; then
    HEADSCALE_CONTAINER="headscale"
  else
    HEADSCALE_CONTAINER="$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /headscale/ {print $1; exit}')"
  fi
fi

if [[ -z "${HEADSCALE_CONTAINER}" ]]; then
  HEADSCALE_CONTAINER="headscale"
  warn "$(msg "No running Headscale container detected, defaulting to: ${HEADSCALE_CONTAINER}" "未检测到运行中的 Headscale 容器，默认使用: ${HEADSCALE_CONTAINER}")"
else
  ok "$(msg "Detected running Headscale container: ${HEADSCALE_CONTAINER}" "检测到运行中的 Headscale 容器: ${HEADSCALE_CONTAINER}")"
fi

# 3. Auto-detect Headscale data dir (db.sqlite) and config dir (config.yaml)
DATA_DIR="${HEADSCALE_DATA_DIR:-}"
CONFIG_DIR="${HEADSCALE_CONFIG_DIR:-}"

if docker inspect "${HEADSCALE_CONTAINER}" >/dev/null 2>&1; then
  while IFS='|' read -r src dst; do
    [[ -z "${src}" ]] && continue
    if [[ -z "${DATA_DIR}" && -f "${src}/db.sqlite" ]]; then
      DATA_DIR="${src}"
    fi
    if [[ -z "${CONFIG_DIR}" && -f "${src}/config.yaml" ]]; then
      CONFIG_DIR="${src}"
    fi
  done < <(docker inspect "${HEADSCALE_CONTAINER}" --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' 2>/dev/null || true)
fi

# Fallback path detection on host
if [[ -z "${DATA_DIR}" ]]; then
  for d in /data/headscale/lib /var/lib/headscale /etc/headscale /opt/headscale/data /root/headscale/data; do
    if [[ -f "${d}/db.sqlite" ]]; then
      DATA_DIR="${d}"
      break
    fi
  done
fi

if [[ -z "${CONFIG_DIR}" ]]; then
  for d in /data/headscale/config /etc/headscale /opt/headscale/config /root/headscale/config; do
    if [[ -f "${d}/config.yaml" ]]; then
      CONFIG_DIR="${d}"
      break
    fi
  done
fi

if [[ -z "${DATA_DIR}" || ! -f "${DATA_DIR}/db.sqlite" ]]; then
  error "$(msg "Could not auto-detect db.sqlite directory. Please specify HEADSCALE_DATA_DIR=/path/to/data" "未能自动定位 db.sqlite 目录，请通过 HEADSCALE_DATA_DIR=/path/to/data 指定")"
fi
ok "$(msg "Detected database directory: ${DATA_DIR} (db.sqlite)" "自动识别数据库目录: ${DATA_DIR} (db.sqlite)")"

if [[ -n "${CONFIG_DIR}" && -f "${CONFIG_DIR}/config.yaml" ]]; then
  ok "$(msg "Detected config directory:   ${CONFIG_DIR} (config.yaml)" "自动识别配置目录:   ${CONFIG_DIR} (config.yaml)")"
else
  CONFIG_DIR="${DATA_DIR}"
  warn "$(msg "No separate config.yaml found, using default CGNAT subnet (100.64.0.0/10)" "未找到独立 config.yaml，将使用默认网段 100.64.0.0/10")"
fi

# 4. Download or update project files in INSTALL_DIR
info "$(msg "Preparing installation directory: ${INSTALL_DIR} ..." "准备安装目录: ${INSTALL_DIR} ...")"
mkdir -p "${INSTALL_DIR}"

if [[ -f "./app.py" && -f "./Dockerfile" ]]; then
  info "$(msg "Copying local source files to ${INSTALL_DIR} ..." "复制当前目录源码至 ${INSTALL_DIR} ...")"
  cp -f ./app.py ./change-node-ip.sh ./Dockerfile ./requirements.txt ./docker-compose.yml "${INSTALL_DIR}/"
elif command -v git >/dev/null 2>&1; then
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "$(msg "Updating existing repository..." "更新已有仓库代码...")"
    git -C "${INSTALL_DIR}" pull --ff-only || true
  else
    rm -rf "${INSTALL_DIR:?}"/*
    git clone --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
  fi
else
  info "$(msg "Downloading project files via curl..." "使用 curl 下载项目文件...")"
  for f in app.py change-node-ip.sh Dockerfile requirements.txt docker-compose.yml; do
    curl -fsSL "${RAW_BASE}/${f}" -o "${INSTALL_DIR}/${f}"
  done
fi

chmod +x "${INSTALL_DIR}/change-node-ip.sh"

# Write .env file for future management
cat > "${INSTALL_DIR}/.env" <<EOF
PORT=${PORT}
HEADSCALE_CONTAINER=${HEADSCALE_CONTAINER}
HEADSCALE_DATA_DIR=${DATA_DIR}
HEADSCALE_CONFIG_DIR=${CONFIG_DIR}
EOF
ok "$(msg "Generated configuration file: ${INSTALL_DIR}/.env" "已生成配置文件: ${INSTALL_DIR}/.env")"

# 5. Build and start container
info "$(msg "Building Docker image ${SERVICE_NAME}:latest ..." "正在构建 Docker 镜像 ${SERVICE_NAME}:latest ...")"
docker build -t "${SERVICE_NAME}:latest" "${INSTALL_DIR}"

if docker ps -a --format '{{.Names}}' | grep -qx "${SERVICE_NAME}"; then
  info "$(msg "Removing existing container ${SERVICE_NAME} ..." "移除旧容器 ${SERVICE_NAME} ...")"
  docker rm -f "${SERVICE_NAME}" >/dev/null
fi

info "$(msg "Starting container ${SERVICE_NAME} (Port: ${PORT}) ..." "正在启动 ${SERVICE_NAME} 容器 (端口: ${PORT}) ...")"
docker run -d \
  --name "${SERVICE_NAME}" \
  --restart unless-stopped \
  --network host \
  -e PORT="${PORT}" \
  -e HEADSCALE_CONTAINER="${HEADSCALE_CONTAINER}" \
  -e HEADSCALE_DB_PATH="/data/db.sqlite" \
  -e HEADSCALE_CONFIG_PATH="/etc/headscale/config.yaml" \
  -v "${DATA_DIR}:/data" \
  -v "${CONFIG_DIR}:/etc/headscale:ro" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "${SERVICE_NAME}:latest" >/dev/null

# 6. Verify service readiness
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1')"
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN} 🎉 Headscale IP WebUI is up and running! / 安装并启动成功！${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e " Web UI URL / 访问地址:      ${BLUE}http://${SERVER_IP}:${PORT}${NC}"
echo -e " Install Dir / 安装目录:     ${INSTALL_DIR}"
echo -e " Database / 数据库挂载:      ${DATA_DIR}/db.sqlite"
echo -e " Container / 关联容器:       ${HEADSCALE_CONTAINER}"
echo ""
echo -e " 🔑 Generate an Admin API Key / 生成管理员 API Key:"
echo -e "    ${YELLOW}docker exec ${HEADSCALE_CONTAINER} headscale apikeys create --expiration 90d${NC}"
echo ""
echo -e " 💻 CLI Tool / 命令行修改或互换节点 IP:"
echo -e "    ${YELLOW}docker exec ${SERVICE_NAME} /app/change-node-ip.sh <node_id> <new_ipv4> [--swap]${NC}"
echo -e "${GREEN}============================================================${NC}"
