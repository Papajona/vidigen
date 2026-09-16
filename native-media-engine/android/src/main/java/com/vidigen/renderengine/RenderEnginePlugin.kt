package com.vidigen.renderengine

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.transformer.Composition
import androidx.media3.transformer.EditedMediaItem
import androidx.media3.transformer.EditedMediaItemSequence
import androidx.media3.transformer.ExportException
import androidx.media3.transformer.ExportResult
import androidx.media3.transformer.ProgressHolder
import androidx.media3.transformer.Transformer
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Real local export via Jetpack Media3 Transformer, replacing the placeholder gateway
 * message ("Export requires the gateway render worker...") that main.jsx currently shows.
 *
 * Consumes the `RenderProject` shape already defined in this module's README: an ordered
 * list of video clips (each with a source URI and a trim window in milliseconds) plus an
 * optional single background-audio track. This first version deliberately covers exactly
 * that — sequential trim + concatenate + mux to one MP4 — and nothing else (no keyframed
 * transforms, no overlays, no color effects yet). Those are real Media3 Transformer
 * capabilities (Effects, ScaleAndRotateTransformation, Crop, etc.) and worth adding next,
 * but bundling them in unverified, on a library I can't compile-test here, would trade
 * correctness for scope. Better to ship a narrow slice that's right than a broad one that
 * silently isn't.
 *
 * API calls here were checked against developer.android.com's current Media3 Transformer
 * docs (Composition/EditedMediaItemSequence/ClippingConfiguration) rather than assumed from
 * memory, since this library's API has changed release to release.
 */
@CapacitorPlugin(name = "RenderEngine")
class RenderEnginePlugin : Plugin() {

    @PluginMethod
    fun exportProject(call: PluginCall) {
        val projectJson = call.getString("project")
        if (projectJson.isNullOrBlank()) {
            call.reject("Missing 'project' (a JSON-encoded RenderProject).")
            return
        }

        val project: JSONObject
        val clipsArray: JSONArray
        try {
            project = JSONObject(projectJson)
            clipsArray = project.getJSONArray("clips")
        } catch (e: Exception) {
            call.reject("Could not parse RenderProject JSON: ${e.message}")
            return
        }
        if (clipsArray.length() == 0) {
            call.reject("RenderProject has no clips to export.")
            return
        }

        val editedItems = mutableListOf<EditedMediaItem>()
        for (i in 0 until clipsArray.length()) {
            val clip = clipsArray.getJSONObject(i)
            val uriString = clip.optString("uri", "")
            if (uriString.isBlank()) {
                call.reject("Clip $i is missing a 'uri'.")
                return
            }
            val trimStartMs = clip.optLong("trimStartMs", 0L)
            val trimEndMs = clip.optLong("trimEndMs", -1L) // -1 = to end of clip

            val mediaItemBuilder = MediaItem.Builder().setUri(Uri.parse(uriString))
            if (trimStartMs > 0 || trimEndMs > 0) {
                val clipConfigBuilder = MediaItem.ClippingConfiguration.Builder()
                    .setStartPositionMs(trimStartMs)
                if (trimEndMs > 0) clipConfigBuilder.setEndPositionMs(trimEndMs)
                mediaItemBuilder.setClippingConfiguration(clipConfigBuilder.build())
            }
            editedItems.add(EditedMediaItem.Builder(mediaItemBuilder.build()).build())
        }

        // Using EditedMediaItemSequence.Builder(trackTypes).addItems(...) rather than the
        // withAudioAndVideoFrom()/withAudioFrom() convenience statics: those were added in a
        // LATER Media3 release than what compiles here (confirmed by a real compiler run —
        // "Unresolved reference" on both). This Builder-based form is documented across a
        // much wider range of Media3 versions, so it's the safer choice given I still can't
        // compile-verify this myself.
        val videoSequence = EditedMediaItemSequence.Builder(setOf(C.TRACK_TYPE_AUDIO, C.TRACK_TYPE_VIDEO))
            .addItems(editedItems)
            .build()
        val sequences = mutableListOf(videoSequence)

        val audioUri = project.optString("backgroundAudioUri", "")
        if (audioUri.isNotBlank()) {
            val audioItem = EditedMediaItem.Builder(MediaItem.fromUri(audioUri)).build()
            val audioSequence = EditedMediaItemSequence.Builder(setOf(C.TRACK_TYPE_AUDIO))
                .addItem(audioItem)
                .setIsLooping(true)
                .build()
            sequences.add(audioSequence)
        }

        // Passing an explicit List<EditedMediaItemSequence> (not a bare vararg call) so the
        // compiler doesn't have to choose between Composition.Builder's two overloads —
        // that ambiguity was the second real error from the compile run.
        val composition: Composition = Composition.Builder(sequences.toList()).build()

        val outputFile = File(context.cacheDir, "vidigen-export-${System.currentTimeMillis()}.mp4")
        val finished = java.util.concurrent.atomic.AtomicBoolean(false)

        val transformer = Transformer.Builder(context)
            .setVideoMimeType(MimeTypes.VIDEO_H265)
            .setAudioMimeType(MimeTypes.AUDIO_AAC)
            .addListener(object : Transformer.Listener {
                override fun onCompleted(composition: Composition, exportResult: ExportResult) {
                    finished.set(true)
                    val result = JSObject()
                    result.put("outputPath", outputFile.absolutePath)
                    result.put("durationMs", exportResult.durationMs)
                    call.resolve(result)
                }

                override fun onError(composition: Composition, exportResult: ExportResult, exportException: ExportException) {
                    finished.set(true)
                    call.reject("Export failed: ${exportException.message}", exportException)
                }
            })
            .build()

        // start(Composition, String) — now unambiguous since `composition` above has a
        // concrete, explicit `Composition` type rather than an inferred one. The original
        // "Overload resolution ambiguity" compiler error for this line was a direct
        // consequence of the Composition.Builder ambiguity above, not a separate bug.
        activity.runOnUiThread {
            transformer.start(composition, outputFile.absolutePath)
            pollProgress(transformer, finished)
        }
    }

    private fun pollProgress(transformer: Transformer, finished: java.util.concurrent.atomic.AtomicBoolean) {
        val holder = ProgressHolder()
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        val poll = object : Runnable {
            override fun run() {
                if (finished.get()) return
                transformer.getProgress(holder)
                val event = JSObject()
                event.put("progress", holder.progress)
                notifyListeners("exportProgress", event)
                handler.postDelayed(this, 250)
            }
        }
        handler.post(poll)
    }
}
