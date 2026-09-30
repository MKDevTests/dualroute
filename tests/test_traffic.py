import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.traffic import attribute_flow, discover_flows, parse_conntrack_line


LINE = (
    "tcp 6 431999 ESTABLISHED "
    "src=172.20.0.5 dst=1.1.1.1 sport=52100 dport=443 packets=10 bytes=1200 "
    "src=1.1.1.1 dst=172.20.0.5 sport=443 dport=52100 packets=15 bytes=6400 "
    "[ASSURED] mark=357 use=1"
)

APPS = [
    {
        "id": "abc123",
        "container_id": "abc1234567890",
        "name": "Jellyfin",
        "type": "docker",
        "ips": ["172.20.0.5"],
    },
    {"id": "service:smb", "container_id": None, "name": "SMB", "type": "system", "ips": []},
]


class TrafficTests(unittest.TestCase):
    @patch("app.traffic.Path.read_text", return_value="1")
    @patch("app.traffic.subprocess.run")
    @patch("app.traffic.shutil.which", return_value="/usr/sbin/conntrack")
    @patch("app.traffic.platform.system", return_value="Linux")
    def test_discovery_reports_limit_and_missing_counters(self, system, which, run, read):
        no_counters = "udp 17 30 src=172.20.0.5 dst=192.168.1.20 sport=10000 dport=53 src=192.168.1.20 dst=172.20.0.5 sport=53 dport=10000 mark=0"
        run.return_value = SimpleNamespace(returncode=0, stdout="ipv4 2 " + LINE + "\n" + no_counters, stderr="")
        result = discover_flows(APPS, limit=1, interfaces=[{"name": "eth0", "address": "192.168.1.131", "network": "192.168.1.0/24"}])
        self.assertTrue(result["available"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["accounting_missing"], 1)
        self.assertEqual(result["items"][0]["application"], "Jellyfin")
        self.assertEqual(result["items"][0]["interface_source"], "Marque de routage conntrack")
        result = discover_flows(APPS, interfaces=[{"name": "eth0", "address": "192.168.1.131", "network": "192.168.1.0/24"}])
        self.assertEqual(result["items"][1]["interface"], "eth0")
        self.assertEqual(result["items"][1]["interface_source"], "Sous-réseau connecté (estimé)")

    @patch("app.traffic.subprocess.run")
    @patch("app.traffic.shutil.which", return_value="/usr/sbin/conntrack")
    @patch("app.traffic.platform.system", return_value="Linux")
    def test_permission_error_is_explained(self, system, which, run):
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="Operation not permitted")
        result = discover_flows(APPS)
        self.assertFalse(result["available"])
        self.assertIn("Operation not permitted", result["message"])
        self.assertEqual(result["items"], [])

    def test_extended_output_includes_family_prefix(self):
        flow = parse_conntrack_line("ipv4 2 " + LINE)
        self.assertEqual(flow["interface"], "eth0")
        self.assertTrue(flow["accounting"])

    def test_absent_counters_are_identified(self):
        line = "udp 17 30 src=172.20.0.5 dst=1.1.1.1 sport=10000 dport=53 src=1.1.1.1 dst=172.20.0.5 sport=53 dport=10000 mark=0"
        self.assertFalse(parse_conntrack_line(line)["accounting"])

    def test_dnat_flow_uses_container_endpoint(self):
        line = "tcp 6 120 ESTABLISHED src=192.168.1.20 dst=192.168.1.131 sport=53000 dport=8096 src=172.20.0.5 dst=192.168.1.20 sport=8096 dport=53000 mark=101"
        flow = attribute_flow(parse_conntrack_line(line), APPS, {"192.168.1.131"})
        self.assertEqual(flow["application"], "Jellyfin")
        self.assertEqual(flow["local_endpoint"], "172.20.0.5:8096")
        self.assertEqual(flow["direction"], "entrant")

    def test_remote_smb_server_is_not_local_smb_service(self):
        line = "tcp 6 120 ESTABLISHED src=192.168.1.131 dst=192.168.1.20 sport=53000 dport=445 src=192.168.1.20 dst=192.168.1.131 sport=445 dport=53000 mark=0"
        flow = attribute_flow(parse_conntrack_line(line), APPS, {"192.168.1.131"})
        self.assertEqual(flow["application"], "Hôte / non attribué")
        self.assertEqual(flow["direction"], "sortant")

    def test_parses_conntrack_counters_and_route_mark(self):
        flow = parse_conntrack_line(LINE)
        self.assertIsNotNone(flow)
        self.assertEqual(flow["protocol"], "TCP")
        self.assertEqual(flow["state"], "ESTABLISHED")
        self.assertEqual(flow["interface"], "eth0")
        self.assertEqual(flow["rule_id"], 1)
        self.assertEqual(flow["original_bytes"], 1200)
        self.assertEqual(flow["reply_bytes"], 6400)

    def test_attributes_outbound_flow_to_container(self):
        flow = attribute_flow(parse_conntrack_line(LINE), APPS)
        self.assertEqual(flow["application"], "Jellyfin")
        self.assertEqual(flow["container_id"], "abc123456789")
        self.assertEqual(flow["direction"], "sortant")
        self.assertEqual(flow["remote_endpoint"], "1.1.1.1:443")
        self.assertEqual(flow["service"], "HTTPS")
        self.assertEqual(flow["rx_bytes"], 6400)
        self.assertEqual(flow["tx_bytes"], 1200)

    def test_attributes_smb_by_port(self):
        line = (
            "tcp 6 120 ESTABLISHED "
            "src=192.168.1.20 dst=192.168.1.131 sport=53000 dport=445 packets=2 bytes=100 "
            "src=192.168.1.131 dst=192.168.1.20 sport=445 dport=53000 packets=3 bytes=200 "
            "[ASSURED] mark=101 use=1"
        )
        flow = attribute_flow(parse_conntrack_line(line), APPS)
        self.assertEqual(flow["application"], "SMB")
        self.assertEqual(flow["direction"], "entrant")
        self.assertEqual(flow["remote_endpoint"], "192.168.1.20:53000")
        self.assertEqual(flow["service"], "SMB")


if __name__ == "__main__":
    unittest.main()
