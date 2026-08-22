import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mihomo_config
import assign_worker_nodes


TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "mihomo.template.yaml")


class TestMihomoConfigRender(unittest.TestCase):
    def setUp(self):
        with open(TEMPLATE, "r", encoding="utf-8") as f:
            self.template_text = f.read()

    def test_template_contains_marker(self):
        """模板必须保留 Worker 策略组注入标记"""
        self.assertIn(mihomo_config.WORKER_GROUPS_MARKER, self.template_text)

    def test_each_listener_binds_dedicated_group(self):
        """每个 listener 必须绑定到自己的 Worker 策略组，而非共享同一组"""
        out = mihomo_config.render(self.template_text, 4, ["http://sub.example/a"])

        for wid in range(1, 5):
            self.assertIn(f"port: {19000 + wid}", out)
            self.assertIn(f"    proxy: {mihomo_config.worker_group_name(wid)}", out)

        # 旧实现让所有 listener 共用 🚀 节点选择，这是 IP 相同的根因
        self.assertNotIn("proxy: 🚀 节点选择", out)

    def test_worker_groups_injected_inside_proxy_groups(self):
        """Worker 策略组须落在 proxy-groups 段内，且在 rules 之前"""
        out = mihomo_config.render(self.template_text, 3, ["http://sub.example/a"])

        groups_idx = out.index("proxy-groups:")
        rules_idx = out.index("\nrules:")
        for wid in range(1, 4):
            gidx = out.index(f"name: {mihomo_config.worker_group_name(wid)}")
            self.assertGreater(gidx, groups_idx)
            self.assertLess(gidx, rules_idx)

    def test_marker_line_fully_replaced(self):
        """标记行含尾部注释，替换后不得留下悬挂文本"""
        out = mihomo_config.render(self.template_text, 2, ["http://sub.example/a"])
        self.assertNotIn(mihomo_config.WORKER_GROUPS_MARKER, out)
        self.assertNotIn("勿删除此标记", out)

    def test_render_is_valid_yaml(self):
        out = mihomo_config.render(self.template_text, 3,
                                   ["http://sub.example/a", "http://sub.example/b"])
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")

        data = yaml.safe_load(out)
        self.assertEqual(len(data["listeners"]), 3)
        self.assertEqual(len(data["proxy-providers"]), 2)

        group_names = {g["name"] for g in data["proxy-groups"]}
        for wid in range(1, 4):
            self.assertIn(mihomo_config.worker_group_name(wid), group_names)

        for idx, listener in enumerate(data["listeners"], start=1):
            self.assertEqual(listener["proxy"], mihomo_config.worker_group_name(idx))

    def test_render_rejects_non_positive_worker_count(self):
        with self.assertRaises(ValueError):
            mihomo_config.render(self.template_text, 0, ["http://sub.example/a"])

    def test_render_rejects_template_without_marker(self):
        with self.assertRaises(ValueError):
            mihomo_config.render("proxy-groups:\n  - {name: x, type: select}\n", 2, [])

    def test_parse_provider_urls_strips_comments_and_blanks(self):
        raw = "http://a\n\n# comment\n  http://b  \nhttp://c\r\n"
        self.assertEqual(mihomo_config.parse_provider_urls(raw),
                         ["http://a", "http://b", "http://c"])

    def test_no_providers_yields_empty_block(self):
        out = mihomo_config.render(self.template_text, 1, [])
        self.assertNotIn("proxy-providers:", out)


def _node(delay, ntype="ss"):
    return {"type": ntype, "history": [{"delay": delay}]}


class TestNodeCandidateSelection(unittest.TestCase):
    def setUp(self):
        self.groups = [assign_worker_nodes.worker_group_name(i) for i in (2, 3, 4)]
        self.proxies = {
            g: {"type": "Selector", "all": ["node-a", "node-b", "node-c", "dead-node",
                                            mihomo_config.FALLBACK_GROUP,
                                            mihomo_config.DIRECT_PROXY]}
            for g in self.groups
        }
        self.proxies.update({
            "node-a": _node(300),
            "node-b": _node(100),
            "node-c": _node(200),
            "dead-node": _node(0),
            mihomo_config.FALLBACK_GROUP: {"type": "URLTest", "history": [{"delay": 90}]},
            mihomo_config.DIRECT_PROXY: {"type": "Direct", "history": [{"delay": 5}]},
        })

    def test_candidates_exclude_groups_and_dead_nodes(self):
        got = assign_worker_nodes.collect_candidate_nodes(self.proxies, self.groups)
        self.assertEqual(got, ["node-b", "node-c", "node-a"])

    def test_candidates_exclude_node_without_history(self):
        self.proxies["node-d"] = {"type": "vmess", "history": []}
        for g in self.groups:
            self.proxies[g]["all"].append("node-d")
        got = assign_worker_nodes.collect_candidate_nodes(self.proxies, self.groups)
        self.assertNotIn("node-d", got)

    def test_assign_gives_distinct_nodes_per_worker(self):
        calls = []
        orig = assign_worker_nodes.select_node
        orig_fetch = assign_worker_nodes.fetch_proxies
        try:
            assign_worker_nodes.select_node = lambda g, n, c, timeout=5: calls.append((g, n))
            assign_worker_nodes.fetch_proxies = lambda c, timeout=5: self.proxies

            result = assign_worker_nodes.assign(worker_count=4, skip_worker_ids=[1], max_wait=1)
        finally:
            assign_worker_nodes.select_node = orig
            assign_worker_nodes.fetch_proxies = orig_fetch

        self.assertEqual(sorted(result.keys()), [2, 3, 4])
        self.assertEqual(len(set(result.values())), 3)
        self.assertEqual([g for g, _ in calls],
                         [assign_worker_nodes.worker_group_name(i) for i in (2, 3, 4)])

    def test_assign_rotates_when_nodes_insufficient(self):
        few = {g: {"type": "Selector", "all": ["node-a", "node-b"]} for g in
               [assign_worker_nodes.worker_group_name(i) for i in (1, 2, 3)]}
        few.update({"node-a": _node(120), "node-b": _node(150)})

        orig = assign_worker_nodes.select_node
        orig_fetch = assign_worker_nodes.fetch_proxies
        try:
            assign_worker_nodes.select_node = lambda g, n, c, timeout=5: None
            assign_worker_nodes.fetch_proxies = lambda c, timeout=5: few
            result = assign_worker_nodes.assign(worker_count=3, skip_worker_ids=[], max_wait=1)
        finally:
            assign_worker_nodes.select_node = orig
            assign_worker_nodes.fetch_proxies = orig_fetch

        self.assertEqual(len(result), 3)
        # 2 个节点分给 3 个 Worker：轮转复用，Worker-1 与 Worker-3 相同
        self.assertEqual(result[1], result[3])
        self.assertNotEqual(result[1], result[2])

    def test_assign_keeps_fallback_when_no_healthy_node(self):
        dead = {assign_worker_nodes.worker_group_name(2): {"type": "Selector", "all": ["dead"]},
                "dead": _node(0)}
        orig_fetch = assign_worker_nodes.fetch_proxies
        try:
            assign_worker_nodes.fetch_proxies = lambda c, timeout=5: dead
            result = assign_worker_nodes.assign(worker_count=2, skip_worker_ids=[1], max_wait=1)
        finally:
            assign_worker_nodes.fetch_proxies = orig_fetch

        self.assertEqual(result, {})

    def test_assign_skips_when_all_workers_direct(self):
        result = assign_worker_nodes.assign(worker_count=1, skip_worker_ids=[1], max_wait=1)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
