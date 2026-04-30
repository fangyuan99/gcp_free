import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import gcp


class FakeEnumValue:
    name = "ONE_TO_ONE_NAT"


class FakeType:
    ONE_TO_ONE_NAT = FakeEnumValue()


class FakeAccessConfig:
    Type = FakeType


class FakeCompute:
    class NetworkInterface(SimpleNamespace):
        pass

    class AccessConfig(FakeAccessConfig, SimpleNamespace):
        pass


class ProfileTests(unittest.TestCase):
    def test_profiles_round_trip(self):
        config = gcp.InstanceConfig(
            instance_name="small-vm",
            zone="us-west1-b",
            machine_type="e2-small",
            disk=gcp.DiskConfig(size_gb=30, type="pd-standard"),
            network=gcp.NetworkConfig(tags=["http-server"]),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profiles.json")
            data = gcp.empty_profiles_document()
            data["profiles"]["small"] = gcp.config_to_profile(config)
            gcp.save_profiles(data, path)

            loaded = gcp.get_profile_config("small", path)
            self.assertEqual(loaded.instance_name, "small-vm")
            self.assertEqual(loaded.machine_type, "e2-small")
            self.assertEqual(loaded.network.tags, ["http-server"])

    def test_imported_profiles_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profiles.json")
            count = gcp.merge_imported_profiles(
                {
                    "profiles": {
                        "free": gcp.config_to_profile(
                            gcp.default_free_instance_config(
                                "us-west1-b",
                                {"name": "Debian", "project": "debian-cloud", "family": "debian-12"},
                            )
                        )
                    }
                },
                path,
            )
            self.assertEqual(count, 1)
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertIn("free", saved["profiles"])


class ConfigTests(unittest.TestCase):
    def test_custom_machine_type_validation(self):
        self.assertEqual(gcp.build_custom_machine_type("e2", 2, 2048), "e2-custom-2-2048")
        with self.assertRaises(ValueError):
            gcp.build_custom_machine_type("e2", 3, 2048)
        with self.assertRaises(ValueError):
            gcp.build_custom_machine_type("e2", 2, 2300)

    def test_paid_confirmation_guard(self):
        free_config = gcp.default_free_instance_config(
            "us-west1-b",
            {"name": "Debian", "project": "debian-cloud", "family": "debian-12"},
        )
        paid_config = gcp.InstanceConfig(
            instance_name="paid-vm",
            zone="us-west1-b",
            machine_type="e2-small",
        )

        self.assertTrue(gcp.confirm_paid_creation_if_needed(free_config, force_prompt=False))
        self.assertFalse(gcp.confirm_paid_creation_if_needed(paid_config, force_prompt=False))
        self.assertTrue(
            gcp.confirm_paid_creation_if_needed(
                paid_config,
                confirm_text=gcp.PAID_CONFIRM_TEXT,
                force_prompt=False,
            )
        )

    def test_network_interface_conversion(self):
        original_compute = gcp.compute_v1
        gcp.compute_v1 = FakeCompute
        try:
            config = gcp.InstanceConfig(
                instance_name="net-vm",
                zone="us-west1-b",
                machine_type="e2-small",
                network=gcp.NetworkConfig(
                    network="global/networks/custom",
                    external_ip_mode="static",
                    static_nat_ip="203.0.113.10",
                    network_tier="PREMIUM",
                    tags=[],
                ),
            )
            interface = gcp.build_network_interface(config)
            self.assertEqual(interface.network, "global/networks/custom")
            self.assertEqual(len(interface.access_configs), 1)
            self.assertEqual(interface.access_configs[0].nat_i_p, "203.0.113.10")
            self.assertEqual(interface.access_configs[0].network_tier, "PREMIUM")
        finally:
            gcp.compute_v1 = original_compute


class PricingTests(unittest.TestCase):
    def test_machine_spec_parsing(self):
        self.assertEqual(gcp.machine_spec_from_type("e2-custom-2-2048"), (2, 2048))
        self.assertEqual(gcp.machine_spec_from_type("n2-standard-4"), (4, 16384))
        self.assertEqual(gcp.machine_spec_from_type("e2-small"), (2, 2048))

    def test_price_estimate_from_catalog_skus(self):
        original_fetch = gcp.fetch_compute_skus
        gcp.fetch_compute_skus = lambda api_key, currency: [
            {
                "description": "E2 Instance Core running in Americas",
                "serviceRegions": ["us-west1"],
                "category": {"resourceFamily": "Compute", "resourceGroup": "CPU"},
                "pricingInfo": [
                    {
                        "pricingExpression": {
                            "tieredRates": [{"unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 31000000}}]
                        }
                    }
                ],
            },
            {
                "description": "E2 Instance Ram running in Americas",
                "serviceRegions": ["us-west1"],
                "category": {"resourceFamily": "Compute", "resourceGroup": "RAM"},
                "pricingInfo": [
                    {
                        "pricingExpression": {
                            "tieredRates": [{"unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 4200000}}]
                        }
                    }
                ],
            },
            {
                "description": "Standard PD Capacity",
                "serviceRegions": ["us-west1"],
                "category": {"resourceFamily": "Storage", "resourceGroup": "PDStandard"},
                "pricingInfo": [
                    {
                        "pricingExpression": {
                            "tieredRates": [{"unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 40000000}}]
                        }
                    }
                ],
            },
        ]
        try:
            config = gcp.InstanceConfig(instance_name="price-vm", zone="us-west1-b", machine_type="e2-small")
            estimate = gcp.estimate_instance_price(config, api_key="fake-key")
            self.assertTrue(estimate["available"])
            self.assertEqual(estimate["missing"], [])
            self.assertEqual(len(estimate["line_items"]), 3)
            self.assertGreater(estimate["total_monthly"], 0)
        finally:
            gcp.fetch_compute_skus = original_fetch


if __name__ == "__main__":
    unittest.main()
