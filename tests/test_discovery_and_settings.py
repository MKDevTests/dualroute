import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.discovery import discover_interfaces
from app.models import SettingInput


class DiscoveryTests(unittest.TestCase):
    @patch("app.discovery._run_json", return_value=[])
    @patch("app.discovery._default_routes", return_value={"eth0": {"gateway": "192.168.1.1"}})
    @patch("app.discovery.psutil.net_io_counters")
    @patch("app.discovery.psutil.net_if_stats")
    @patch("app.discovery.psutil.net_if_addrs")
    def test_reports_unmanaged_interfaces_without_managing_them(
        self, addresses, stats, counters, _routes, _json
    ):
        ipv4 = lambda address: SimpleNamespace(
            family=socket.AF_INET, address=address, netmask="255.255.255.0"
        )
        addresses.return_value = {
            "eth0": [ipv4("192.168.1.131")],
            "docker0": [ipv4("172.17.0.1")],
            "lo": [ipv4("127.0.0.1")],
        }
        stats.return_value = {
            name: SimpleNamespace(isup=True, speed=1000, mtu=1500)
            for name in addresses.return_value
        }
        counters.return_value = {
            name: SimpleNamespace(bytes_recv=10, bytes_sent=20)
            for name in addresses.return_value
        }

        result = discover_interfaces()

        self.assertEqual([item["name"] for item in result], ["eth0", "docker0"])
        self.assertTrue(result[0]["managed"])
        self.assertFalse(result[1]["managed"])


class SettingTests(unittest.TestCase):
    def test_display_preferences_are_validated(self):
        settings = SettingInput(
            enforcement_enabled=False,
            display_rate_unit="MBps",
            dashboard_managed_only=False,
        )
        self.assertEqual(settings.display_rate_unit, "MBps")
        self.assertFalse(settings.dashboard_managed_only)

    def test_unknown_display_unit_is_rejected(self):
        with self.assertRaises(ValidationError):
            SettingInput(enforcement_enabled=False, display_rate_unit="GBps")


if __name__ == "__main__":
    unittest.main()
