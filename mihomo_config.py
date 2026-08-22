#!/usr/bin/env python3
"""
Mihomo 运行配置渲染器

在 mihomo.template.yaml 之上追加三段动态配置：
1. 每个 Worker 的专属策略组 (🎯 Worker-N)，用于绑定互不相同的固定出口节点
2. proxy-providers 订阅源
3. listeners 混合代理端口 (19001..19000+N)，逐一指向对应 Worker 策略组

被 start.sh (容器) 与 run_local.py (本地) 共用，保证两条启动路径生成完全一致的配置。
"""

import os
import argparse

BASE_PROXY_PORT = 19000
# 模板 proxy-groups 段内的注入锚点，Worker 专属策略组按整行替换写入
WORKER_GROUPS_MARKER = "__WORKER_GROUPS__"
WORKER_GROUP_PREFIX = "🎯 Worker-"
FALLBACK_GROUP = "♻️ 自动选择"
DIRECT_PROXY = "🟢 直连"
# 排除订阅中的直连类节点，只保留真实代理出口
NODE_FILTER = "^((?!(直连|Direct|DIRECT)).)*$"


def worker_group_name(worker_id):
    """Worker 专属策略组名称"""
    return f"{WORKER_GROUP_PREFIX}{worker_id}"


def build_worker_groups_block(worker_count):
    """
    渲染各 Worker 专属策略组。

    采用 select + include-all：默认选中 FALLBACK_GROUP (url-test 自动择优)，
    保证在节点分配未完成或分配失败时依旧可用；
    随后由 assign_worker_nodes.py 通过 REST API 改选为互不相同的固定节点。
    """
    lines = []
    for i in range(worker_count):
        group = worker_group_name(i + 1)
        lines.append(
            f'  - {{name: {group}, type: select, include-all: true, '
            f'filter: "{NODE_FILTER}", proxies: [{FALLBACK_GROUP}, {DIRECT_PROXY}]}}'
        )
    return "\n".join(lines) + "\n" if lines else ""


def build_providers_block(provider_urls):
    """渲染 proxy-providers 订阅源"""
    urls = [u.strip() for u in provider_urls if u and u.strip()]
    if not urls:
        return ""

    blocks = ["\nproxy-providers:"]
    for idx, url in enumerate(urls, start=1):
        blocks.append(
            f"  sub-{idx}:\n"
            f"    type: http\n"
            f'    url: "{url}"\n'
            f"    interval: 3600\n"
            f"    path: ./sub-{idx}.yaml\n"
            f"    health-check:\n"
            f"      enable: true\n"
            f"      interval: 180\n"
            f"      url: https://www.gstatic.com/generate_204"
        )
    return "\n".join(blocks) + "\n"


def build_listeners_block(worker_count, base_proxy_port=BASE_PROXY_PORT):
    """渲染 listeners，每个端口独立绑定到对应 Worker 策略组"""
    if worker_count <= 0:
        return ""

    blocks = ["\nlisteners:"]
    for i in range(worker_count):
        wid = i + 1
        blocks.append(
            f"  - name: mixed-{base_proxy_port + wid}\n"
            f"    type: mixed\n"
            f"    port: {base_proxy_port + wid}\n"
            f"    proxy: {worker_group_name(wid)}"
        )
    return "\n".join(blocks) + "\n"


def render(template_text, worker_count, provider_urls, base_proxy_port=BASE_PROXY_PORT):
    """基于模板文本生成完整配置 (纯函数，不修改入参)"""
    if worker_count <= 0:
        raise ValueError(f"worker_count must be positive, got {worker_count}")

    if WORKER_GROUPS_MARKER not in template_text:
        raise ValueError(
            f"Template missing required marker `{WORKER_GROUPS_MARKER}` inside proxy-groups"
        )

    # Worker 策略组必须落在 proxy-groups 段内，故整行替换标记行
    # (标记行含尾部说明注释，按行替换避免残留文本破坏 YAML)
    groups = build_worker_groups_block(worker_count).rstrip("\n")
    lines = template_text.splitlines()
    body_lines = [groups if WORKER_GROUPS_MARKER in line else line for line in lines]
    body = "\n".join(body_lines) + "\n"

    return (
        body
        + build_providers_block(provider_urls)
        + build_listeners_block(worker_count, base_proxy_port)
    )


def parse_provider_urls(raw):
    """解析多行订阅文本，去除空行、注释与 CR"""
    if not raw:
        return []
    return [
        line.strip().rstrip("\r")
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def write_config(template_path, out_path, worker_count, provider_urls,
                 base_proxy_port=BASE_PROXY_PORT):
    """读取模板、渲染并写出配置文件"""
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Mihomo template not found: {template_path}")

    with open(template_path, "r", encoding="utf-8") as f:
        template_text = f.read()

    config = render(template_text, worker_count, provider_urls, base_proxy_port)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(config)

    return config


def main():
    parser = argparse.ArgumentParser(description="Render Mihomo runtime configuration")
    parser.add_argument("--template", required=True, help="Path to mihomo.template.yaml")
    parser.add_argument("--out", required=True, help="Output config path")
    parser.add_argument("--workers", type=int, required=True, help="Worker count")
    parser.add_argument("--base-proxy-port", type=int, default=BASE_PROXY_PORT,
                        help=f"Base listener port (default: {BASE_PROXY_PORT})")
    parser.add_argument("--providers-env", default="PROVIDER_URLS",
                        help="Environment variable holding newline separated subscription URLs")
    args = parser.parse_args()

    urls = parse_provider_urls(os.environ.get(args.providers_env, ""))
    if not urls:
        print(f"[Mihomo] No subscription URL found in ${args.providers_env}.", flush=True)
        return 1

    try:
        write_config(args.template, args.out, args.workers, urls, args.base_proxy_port)
    except Exception as e:
        print(f"[Mihomo] Failed to render config: {e}", flush=True)
        return 1

    print(f"[Mihomo] Rendered config with {len(urls)} provider(s) and "
          f"{args.workers} dedicated worker group(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
