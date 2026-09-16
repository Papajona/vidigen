# Payment TEST release notes

- Added isolated Paystack TEST checkout and verification endpoints.
- Refused live Paystack keys from the Paystack TEST endpoint.
- Added exact test prices: GHS 10, GHS 20, GHS 50.
- Added Google Pay Web TEST integration using Google's TEST environment.
- Removed the incorrect production routing of the Google Pay button through Paystack.
- Production Google Pay remains gated pending Google approval and confirmation of a supported production processor.
- Added automated tests for the three amounts and their Paystack subunit conversions.
