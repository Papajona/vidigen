from gateway.providers import _output_url, _status_url


def test_output_url_accepts_common_provider_shapes():
    assert _output_url({"output": "https://cdn.example/video.mp4"}) == "https://cdn.example/video.mp4"
    assert _output_url({"output": ["https://cdn.example/video.mp4"]}) == "https://cdn.example/video.mp4"
    assert _output_url({"videoUrl": "https://cdn.example/video.mp4"}) == "https://cdn.example/video.mp4"


def test_status_url_accepts_replicate_and_generic_shapes():
    assert _status_url({"urls": {"get": "https://provider.example/jobs/123"}}) == "https://provider.example/jobs/123"
    assert _status_url({"status_url": "https://provider.example/jobs/123"}) == "https://provider.example/jobs/123"
