"""城市刷新只写调用方提供的持久化路径。"""

import json
import tempfile
import unittest
from pathlib import Path

from core.regions import build_regions, load_regions, resolve_city


class FakeClient:
    async def post(self, endpoint, payload):
        if endpoint.endswith("/province"):
            return {"data": [{"province_name": "福建省", "province_code": "35"}]}
        return {"data": [
            {"city_name": "厦门市", "city_code": "350200"},
            {"city_name": "思明区", "city_code": "350203"},
        ]}


class RegionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def test_bundled_dropdown_matches_bundled_regions(self):
        plugin_dir = Path(__file__).resolve().parents[1]
        regions = load_regions(plugin_dir / "data" / "regions.json")
        schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))
        options = schema["subscriptions"]["templates"]["subscription"]["items"]["city"]["options"]
        self.assertEqual(options, sorted(region["name"] for region in regions))

    async def test_refresh_writes_only_explicit_cache_path(self):
        plugin_dir = Path(__file__).resolve().parents[1]
        bundled_path = plugin_dir / "data" / "regions.json"
        schema_path = plugin_dir / "_conf_schema.json"
        original_bundle = bundled_path.read_bytes()
        original_schema = schema_path.read_bytes()

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "plugin_data" / "astrbot_plugin_birdreport" / "regions.json"
            regions = await build_regions(FakeClient(), cache_path)
            self.assertEqual(regions, [{"name": "厦门市", "province": "福建省", "city": "厦门市"}])
            self.assertEqual(load_regions(cache_path), regions)
            self.assertEqual(resolve_city(regions, "厦门"), ("福建省", "厦门市"))
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["count"], 1)

        self.assertEqual(bundled_path.read_bytes(), original_bundle)
        self.assertEqual(schema_path.read_bytes(), original_schema)


if __name__ == "__main__":
    unittest.main()
