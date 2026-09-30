import asyncio
import shutil
import uuid
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.db import Store
from app.discovery import container_stats, discover_apps, public_apps
from app.metrics import MetricsCollector
from app.monitor import Monitor, RateTracker
from app.processes import SocketIndex, decode_address
from app.traffic import attribute_flow, parse_conntrack_line
from app.vpn import VPNMonitor


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        test_directory = temp_root / uuid.uuid4().hex
        test_directory.mkdir()
        def cleanup():
            assert temp_root.resolve() in test_directory.resolve().parents
            shutil.rmtree(test_directory)
        self.addCleanup(cleanup)
        self.store = Store(test_directory / "test.db")

    def test_history_aggregates_legacy_samples_and_excludes_bridges(self):
        self.store.add_samples([
            ("2026-09-30T10:00:01+00:00", "eth0", 100, 20, 10, 2),
            ("2026-09-30T10:00:03+00:00", "eth0", 300, 40, 30, 4),
            ("2026-09-30T10:00:03+00:00", "docker0", 999, 999, 99, 99),
            ("2026-09-30T10:00:03+00:00", "vpn:nord", 80, 20, 20, 5),
        ])
        rows = self.store.samples("2026-09-30", bucket_seconds=60)
        self.assertEqual(len(rows), 2)
        eth = next(row for row in rows if row["interface"] == "eth0")
        self.assertEqual(eth["rx_bps"], 200)
        self.assertEqual(eth["tx_bps"], 30)
        self.assertEqual(eth["rx_bytes"], 30)
        self.assertEqual(eth["ts"], "2026-09-30T10:00:00+00:00")

    @patch("app.metrics.psutil.net_io_counters")
    def test_live_samples_do_not_write_every_cycle(self, counters):
        counters.return_value = {"eth0": SimpleNamespace(bytes_recv=100, bytes_sent=20), "veth123": SimpleNamespace(bytes_recv=99, bytes_sent=30)}
        collector = MetricsCollector(self.store)
        for _ in range(12):
            collector.collect()
        self.assertEqual(self.store.samples("2000"), [])
        collector._last_save -= 61
        collector.collect()
        rows = self.store.samples("2000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["interface"], "eth0")
        self.assertIn("veth123", collector.latest)

    def test_secrets_never_appear_in_settings_or_public_apps(self):
        self.store.set_setting("secret:vpn:nord", "private-token")
        self.assertNotIn("private-token", str(self.store.settings()))
        self.assertNotIn("private-token", str(public_apps([{"name":"nord", "_vpn_auth":{"apikey":"private-token"}}])))

    def test_rates_use_collection_time_and_counter_reset_is_unavailable(self):
        tracker = RateTracker()
        first = [{"id":"vpn:1", "accounting":True, "rx_bytes":100, "tx_bytes":200}]
        tracker.apply(first, 10)
        self.assertIsNone(first[0]["rx_bps"])
        second = [{"id":"vpn:1", "accounting":True, "rx_bytes":200, "tx_bytes":500}]
        tracker.apply(second, 15)
        self.assertEqual(second[0]["rx_bps"], 160)
        self.assertEqual(second[0]["tx_bps"], 480)
        reset = [{"id":"vpn:1", "accounting":True, "rx_bytes":10, "tx_bytes":20}]
        tracker.apply(reset, 20)
        self.assertIsNone(reset[0]["rx_bps"])

    @patch("app.discovery._port_listening", return_value=False)
    @patch("app.discovery._docker_client")
    def test_gluetun_detection_shared_namespace_and_stopped_vpn(self, client_factory, listening):
        def container(identity, name, image, mode, status="running"):
            return SimpleNamespace(id=identity*64, name=name, status=status, attrs={
                "Config":{"Image":image, "Labels":{"com.docker.compose.service":"app"}},
                "State":{"Pid":123, "Health":{"Status":"healthy"}},
                "HostConfig":{"NetworkMode":mode}, "NetworkSettings":{"Networks":{"vpn":{"IPAddress":"172.20.0.2"}}}})
        owner = container("a", "nord", "qmcgaw/gluetun:latest", "bridge")
        child = container("b", "qbittorrent", "qb:latest", "container:" + owner.id)
        stopped = container("c", "proton", "qmcgaw/gluetun:v3", "bridge", "exited")
        client_factory.return_value.containers.list.return_value = [owner, child, stopped]
        apps = discover_apps()
        qb = next(app for app in apps if app["name"] == "qbittorrent")
        self.assertEqual(qb["vpn_id"], owner.id[:12])
        self.assertEqual(len([app for app in apps if app.get("is_vpn")]), 2)

    @patch("app.discovery._docker_client")
    def test_docker_stats_are_one_shot(self, client_factory):
        client_factory.return_value.api.stats.return_value = {"networks":{"eth0":{"rx_bytes":30,"tx_bytes":60}}}
        result = container_stats([{"id":"a", "container_id":"aa", "status":"running"}])
        self.assertEqual(result["a"]["rx_bytes"], 30)
        client_factory.return_value.api.stats.assert_called_once_with("aa", stream=False, one_shot=True)

    @patch("app.vpn.network_counters", return_value={"tun0":{"rx_bytes":100,"tx_bytes":20},"eth0":{"rx_bytes":999,"tx_bytes":999}})
    def test_vpn_health_unknown_is_not_connected_and_no_double_counting(self, counters):
        vpn = VPNMonitor(self.store)
        vpn.read_api = MagicMock(return_value={"api_status":"running", "public_ip":"1.2.3.4"})
        app = {"id":"a", "docker_name":"nord", "is_vpn":True, "pid":123,"status":"running","health":"unknown"}
        rows = vpn.collect([app])
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertEqual(rows[0]["metric"]["rx_bytes"], 100)
        app["health"] = "healthy"
        self.assertEqual(vpn.collect([app])[0]["status"], "connected")
        app["health"] = "unhealthy"
        self.assertEqual(vpn.collect([app])[0]["status"], "reconnecting")
        self.assertEqual(vpn.collect([]), [])

    def test_process_socket_overrides_shared_ip_and_orients_reply_counters(self):
        apps = [{"id":"vpn", "name":"Gluetun", "ips":["172.20.0.5"]}, {"id":"qb", "name":"qBittorrent", "type":"docker", "container_id":"b"*64,"vpn_id":"vpn"}]
        sockets = MagicMock()
        sockets.match.side_effect = [None, ({"app":apps[1],"process":"qbittorrent","pid":42}, "dst")]
        line = "tcp 6 100 ESTABLISHED src=172.20.0.5 dst=1.1.1.1 sport=53000 dport=443 bytes=100 src=1.1.1.1 dst=172.20.0.5 sport=443 dport=53000 bytes=500 mark=0"
        flow = attribute_flow(parse_conntrack_line(line), apps, sockets=sockets)
        self.assertEqual(flow["application"], "qBittorrent")
        self.assertEqual(flow["direction"], "sortant")
        self.assertEqual(flow["rx_bytes"], 500)
        self.assertEqual(flow["tx_bytes"], 100)
        self.assertEqual(flow["vpn_id"], "vpn")

    def test_namespace_socket_matches_are_isolated(self):
        index = SocketIndex.__new__(SocketIndex)
        index.host_namespace = "host"
        index.namespace_ids = {100:"vpn1", 200:"vpn2"}
        index.listeners, index.wildcards = {}, {}
        index.exact = {("vpn1","TCP","10.0.0.2",53000,"1.1.1.1",443):{"pid":100},
                       ("vpn2","TCP","10.0.0.2",53000,"1.1.1.1",443):{"pid":200}}
        values = {"src":"10.0.0.2","sport":"53000","dst":"1.1.1.1","dport":"443"}
        self.assertIsNone(index.match("TCP", values))
        self.assertEqual(index.match("TCP", values, namespace_pid=100)[0]["pid"], 100)
        self.assertEqual(index.match("TCP", values, namespace_pid=200)[0]["pid"], 200)

    def test_proc_address_decoding(self):
        self.assertEqual(decode_address("0100007F:01BD"), ("127.0.0.1",445))
        self.assertEqual(decode_address("00000000000000000000000001000000:01BD"), ("::1",445))

    def test_snapshot_never_waits_for_docker_stats(self):
        from app import main
        monitor = Monitor(self.store, MetricsCollector(self.store))
        monitor.interfaces = [{"name":"eth0","managed":True}]
        monitor.apps = [{"id":"a","name":"app","_vpn_auth":{"apikey":"private"}}]
        with patch.object(main, "monitor", monitor), patch.object(main, "store", self.store), patch("app.monitor.container_stats", side_effect=AssertionError("Statistics must not run in HTTP request")):
            snapshot = main.current_state(include_stats=True)
        self.assertEqual(snapshot["interfaces"][0]["name"], "eth0")
        self.assertNotIn("private", str(snapshot))

    def test_independent_inventory_continues_while_statistics_are_blocked(self):
        async def run():
            monitor = Monitor(self.store, MetricsCollector(self.store))
            blocked = threading.Event()
            inventory_done = threading.Event()
            def statistics():
                blocked.wait(2)
            def inventory():
                monitor.interfaces = [{"name":"eth0"}]
                inventory_done.set()
            tasks = [asyncio.create_task(monitor.loop("statistics", statistics, 30)), asyncio.create_task(monitor.loop("interfaces", inventory, 15))]
            try:
                for _ in range(20):
                    if inventory_done.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(inventory_done.is_set())
                self.assertEqual(monitor.interfaces[0]["name"], "eth0")
            finally:
                blocked.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
