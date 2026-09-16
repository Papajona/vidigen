# Integrating RenderEnginePlugin — corrected guidance

**Correction from last pass:** I originally described this as a separate Gradle library
module wired into `settings.gradle` with `implementation project(':capacitor-android')`.
That's the right shape *only* if you intend to publish this as a reusable, installable
Capacitor plugin package for other apps. Since this plugin is specific to this one app, that
adds real failure surface (module wiring, duplicate dependency resolution) for no benefit.
The simpler, standard approach for an app-local native plugin:

## Steps (after `npx cap add android` has generated the `android/` folder)

1. Copy `RenderEnginePlugin.kt` directly into your app module's own package, e.g.:
   `android/app/src/main/java/<your-package>/RenderEnginePlugin.kt`
   (change its `package` declaration at the top of the file to match `<your-package>`
   instead of `com.vidigen.renderengine` — it doesn't need its own package.)

2. Add the Media3 dependencies straight to the **existing** `android/app/build.gradle`
   (not a new module):
   ```gradle
   dependencies {
       // ... existing Capacitor dependencies stay as-is ...
       implementation "androidx.media3:media3-transformer:1.5.1" // check current version first
       implementation "androidx.media3:media3-common:1.5.1"
       implementation "androidx.media3:media3-effect:1.5.1"
   }
   ```

3. Register the plugin in `android/app/src/main/java/<your-package>/MainActivity.kt`:
   ```kotlin
   class MainActivity : BridgeActivity() {
       override fun onCreate(savedInstanceState: Bundle?) {
           registerPlugin(RenderEnginePlugin::class.java) // must come before super.onCreate()
           super.onCreate(savedInstanceState)
       }
   }
   ```

That's the entire integration — no `settings.gradle` changes, no separate module. The
`native-media-engine/android/` folder in this zip (with its own `build.gradle`) is left in
place as the "if you later want this as a standalone reusable plugin" path, not the
recommended one for just running this app.
