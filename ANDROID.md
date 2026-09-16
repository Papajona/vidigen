# Android build and test

This repository uses Capacitor to package the React/Vite web editor as an Android app.

```bash
npm install
npm run build
npx cap add android   # first time only
npm run android:sync
npm run android:open
```

In Android Studio, select an emulator or physical device and press Run. Use Logcat and the Android Studio test runner.

For a remote local gateway, use a LAN/TLS endpoint and a gateway token. Do not use `127.0.0.1` unless the gateway actually runs on the Android device.

## What can be verified before Android Studio

The repository itself can run the Node unit suite and gateway syntax checks. A native Android emulator test requires Android Studio, the Android SDK and a device/emulator on the machine where the project is opened. Android Studio is an IDE distributed by Google; it is not a remotely callable test service from this repository.

The first `npx cap add android` command creates the native `android/` project. This repository intentionally keeps the generated Android project out of source control; after dependencies are installed, run the command once and commit the generated `android/` directory if you want a self-contained Android Studio project.

For a production Android build, do not use an unauthenticated LAN gateway. Configure a strong `VIDIGEN_GATEWAY_TOKEN`, restrict `VIDIGEN_ALLOWED_ORIGINS` to the app's trusted origin, and keep ComfyUI bound to a private interface.
