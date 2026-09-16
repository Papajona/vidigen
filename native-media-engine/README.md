# Vidigen Native Media Engine — V12 boundary

This directory defines the production Android boundary for the next native media layer. V12 keeps the editor UI in React/Capacitor while reserving expensive media work for Kotlin/C++.

## Production responsibilities

- Media3/ExoPlayer playback and timeline synchronization
- MediaCodec hardware decode/encode
- Vulkan/OpenGL GPU compositing and shader effects
- MediaExtractor/MediaMuxer for deterministic export
- frame-by-frame processing without loading whole videos into RAM
- future MediaPipe/OpenCV segmentation/tracking bridge

## Bridge contract

The JavaScript layer should send a serializable `RenderProject` containing project settings, ordered tracks, clips, transforms, keyframes, overlays, captions and audio references. The native layer returns job progress and a final local URI.

Do not place provider API keys in this layer or in the APK. Cloud generation remains behind the Vidigen gateway.
