# Vidigen provider manifest

Vidigen's main generation path is provider-agnostic. The built-in providers remain available,
but additional generation APIs can be added as data rather than by changing React or gateway
routing code.

## Add a provider

1. Add a provider object to gateway/providers.json, or provide the same JSON through
   VIDIGEN_PROVIDER_CONFIG_JSON.
2. Put the provider API credential in a server-side environment/secret and set token_env to
   its environment-variable name.
3. Give the provider at least one generation capability:
   video, image, image-to-video, or video-to-video.
4. Define the request template for the vendor. A provider can have separate templates and
   endpoints for each operation.
5. Put the provider name in VIDIGEN_PROVIDER_PRIORITY if you want it tried earlier.

## Manifest shape

{
  "name": "my-provider",
  "token_env": "MY_PROVIDER_API_KEY",
  "auth": "bearer",
  "capabilities": ["video", "image", "image-to-video", "video-to-video"],
  "models": {
    "video": "vendor-video-model",
    "image": "vendor-image-model"
  },
  "submit_url": "https://api.example.com/v1/generations",
  "status_url_template": "https://api.example.com/v1/generations/{id}",
  "request_templates": {
    "video": {
      "model": "{{model}}",
      "prompt": "{{prompt}}",
      "aspect_ratio": "{{ratio}}",
      "duration": "{{duration_seconds}}"
    },
    "image": {
      "model": "{{model}}",
      "prompt": "{{prompt}}",
      "size": "{{ratio}}"
    },
    "image-to-video": {
      "model": "{{model}}",
      "prompt": "{{prompt}}",
      "image_url": "{{source_url}}"
    },
    "video-to-video": {
      "model": "{{model}}",
      "prompt": "{{prompt}}",
      "video_url": "{{source_url}}"
    }
  },
  "response": {
    "id": ["id", "job_id", "data.id"],
    "status": ["status", "state", "data.status"],
    "output": ["output.url", "output", "data.output"],
    "status_url": ["urls.get", "status_url", "data.status_url"]
  }
}

## Supported request placeholders

{{prompt}}
{{mode}}
{{ratio}}
{{duration}}
{{duration_seconds}}
{{source_url}}
{{sourceUrl}}
{{model}}
{{resolution}}
{{generate_audio}}
{{operation}}

An exact placeholder preserves the original JSON type. For example,
"duration": "{{duration_seconds}}" becomes a number.

## Authentication

Supported values for auth are:
- bearer / token
- api-key / x-api-key
- header
- query
- none

For query authentication, set auth_query_name. The token is never sent to the browser.

## Synchronous APIs

An API may return the final media URL immediately without a job ID. The adapter creates an
internal sync job identifier and the gateway returns outputUrl immediately, so the normal
frontend generation flow still works.

## Multiple providers and keys

The same vendor can be registered more than once under different names if you need separate
accounts, regions, keys, or model policies. The router treats each manifest entry as an
independent fallback candidate.

Example priority:

VIDIGEN_PROVIDER_PRIORITY=provider-a,provider-b,replicate,runway

The gateway bills the logical generation once. Fallback providers are execution retries and
must not cause a second user charge.

## Security

Do not put API keys, bearer tokens, cookies, or other secrets in providers.json or
VIDIGEN_PROVIDER_CONFIG_JSON. Reference the secret with token_env and keep the secret in the
server deployment/secret manager.

The manifest is intentionally limited to HTTP JSON execution. Providers that require a
multi-step upload/signature protocol, websocket stream, proprietary SDK, or unusual binary
protocol should get a dedicated adapter class inside gateway/providers.py.


## Optional free/no-key image test slot

The repository includes an opt-in `free-image-test` slot in `gateway/providers.json`. It is disabled unless both `VIDIGEN_FREE_IMAGE_TEST_ENABLED=true` and `VIDIGEN_FREE_IMAGE_TEST_URL_TEMPLATE` are set on the gateway. The URL template must be supplied only after the operator has independently verified the service's current availability, terms, rate limits and output licensing. This slot is for controlled quality testing; it is not described as a permanently free or production SLA-backed provider.
