# Vidigen V12 — Competitive Production Architecture

## Product loop

Idea → AI Director → shot graph → model router → generation jobs → quality gate → timeline → AI editing → captions/audio → native render.

## Frontend

React + TypeScript/JavaScript + Capacitor remains the product shell. The timeline is a structured project model rather than a collection of UI-only controls. V12 adds undo/redo, split/duplicate/reorder, keyframes, natural-language edit commands, audio import/recording, captions, overlays and an export-plan boundary.

## Gateway

The FastAPI gateway owns authentication, provider secrets, generation jobs, status, captions and constrained timeline planning. `/api/edit-plan` deliberately returns a small safe operation vocabulary; the client never executes arbitrary code returned by a model.

## Native Android boundary

Kotlin/C++ should own hardware media decode/encode and GPU compositing. The native Android render worker is now connected in the app module via the Capacitor RenderEngine plugin and Media3 Transformer. The remaining release requirement is a successful signed Android build/test on the target machine.

## Provider routing

Users select Auto/quality intent while the gateway chooses a configured provider. Provider credentials remain server-side.

## Quality gate

Every production generation should eventually pass visual checks for prompt adherence, subject consistency, motion artifacts, flicker and output validity before automatic assembly.

## Security

Gateway tokens are session-scoped in the browser testing shell. Production authentication should use short-lived user sessions. Never ship Replicate, Runway, Seedance, ComfyUI or other provider secrets in an Android package.
