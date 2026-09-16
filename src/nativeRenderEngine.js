import { registerPlugin, Capacitor } from '@capacitor/core';
import { Filesystem, Directory } from '@capacitor/filesystem';

// Only resolves to a real native implementation inside a compiled Android app — i.e. NOT
// inside Google AI Studio's browser preview, and NOT in `npm run dev`. Both of those run
// the JS layer in a plain browser/WebView with no native plugin registered, so this must
// degrade gracefully rather than throw, or every non-native preview breaks.
const RenderEngine = registerPlugin('RenderEngine');

export function nativeExportAvailable() {
  return Capacitor.isNativePlatform() && Capacitor.isPluginAvailable('RenderEngine');
}

/**
 * Fixes the gap from the previous pass: `URL.createObjectURL(file)` produces a `blob:` URL
 * that only exists inside the WebView's JS context — Media3 on the native side can't open
 * it. When running natively, this copies the picked file to the app's cache directory via
 * Capacitor Filesystem and returns a real `file://` URI that native code CAN open. Returns
 * null when not running natively (caller should keep using the blob URL for preview only).
 */
export async function copyFileToNativeStorage(file) {
  if (!Capacitor.isNativePlatform()) return null;
  const base64Data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  const fileName = `import-${Date.now()}-${file.name.replace(/[^a-zA-Z0-9._-]/g, '_')}`;
  await Filesystem.writeFile({ path: fileName, data: base64Data, directory: Directory.Cache });
  const { uri } = await Filesystem.getUri({ path: fileName, directory: Directory.Cache });
  return uri;
}

/**
 * @param {{clips: Array<{uri: string, trimStartMs?: number, trimEndMs?: number}>, backgroundAudioUri?: string}} renderProject
 * @param {(progress: number) => void} onProgress
 * @returns {Promise<{outputPath: string, durationMs: number}>}
 */
export async function exportProjectNative(renderProject, onProgress) {
  if (!nativeExportAvailable()) {
    throw new Error('Native export is only available in the compiled Android app, not in this preview.');
  }
  const listener = await RenderEngine.addListener('exportProgress', (e) => {
    if (onProgress) onProgress(e.progress);
  });
  try {
    return await RenderEngine.exportProject({ project: JSON.stringify(renderProject) });
  } finally {
    listener.remove();
  }
}

