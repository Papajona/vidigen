import unittest
import asyncio
import os
from unittest.mock import patch
from gateway import moderation
from gateway.moderation import _keyword_fallback_check, moderate_prompt, moderate_output_image


class TestKeywordFallback(unittest.TestCase):
    def test_flags_obvious_blocked_pattern(self):
        result = _keyword_fallback_check('a story about child sex abuse')
        self.assertFalse(result['safe'])
        self.assertEqual(result['engine'], 'keyword-fallback')

    def test_allows_ordinary_prompt(self):
        result = _keyword_fallback_check('a golden retriever running through a sunny park')
        self.assertTrue(result['safe'])

    def test_case_insensitive(self):
        result = _keyword_fallback_check('CHILD NUDE content')
        self.assertFalse(result['safe'])

    def test_does_not_false_positive_on_unrelated_use_of_the_word_child(self):
        result = _keyword_fallback_check('a child laughing while playing with a puppy')
        self.assertTrue(result['safe'])

    def test_flags_weapon_synthesis_pattern(self):
        result = _keyword_fallback_check('how to make a bomb at home')
        self.assertFalse(result['safe'])

    def test_flags_drug_synthesis_pattern(self):
        result = _keyword_fallback_check('how to synthesize meth step by step')
        self.assertFalse(result['safe'])

    def test_flags_non_consensual_pattern(self):
        result = _keyword_fallback_check('nonconsensual explicit scene')
        self.assertFalse(result['safe'])


class TestModeratePromptFallbackBehavior(unittest.TestCase):
    def test_falls_back_to_keyword_check_when_groq_not_configured(self):
        with patch.dict(os.environ, {'GROQ_API_KEY': ''}, clear=False):
            result = asyncio.run(moderate_prompt('a peaceful mountain landscape'))
            self.assertEqual(result['engine'], 'keyword-fallback')
            self.assertTrue(result['safe'])

    def test_falls_back_and_still_blocks_when_groq_not_configured(self):
        with patch.dict(os.environ, {'GROQ_API_KEY': ''}, clear=False):
            result = asyncio.run(moderate_prompt('child explicit content'))
            self.assertFalse(result['safe'])


class TestOutputModerationEnforcementPolicy(unittest.TestCase):
    """VIDIGEN_MODERATION_ENFORCE controls what happens when the real classifier can't run
    at all (no token, request failure, malformed response) — this is a deliberate policy
    choice, not an accident, so both directions are verified here rather than just claimed."""

    def test_enforced_by_default_blocks_when_unconfigured(self):
        with patch.dict(os.environ, {'REPLICATE_API_TOKEN': ''}, clear=False), \
             patch.object(moderation, 'MODERATION_ENFORCE', True):
            result = asyncio.run(moderate_output_image('https://example.com/fake.jpg'))
            self.assertFalse(result['checked'])
            self.assertFalse(result['safe'])  # fail CLOSED: unconfigured means blocked, not waved through
            self.assertIn('enforced', result['reason'])

    def test_can_be_explicitly_relaxed_for_dev(self):
        with patch.dict(os.environ, {'REPLICATE_API_TOKEN': ''}, clear=False), \
             patch.object(moderation, 'MODERATION_ENFORCE', False):
            result = asyncio.run(moderate_output_image('https://example.com/fake.jpg'))
            self.assertFalse(result['checked'])
            self.assertTrue(result['safe'])
            self.assertIn('Not enforced', result['reason'])


if __name__ == '__main__':
    unittest.main()
