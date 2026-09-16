# Vidigen Production Billing & Pricing

The release uses four plans: Free, Creator, Pro and Studio. The default prices are application pricing decisions, not claims about third-party model costs:

| Plan | Monthly | Credits | Storage |
|---|---:|---:|---:|
| Free | GHS 0 | 50 | 2 GB |
| Creator | GHS 149 | 600 | 25 GB |
| Pro | GHS 399 | 1,800 | 100 GB |
| Studio | GHS 999 | 5,000 | 500 GB |

Because premium video-model pricing varies by provider, model, duration and resolution, Vidigen charges internal credit weights per operation. The default weights are conservative for premium generation and are adjustable from the Admin Billing panel. They are not provider invoice prices.

The administrator can change each plan's price, monthly credits, storage, watermark, commercial-use flag and priority flag without changing application code. Paystack recurring-plan synchronization is attempted when a Paystack secret is configured.

## Google Pay

The client includes a Google Pay checkout option that sends the request through the configured Paystack hosted checkout path. The release does **not** claim that Google Pay is available on every Paystack merchant/account. Enable it only after verifying that the current Paystack merchant account/checkout presents Google Pay. A direct Google Pay token processor is intentionally not fabricated.

## Agent learning

The Brain automatically samples completed generated outputs when the output host is explicitly allowlisted. It records media metadata, sampled-frame statistics, prompt/tags, rating and acceptance signals, then stores reusable creative patterns. The agent can retrieve those patterns for later image/video prompts and series generation. This is retrieval/adaptation learning, not silent retraining of third-party foundation models.

All imported external training/learning resources must remain user-owned or appropriately licensed and keep provenance/license metadata.
