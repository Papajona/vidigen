# Vidigen test plan

## Local static/unit tests

Run:

```bash
npm install
npm test
npm run build
```

The repository includes dependency-free Node tests for request classification, preference learning, safety flags and memory validation.

## Android Studio

1. Install Android Studio and an Android SDK.
2. Install a Java version compatible with the generated Android/Gradle project.
3. For this Capacitor project, run `npm install`, `npm run build`, then `npx cap add android` (first time) and `npm run android:sync`.
4. Open the `android` directory in Android Studio.
5. Use an emulator or physical Android device.
6. Run the app with the Run button.
7. For automated tests, use Android Studio's test source sets (`test` for local JVM tests and `androidTest` for device/emulator tests).
8. Use Logcat to diagnose runtime failures.
9. Use the Android Studio Profiler for CPU, memory and network behavior.
10. Before release, run lint/static analysis, test on multiple API levels, and build a signed release artifact.

## AI Studio limitation

Google AI Studio browser preview can validate the web layer. It is not a substitute for an Android emulator/device test. The current environment cannot open the private AI Studio app URL or operate its UI, so that must be tested from the user's AI Studio session.

## Production-candidate smoke test

Run `python3 tests/run_smoke.py` after installing `gateway/requirements.txt`. This validates gateway authentication, health, analyzer fallback, and request validation without requiring a paid AI provider.

## Payment integration TEST

Use the dedicated Billing → Payment Integration TEST panel. The only test amounts are GHS 10, GHS 20 and GHS 50. Paystack TEST requires a `sk_test_` key and is independently verified from Google Pay TEST. Google Pay TEST uses Google's `PaymentsClient` TEST environment and does not grant production entitlements.
