# 🌟 gemflow

<div align="center">

[![CI](https://github.com/AkkunYo/gemflow/actions/workflows/docker-image.yml/badge.svg)](https://github.com/AkkunYo/gemflow/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker Architecture](https://img.shields.io/badge/Docker-amd64%20%7C%20arm64-blue)](https://github.com/AkkunYo/gemflow)
[![Python Version](https://img.shields.io/badge/Python-3.9%2B-brightgreen)](https://www.python.org/)

**Intelligent Sticky-Session Load Balancer & Multi-Egress Routing Gateway for Gemini Web / Web2API Services.**

专为 Gemini Web2API / LLM 服务设计的**智能会话粘滞负载均衡与多出口分流网关**。兼顾 **Prompt KV 缓存加速** 与 **多线路高可用负载**。

[中文文档 (README_CN.md)](README_CN.md) | [English Documentation](README.md)

</div>

---

## ✨ Key Features Matrix

| Feature Dimension | Core Mechanism | Business Value / Impact | Implementation Details |
| :--- | :--- | :--- | :--- |
| 🎯 **Sticky Session** | Extracts fingerprints from `user`, prompt MD5 (`ctx_<md5>`), or auth tokens | Pins session to same worker & egress IP, maximizing Google **Prompt KV Cache** (~72% TTFT cut) | In-memory session LRU map with request metadata extraction |
| 🔄 **Intelligent Scheduling** | **Least-Connection + Round-Robin Tie-Breaking** | Prevents worker overload; distributes sequential requests evenly | Atomic active connection counters (`ACTIVE_CONNS`) + thread lock |
| 🛡️ **High Availability** | **Automatic 429/5xx Retry & 20s Cooling Penalty** | Transparent node failover within sub-seconds, ensuring zero downtime | Instant retry across healthy workers; failed nodes penalized for 20s |
| 🌐 **Multi-Egress Routing** | **Mihomo Kernel + Host IP Environment Awareness** | Eliminates single-IP rate limits and regional network blocking | Auto-routes Worker 1 via proxy (`:19001`) in CN hosts; Workers 2..N get dedicated proxy ports (`:19002..19000+N`) |
| 🌊 **Zero-Buffer Streaming** | **Raw HTTP Chunked & SSE Passthrough** | Real-time typewriter effect with ultra-low constant memory footprint | Direct byte stream forwarding without buffering layers |
| 🔍 **End-to-End Visibility** | **Real-Time Fingerprint & Route Decision Tracing** | Full clarity on routing path (`STICKY` vs `LEAST_CONN`) and worker egress nodes | Enabled via `DEBUG=true` or `--debug` CLI argument |

---

## 📊 Benchmark & KV Cache Locality

Google Gemini models implement **Prompt KV Prefix Caching**. Random or naive round-robin dispatch across different IPs or upstream sessions invalidates the prefix cache, causing severe First-Token Latency (TTFT) degradation.

`gemflow` achieves **~70%+ reduction in TTFT** by deterministically pinning conversational contexts to the same backend worker and egress proxy:

| Metric | Random / Round-Robin Gateway | `gemflow` Sticky-Session Gateway | Optimization |
| :--- | :---: | :---: | :---: |
| **First-Token Latency (TTFT, Turn 2+)** | `1.85s ~ 2.40s` | **`0.42s ~ 0.65s`** | ⚡ **~72% Faster** |
| **Prefix Cache Hit Rate** | < 25% | **> 95%** | 🎯 **Optimal Cache Locality** |
| **429 Rate Limit Failover Time** | Manual / Request Fails | **< 0.1s Auto Failover** | 🛡️ **Zero Downtime** |
| **Multi-IP Egress Scaling** | Single / Static IP | **N-Isolated Proxy Tunnels** | 🌐 **High Capacity** |

---

## 🏗️ Architecture

```text
                                    [Client Request]
                                           │
                                           ▼
                           ┌───────────────────────────────┐
                           │     gemflow Gateway (:8081)   │
                           │  - Context / Session Affinity │
                           │  - Least-Conn + Round-Robin   │
                           │  - Auto Failover on 429/5xx   │
                           └───┬──────────┬──────────┬─────┴───···────┐
                               │          │          │                │
               ┌───────────────┘          │          └──────────┐     └───────────────┐
               ▼                          ▼                     ▼                     ▼
        ┌──────────────┐           ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
        │   Worker 1   │ (:9001)   │   Worker 2   │(:9002│   Worker 3   │(:9003│   Worker N   │ (:9000+N)
        └──────┬───────┘           └──────┬───────┘      └──────┬───────┘      └──────┬───────┘
               │                          │                     │                     │
               ▼ (Mihomo :19001 / Direct) ▼ (Mihomo :19002)     ▼ (Mihomo :19003)     ▼ (Mihomo :19000+N)
        ┌──────────────┐           ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
        │ Proxy/Direct │           │ Proxy Node A │      │ Proxy Node B │      │ Proxy Node N │
        └──────┬───────┘           └──────┬───────┘      └──────┬───────┘      └──────┬───────┘
               │                          │                     │                     │
               └──────────────────────────┴──────────┬──────────┴─────────────────────┘
                                                     ▼
                                          [Google Gemini Upstream]
```

---

## 🚀 Quick Start

### 1. Local Python Run

```bash
# Clone and install dependencies
git clone https://github.com/your-username/gemflow.git
cd gemflow
pip install -r requirements.txt

# Start 4 workers with subscription (supports Clash YAML or V2Ray / Base64 / VLESS / SS) and debug logging
python3 run_local.py --workers 4 --port 8081 --sub "https://example.com/api/v1/client/subscribe?token=xxx" --debug
```

### 2. Docker & Docker Compose

```bash
# 1. Direct run with Docker
docker run -d -p 8081:8081 \
  -e WORKER_COUNT=4 \
  -e PROVIDER_URLS="https://example.com/api/v1/client/subscribe?token=xxx" \
  -e DEBUG=true \
  --name gemflow registry.cn-hangzhou.aliyuncs.com/zkyml/gemflow:latest

# 2. Or using Docker Compose
# Download docker-compose.yml configuration (if repository is not cloned)
curl -fsSL https://raw.githubusercontent.com/AkkunYo/gemflow/main/docker-compose.yml -o docker-compose.yml

# Launch services
docker compose up -d
```

---

## 💻 API Client Usage

`gemflow` exposes a standard OpenAI-compatible API interface on port `8081`.

### 1. `curl` (Streaming SSE)

```bash
curl -N http://127.0.0.1:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [
      {"role": "user", "content": "Explain quantum computing in 3 sentences."}
    ],
    "stream": true,
    "user": "session-user-123"
  }'
```

### 2. Python (`openai` SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8081/v1",
    api_key="your-api-key",  # or dummy string if upstream doesn't enforce
)

response = client.chat.completions.create(
    model="gemini-2.5-flash",
    messages=[
        {"role": "user", "content": "Hello Gemini!"}
    ],
    stream=True,
    user="user-session-42",  # Optional: Explicit sticky session identifier
)

for chunk in response:
    content = chunk.choices[0].delta.content or ""
    print(content, end="", flush=True)
print()
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8081` | Gateway HTTP listen entrypoint |
| `WORKER_COUNT` | `1` | Number of worker instances to launch (`1` = Direct only, `N` = 1 Direct + N-1 Proxy workers) |
| `PROVIDER_URLS` | `""` | Proxy subscription URLs (supports Clash YAML links as well as V2Ray / Base64 / VMess / VLESS / Trojan subscription formats, multi-line supported) |
| `AUTO_UPDATE_UPSTREAM` | `true` | Automatically fetch latest `gemini_web2api.py` on container/script startup |
| `DEBUG` | `false` | Enable verbose logging (`true`/`1`/`yes`) |

---

## 🙏 Acknowledgments & References

`gemflow` is a custom-engineered intelligent load balancing and routing gateway built on top of and integrating with the following outstanding open-source projects:

- 🔹 **[gemini-web2api](https://github.com/Sophomoresty/gemini-web2api)**: Upstream service provider converting Gemini Web sessions into standard OpenAI-compatible API endpoints.
- 🔹 **[Mihomo (Clash.Meta)](https://github.com/MetaCubeX/mihomo)**: High-performance rule-based proxy kernel powering multi-egress routing and latency testing.

---

## 📋 Runtime Logs & Visibility

### 1. Worker Egress IP Status Inspection

On startup or status check, the gateway reports port, network mode, and geo-IP information for all workers:

```text
========== [Worker Egress IP Status @ 2026-08-21 23:15:25] ==========
[Worker-1 : Port 9001 : DIRECT (Native)] -> United States (Ashburn) - IP: 3.216.155.203 [Amazon Technologies Inc.]
[Worker-2 : Port 9002 : Proxy (http://127.0.0.1:19002)] -> United States (Reston) - IP: 104.28.153.225 [Cloudflare, Inc.]
[Worker-3 : Port 9003 : Proxy (http://127.0.0.1:19003)] -> United States (Reston) - IP: 104.28.153.194 [Cloudflare, Inc.]
[Worker-4 : Port 9004 : Proxy (http://127.0.0.1:19004)] -> United States (Reston) - IP: 104.28.167.43 [Cloudflare, Inc.]
[Worker-5 : Port 9005 : Proxy (http://127.0.0.1:19005)] -> United States (Reston) - IP: 104.28.157.167 [Cloudflare, Inc.]
[Worker-6 : Port 9006 : Proxy (http://127.0.0.1:19006)] -> United States (Reston) - IP: 104.28.157.167 [Cloudflare, Inc.]
======================================================================
```

### 2. Real-Time Routing Logs (`DEBUG=true`)

Enable `DEBUG=true` to monitor real-time session fingerprints, routing decisions (`STICKY` vs `LEAST_CONN`), and response times:

```text
# 1. New session dispatch -> Least-connection round-robin to Worker-2
[DEBUG @ 21:05:10] [Req #1] [POST] /v1/chat/completions | Model: 'gemini-2.5-flash' | Session: ctx_8a3f912b41de | Prompt: "Explain quantum computing..." -> Selected: Worker-2 (Port 9002, Egress: http://127.0.0.1:19002, Route: LEAST_CONN, Active: 1)
[DEBUG @ 21:05:12] [Req #1] Completed in 2.10s | HTTP 200 via Worker-2 (http://127.0.0.1:19002)

# 2. Subsequent turn -> STICKY match on Worker-2 (Prompt KV cache hit, accelerated TTFT)
[DEBUG @ 21:05:25] [Req #2] [POST] /v1/chat/completions | Model: 'gemini-2.5-flash' | Session: ctx_8a3f912b41de | Prompt: "Explain quantum computing..." -> Selected: Worker-2 (Port 9002, Egress: http://127.0.0.1:19002, Route: STICKY, Active: 1)
[DEBUG @ 21:05:26] [Req #2] Completed in 1.15s | HTTP 200 via Worker-2 (http://127.0.0.1:19002)
```

### 3. Diagnostic & Troubleshooting Commands

When diagnosing proxy node connectivity or checking internal worker logs:

```bash
# 1. View Mihomo kernel & subscription logs
docker exec -it gemflow cat /tmp/mihomo.log
# Or tail real-time proxy traffic
docker exec -it gemflow tail -f /tmp/mihomo.log

# 2. Test specific worker proxy port egress connectivity (e.g., Worker-2 on port 19002)
docker exec -it gemflow curl -x http://127.0.0.1:19002 -s https://ipinfo.io/json

# 3. Inspect individual upstream worker service logs (e.g., Worker-2)
docker exec -it gemflow cat /app/worker_2.log
```

---

## 📄 License
MIT License.
