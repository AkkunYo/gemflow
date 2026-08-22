import json
import time
import unittest
import os
import sys

# Ensure root directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import OrderedDict
from lb_gateway import (
    extract_request_meta,
    select_worker,
    cleanup_stale_sessions,
    record_session,
    record_worker_failure,
    record_worker_success,
    LOCK,
)
import lb_gateway


class TestGatewayCore(unittest.TestCase):
    def setUp(self):
        """每个测试用例前重置网关状态"""
        with LOCK:
            lb_gateway.WORKERS = [
                {"id": 1, "port": 9001, "proxy": None},
                {"id": 2, "port": 9002, "proxy": "http://127.0.0.1:19002"},
                {"id": 3, "port": 9003, "proxy": "http://127.0.0.1:19003"},
            ]
            lb_gateway.ACTIVE_CONNS = {1: 0, 2: 0, 3: 0}
            lb_gateway.SESSION_MAP = OrderedDict()
            lb_gateway.WORKER_STATUS = {
                1: {"last_fail": 0, "fail_count": 0},
                2: {"last_fail": 0, "fail_count": 0},
                3: {"last_fail": 0, "fail_count": 0},
            }
            lb_gateway.RR_INDEX = 0
            lb_gateway.REQ_COUNTER = 0

    def test_explicit_user_field(self):
        body = json.dumps({
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "Hello"}],
            "user": "user-abc-123"
        }).encode("utf-8")

        session_id, model, snippet = extract_request_meta({}, body)
        self.assertEqual(session_id, "usr_user-abc-123")
        self.assertEqual(model, "gemini-2.5-flash")
        self.assertEqual(snippet, "Hello")

    def test_prompt_content_fingerprint(self):
        body = json.dumps({
            "model": "gemini-2.5-pro",
            "messages": [{"role": "user", "content": "Explain quantum physics in detail"}]
        }).encode("utf-8")

        session_id, model, snippet = extract_request_meta({}, body)
        self.assertTrue(session_id.startswith("ctx_"))
        self.assertEqual(model, "gemini-2.5-pro")
        self.assertEqual(snippet, "Explain quantum physics in detail")

    def test_auth_header_fallback(self):
        headers = {"Authorization": "Bearer sk-secret-token-xyz"}
        body = b"{}"
        session_id, model, snippet = extract_request_meta(headers, body)
        self.assertTrue(session_id.startswith("auth_"))

    def test_custom_cookie_ctoken_header(self):
        # 1. 测试 X-Gemini-Cookie Header
        headers = {"X-Gemini-Cookie": "SIDCC=dummy-sidcc-token; __Secure-1PSID=xyz"}
        body = b"{}"
        session_id, _, _ = extract_request_meta(headers, body)
        self.assertTrue(session_id.startswith("cookie_"))

        # 2. 测试 X-Ctoken Header
        headers2 = {"X-Ctoken": "ctoken_secret_value_123"}
        session_id2, _, _ = extract_request_meta(headers2, body)
        self.assertTrue(session_id2.startswith("cookie_"))

    def test_sticky_session_pinning(self):
        # 第一次请求分配 Worker
        worker1, route1 = select_worker("ctx_test_session_1")
        self.assertEqual(route1, "LEAST_CONN")

        # 第二次相同 session 请求必须命中相同的 Worker (STICKY)
        worker2, route2 = select_worker("ctx_test_session_1")
        self.assertEqual(route2, "STICKY")
        self.assertEqual(worker1["id"], worker2["id"])

    def test_least_conn_and_round_robin(self):
        # 当所有 worker 活跃连接为 0 时，应轮询分配
        w1, _ = select_worker(None)
        w2, _ = select_worker(None)
        w3, _ = select_worker(None)

        self.assertEqual([w1["id"], w2["id"], w3["id"]], [1, 2, 3])

    def test_failover_cooling_penalty(self):
        # 绑定 session 到 Worker 1
        w1, _ = select_worker("ctx_session_fail")
        self.assertEqual(w1["id"], 1)

        # 标记 Worker 1 故障
        record_worker_failure(1)

        # 再次路由该 session，由于 Worker 1 在冷却池中，应自动调度到其他健康节点
        w_next, route = select_worker("ctx_session_fail")
        self.assertNotEqual(w_next["id"], 1)
        self.assertEqual(route, "LEAST_CONN")

    def test_cleanup_expired_sessions(self):
        # 注入一个过期 session (1小时前)
        with LOCK:
            lb_gateway.SESSION_MAP["expired_session"] = (1, time.time() - 4000)
            lb_gateway.SESSION_MAP["active_session"] = (2, time.time())

        cleanup_stale_sessions()

        with LOCK:
            self.assertNotIn("expired_session", lb_gateway.SESSION_MAP)
            self.assertIn("active_session", lb_gateway.SESSION_MAP)

    def test_lru_session_cap_eviction(self):
        orig_max = lb_gateway.MAX_SESSIONS
        try:
            lb_gateway.MAX_SESSIONS = 3
            with LOCK:
                record_session("s1", 1)
                record_session("s2", 2)
                record_session("s3", 3)
                self.assertEqual(len(lb_gateway.SESSION_MAP), 3)

                # 访问 s1，刷新 LRU 顺序
                record_session("s1", 1)

                # 插入 s4，此时应淘汰最久未访问的 s2
                record_session("s4", 1)
                self.assertEqual(len(lb_gateway.SESSION_MAP), 3)
                self.assertIn("s1", lb_gateway.SESSION_MAP)
                self.assertNotIn("s2", lb_gateway.SESSION_MAP)
                self.assertIn("s3", lb_gateway.SESSION_MAP)
                self.assertIn("s4", lb_gateway.SESSION_MAP)
        finally:
            lb_gateway.MAX_SESSIONS = orig_max


    def test_probe_single_worker_egress(self):
        worker = {"id": 1, "port": 9001, "proxy": None}
        res = lb_gateway.probe_single_worker_egress(worker, timeout=1)
        self.assertTrue("Worker-1" in res)
        self.assertTrue("Port 9001" in res)


def _line(wid, ip):
    return (wid, f"[Worker-{wid} : Port 900{wid} : x] -> United States (Ashburn) - IP: {ip} [ISP]")


class TestEgressReadiness(unittest.TestCase):
    """首次打印门控：确认代理链路真正生效后才输出状态面板"""

    DIRECT_W = {"id": 1, "port": 9001, "proxy": None}

    @staticmethod
    def _proxy_w(wid):
        return {"id": wid, "port": 9000 + wid, "proxy": f"http://127.0.0.1:1900{wid}"}

    def test_not_ready_when_proxy_falls_back_to_direct_ip(self):
        """代理端口出口与原生直连完全相同 => mihomo 未生效，不该打印"""
        workers = [self.DIRECT_W, self._proxy_w(2), self._proxy_w(3)]
        results = [_line(1, "44.196.116.2"), _line(2, "44.196.116.2"), _line(3, "44.196.116.2")]
        self.assertFalse(lb_gateway.evaluate_egress_readiness(workers, results))

    def test_ready_when_proxy_ip_differs_from_direct(self):
        workers = [self.DIRECT_W, self._proxy_w(2), self._proxy_w(3)]
        results = [_line(1, "44.196.116.2"), _line(2, "137.131.35.71"), _line(3, "5.6.7.8")]
        self.assertTrue(lb_gateway.evaluate_egress_readiness(workers, results))

    def test_ready_when_proxies_share_node_but_differ_from_direct(self):
        """健康节点少于 Worker 数时轮转复用是合法结果，不应卡住首次打印"""
        workers = [self.DIRECT_W, self._proxy_w(2), self._proxy_w(3)]
        results = [_line(1, "44.196.116.2"), _line(2, "137.131.35.71"), _line(3, "137.131.35.71")]
        self.assertTrue(lb_gateway.evaluate_egress_readiness(workers, results))

    def test_ready_with_single_proxy_worker_and_no_direct(self):
        """全代理且仅 1 个 Worker：无从比较，探测成功即视为就绪"""
        workers = [self._proxy_w(1)]
        results = [_line(1, "137.131.35.71")]
        self.assertTrue(lb_gateway.evaluate_egress_readiness(workers, results))

    def test_ready_in_direct_only_mode(self):
        workers = [self.DIRECT_W]
        results = [_line(1, "44.196.116.2")]
        self.assertTrue(lb_gateway.evaluate_egress_readiness(workers, results))

    def test_not_ready_when_any_probe_failed(self):
        workers = [self.DIRECT_W, self._proxy_w(2)]
        results = [_line(1, "44.196.116.2"),
                   (2, "[Worker-2 : Port 9002 : x] -> Connection Failed (URLError)")]
        self.assertFalse(lb_gateway.evaluate_egress_readiness(workers, results))

    def test_not_ready_on_empty_results(self):
        self.assertFalse(lb_gateway.evaluate_egress_readiness([self.DIRECT_W], []))


if __name__ == "__main__":
    unittest.main()
