# Vidigen AI V12 — AI Production Studio

Vidigen V12 is an AI-native media production foundation designed to combine a simple generative workflow with professional timeline control.

## Competitive workflow

**Idea → AI Director → shot graph → provider routing → generation jobs → quality gate → timeline → AI editing → captions/audio → native render**

### V12 product capabilities
- AI Director multi-shot generation
- provider abstraction with Auto Router
- structured editable timeline
- undo/redo, split, duplicate, reorder, delete
- keyframe data model
- natural-language AI Editor
- professional color, transform, speed and opacity controls
- text overlays
- audio import + microphone recording
- real faster-whisper caption integration
- SRT export
- creative preference memory
- gateway health/provider visibility
- secure server-side provider boundary
- Android native media-engine boundary
- explicit export-plan contract without pretending a render worker exists

## Testing

The current environment can run the dependency-free Node learning tests and Python gateway tests. The frontend build requires npm dependencies to be installed; this sandbox does not have the package registry cache required to install them offline.

```bash
npm install
npm test
npm run build
python3 -m pytest tests/v12_gateway_test.py
```

## Android direction

Keep React/Capacitor for the product UI. Connect a Kotlin/C++ native media engine for Media3, MediaCodec, Vulkan/OpenGL, MediaMuxer and future MediaPipe/OpenCV processing. Provider secrets must remain on the gateway and never be embedded in the APK.
