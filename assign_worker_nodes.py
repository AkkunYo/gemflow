#!/usr/bin/env python3
"""
Worker 出口节点分配器

通过 mihomo external-controller REST API，为每个 Worker 专属策略组
(🎯 Worker-N) 绑定互不相同的健康节点，使各 Worker 监听端口拥有独立出口 IP。

分配失败或健康节点不足时保留策略组默认的 ♻️ 自动选择，保证可用性优先。
"""

import json
import time
import argparse
import urllib.parse
import urllib.request
import urllib.error

from mihomo_config import worker_group_name, FALLBACK_GROUP, DIRECT_PROXY

DEFAULT_CONTROLLER = "127.0.0.1:9090"
# 非真实出口节点：策略组自身与直连类节点
EXCLUDED_NODE_TYPES = {"selector", "urltest", "fallback", "loadbalance", "relay", "direct", "reject"}
EXCLUDED_NODE_NAMES = {FALLBACK_GROUP, DIRECT_PROXY, "DIRECT", "REJECT", "PASS", "COMPATIBLE", "GLOBAL"}


def _api_url(controller, path):
    return f"http://{controller}{path}"


def _request(controller, path, method="GET", payload=None, timeout=5):
    """调用 mihomo REST API，返回解析后的 JSON (无响应体时返回 None)"""
    url = _api_url(controller, path)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


def fetch_proxies(controller, timeout=5):
    """拉取全部代理信息 {name: detail}"""
    data = _request(controller, "/proxies", timeout=timeout) or {}
    return data.get("proxies", {})


def is_node_alive(detail):
    """依据 health-check 历史判断节点是否可用 (delay > 0)"""
    history = detail.get("history") or []
    if not history:
        return False
    last = history[-1]
    return isinstance(last, dict) and isinstance(last.get("delay"), int) and last["delay"] > 0


def node_latency(detail):
    """最近一次探测延迟，未知时视为极大值以便排序靠后"""
    history = detail.get("history") or []
    if not history:
        return 10 ** 9
    last = history[-1]
    delay = last.get("delay") if isinstance(last, dict) else None
    return delay if isinstance(delay, int) and delay > 0 else 10 ** 9


def collect_candidate_nodes(proxies, group_names):
    """
    从策略组的 all 列表中收集真实出口节点候选，按延迟升序排列。

    只取 Worker 策略组可见的节点 (即订阅节点)，排除策略组自身与直连类节点。
    """
    visible = set()
    for group in group_names:
        detail = proxies.get(group) or {}
        for name in detail.get("all") or []:
            visible.add(name)

    candidates = []
    for name in visible:
        if name in EXCLUDED_NODE_NAMES:
            continue
        detail = proxies.get(name)
        if not detail:
            continue
        if (detail.get("type") or "").lower() in EXCLUDED_NODE_TYPES:
            continue
        if not is_node_alive(detail):
            continue
        candidates.append(name)

    candidates.sort(key=lambda n: (node_latency(proxies[n]), n))
    return candidates


def select_node(group, node, controller, timeout=5):
    """将策略组切换到指定节点"""
    path = f"/proxies/{urllib.parse.quote(group, safe='')}"
    _request(controller, path, method="PUT", payload={"name": node}, timeout=timeout)


def wait_for_candidates(controller, group_names, worker_count, max_wait, poll_interval=3.0):
    """
    轮询等待订阅加载与 health-check 完成。

    达到 worker_count 个健康节点即刻返回；超时则返回当前已知候选 (可能为空)。
    """
    deadline = time.time() + max_wait
    candidates = []

    while True:
        try:
            proxies = fetch_proxies(controller)
            candidates = collect_candidate_nodes(proxies, group_names)
            if len(candidates) >= worker_count:
                return candidates
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[NodeAssign] Controller not ready yet: {type(e).__name__}", flush=True)

        if time.time() >= deadline:
            return candidates
        time.sleep(min(poll_interval, max(0.5, deadline - time.time())))


def assign(controller=DEFAULT_CONTROLLER, worker_count=1, skip_worker_ids=(), max_wait=45.0):
    """
    为各 Worker 策略组分配互不相同的节点。

    skip_worker_ids: 走直连的 Worker (无需代理端口)，跳过分配。
    返回 {worker_id: node_name} 的实际分配结果。
    """
    target_ids = [i + 1 for i in range(worker_count) if (i + 1) not in set(skip_worker_ids)]
    if not target_ids:
        print("[NodeAssign] No proxied worker to assign. Skipped.", flush=True)
        return {}

    group_names = [worker_group_name(wid) for wid in target_ids]
    candidates = wait_for_candidates(controller, group_names, len(target_ids), max_wait)

    if not candidates:
        print("[NodeAssign] Warning: no healthy node found. "
              f"All workers stay on {FALLBACK_GROUP} (shared egress).", flush=True)
        return {}

    if len(candidates) < len(target_ids):
        print(f"[NodeAssign] Notice: only {len(candidates)} healthy node(s) for "
              f"{len(target_ids)} proxied worker(s). Nodes will be reused in rotation.", flush=True)

    assigned = {}
    for idx, wid in enumerate(target_ids):
        node = candidates[idx % len(candidates)]
        group = worker_group_name(wid)
        try:
            select_node(group, node, controller)
            assigned[wid] = node
            print(f"[NodeAssign] Worker-{wid} -> {node}", flush=True)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[NodeAssign] Worker-{wid} assignment failed ({type(e).__name__}); "
                  f"keeping {FALLBACK_GROUP}.", flush=True)

    distinct = len(set(assigned.values()))
    print(f"[NodeAssign] Completed: {len(assigned)}/{len(target_ids)} worker(s) bound, "
          f"{distinct} distinct egress node(s).", flush=True)
    return assigned


def main():
    parser = argparse.ArgumentParser(description="Assign distinct egress nodes to worker groups")
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER,
                        help=f"Mihomo external controller (default: {DEFAULT_CONTROLLER})")
    parser.add_argument("--workers", type=int, required=True, help="Total worker count")
    parser.add_argument("--skip", default="",
                        help="Comma separated worker ids that use direct connection")
    parser.add_argument("--max-wait", type=float, default=45.0,
                        help="Max seconds to wait for provider health-check (default: 45)")
    args = parser.parse_args()

    skip_ids = []
    for part in args.skip.split(","):
        part = part.strip()
        if part.isdigit():
            skip_ids.append(int(part))

    assign(controller=args.controller, worker_count=args.workers,
           skip_worker_ids=skip_ids, max_wait=args.max_wait)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
