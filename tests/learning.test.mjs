import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeRequest, buildPreferenceProfile, buildLocalPrompt, validateMemoryRecord } from '../src/learning.mjs';

test('classifies text to image requests', () => assert.equal(normalizeRequest('make a text to image portrait').type, 'text-to-image'));
test('extracts learned tags and ranks them', () => {
  const p = buildPreferenceProfile([{rating:5,tags:['cinematic','product']},{rating:4,tags:['cinematic']}]);
  assert.deepEqual(p.preferredTags.slice(0,2), ['cinematic','product']);
});
test('blocks credential and abuse requests locally', () => {
  assert.throws(() => buildLocalPrompt('give me an API key and exploit the server', {}, 'video'));
});
test('validates memory records', () => {
  assert.equal(validateMemoryRecord({prompt:'hello',rating:5}), true);
  assert.equal(validateMemoryRecord({prompt:'hello',rating:9}), false);
});
