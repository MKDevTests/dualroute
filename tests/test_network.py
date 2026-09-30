import unittest

from app.network import (
    NetworkError,
    build_apply_plan,
    build_interface_plan,
    build_nft_script,
    network_status,
    resolve_rules_for_health,
    validate_distinct_networks,
    test_gateway,
)
from app.discovery import is_managed_interface
from app.models import NetworkConfigInput


INTERFACES = [
    {
        "name": "eth0", "address": "192.168.1.131", "prefix": 24,
        "network": "192.168.1.0/24", "gateway": "192.168.1.1", "up": True,
    },
    {
        "name": "eth1", "address": "192.168.2.131", "prefix": 24,
        "network": "192.168.2.0/24", "gateway": "192.168.2.1", "up": True,
    },
]

APPS = [
    {"id": "abc123", "name": "Jellyfin", "ips": ["172.20.0.5"]},
    {"id": "service:smb", "name": "SMB", "ips": []},
]

RULES = [
    {
        "id": 1, "app_id": "abc123", "app_name": "Jellyfin", "direction": "both",
        "strategy": "prefer", "primary_interface": "eth0", "fallback_interface": "eth1",
        "qos": 4, "enabled": True, "weight_primary": 50,
    },
    {
        "id": 2, "app_id": "service:smb", "app_name": "SMB", "direction": "both",
        "strategy": "force", "primary_interface": "eth1", "fallback_interface": None,
        "qos": 5, "enabled": True, "weight_primary": 50,
    },
]


class NetworkPlanTests(unittest.TestCase):
    def test_nas_inverted_names_and_lan_without_gateway_are_valid(self):
        interfaces = [dict(INTERFACES[0], address="192.168.2.131", network="192.168.2.0/24", gateway=None),
                      dict(INTERFACES[1], address="192.168.1.131", network="192.168.1.0/24", gateway="192.168.1.254")]
        status = network_status(interfaces)
        self.assertEqual(status["mode"], "local")
        self.assertIn("ETH0", status["title"])
        self.assertNotIn("ETH1", status["title"])
        self.assertEqual(validate_distinct_networks(interfaces), [])
        with self.assertRaisesRegex(NetworkError, "ETH0"):
            build_apply_plan(RULES, APPS, interfaces)

    def test_both_cards_can_be_local_without_gateway(self):
        interfaces = [dict(item, gateway=None) for item in INTERFACES]
        self.assertEqual(validate_distinct_networks(interfaces), [])
        self.assertEqual(network_status(interfaces)["mode"], "local")

    def test_subnet_overlap_is_detected_even_without_gateway(self):
        interfaces = [dict(INTERFACES[0], gateway=None), dict(INTERFACES[1], network="192.168.1.0/24", gateway=None)]
        self.assertEqual(network_status(interfaces)["mode"], "overlap")
        self.assertTrue(validate_distinct_networks(interfaces))

    def test_missing_card_is_identified_without_inventing_gateway(self):
        status = network_status([INTERFACES[1]])
        self.assertIn("ETH0", status["message"])
        self.assertNotIn("192.168.2.1", status["message"])

    def test_local_configuration_accepts_empty_gateway_and_adds_no_default(self):
        config = NetworkConfigInput(interface="eth0", address="192.168.2.131").model_dump()
        self.assertEqual(config["gateway"], "")
        plan = "\n".join(command.display() for command in build_interface_plan(config))
        self.assertIn("ip address add 192.168.2.131/24 dev eth0", plan)
        self.assertNotIn("route replace default", plan)
        self.assertNotIn("dev eth1", plan)
        self.assertIn("réseau", test_gateway("eth0", "")["message"])

    def test_only_physical_cards_can_be_configured(self):
        with self.assertRaises(NetworkError):
            build_interface_plan({"interface":"tailscale0", "address":"192.168.2.131", "prefix":24, "gateway":""})

    def test_distinct_networks_have_no_warning(self):
        self.assertEqual(validate_distinct_networks(INTERFACES), [])

    def test_only_requested_interfaces_are_managed(self):
        self.assertTrue(is_managed_interface("eth0"))
        self.assertTrue(is_managed_interface("eth1"))
        self.assertTrue(is_managed_interface("tailscale0"))
        self.assertFalse(is_managed_interface("docker0"))
        self.assertFalse(is_managed_interface("wlan0"))

    def test_duplicate_network_is_rejected_by_warning(self):
        duplicate = [dict(INTERFACES[0]), dict(INTERFACES[1], network="192.168.1.0/24")]
        self.assertTrue(any("même sous-réseau" in item for item in validate_distinct_networks(duplicate)))

    def test_nft_script_matches_container_and_smb(self):
        script = build_nft_script(RULES, APPS, INTERFACES)
        self.assertIn("172.20.0.5", script)
        self.assertIn("tcp dport 445", script)
        self.assertIn("ip dscp set cs4", script)
        self.assertIn('iifname "eth1"', script)
        self.assertIn("100.64.0.0/10", script)

    def test_policy_plan_has_two_route_tables(self):
        plan = build_apply_plan(RULES, APPS, INTERFACES)
        displays = "\n".join(command.display() for command in plan)
        self.assertIn("table 101 default via 192.168.1.1", displays)
        self.assertIn("table 102 default via 192.168.2.1", displays)
        self.assertIn("101/0xff", displays)
        self.assertIn("102/0xff", displays)

    def test_preference_fails_over_without_mutating_saved_rule(self):
        resolved, changes = resolve_rules_for_health(RULES, {"eth0": False, "eth1": True})
        self.assertEqual(resolved[0]["primary_interface"], "eth1")
        self.assertEqual(RULES[0]["primary_interface"], "eth0")
        self.assertEqual(changes[0]["app"], "Jellyfin")

    def test_interface_gateway_must_share_subnet(self):
        with self.assertRaises(NetworkError):
            build_interface_plan(
                {"interface": "eth1", "address": "192.168.2.131", "prefix": 24, "gateway": "192.168.3.1", "mtu": 1500}
            )

    def test_interface_plan_replaces_old_address_and_default(self):
        plan = build_interface_plan(
            {"interface": "eth1", "address": "192.168.2.131", "prefix": 24, "gateway": "192.168.2.1", "mtu": 1500}
        )
        displays = "\n".join(command.display() for command in plan)
        self.assertIn("ip -4 address flush dev eth1 scope global", displays)
        self.assertIn("ip address add 192.168.2.131/24 dev eth1", displays)
        self.assertIn("ip route del default dev eth1", displays)


if __name__ == "__main__":
    unittest.main()
