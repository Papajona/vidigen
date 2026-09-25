"""Extensible, server-side AI provider adapters.

The generation gateway is intentionally provider-neutral. Built-in adapters cover
Replicate and legacy generic HTTP providers, while additional providers/models can be
added with VIDIGEN_PROVIDER_CONFIG_JSON without changing application code.

Secrets are always referenced by environment-variable name and never stored in the
provider manifest itself.
"""
from dataclasses import dataclass
from typing import Any
import asyncio
import json
import os
import re
import time
import uuid

import httpx


@dataclass
class GenerationResult:
    provider: str
    job_id: str
    status: str
    output_url: str | None = None
    raw: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    pass


FINAL_STATUSES = {
    "succeeded", "completed", "successful", "complete",
    "failed", "canceled", "cancelled", "error", "rejected",
}


def _path_get(data: Any, path: Any, default=None):
    """Read a nested value from a JSON response using dotted paths or path lists."""
    if path is None:
        return default
    parts = path if isinstance(path, list) else str(path).split(".")
    cur = data
    for part in parts:
        if part == "":
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, list) and str(part).isdigit():
            idx = int(part)
            if idx >= len(cur):
                return default
            cur = cur[idx]
        else:
            return default
    return cur


def _first_path(data: dict, paths: list[Any] | None, default=None):
    for path in paths or []:
        value = _path_get(data, path, None)
        if value not in (None, "", []):
            return value
    return default


def _output_url(data: dict) -> str | None:
    out = data.get("output")
    if isinstance(out, str) and out:
        return out
    if isinstance(out, list):
        for item in out:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict):
                nested = _output_url(item)
                if nested:
                    return nested
    for key in (
        "output_url", "outputUrl", "url", "video_url", "videoUrl",
        "image_url", "imageUrl", "result_url", "resultUrl",
    ):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    for envelope in ("data", "result", "prediction"):
        nested = data.get(envelope)
        if isinstance(nested, dict):
            value = _output_url(nested)
            if value:
                return value
    return None


def _status_url(data: dict) -> str | None:
    urls = data.get("urls") or {}
    if isinstance(urls, dict):
        for key in ("get", "status"):
            value = urls.get(key)
            if isinstance(value, str) and value:
                return value
    for key in (
        "status_url", "statusUrl", "status_endpoint", "statusEndpoint",
        "poll_url", "pollUrl",
    ):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_capability(cap: str) -> str:
    return str(cap or "").strip().lower().replace("_", "-")


def _operation_capability(mode: str) -> str:
    text = str(mode or "").strip().lower().replace("→", "to")
    if text in ("text to image", "text-image"):
        return "image"
    if text in ("image to video", "image-video"):
        return "image-to-video"
    if text in ("video to video", "video-video"):
        return "video-to-video"
    return "video"


def _render_template(value: Any, context: dict[str, Any]) -> Any:
    """Expand {{variable}} placeholders recursively in JSON templates.

    A string that is exactly a placeholder preserves the underlying value type;
    embedded placeholders become strings. This makes it possible to pass numbers,
    booleans, URLs and nulls without custom provider code.
    """
    if isinstance(value, dict):
        return {k: _render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(v, context) for v in value]
    if not isinstance(value, str):
        return value

    exact = re.fullmatch(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", value)
    if exact:
        return context.get(exact.group(1))

    def repl(match):
        key = match.group(1)
        val = context.get(key)
        return "" if val is None else str(val)

    return re.sub(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", repl, value)


class BaseGenerationProvider:
    name = "provider"
    capabilities: set[str] = set()

    def configured(self) -> bool:
        return True

    def supports(self, capability: str) -> bool:
        capability = _normalize_capability(capability)
        return capability in self.capabilities or (
            capability == "image-to-video" and "video" in self.capabilities
        ) or (
            capability == "video-to-video" and "video" in self.capabilities
        )

    def model_for(self, payload: dict) -> str:
        return self.name

    def prepare(self, payload: dict) -> dict[str, Any]:
        return {"input": payload, "model": self.model_for(payload)}

    async def submit(self, request: dict[str, Any]) -> GenerationResult:
        raise NotImplementedError

    async def status(self, job_id: str, status_url: str | None = None) -> GenerationResult:
        raise NotImplementedError


class ReplicateProvider(BaseGenerationProvider):
    name = "replicate"
    capabilities = {"video", "image", "image-to-video", "video-to-video"}

    def __init__(self):
        self.token = os.getenv("REPLICATE_API_TOKEN", "")
        self.model = os.getenv("REPLICATE_MODEL", "")
        self.image_model = os.getenv("REPLICATE_IMAGE_MODEL", "black-forest-labs/flux-schnell")

    def configured(self) -> bool:
        # Image generation and video generation have separate model settings.
        # Requiring REPLICATE_MODEL here incorrectly marked Replicate as unavailable
        # for Text → Image when only REPLICATE_IMAGE_MODEL was configured.
        return bool(
            os.getenv("REPLICATE_API_TOKEN", "")
            and (os.getenv("REPLICATE_MODEL", "") or os.getenv("REPLICATE_IMAGE_MODEL", ""))
        )

    def model_for(self, payload: dict) -> str:
        if _operation_capability(payload.get("mode")) == "image":
            return os.getenv("REPLICATE_IMAGE_MODEL", self.image_model)
        return os.getenv("REPLICATE_MODEL", self.model)

    def prepare(self, payload: dict) -> dict[str, Any]:
        mode_text = str(payload.get("mode") or "").strip().lower()
        if _operation_capability(mode_text) == "image":
            if payload.get("sourceUrl"):
                raise ProviderError("Text → Image does not accept source media.")
            provider_input = {
                "prompt": payload.get("prompt", ""),
                "aspect_ratio": payload.get("ratio") or "1:1",
                "output_format": "png",
            }
        else:
            provider_input = {
                "prompt": payload.get("prompt", ""),
                "duration": int(str(payload.get("duration", "5s")).rstrip("s")),
                "aspect_ratio": payload.get("ratio") or payload.get("aspect_ratio") or "16:9",
                "resolution": payload.get("resolution", "720p"),
                "generate_audio": bool(payload.get("generate_audio", True)),
            }
            source = payload.get("sourceUrl")
            source_type = payload.get("sourceType")
            if source:
                if _operation_capability(mode_text) == "image-to-video":
                    if source_type and source_type != "image":
                        raise ProviderError("Image → Video requires an image source.")
                    provider_input["image"] = source
                elif _operation_capability(mode_text) == "video-to-video":
                    if source_type and source_type != "video":
                        raise ProviderError("Video → Video requires a video source.")
                    provider_input["reference_videos"] = [source]
        return {"input": provider_input, "model": self.model_for(payload)}

    async def submit(self, request):
        token = self.token
        model = str(request.get("model") or self.model)
        if not token or not model:
            raise ProviderError("Replicate is not configured on the server.")
        provider_input = request.get("input", request)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
            r = await c.post(
                f"https://api.replicate.com/v1/models/{model}/predictions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"input": provider_input},
            )
        if r.status_code >= 400:
            detail = r.text[:800].replace("\n", " ")
            raise ProviderError(f"Replicate rejected the request ({r.status_code}): {detail}")
        d = r.json()
        return GenerationResult(
            self.name,
            str(d.get("id", "")),
            str(d.get("status", "starting")),
            _output_url(d),
            d,
        )

    async def status(self, job_id, status_url=None):
        if not self.token:
            raise ProviderError("Replicate is not configured.")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            r = await c.get(
                status_url or f"https://api.replicate.com/v1/predictions/{job_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
        if r.status_code >= 400:
            raise ProviderError(f"Replicate status failed ({r.status_code}).")
        d = r.json()
        return GenerationResult(
            self.name, job_id, str(d.get("status", "unknown")), _output_url(d), d
        )


class ConfiguredHTTPProvider(BaseGenerationProvider):
    """Generic JSON-over-HTTP provider used for legacy Seedance/Runway wiring."""

    def __init__(
        self,
        name: str,
        url_env: str,
        token_env: str,
        status_url_env: str | None = None,
        capabilities: set[str] | None = None,
    ):
        self.name = name
        self.url_env = url_env
        self.token_env = token_env
        self.status_url_env = status_url_env
        self.capabilities = capabilities or {"video"}

    def configured(self) -> bool:
        return bool(os.getenv(self.url_env, "") and os.getenv(self.token_env, ""))

    def model_for(self, payload: dict) -> str:
        return os.getenv(f"{self.name.upper()}_MODEL", self.name)

    def prepare(self, payload: dict) -> dict[str, Any]:
        return {"input": payload, "model": self.model_for(payload)}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {os.getenv(self.token_env, '')}",
            "Content-Type": "application/json",
        }

    async def submit(self, request):
        url = os.getenv(self.url_env, "")
        token = os.getenv(self.token_env, "")
        if not url or not token:
            raise ProviderError(f"{self.name} is not configured on the server.")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
            r = await c.post(url, headers=self._headers(), json=request.get("input", request))
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} rejected the request ({r.status_code}).")
        d = r.json()
        return GenerationResult(
            self.name,
            str(d.get("id") or d.get("job_id") or d.get("task_id") or ""),
            str(d.get("status", "queued")),
            _output_url(d),
            d,
        )

    async def status(self, job_id, status_url=None):
        template = status_url or os.getenv(self.status_url_env or "", "")
        url = template.replace("{id}", str(job_id)) if template else ""
        if not url:
            raise ProviderError(
                f"{self.name} status polling is not configured. Set "
                f"{self.status_url_env or self.name.upper() + '_STATUS_URL_TEMPLATE'}."
            )
        if not os.getenv(self.token_env, ""):
            raise ProviderError(f"{self.name} is not configured on the server.")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            r = await c.get(url, headers=self._headers())
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} status failed ({r.status_code}).")
        d = r.json()
        return GenerationResult(
            self.name, job_id, str(d.get("status", "unknown")), _output_url(d), d
        )


class ManifestHTTPProvider(BaseGenerationProvider):
    """Provider defined entirely by JSON configuration.

    Manifest fields:
      name, submit_url, status_url_template, token_env, auth,
      capabilities, models, request_template, response.

    request_template can contain {{prompt}}, {{mode}}, {{ratio}},
    {{duration_seconds}}, {{source_url}}, {{model}}, and related fields.
    """

    def __init__(self, spec: dict[str, Any]):
        self.spec = spec
        self.name = str(spec.get("name", "")).strip().lower()
        self.capabilities = {
            _normalize_capability(x) for x in spec.get("capabilities", ["video"])
        }
        self.submit_url = str(spec.get("submit_url", "")).strip()
        self.status_url_template = str(spec.get("status_url_template", "")).strip()
        self.token_env = str(spec.get("token_env", "")).strip()
        self.auth = str(spec.get("auth", "bearer")).strip().lower()
        self.auth_header = str(spec.get("auth_header", "Authorization")).strip()
        self.auth_query_name = str(spec.get("auth_query_name", "api_key")).strip()
        self.submit_method = str(spec.get("submit_method", "POST")).upper()
        self.status_method = str(spec.get("status_method", "GET")).upper()
        self.request_template = spec.get("request_template")
        self.request_templates = spec.get("request_templates") or {}
        self.models = spec.get("models") or {}
        self.default_model = str(spec.get("default_model", self.name)).strip()
        self.response = spec.get("response") or {}
        self.submit_urls = spec.get("submit_urls") or {}
        self.status_urls = spec.get("status_urls") or {}
        self.extra_headers = {
            str(k): str(v) for k, v in (spec.get("extra_headers") or {}).items()
            if isinstance(k, str)
        }

    def configured(self) -> bool:
        submit_available = bool(self.submit_url or any(str(v).strip() for v in self.submit_urls.values()))
        return bool(submit_available and (not self.token_env or os.getenv(self.token_env, "")))

    def model_for(self, payload: dict) -> str:
        operation = _operation_capability(payload.get("mode"))
        selected = payload.get("model")
        if selected and str(selected).lower() not in {"auto", self.name}:
            return str(selected)
        if isinstance(self.models, dict):
            candidate = self.models.get(operation) or self.models.get(
                "video" if operation != "image" else "image"
            )
            if isinstance(candidate, dict):
                candidate = candidate.get("model") or candidate.get("id")
            if candidate:
                return str(candidate)
        return self.default_model

    def _build_context(self, payload: dict, model: str) -> dict[str, Any]:
        duration = payload.get("duration", "5s")
        try:
            duration_seconds = int(str(duration).rstrip("s"))
        except ValueError:
            duration_seconds = 5
        return {
            "prompt": payload.get("prompt", ""),
            "mode": payload.get("mode", ""),
            "ratio": payload.get("ratio") or payload.get("aspect_ratio") or "16:9",
            "duration": duration,
            "duration_seconds": duration_seconds,
            "source_url": payload.get("sourceUrl"),
            "sourceUrl": payload.get("sourceUrl"),
            "source_type": payload.get("sourceType"),
            "sourceType": payload.get("sourceType"),
            "model": model,
            "resolution": payload.get("resolution", "720p"),
            "generate_audio": bool(payload.get("generate_audio", True)),
            "operation": _operation_capability(payload.get("mode")),
        }

    def prepare(self, payload: dict) -> dict[str, Any]:
        model = self.model_for(payload)
        context = self._build_context(payload, model)
        operation = context["operation"]
        template = self.request_templates.get(operation) or self.request_template
        if template is None:
            body = {
                "prompt": payload.get("prompt", ""),
                "model": model,
            }
            if payload.get("ratio"):
                body["aspect_ratio"] = payload["ratio"]
            if payload.get("duration"):
                body["duration"] = context["duration_seconds"]
            if payload.get("sourceUrl"):
                body["source_url"] = payload["sourceUrl"]
        else:
            body = _render_template(template, context)
        submit_url = self.submit_urls.get(operation) or self.submit_url
        status_template = self.status_urls.get(operation) or self.status_url_template
        return {
            "input": body,
            "model": model,
            "_submit_url": submit_url,
            "_status_url_template": status_template,
        }

    def _headers(self) -> dict[str, str]:
        token = os.getenv(self.token_env, "") if self.token_env else ""
        headers = {"Content-Type": "application/json"}
        if not token or self.auth == "none":
            return headers
        if self.auth in {"bearer", "token"}:
            headers["Authorization"] = f"Bearer {token}"
        elif self.auth in {"api-key", "x-api-key"}:
            headers[self.auth_header or "X-API-Key"] = token
        elif self.auth == "header":
            headers[self.auth_header or "Authorization"] = token
        elif self.auth == "query":
            # Query-key auth is supported for vendors that do not accept API keys in
            # headers. The actual query injection happens in _auth_url().
            pass
        else:
            raise ProviderError(
                f"Unsupported auth type '{self.auth}' for provider {self.name}."
            )
        headers.update(self.extra_headers)
        return headers

    def _auth_url(self, url: str) -> str:
        token = os.getenv(self.token_env, "") if self.token_env else ""
        if self.auth != "query" or not token:
            return url
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[self.auth_query_name or "api_key"] = token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _response_value(self, data: dict, key: str, defaults: list[str]) -> Any:
        configured = self.response.get(key)
        paths = configured if isinstance(configured, list) else ([configured] if configured else [])
        value = _first_path(data, paths, None)
        return value if value is not None else _first_path(data, defaults, None)

    async def submit(self, request):
        if not self.configured():
            raise ProviderError(f"{self.name} is not configured on the server.")
        context = {"model": request.get("model", self.default_model)}
        submit_url = request.get("_submit_url") or self.submit_url
        url = _render_template(submit_url, context)
        url = self._auth_url(url)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
            try:
                r = await c.request(
                    self.submit_method,
                    url,
                    headers=self._headers(),
                    json=request.get("input", request),
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"{self.name} network error: {exc}") from exc
        if r.status_code >= 400:
            raise ProviderError(
                f"{self.name} rejected the request ({r.status_code}): "
                f"{r.text[:800].replace(chr(10), ' ')}"
            )
        try:
            data = r.json()
        except Exception as exc:
            raise ProviderError(f"{self.name} returned non-JSON output.") from exc
        job_id = self._response_value(
            data, "id",
            ["id", "job_id", "jobId", "task_id", "taskId", "prediction.id", "data.id"],
        )
        output = self._response_value(
            data,
            "output",
            ["output", "output_url", "outputUrl", "url", "result.url", "data.output"],
        )
        if isinstance(output, list):
            output = next((x for x in output if isinstance(x, str) and x), None)
        if job_id in (None, "") and output:
            job_id = f"sync-{uuid.uuid4().hex}"
            status = self._response_value(
                data, "status", ["status", "state", "data.status", "prediction.status"]
            ) or "completed"
        elif job_id in (None, ""):
            raise ProviderError(f"{self.name} returned no job ID or synchronous output.")
        else:
            status = self._response_value(
                data, "status", ["status", "state", "data.status", "prediction.status"]
            ) or "queued"
        status_url = self._response_value(
            data,
            "status_url",
            ["status_url", "statusUrl", "urls.get", "urls.status", "data.status_url"],
        )
        raw = dict(data)
        if status_url:
            raw["status_url"] = status_url
        elif request.get("_status_url_template"):
            raw["status_url"] = request["_status_url_template"]
        return GenerationResult(self.name, str(job_id), str(status), output, raw)

    async def status(self, job_id, status_url=None):
        if not self.configured():
            raise ProviderError(f"{self.name} is not configured on the server.")
        template = status_url or self.status_url_template
        if not template:
            raise ProviderError(
                f"{self.name} has no status URL template. Set status_url_template."
            )
        url = self._auth_url(template.replace("{id}", str(job_id)).replace("{job_id}", str(job_id)))
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            try:
                r = await c.request(self.status_method, url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise ProviderError(f"{self.name} status network error: {exc}") from exc
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} status failed ({r.status_code}).")
        try:
            data = r.json()
        except Exception as exc:
            raise ProviderError(f"{self.name} returned non-JSON status output.") from exc
        status = self._response_value(
            data, "status", ["status", "state", "data.status", "prediction.status"]
        ) or "unknown"
        output = self._response_value(
            data,
            "output",
            ["output", "output_url", "outputUrl", "url", "result.url", "data.output"],
        )
        if isinstance(output, list):
            output = next((x for x in output if isinstance(x, str) and x), None)
        normalized = dict(data)
        if str(status).lower() in FINAL_STATUSES:
            normalized["_final"] = True
        return GenerationResult(self.name, job_id, str(status), output, normalized)


class PublicImageURLProvider(BaseGenerationProvider):
    """Opt-in public/no-key image generator for controlled quality testing.

    The actual URL template is deployment configuration. Vidigen intentionally does not
    embed a guessed third-party "free" endpoint and call it production-ready.
    """
    def __init__(self, spec: dict[str, Any]):
        self.name = str(spec.get("name", "free-image-test")).strip().lower()
        self.capabilities = {"image"}
        url_template = str(spec.get("url_template") or spec.get("free_image_url_template") or "").strip()
        url_env = str(spec.get("url_template_env") or "").strip()
        self.url_template = os.getenv(url_env, "") if url_env else url_template
        self.enabled_env = str(spec.get("enabled_env") or "").strip()
        self.models = spec.get("models") or {}
        self.default_model = str(spec.get("default_model", "free-image-test")).strip()

    def configured(self) -> bool:
        if not self.url_template:
            return False
        if self.enabled_env:
            return os.getenv(self.enabled_env, "false").lower() in {"1", "true", "yes", "on"}
        return True

    def model_for(self, payload: dict) -> str:
        return str(self.models.get("image") or self.default_model)

    def prepare(self, payload: dict) -> dict[str, Any]:
        if _operation_capability(payload.get("mode")) != "image":
            raise ProviderError(f"{self.name} is an image-only provider.")
        if payload.get("sourceUrl"):
            raise ProviderError("Text → Image does not accept source media.")
        from urllib.parse import quote
        prompt = quote(str(payload.get("prompt") or ""), safe="")
        ratio = str(payload.get("ratio") or "1:1")
        model = self.model_for(payload)
        url = (
            self.url_template
            .replace("{{prompt}}", prompt)
            .replace("{{model}}", quote(model, safe=""))
            .replace("{{ratio}}", quote(ratio, safe=""))
        )
        return {"input": {}, "model": model, "_direct_output_url": url}

    async def submit(self, request):
        url = request.get("_direct_output_url")
        if not url:
            raise ProviderError(f"{self.name} did not produce an image URL.")
        job_id = f"image-{uuid.uuid4().hex}"
        return GenerationResult(self.name, job_id, "completed", str(url), {"output_url": str(url)})

    async def status(self, job_id: str, status_url: str | None = None):
        raise ProviderError(f"{self.name} uses synchronous URL output; status polling is not required.")


def _manifest_specs() -> list[dict[str, Any]]:
    raw = os.getenv("VIDIGEN_PROVIDER_CONFIG_JSON", "").strip()
    config_file = os.getenv("VIDIGEN_PROVIDER_CONFIG_FILE", str(
        __import__("pathlib").Path(__file__).with_name("providers.json")
    ))
    if not raw:
        try:
            path = __import__("pathlib").Path(config_file)
            if path.exists():
                raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Provider manifest is invalid JSON ({config_file} / VIDIGEN_PROVIDER_CONFIG_JSON): {exc}"
        ) from exc
    if isinstance(data, dict):
        data = data.get("providers", [])
    if not isinstance(data, list):
        raise ProviderError(
            "VIDIGEN_PROVIDER_CONFIG_JSON must be a JSON array or "
            '{"providers": [...]} object.'
        )
    return [x for x in data if isinstance(x, dict)]


def _build_providers() -> dict[str, BaseGenerationProvider]:
    providers: dict[str, BaseGenerationProvider] = {
        "replicate": ReplicateProvider(),
        "seedance": ConfiguredHTTPProvider(
            "seedance", "SEEDANCE_API_URL", "SEEDANCE_API_TOKEN",
            "SEEDANCE_STATUS_URL_TEMPLATE", {"video"}
        ),
        "runway": ConfiguredHTTPProvider(
            "runway", "RUNWAY_API_URL", "RUNWAY_API_TOKEN",
            "RUNWAY_STATUS_URL_TEMPLATE", {"video"}
        ),
    }
    try:
        specs = _manifest_specs()
    except ProviderError:
        specs = []
    for spec in specs:
        name = str(spec.get("name", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,48}", name):
            continue
        if name in providers and not spec.get("replace_builtin"):
            continue
        if str(spec.get("type", "")).strip().lower() in {"image-url", "public-image-url"}:
            providers[name] = PublicImageURLProvider(spec)
        else:
            providers[name] = ManifestHTTPProvider(spec)
    return providers


PROVIDERS = _build_providers()


def _provider_is_configured(provider: Any) -> bool:
    configured = getattr(provider, "configured", None)
    if callable(configured):
        return bool(configured())
    # Backwards-compatible duck typing for test doubles/custom adapters that predate the
    # formal BaseGenerationProvider interface. Real registry entries always implement it.
    return True

def _provider_supports(provider: Any, capability: str) -> bool:
    supports = getattr(provider, "supports", None)
    if callable(supports):
        return bool(supports(capability))
    capabilities = getattr(provider, "capabilities", {"video"})
    return capability in capabilities or (
        capability in {"image-to-video", "video-to-video"} and "video" in capabilities
    )

def _provider_model(provider: Any, payload: dict) -> str:
    model_for = getattr(provider, "model_for", None)
    if callable(model_for):
        return str(model_for(payload))
    return str(getattr(provider, "name", "provider"))

def configured_providers(capability: str | None = None) -> list[str]:
    names = []
    for name, provider in PROVIDERS.items():
        if not _provider_is_configured(provider):
            continue
        if capability and not _provider_supports(provider, capability):
            continue
        names.append(name)
    return names


def provider_inventory() -> list[dict[str, Any]]:
    inventory = []
    for name, provider in PROVIDERS.items():
        capabilities = sorted(getattr(provider, "capabilities", {"video"}))
        inventory.append({
            "key": name,
            "capability": capabilities[0] if capabilities else "generation",
            "capabilities": capabilities,
            "configured": _provider_is_configured(provider),
            "model": _provider_model(provider, {"mode": "Text → Video"}),
        })
    return inventory


# --- Background removal (image + video) -----------------------------------------------
# These fixed utility adapters intentionally remain separate from the extensible main
# generation registry because the utility calls use model/version-specific contracts.
REMBG_IMAGE_VERSION = "34bd50c3cdcf667a839abdcdde7201d5b39bbebb54aa037da542ee6e670d9786"
REMBG_VIDEO_MODEL = "lucataco/rembg-video"


async def remove_background(image_url: str) -> GenerationResult:
    token = os.getenv("REPLICATE_API_TOKEN", "")
    if not token:
        raise ProviderError("Replicate is not configured on the server.")
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as c:
        r = await c.post(
            "https://api.replicate.com/v1/predictions",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "wait=25",
            },
            json={"version": REMBG_IMAGE_VERSION, "input": {"image": image_url}},
        )
    if r.status_code >= 400:
        raise ProviderError("Background removal request rejected ({0}).".format(r.status_code))
    d = await _poll_replicate_prediction(r.json(), token, timeout_seconds=90)
    out = d.get("output")
    out_url = out if isinstance(out, str) else None
    return GenerationResult(
        "replicate-rembg", str(d.get("id", "")), str(d.get("status", "starting")), out_url, d
    )


async def remove_background_video(video_url: str) -> GenerationResult:
    token = os.getenv("REPLICATE_API_TOKEN", "")
    if not token:
        raise ProviderError("Replicate is not configured on the server.")
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as c:
        r = await c.post(
            f"https://api.replicate.com/v1/models/{REMBG_VIDEO_MODEL}/predictions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"input": {"video": video_url}},
        )
    if r.status_code >= 400:
        raise ProviderError("Video background removal request rejected ({0}).".format(r.status_code))
    d = await _poll_replicate_prediction(r.json(), token, timeout_seconds=240)
    out = d.get("output")
    out_url = out if isinstance(out, str) else (
        out[0] if isinstance(out, list) and out and isinstance(out[0], str) else None
    )
    return GenerationResult(
        "replicate-rembg-video", str(d.get("id", "")), str(d.get("status", "starting")), out_url, d
    )


async def _poll_replicate_prediction(prediction: dict, token: str, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    backoff = 1.5
    poll_url = (
        (prediction.get("urls") or {}).get("get")
        or f"https://api.replicate.com/v1/predictions/{prediction.get('id')}"
    )
    async with httpx.AsyncClient(timeout=30) as c:
        while prediction.get("status") not in ("succeeded", "failed", "canceled") and time.time() < deadline:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.4, 6.0)
            resp = await c.get(poll_url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code < 400:
                prediction = resp.json()
    return prediction
