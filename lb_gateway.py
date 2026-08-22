#!/usr/bin/env python3
"""
Smart Load Balancer Gateway for Gemini-Web2API
- Port: 8081 (Unified Entrypoint)
- Sticky Session based on User / First-Prompt MD5 fingerprint
- Least-Connection scheduling for new sessions
- Automatic failover & retry on 429/5xx/ConnectionError
- Full SSE stream & chunked response pass-through
- Debug logging mode via DEBUG=true / --debug
"""

import sys
import os
import json
import time
import hashlib
import threading
import argparse
from collections import OrderedDict
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
import http.client

# 全局状态锁与路由表
LOCK = threading.Lock()
ACTIVE_CONNS = {}       # {worker_id: count}
SESSION_MAP = OrderedDict()  # {session_id: (worker_id, last_seen_time)} - LRU 映射表
WORKER_STATUS = {}      # {worker_id: {"last_fail": 0, "fail_count": 0}}
WORKERS = []            # [{"id": 1, "port": 9001, "proxy": None}, ...]
RR_INDEX = 0            # 轮询游标
REQ_COUNTER = 0
DEBUG_ENABLED = False

SESSION_TTL = 1800      # 会话粘滞有效期 (30分钟)
MAX_SESSIONS = 50000    # 最大缓存会话数上限 (LRU 淘汰防止内存无限增长)
FAIL_PENALTY_SEC = 20   # 故障节点冷却降权时间 (秒)


def log_debug(msg):
    if DEBUG_ENABLED:
        print(f"[DEBUG @ {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_workers(config_path="workers.json"):
    global WORKERS, ACTIVE_CONNS, WORKER_STATUS
    if not os.path.exists(config_path):
        WORKERS = [{"id": 1, "port": 9001, "proxy": None}]
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                WORKERS = [w for w in data.get("workers", []) if w.get("port") != 8081]
                if not WORKERS:
                    WORKERS = data.get("workers", [{"id": 1, "port": 9001, "proxy": None}])
        except Exception as e:
            print(f"[LB] Failed to read {config_path}: {e}")
            WORKERS = [{"id": 1, "port": 9001, "proxy": None}]

    with LOCK:
        for w in WORKERS:
            wid = w["id"]
            if wid not in ACTIVE_CONNS:
                ACTIVE_CONNS[wid] = 0
            if wid not in WORKER_STATUS:
                WORKER_STATUS[wid] = {"last_fail": 0, "fail_count": 0}

    # 各 Worker 的端口与出口路由已由启动脚本逐行打印，此处只报总数避免重复长行输出
    proxied = sum(1 for w in WORKERS if w.get("proxy"))
    print(f"[LB] Loaded {len(WORKERS)} worker(s) "
          f"({proxied} proxied, {len(WORKERS) - proxied} direct).", flush=True)


def cleanup_stale_sessions():
    now = time.time()
    with LOCK:
        stale_keys = [k for k, v in SESSION_MAP.items() if now - v[1] > SESSION_TTL]
        for k in stale_keys:
            del SESSION_MAP[k]


def record_session(session_id, wid, seen_time=None):
    """记录/更新 Session 映射 (带 LRU 容量上限保护)"""
    if not session_id:
        return
    if seen_time is None:
        seen_time = time.time()
    # 移至最新位置
    if session_id in SESSION_MAP:
        SESSION_MAP.move_to_end(session_id)
    SESSION_MAP[session_id] = (wid, seen_time)

    # 达到上限时弹出最老未使用的会话 (FIFO / LRU 头部)
    while len(SESSION_MAP) > MAX_SESSIONS:
        SESSION_MAP.popitem(last=False)


def extract_request_meta(headers, body_bytes):
    """
    提取会话特征指纹与调试摘要
    """
    session_id = None
    prompt_snippet = ""
    model_name = ""

    try:
        if body_bytes:
            body = json.loads(body_bytes.decode("utf-8"))
            model_name = body.get("model", "")

            # 1. 客户端显式传递的 user 标识
            if body.get("user"):
                session_id = f"usr_{body['user']}"

            # 2. 对话首条 user prompt 特征指纹
            messages = body.get("messages", [])
            if isinstance(messages, list):
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        content = m.get("content", "")
                        if isinstance(content, list):
                            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                            content = "".join(text_parts)
                        if content:
                            prompt_snippet = str(content).strip()[:60].replace("\n", " ")
                            if not session_id:
                                h = hashlib.md5(str(content)[:128].encode("utf-8")).hexdigest()[:12]
                                session_id = f"ctx_{h}"
                            break
    except Exception:
        pass

    # 3. 兜底按 Cookie / Ctoken / Authorization 特征指纹粘滞
    if not session_id:
        cookie_header = headers.get("Cookie") or headers.get("cookie") or headers.get("X-Gemini-Cookie") or headers.get("x-gemini-cookie") or headers.get("X-Ctoken") or headers.get("x-ctoken")
        if cookie_header:
            h = hashlib.md5(cookie_header.encode("utf-8")).hexdigest()[:10]
            session_id = f"cookie_{h}"

    # 4. 兜底按 Authorization Key 粘滞
    if not session_id:
        auth = headers.get("Authorization") or headers.get("authorization")
        if auth:
            h = hashlib.md5(auth.encode("utf-8")).hexdigest()[:10]
            session_id = f"auth_{h}"

    return session_id, model_name, prompt_snippet


def select_worker(session_id, exclude_wids=None):
    global RR_INDEX
    if exclude_wids is None:
        exclude_wids = set()

    now = time.time()
    with LOCK:
        available_workers = [w for w in WORKERS if w["id"] not in exclude_wids]
        if not available_workers:
            available_workers = WORKERS

        # 检查是否为老会话
        if session_id and session_id in SESSION_MAP:
            bound_wid, _ = SESSION_MAP[session_id]
            st = WORKER_STATUS.get(bound_wid, {"last_fail": 0})
            is_cooling_down = (now - st.get("last_fail", 0)) < FAIL_PENALTY_SEC

            # 若绑定的 Worker 正常且未被本次请求排除，优先命中
            if bound_wid not in exclude_wids and not is_cooling_down:
                target = next((w for w in available_workers if w["id"] == bound_wid), None)
                if target:
                    record_session(session_id, bound_wid, now)
                    return target, "STICKY"

        # 最少连接 + 严格循环轮询（并发相等时严格依次推进轮换）
        def score(w):
            wid = w["id"]
            base_conn = ACTIVE_CONNS.get(wid, 0)
            st = WORKER_STATUS.get(wid, {"last_fail": 0, "fail_count": 0})
            penalty = 100 if (now - st["last_fail"] < FAIL_PENALTY_SEC) else 0
            return base_conn + penalty

        min_score = min(score(w) for w in available_workers)
        candidates = [w for w in available_workers if score(w) == min_score]

        best_worker = candidates[RR_INDEX % len(candidates)]
        RR_INDEX += 1

        if session_id:
            record_session(session_id, best_worker["id"], now)
        return best_worker, "LEAST_CONN"


def record_worker_success(wid):
    with LOCK:
        if wid in WORKER_STATUS:
            WORKER_STATUS[wid]["fail_count"] = 0


def record_worker_failure(wid):
    with LOCK:
        if wid in WORKER_STATUS:
            WORKER_STATUS[wid]["last_fail"] = time.time()
            WORKER_STATUS[wid]["fail_count"] += 1


class LBProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._proxy_request("GET")

    def do_POST(self):
        self._proxy_request("POST")

    def do_HEAD(self):
        self._proxy_request("HEAD")

    def _proxy_request(self, method):
        global REQ_COUNTER
        with LOCK:
            REQ_COUNTER += 1
            req_id = REQ_COUNTER

        start_time = time.time()

        # 1. 读取请求 Body
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else b""

        # 2. 会话指纹与调试信息提取
        session_id, model_name, prompt_snippet = extract_request_meta(self.headers, body_bytes) if method == "POST" else (None, "", "")

        # 3. 准备转发 Header
        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "connection"):
                fwd_headers[k] = v
        fwd_headers["Connection"] = "close"

        exclude_wids = set()
        max_attempts = min(len(WORKERS), 3) if len(WORKERS) > 1 else 1

        for attempt in range(max_attempts):
            worker, route_type = select_worker(session_id, exclude_wids)
            wid = worker["id"]
            wport = worker["port"]
            wproxy = worker.get("proxy") or "DIRECT"

            with LOCK:
                ACTIVE_CONNS[wid] = ACTIVE_CONNS.get(wid, 0) + 1
                curr_active = ACTIVE_CONNS[wid]

            target_url = f"http://127.0.0.1:{wport}{self.path}"

            if attempt == 0:
                log_debug(
                    f"[Req #{req_id}] [{method}] {self.path} | Model: '{model_name or 'N/A'}' | Session: {session_id or 'NONE'} | "
                    f"Prompt: \"{prompt_snippet[:40]}...\" -> Selected: Worker-{wid} (Port {wport}, Egress: {wproxy}, Route: {route_type}, Active: {curr_active})"
                )
            else:
                log_debug(
                    f"[Req #{req_id}] [RETRY #{attempt}] Failover -> Worker-{wid} (Port {wport}, Egress: {wproxy})"
                )

            req = urllib.request.Request(
                target_url,
                data=body_bytes if method in ("POST", "PUT") else None,
                headers=fwd_headers,
                method=method
            )

            try:
                resp = urllib.request.urlopen(req, timeout=180)
                record_worker_success(wid)

                self.send_response(resp.status)
                for hk, hv in resp.getheaders():
                    if hk.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(hk, hv)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                # 流式透传
                try:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                finally:
                    resp.close()

                with LOCK:
                    ACTIVE_CONNS[wid] = max(0, ACTIVE_CONNS.get(wid, 1) - 1)

                elapsed = time.time() - start_time
                log_debug(f"[Req #{req_id}] Completed in {elapsed:.2f}s | HTTP {resp.status} via Worker-{wid} ({wproxy})")
                return

            except urllib.error.HTTPError as e:
                with LOCK:
                    ACTIVE_CONNS[wid] = max(0, ACTIVE_CONNS.get(wid, 1) - 1)

                if e.code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                    log_debug(f"[Req #{req_id}] Worker-{wid} failed with HTTP {e.code}, triggering failover...")
                    record_worker_failure(wid)
                    exclude_wids.add(wid)
                    continue

                record_worker_failure(wid)
                err_body = e.read()
                self.send_response(e.code)
                for hk, hv in e.headers.items():
                    if hk.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(hk, hv)
                self.send_header("Content-Length", str(len(err_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(err_body)
                elapsed = time.time() - start_time
                log_debug(f"[Req #{req_id}] Finished with Error HTTP {e.code} in {elapsed:.2f}s via Worker-{wid}")
                return

            except Exception as e:
                with LOCK:
                    ACTIVE_CONNS[wid] = max(0, ACTIVE_CONNS.get(wid, 1) - 1)

                record_worker_failure(wid)
                exclude_wids.add(wid)

                if attempt < max_attempts - 1:
                    log_debug(f"[Req #{req_id}] Worker-{wid} connection error ({e}), triggering failover...")
                    continue

                elapsed = time.time() - start_time
                log_debug(f"[Req #{req_id}] All workers failed after {elapsed:.2f}s: {e}")
                err_msg = json.dumps({"error": {"message": f"Load balancer: all upstream workers unavailable ({e})", "type": "bad_gateway", "code": 502}}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_msg)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(err_msg)
                return


def probe_single_worker_egress(worker, timeout=5):
    """探测单个 Worker 实例的实际出口 IP 与归属地信息"""
    wid = worker["id"]
    wport = worker["port"]
    proxy = worker.get("proxy")

    if proxy:
        port_str = proxy.rstrip("/").split(":")[-1]
        route_desc = f"Proxy (:{port_str})"
    else:
        route_desc = "DIRECT (Native)"

    headers = {"User-Agent": "gemflow-egress-probe/1.0"}
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(proxy_handler)
    else:
        opener = urllib.request.build_opener()

    info_str = "Unknown / Probe Failed"
    # 1. 尝试 ip-api.com (HTTP, 免 API Key)
    try:
        req = urllib.request.Request("http://ip-api.com/json", headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == "success":
                    country = data.get("country", "Unknown")
                    city = data.get("city", "")
                    query = data.get("query", "Unknown")
                    org = data.get("org") or data.get("isp") or "Unknown"
                    location = f"{country} ({city})" if city else country
                    info_str = f"{location} - IP: {query} [{org}]"
                    return f"[{f'Worker-{wid} : Port {wport} : {route_desc}':<38}] -> {info_str}"
    except Exception:
        pass

    # 2. 兜底尝试 ipinfo.io (HTTPS)
    try:
        req = urllib.request.Request("https://ipinfo.io/json", headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                ip = data.get("ip", "Unknown")
                country = data.get("country", "Unknown")
                city = data.get("city", "")
                org = data.get("org", "Unknown")
                location = f"{country} ({city})" if city else country
                info_str = f"{location} - IP: {ip} [{org}]"
                return f"[{f'Worker-{wid} : Port {wport} : {route_desc}':<38}] -> {info_str}"
    except Exception as e:
        info_str = f"Connection Failed ({type(e).__name__})"

    return f"[{f'Worker-{wid} : Port {wport} : {route_desc}':<38}] -> {info_str}"


def evaluate_egress_readiness(workers, results):
    """
    判定本轮探测是否代表"代理链路已生效"，可以打印首次状态面板。

    纯函数，便于单测。判定口径：
    - 所有 Worker 探测均成功（无连接/探测失败）
    - 若同时存在直连 Worker，则代理 Worker 至少有一个出口 IP 与原生直连 IP 不同
      （用于排除 mihomo 尚未就绪、代理端口实际回落直连的情况）

    不要求各代理出口 IP 互不相同：健康节点少于 Worker 数时轮转复用是合法结果。
    """
    ip_by_wid = {}
    for wid, line in results:
        if "IP: " in line:
            ip_by_wid[wid] = line.split("IP: ")[1].split()[0]

    has_failed = any("Connection Failed" in line or "Probe Failed" in line
                     for _, line in results)
    if has_failed or not results:
        return False

    proxy_wids = {w["id"] for w in workers if w.get("proxy")}
    direct_wids = {w["id"] for w in workers if not w.get("proxy")}

    if not proxy_wids:
        return True

    proxy_ips = {ip_by_wid[wid] for wid in proxy_wids if wid in ip_by_wid}
    if not proxy_ips:
        return False

    direct_ips = {ip_by_wid[wid] for wid in direct_wids if wid in ip_by_wid}
    if direct_ips:
        return bool(proxy_ips - direct_ips)

    return True


def async_inspect_egress_ips(initial_delay=3.0, poll_interval=300.0,
                             first_print_deadline=180.0):
    """
    后台异步守护线程：
    持续探测各 Worker 出口 IP，每轮探测成功后打印完整状态面板。

    首次打印需等待代理链路实际生效（见 evaluate_egress_readiness），
    但最长只等待 first_print_deadline 秒，超时后无条件打印当前实况，
    避免节点异常时状态面板永久静默；此后每 poll_interval 周期打印一次。
    """
    def _worker_task():
        if initial_delay > 0:
            time.sleep(initial_delay)

        has_ever_printed = False
        started_at = time.time()

        while True:
            with LOCK:
                workers_copy = list(WORKERS)
            if not workers_copy:
                time.sleep(poll_interval)
                continue

            results = []
            threads = []
            res_lock = threading.Lock()

            def _probe_w(w):
                res = probe_single_worker_egress(w)
                with res_lock:
                    results.append((w["id"], res))

            for w in workers_copy:
                t = threading.Thread(target=_probe_w, args=(w,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=8)

            results.sort(key=lambda x: x[0])

            if has_ever_printed:
                # 稳定期：每轮如实打印当前出口实况
                should_print = True
            elif evaluate_egress_readiness(workers_copy, results):
                should_print = True
            else:
                # 代理链路迟迟未生效时，超过截止时间也打印一次实况便于排查
                should_print = (time.time() - started_at) >= first_print_deadline
                if should_print:
                    print("[LB] Warning: proxy egress not confirmed within "
                          f"{int(first_print_deadline)}s. Printing current state as-is.",
                          flush=True)

            if should_print:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print("\n" + f"========== [Worker Egress IP Status @ {timestamp}] ==========", flush=True)
                for _, line in results:
                    print(line, flush=True)
                print("=" * 70 + "\n", flush=True)
                has_ever_printed = True

            time.sleep(5.0 if not has_ever_printed else poll_interval)

    t = threading.Thread(target=_worker_task, daemon=True)
    t.start()


def start_lb_server(port=8081, config_path="workers.json", debug=False):
    global DEBUG_ENABLED
    DEBUG_ENABLED = debug
    if DEBUG_ENABLED:
        print("[LB] >>> DEBUG logging mode is ENABLED <<<", flush=True)

    load_workers(config_path)

    # 启动后台异步出口 IP 探测
    async_inspect_egress_ips(initial_delay=15.0)

    def timer_loop():
        while True:
            time.sleep(300)
            cleanup_stale_sessions()

    t = threading.Thread(target=timer_loop, daemon=True)
    t.start()

    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, LBProxyHandler)
    print(f"[LB] Gemini Load Balancer Gateway listening on http://0.0.0.0:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gemini-Web2API Load Balancer Gateway")
    parser.add_argument("--port", type=int, default=8081, help="Gateway listen port (default: 8081)")
    parser.add_argument("--config", type=str, default="workers.json", help="Workers config JSON file")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    # 从命令行参数或环境变量 DEBUG=true / DEBUG=1 中读取调试开关
    debug_mode = args.debug or os.environ.get("DEBUG", "").strip().lower() in ("true", "1", "yes", "on")
    start_lb_server(port=args.port, config_path=args.config, debug=debug_mode)
