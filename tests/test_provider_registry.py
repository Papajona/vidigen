import json
import os
import unittest

from gateway.providers import ManifestHTTPProvider, _render_template, _operation_capability


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.old_key = os.environ.get("TEST_PROVIDER_KEY")
        os.environ["TEST_PROVIDER_KEY"] = "secret"

    def tearDown(self):
        if self.old_key is None:
            os.environ.pop("TEST_PROVIDER_KEY", None)
        else:
            os.environ["TEST_PROVIDER_KEY"] = self.old_key

    def test_template_preserves_exact_types(self):
        rendered = _render_template(
            {
                "prompt": "{{prompt}}",
                "seconds": "{{duration_seconds}}",
                "nested": {"source": "{{source_url}}"},
                "enabled": "{{generate_audio}}",
            },
            {
                "prompt": "hello",
                "duration_seconds": 8,
                "source_url": None,
                "generate_audio": True,
            },
        )
        self.assertEqual(rendered["prompt"], "hello")
        self.assertEqual(rendered["seconds"], 8)
        self.assertIsNone(rendered["nested"]["source"])
        self.assertTrue(rendered["enabled"])

    def test_operation_mapping(self):
        self.assertEqual(_operation_capability("Text → Image"), "image")
        self.assertEqual(_operation_capability("Image → Video"), "image-to-video")
        self.assertEqual(_operation_capability("Video → Video"), "video-to-video")
        self.assertEqual(_operation_capability("Text → Video"), "video")

    def test_manifest_prepares_operation_specific_contract(self):
        spec = {
            "name": "demo",
            "token_env": "TEST_PROVIDER_KEY",
            "submit_urls": {
                "image": "https://example.invalid/image",
                "video": "https://example.invalid/video",
            },
            "status_urls": {
                "image": "https://example.invalid/image/{id}",
                "video": "https://example.invalid/video/{id}",
            },
            "models": {
                "image": "image-model",
                "video": "video-model",
            },
            "capabilities": ["image", "video", "image-to-video"],
            "request_templates": {
                "image": {"model": "{{model}}", "prompt": "{{prompt}}"},
                "video": {
                    "model": "{{model}}",
                    "prompt": "{{prompt}}",
                    "seconds": "{{duration_seconds}}",
                },
                "image-to-video": {
                    "model": "{{model}}",
                    "image": "{{source_url}}",
                },
            },
        }
        provider = ManifestHTTPProvider(spec)
        image = provider.prepare({"mode": "Text → Image", "prompt": "cat", "duration": "5s"})
        self.assertEqual(image["model"], "image-model")
        self.assertEqual(image["input"]["model"], "image-model")
        self.assertEqual(image["_submit_url"], "https://example.invalid/image")
        self.assertEqual(image["_status_url_template"], "https://example.invalid/image/{id}")

        i2v = provider.prepare({
            "mode": "Image → Video",
            "prompt": "animate this",
            "sourceUrl": "https://cdn.example/image.png",
            "duration": "5s",
        })
        self.assertEqual(i2v["model"], "video-model")
        self.assertEqual(i2v["input"]["image"], "https://cdn.example/image.png")

    def test_provider_supports_video_as_fallback_for_transform_video_modes(self):
        spec = {"name": "video-only", "capabilities": ["video"], "submit_url": "x"}
        provider = ManifestHTTPProvider(spec)
        self.assertTrue(provider.supports("video"))
        self.assertTrue(provider.supports("image-to-video"))
        self.assertTrue(provider.supports("video-to-video"))
        self.assertFalse(provider.supports("image"))


if __name__ == "__main__":
    unittest.main()
