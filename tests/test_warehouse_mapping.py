from unittest import TestCase

from app.config import ConfigurationError, OneCBaseConfig, Settings, WarehouseMappingConfig
from app.models import Warehouse
from app.warehouse_mapping import resolve_warehouse_mappings


TRADE = OneCBaseConfig(name="Trade", url="trade", base_id="trade")
BUH2019 = OneCBaseConfig(name="BUH2019", url="buh2019", base_id="buh2019")
BUH2026 = OneCBaseConfig(name="BUH2026", url="buh2026", base_id="buh2026")


def settings(*mappings: WarehouseMappingConfig) -> Settings:
    return Settings(TRADE, [BUH2019, BUH2026], warehouse_mappings=list(mappings))


class WarehouseMappingTests(TestCase):
    def test_groups_trade_warehouses_and_resolves_each_accounting_base(self):
        directories = {
            "trade": [
                Warehouse("T1", "Section 1", accounting_name=" Main  warehouse "),
                Warehouse("T2", "Section 2", accounting_name="main warehouse"),
                Warehouse("T3", "Not mapped"),
                Warehouse("T4", "Unknown", accounting_name="Missing warehouse"),
            ],
            "buh2019": [Warehouse("B19", "Main warehouse")],
            "buh2026": [Warehouse("B26", "MAIN WAREHOUSE")],
        }

        resolution = resolve_warehouse_mappings(settings(), directories)

        self.assertEqual(len(resolution.mappings), 1)
        mapping = resolution.mappings[0]
        self.assertEqual(mapping.mapping_id, "auto:buh2019:B19")
        self.assertEqual(mapping.codes_for("trade"), ("T1", "T2"))
        self.assertEqual(mapping.codes_for("buh2019"), ("B19",))
        self.assertEqual(mapping.codes_for("buh2026"), ("B26",))
        self.assertEqual(
            {key: resolution.diagnostics[key] for key in (
                "mapped_logical_count", "mapped_trade_count", "blank_trade_count", "unresolved_trade_count"
            )},
            {
                "mapped_logical_count": 1,
                "mapped_trade_count": 2,
                "blank_trade_count": 1,
                "unresolved_trade_count": 1,
            },
        )

    def test_manual_mapping_preserves_id_and_extends_automatic_group(self):
        manual = WarehouseMappingConfig(
            "legacy-main",
            "Legacy",
            {"trade": ["T1", "T3"], "buh2019": "B19", "buh2026": "B26"},
        )
        directories = {
            "trade": [
                Warehouse("T1", "Section 1", accounting_name="Main"),
                Warehouse("T2", "Section 2", accounting_name="Main"),
                Warehouse("T3", "Manual exception"),
            ],
            "buh2019": [Warehouse("B19", "Main")],
            "buh2026": [Warehouse("B26", "Main")],
        }

        resolution = resolve_warehouse_mappings(settings(manual), directories)

        self.assertEqual(len(resolution.mappings), 1)
        self.assertEqual(resolution.mappings[0].mapping_id, "legacy-main")
        self.assertEqual(resolution.mappings[0].label, "Main")
        self.assertEqual(resolution.mappings[0].codes_for("trade"), ("T1", "T2", "T3"))
        self.assertEqual(resolution.diagnostics["blank_trade_count"], 0)

    def test_ambiguous_accounting_name_is_reported_and_not_mapped(self):
        directories = {
            "trade": [Warehouse("T1", "Trade", accounting_name="Main")],
            "buh2019": [Warehouse("B1", "Main"), Warehouse("B2", "main")],
            "buh2026": [Warehouse("B3", "Main")],
        }

        resolution = resolve_warehouse_mappings(settings(), directories)

        self.assertEqual(resolution.mappings, ())
        issue = resolution.diagnostics["unresolved_links"][0]
        self.assertEqual(issue["ambiguous_bases"], ["buh2019"])

    def test_conflicting_manual_mapping_is_rejected(self):
        manual = WarehouseMappingConfig(
            "wrong",
            "Wrong",
            {"trade": "T1", "buh2019": "B2", "buh2026": "B4"},
        )
        directories = {
            "trade": [Warehouse("T1", "Trade", accounting_name="Main")],
            "buh2019": [Warehouse("B1", "Main"), Warehouse("B2", "Other")],
            "buh2026": [Warehouse("B3", "Main"), Warehouse("B4", "Other")],
        }

        with self.assertRaisesRegex(ConfigurationError, "противоречит СкладБухгалтерия"):
            resolve_warehouse_mappings(settings(manual), directories)
