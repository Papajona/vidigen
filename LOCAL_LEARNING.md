# Vidigen Brain — production learning contract

## What is real today

Vidigen has two complementary learning layers.

### 1. Persistent server Brain

When an authenticated generation completes, the gateway can automatically run the Brain learning pipeline. The pipeline downloads the completed output from an allowlisted source, validates/decodes it, samples video frames or image statistics, and stores:

- the source prompt
- provider/model tags
- media metadata
- output analysis
- a compact reusable creative pattern

These records live in Supabase under the owning user. They are not browser-only state.

The gateway exposes a private /api/brain/profile endpoint. It aggregates positive feedback and accepted outputs into a compact profile of learned style tags and successful prompt patterns. The frontend fetches this profile and injects the learned style signals into later generation briefs.

### 2. Local convenience memory

The browser also keeps a small local history for fast UI personalization and offline continuity. Clearing browser storage does not delete the server Brain.

## What counts as learning

A completed job is observed and analyzed automatically. A job is not treated as a positive preference merely because it completed.

Explicit feedback is the confidence signal. Ratings of 4–5 or accepted=true contribute to learned style preferences. This prevents failed or merely completed jobs from becoming preferred styles.

Feedback is stored server-side through /api/feedback and is checked against the authenticated user's generation job before it is recorded.

## What Vidigen does not claim

Vidigen does not currently fine-tune or update the weights of Replicate, Seedance, Runway, or another third-party foundation model after each job.

The current learning mode is retrieval + adaptation:
1. generate
2. wait for completion
3. validate/moderate the output
4. analyze the output
5. store reusable metadata/patterns
6. collect explicit feedback
7. retrieve the user's positive signals on a later generation

That is real application-level learning, not automatic model retraining.

## Multi-provider learning

The provider-agnostic generation path records the provider and semantic request tags. Replicate is the default priority, followed by configured providers in VIDIGEN_PROVIDER_PRIORITY.

For HTTP providers such as Seedance or Runway, the gateway can persist a returned status URL or use a configured *_STATUS_URL_TEMPLATE. Once the provider job reaches a completed state through polling, the same moderation and Brain-learning path can run.

## Resource learning

Future imports should be user-provided or appropriately licensed. Resource records should retain source URL, owner/author where known, license, retrieval date, checksum where practical, and permitted-use notes.

Vidigen must not silently scrape arbitrary websites, bypass access controls, or train on copyrighted material without appropriate rights.
