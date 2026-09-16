import unittest, hashlib, hmac
from gateway.billing import DEFAULT_PLANS, _amount_subunit

class BillingTests(unittest.TestCase):
    def test_plan_defaults_are_cost_controlled(self):
        self.assertEqual(_amount_subunit(149),14900)
        self.assertEqual(DEFAULT_PLANS[1]['monthly_credits'],600)
        self.assertEqual(DEFAULT_PLANS[-1]['name'],'Studio')
        self.assertGreater(DEFAULT_PLANS[-1]['price_ghs'], DEFAULT_PLANS[1]['price_ghs'])

    def test_paystack_signature_shape(self):
        secret=b'test-secret'; body=b'{"event":"charge.success"}'
        sig=hmac.new(secret,body,hashlib.sha512).hexdigest()
        self.assertEqual(len(sig),128)


class PaymentTestPricingTests(unittest.TestCase):
    def test_payment_test_amounts_are_exact(self):
        from gateway.billing import TEST_PAYMENT_PRICES_GHS
        self.assertEqual(TEST_PAYMENT_PRICES_GHS, (10.0, 20.0, 50.0))

    def test_test_amounts_are_converted_to_pesewa(self):
        self.assertEqual(_amount_subunit(10), 1000)
        self.assertEqual(_amount_subunit(20), 2000)
        self.assertEqual(_amount_subunit(50), 5000)

if __name__=='__main__': unittest.main()
