#!/usr/bin/env bash
# gemflow 统一容器启动入口 (独立版)
set -eo pipefail

APP_DIR="${APP_DIR:-/app}"
cd "$APP_DIR"

WORKER_COUNT="${WORKER_COUNT:-1}"
PROVIDER_URLS="${PROVIDER_URLS:-}"
PORT="${PORT:-8081}"
DEBUG="${DEBUG:-false}"
BUILD_VERSION="${BUILD_VERSION:-unknown}"
BUILD_TIME="${BUILD_TIME:-unknown}"
AUTO_UPDATE_UPSTREAM="${AUTO_UPDATE_UPSTREAM:-true}"
UPSTREAM_URL="${UPSTREAM_URL:-https://raw.githubusercontent.com/Sophomoresty/gemini-web2api/refs/heads/main/gemini_web2api.py}"
UPSTREAM_MIRROR_URL="${UPSTREAM_MIRROR_URL:-https://ghfast.top/https://raw.githubusercontent.com/Sophomoresty/gemini-web2api/refs/heads/main/gemini_web2api.py}"
WORKERS_JSON="$APP_DIR/workers.json"
MIHOMO_CONFIG="$APP_DIR/mihomo.yaml"
BASE_WORKER_PORT=9000
BASE_PROXY_PORT=19000

echo "=================================================="
echo "          Starting gemflow Gateway Engine        "
echo "=================================================="
echo "-> Image Version       : $BUILD_VERSION"
echo "-> Image Build Time    : $BUILD_TIME"
echo "-> Unified Listen Port : $PORT"
echo "-> Target Worker Count : $WORKER_COUNT"
echo "-> Auto Update Upstream: $AUTO_UPDATE_UPSTREAM"
echo "-> Debug Logging Mode  : $DEBUG"
echo "=================================================="

# 0. 自动拉取/更新最新版 gemini_web2api.py
TARGET_SCRIPT="$APP_DIR/gemini_web2api.py"
if [ "$AUTO_UPDATE_UPSTREAM" = "true" ] || [ "$AUTO_UPDATE_UPSTREAM" = "1" ]; then
    echo "[Upstream] Checking and downloading latest gemini_web2api.py..."
    TMP_SCRIPT="/tmp/gemini_web2api_latest.py"
    DOWNLOADED=false

    # 优先从主源下载，失败则尝试镜像加速源
    if curl -fsSL --connect-timeout 8 --max-time 20 "$UPSTREAM_URL" -o "$TMP_SCRIPT" && [ -s "$TMP_SCRIPT" ]; then
        DOWNLOADED=true
    elif [ -n "$UPSTREAM_MIRROR_URL" ] && curl -fsSL --connect-timeout 8 --max-time 20 "$UPSTREAM_MIRROR_URL" -o "$TMP_SCRIPT" && [ -s "$TMP_SCRIPT" ]; then
        DOWNLOADED=true
    fi

    if [ "$DOWNLOADED" = "true" ]; then
        # 简单验证下载的内容包含 python 关键字
        if grep -q "import" "$TMP_SCRIPT" || grep -q "def " "$TMP_SCRIPT"; then
            cp "$TMP_SCRIPT" "$TARGET_SCRIPT"
            chmod +x "$TARGET_SCRIPT"
            echo "[Upstream] Successfully updated gemini_web2api.py to latest version."
        else
            echo "[Upstream] Downloaded file invalid. Keeping existing script."
        fi
        rm -f "$TMP_SCRIPT"
    else
        if [ -f "$TARGET_SCRIPT" ]; then
            echo "[Upstream] Notice: Network failed to fetch latest upstream. Using existing cached gemini_web2api.py."
        else
            echo "[Upstream] Error: Failed to fetch gemini_web2api.py and no local copy exists."
        fi
    fi
fi

# 1. 检查并准备订阅与 Mihomo 代理配置
USE_PROXIES=false
IS_CN_HOST=false

# 预检宿主机直连 IP 归属地与网络环境
echo "[Network] Inspecting host native network environment..."
GEO_COUNTRY=$(curl -fsSL --connect-timeout 2 -m 4 "http://ip-api.com/line?fields=countryCode" 2>/dev/null || curl -fsSL --connect-timeout 2 -m 4 "https://ipinfo.io/country" 2>/dev/null || true)
GEO_COUNTRY=$(echo "$GEO_COUNTRY" | tr -d '\r\n ' | tr '[:lower:]' '[:upper:]')

if [ "$GEO_COUNTRY" = "CN" ]; then
    IS_CN_HOST=true
    echo "[Network] Detected host native IP in China ($GEO_COUNTRY). Worker-1 will be routed through proxy."
elif [ -n "$GEO_COUNTRY" ]; then
    echo "[Network] Detected host native IP overseas ($GEO_COUNTRY). Worker-1 will use DIRECT connection."
else
    # 兜底测试 Google 直连连通性
    if ! curl -fsSL --connect-timeout 2 -m 3 "https://www.gstatic.com/generate_204" >/dev/null 2>&1; then
        IS_CN_HOST=true
        echo "[Network] Direct Google connection blocked. Worker-1 will be routed through proxy."
    else
        echo "[Network] Direct Google connection available. Worker-1 will use DIRECT connection."
    fi
fi

if [ -n "$PROVIDER_URLS" ]; then
    echo "[Mihomo] Generating proxy configuration for $WORKER_COUNT workers..."

    # 清除旧 provider 缓存文件，确保每次启动从订阅 URL 拉取最新节点
    rm -f "$APP_DIR"/sub-*.yaml

    # 由 mihomo_config.py 统一渲染 Worker 专属策略组 / providers / listeners
    if python3 "$APP_DIR/mihomo_config.py" \
        --template "$APP_DIR/mihomo.template.yaml" \
        --out "$MIHOMO_CONFIG" \
        --workers "$WORKER_COUNT" \
        --base-proxy-port "$BASE_PROXY_PORT"; then

        echo "[Mihomo] Starting mihomo daemon..."
        mihomo -d "$APP_DIR" -f "$MIHOMO_CONFIG" > /tmp/mihomo.log 2>&1 &
        MIHOMO_PID=$!
        sleep 10

        if kill -0 "$MIHOMO_PID" 2>/dev/null; then
            echo "[Mihomo] Started successfully (PID $MIHOMO_PID)."
            USE_PROXIES=true
        else
            echo "[Mihomo] Warning: mihomo failed to start. Falling back to direct native routing."
        fi
    else
        echo "[Mihomo] Warning: failed to render config. Falling back to direct native routing."
    fi
else
    echo "[Info] Running in DIRECT mode (No subscription provided)."
fi

# 2. 生成 workers.json
echo "{\"workers\": [" > "$WORKERS_JSON"
for ((i=0; i<WORKER_COUNT; i++)); do
    W_ID=$((i + 1))
    W_PORT=$((BASE_WORKER_PORT + W_ID))

    if [ "$USE_PROXIES" = "true" ]; then
        if [ "$i" -eq 0 ] && [ "$IS_CN_HOST" != "true" ]; then
            PROXY_URL="null"
        else
            PROXY_PORT=$((BASE_PROXY_PORT + W_ID))
            PROXY_URL="\"http://127.0.0.1:$PROXY_PORT\""
        fi
    else
        PROXY_URL="null"
    fi

    COMMA=","
    if [ "$i" -eq $((WORKER_COUNT - 1)) ]; then
        COMMA=""
    fi
    echo "  {\"id\": $W_ID, \"port\": $W_PORT, \"proxy\": $PROXY_URL}$COMMA" >> "$WORKERS_JSON"
done
echo "]}" >> "$WORKERS_JSON"

# 2.1 为各 Worker 策略组分配互不相同的出口节点
if [ "$USE_PROXIES" = "true" ]; then
    SKIP_IDS=""
    if [ "$IS_CN_HOST" != "true" ]; then
        SKIP_IDS="1"
    fi
    python3 "$APP_DIR/assign_worker_nodes.py" \
        --workers "$WORKER_COUNT" \
        --skip "$SKIP_IDS" \
        --max-wait 45 || echo "[NodeAssign] Skipped due to error; workers share the auto-select egress."
fi

# 3. 启动所有 gemini_web2api 实例
for ((i=0; i<WORKER_COUNT; i++)); do
    W_ID=$((i + 1))
    W_PORT=$((BASE_WORKER_PORT + W_ID))
    W_DIR="$APP_DIR/instances/w$W_ID"
    mkdir -p "$W_DIR"

    # 生成 config.json
    W_PROXY=""
    if [ "$USE_PROXIES" = "true" ]; then
        if [ "$i" -gt 0 ] || [ "$IS_CN_HOST" = "true" ]; then
            PROXY_PORT=$((BASE_PROXY_PORT + W_ID))
            W_PROXY="http://127.0.0.1:$PROXY_PORT"
        fi
    fi

    cat <<EOF > "$W_DIR/config.json"
{
  "port": $W_PORT,
  "api_keys": [],
  "cookie": "",
  "proxy": $([ -n "$W_PROXY" ] && echo "\"$W_PROXY\"" || echo "null"),
  "log_requests": false
}
EOF

    echo "[Worker-$W_ID] Starting gemini_web2api on port $W_PORT (proxy: ${W_PROXY:-DIRECT})..."
    (
        cd "$W_DIR"
        while true; do
            python3 "$APP_DIR/gemini_web2api.py" --port "$W_PORT" --config "$W_DIR/config.json" > "$APP_DIR/worker_$W_ID.log" 2>&1 || true
            sleep 1
        done
    ) &
done

# 4. 启动轻量负载网关 lb_gateway.py
echo "[LB] Starting gemflow Load Balancer Gateway on port $PORT..."
EXTRA_ARGS=""
if [ "$DEBUG" = "true" ] || [ "$DEBUG" = "1" ]; then
    EXTRA_ARGS="--debug"
fi

python3 "$APP_DIR/lb_gateway.py" --port "$PORT" --config "$WORKERS_JSON" $EXTRA_ARGS
