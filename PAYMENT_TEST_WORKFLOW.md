# Vidigen Payment TEST Workflow

## Verified separation

Vidigen treats these as two independent payment integrations:

1. **Paystack TEST** — Paystack's API with a `sk_test_` key. This endpoint can initialize and verify Paystack test transactions.
2. **Google Pay TEST** — Google Pay Web API with `PaymentsClient({environment: 'TEST'})`. This flow tests Google Pay's browser/payment-data integration and does not assume Paystack is its processor.

A Google Pay TEST success means Google Pay returned test payment data. It is **not** a live charge and does not prove production processor approval.

## Test amounts

Only these amounts are exposed by the Vidigen payment TEST lab:

- GHS 10.00 = 1,000 pesewas
- GHS 20.00 = 2,000 pesewas
- GHS 50.00 = 5,000 pesewas

The Paystack documentation supplied for this project states that amounts are sent in currency subunits and lists GHS as pesewa, with GHS available in Ghana.

## Paystack TEST procedure

1. Configure `PAYSTACK_SECRET_KEY` with a Paystack **test** secret key (`sk_test_...`).
2. Set `VIDIGEN_PAYMENT_TEST_MODE=true`.
3. Open Vidigen Billing → Payment Integration TEST.
4. Select GHS 10, 20, or 50 → **Paystack TEST**.
5. Complete the Paystack test checkout using Paystack's test credentials/cards.
6. Verify the returned reference with `/api/billing/test/paystack/verify/{reference}`.
7. Test payments are recorded as `test_pending`/`test_success` and never grant a production subscription entitlement.

## Google Pay TEST procedure

1. Set `GOOGLE_PAY_TEST_MODE=true`.
2. The browser loads Google's Google Pay JavaScript library and creates `PaymentsClient` with `environment: 'TEST'`.
3. Open Vidigen Billing → Payment Integration TEST.
4. Select GHS 10, 20, or 50 → **Google Pay TEST**.
5. If the device/browser is ready, Google Pay opens its TEST payment sheet.
6. A returned test payment payload is treated as an integration-test success only. Vidigen does not record it as a live Paystack payment or grant a subscription.

The Google Pay material supplied for this project requires a verified HTTPS website, an integration type (Gateway or Direct), buy-flow screenshots, and review/approval before full production access. Production processor configuration is therefore deliberately left gated.
