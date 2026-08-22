#!/usr/bin/env python3
"""
gemflow - 本地一键启动与多实例负载编排脚本 (macOS / Linux / Windows)
支持:
1. 自动生成多 worker 配置 (workers.json) 与多实例工作目录
2. 自动配置与拉起 Mihomo 多端口代理 (若指定 --sub 订阅链接)
3. 批量拉起 gemini_web2api 实例并常驻保活
4. 启动轻量智能粘滞负载网关 (lb_gateway.py)
"""

import sys
import os
import time
import json
import signal
import shutil
import glob
import argparse
import subprocess
import urllib.request

import mihomo_config
import assign_worker_nodes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKERS_JSON = os.path.join(BASE_DIR, "workers.json")
MIHOMO_CONFIG = os.path.join(BASE_DIR, "mihomo.yaml")
TEMPLATE_YAML = os.path.join(BASE_DIR, "mihomo.template.yaml")
SUB_FILE = os.path.join(BASE_DIR, "provider_urls.txt")
UPSTREAM_PY_URL = "https://raw.githubusercontent.com/Sophomoresty/gemini-web2api/refs/heads/main/gemini_web2api.py"
UPSTREAM_MIRROR_PY_URL = "https://ghfast.top/https://raw.githubusercontent.com/Sophomoresty/gemini-web2api/refs/heads/main/gemini_web2api.py"

BASE_WORKER_PORT = 9000
BASE_PROXY_PORT = 19000

PROCESSES = []


def fetch_latest_upstream(target_path, force=False):
    """自动拉取 upstream gemini_web2api.py 保证最新"""
    if os.path.exists(target_path) and not force:
        return True

    print("[gemflow] Fetching latest `gemini_web2api.py` from upstream repository...")
    urls = [
        os.environ.get("UPSTREAM_URL", UPSTREAM_PY_URL),
        os.environ.get("UPSTREAM_MIRROR_URL", UPSTREAM_MIRROR_PY_URL),
    ]

    for u in urls:
        if not u:
            continue
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "gemflow-launcher"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    code = resp.read().decode("utf-8")
                    if "import" in code and "def " in code:
                        with open(target_path, "w", encoding="utf-8") as f:
                            f.write(code)
                        print(f"[gemflow] Successfully fetched latest upstream script -> {target_path}")
                        return True
        except Exception as e:
            print(f"[gemflow] Fetch failed via {u}: {e}")

    if os.path.exists(target_path):
        print(f"[gemflow] Using existing cached `{target_path}`.")
        return True

    return False


def terminate_all(signum=None, frame=None):
    print("\n[gemflow] Shutting down all services...")
    for p in PROCESSES:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    time.sleep(1)
    for p in PROCESSES:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    print("[gemflow] All processes stopped.")
    sys.exit(0)


def detect_is_cn_host():
    """检测宿主机原生直连 IP 是否在中国大陆 (CN) 或直连 Google 受限"""
    # 1. 尝试 ip-api.com
    try:
        req = urllib.request.Request("http://ip-api.com/line?fields=countryCode", headers={"User-Agent": "curl/7.88.1"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            code = resp.read().decode("utf-8").strip().upper()
            if code == "CN":
                return True
            if code:
                return False
    except Exception:
        pass

    # 2. 尝试 ipinfo.io
    try:
        req = urllib.request.Request("https://ipinfo.io/country", headers={"User-Agent": "curl/7.88.1"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            code = resp.read().decode("utf-8").strip().upper()
            if code == "CN":
                return True
            if code:
                return False
    except Exception:
        pass

    # 3. 兜底测试 Google 直连
    try:
        req = urllib.request.Request("https://www.gstatic.com/generate_204")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 204:
                return False
    except Exception:
        return True

    return False


def generate_mihomo_config(worker_count, sub_urls):
    """渲染 Mihomo 运行配置 (与容器启动路径共用 mihomo_config 渲染器)"""
    # 清除旧 provider 缓存，确保每次启动都从订阅 URL 拉取最新节点
    for stale in glob.glob(os.path.join(BASE_DIR, "sub-*.yaml")):
        try:
            os.remove(stale)
        except OSError:
            pass

    try:
        mihomo_config.write_config(TEMPLATE_YAML, MIHOMO_CONFIG, worker_count,
                                   sub_urls, BASE_PROXY_PORT)
    except Exception as e:
        print(f"[Warning] Failed to render Mihomo config: {e}. Skipping proxy setup.")
        return False

    return True


def main():
    parser = argparse.ArgumentParser(description="gemflow - Gemini Multi-Instance Load Balancer")
    parser.add_argument("--workers", "-w", type=int, default=1, help="Number of worker instances (default: 1)")
    parser.add_argument("--port", "-p", type=int, default=8081, help="LB Gateway entryport (default: 8081)")
    parser.add_argument("--sub", "-s", type=str, default="", help="Subscription URL or path to subscription file")
    parser.add_argument("--update", action="store_true", help="Force update gemini_web2api.py from upstream repository")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logs")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, terminate_all)
    signal.signal(signal.SIGTERM, terminate_all)

    # 1. 收集订阅
    sub_urls = []
    if args.sub:
        if os.path.isfile(args.sub):
            with open(args.sub, "r", encoding="utf-8") as f:
                sub_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        else:
            sub_urls = [args.sub.strip()]
    elif os.path.exists(SUB_FILE):
        with open(SUB_FILE, "r", encoding="utf-8") as f:
            sub_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    env_subs = os.environ.get("PROVIDER_URLS", "")
    if env_subs:
        sub_urls.extend([line.strip() for line in env_subs.splitlines() if line.strip()])

    use_proxies = False
    is_cn_host = False
    if sub_urls:
        print(f"[gemflow] Configuring Mihomo for {args.workers} workers using {len(sub_urls)} subscription source(s)...")
        if generate_mihomo_config(args.workers, sub_urls):
            # 检查 mihomo 二进制是否在 PATH 中
            mihomo_bin = shutil.which("mihomo") or shutil.which("clash-meta")
            if mihomo_bin:
                try:
                    p = subprocess.Popen([mihomo_bin, "-d", BASE_DIR, "-f", MIHOMO_CONFIG],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    PROCESSES.append(p)
                    time.sleep(2)
                    if p.poll() is None:
                        use_proxies = True
                        print("[Mihomo] Started successfully.")
                    else:
                        print("[Mihomo] Warning: process exited early.")
                except Exception as e:
                    print(f"[Mihomo] Failed to spawn: {e}")
            else:
                print("[Mihomo] Binary 'mihomo' not found in PATH. Defaulting to DIRECT connections.")

    if use_proxies:
        is_cn_host = detect_is_cn_host()
        if is_cn_host:
            print("[Network] Host native IP detected in China (or direct Google blocked). Worker-1 proxy enabled.")
        else:
            print("[Network] Host native IP detected overseas. Worker-1 DIRECT native connection enabled.")

    # 2. 生成 workers.json
    workers = []
    for i in range(args.workers):
        wid = i + 1
        wport = BASE_WORKER_PORT + wid
        proxy = None
        if use_proxies:
            if i > 0 or is_cn_host:
                proxy = f"http://127.0.0.1:{BASE_PROXY_PORT + wid}"
        workers.append({"id": wid, "port": wport, "proxy": proxy})

    with open(WORKERS_JSON, "w", encoding="utf-8") as f:
        json.dump({"workers": workers}, f, indent=2)

    print(f"[gemflow] Created {WORKERS_JSON} with {len(workers)} worker(s):")
    for w in workers:
        print(f"  -> Worker-{w['id']}: Port {w['port']} [Egress: {w['proxy'] or 'DIRECT'}]")

    # 2.1 为各 Worker 策略组绑定互不相同的出口节点
    if use_proxies:
        direct_ids = [w["id"] for w in workers if not w.get("proxy")]
        try:
            assign_worker_nodes.assign(worker_count=args.workers,
                                       skip_worker_ids=direct_ids,
                                       max_wait=45.0)
        except Exception as e:
            print(f"[NodeAssign] Skipped due to error: {e}")

    # 3. 检查/拉取并启动 gemini_web2api 实例
    web2api_script = os.path.join(BASE_DIR, "gemini_web2api.py")
    fetch_latest_upstream(web2api_script, force=args.update)

    if not os.path.exists(web2api_script):
        print(f"\n[Notice] {web2api_script} not found in root.")
        print("  Place your `gemini_web2api.py` into this folder to automatically launch workers,")
        print("  or start your upstream servers independently on the ports listed above.")
    else:
        for w in workers:
            wid = w["id"]
            wport = w["port"]
            wdir = os.path.join(BASE_DIR, "instances", f"w{wid}")
            os.makedirs(wdir, exist_ok=True)

            w_cfg = {
                "port": wport,
                "api_keys": [],
                "cookie": "",
                "proxy": w["proxy"],
                "log_requests": False
            }
            with open(os.path.join(wdir, "config.json"), "w", encoding="utf-8") as f:
                json.dump(w_cfg, f, indent=2)

            log_file = open(os.path.join(BASE_DIR, f"worker_{wid}.log"), "w")
            p = subprocess.Popen([sys.executable, web2api_script, "--port", str(wport), "--config", os.path.join(wdir, "config.json")],
                                 cwd=wdir, stdout=log_file, stderr=log_file)
            PROCESSES.append(p)
            print(f"[Worker-{wid}] Started PID {p.pid} on port {wport}")

    # 4. 启动网关并展示访问信息
    print("\n" + "=" * 54)
    print(f"  🌟 gemflow Gateway Started: http://127.0.0.1:{args.port}")
    print("=" * 54)
    print("  API Entrypoint : http://127.0.0.1:" + str(args.port) + "/v1/chat/completions")
    print("  Models List    : http://127.0.0.1:" + str(args.port) + "/v1/models")
    print(f"  Debug Mode     : {'ENABLED' if (args.debug or os.environ.get('DEBUG', '').lower() in ('true', '1', 'yes')) else 'DISABLED'}")
    print("=" * 54 + "\n")

    gateway_script = os.path.join(BASE_DIR, "lb_gateway.py")
    cmd = [sys.executable, gateway_script, "--port", str(args.port), "--config", WORKERS_JSON]
    if args.debug or os.environ.get("DEBUG", "").lower() in ("true", "1", "yes"):
        cmd.append("--debug")

    p_gw = subprocess.Popen(cmd)
    PROCESSES.append(p_gw)
    p_gw.wait()


if __name__ == "__main__":
    main()
