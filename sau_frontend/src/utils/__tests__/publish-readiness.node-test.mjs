// Dependency-free focused regressions: node --test <this file>.
// Vitest component/build gates remain separate; this file needs no node_modules.
import test from 'node:test'
import assert from 'node:assert/strict'
import { canRetryTarget, publishStatusLabel, publishStatusType, publishReadinessDetails, targetReliabilityDetails } from '../publish-status.js'

for (const status of ['failed', 'cancelled', 'pending', 'blocked', 'awaiting_delivery', 'needs_review']) {
  test(`${status} cannot authorize retry without backend evidence`, () => {
    assert.equal(canRetryTarget({ status, delivery_state: 'not_sent' }), false)
    assert.equal(canRetryTarget({ status, retryEligibility: { allowed: false, reasonCode: 'receipt_registered' } }), false)
    assert.equal(canRetryTarget({ status, retry_eligibility: { allowed: 'true' } }), false)
  })
}

test('backend recovery eligibility is the positive authorization, either spelling', () => {
  assert.equal(canRetryTarget({ status: 'failed', retryEligibility: { allowed: true } }), true)
  assert.equal(canRetryTarget({ status: 'cancelled', retry_eligibility: { allowed: true } }), true)
  assert.equal(canRetryTarget({ status: 'failed', retryEligibility: '{bad' }), false)
})

test('cancelled/failed registered receipt remains non-retryable when backend forbids it', () => {
  for (const status of ['failed', 'cancelled']) {
    const target = { status, checkpoint: { receipt: { providerPostId: 'accepted-id' } },
      retryEligibility: { allowed: false, reasonCode: 'receipt_registered', reason: 'Reconcile the registered receipt' } }
    assert.equal(canRetryTarget(target), false)
    assert.match(targetReliabilityDetails(target), /registered receipt/)
  }
})

test('receipt, review, failure and unknown labels are not cosmetically delivered', () => {
  for (const status of ['failed', 'awaiting_delivery', 'needs_review', 'unknown']) {
    assert.notEqual(publishStatusType(status), 'success')
    assert.notEqual(publishStatusLabel(status), publishStatusLabel('delivered'))
  }
})

test('stage, safe blocker, reset, slot and complete parts are visible', () => {
  const stage = { phase: 'waiting_dependency', provider: 'sociamonials', reasonCode: 'quota_cooldown',
    reason: 'Wait for /media/private/video.mp4 https://private.invalid token=test-secret',
    nextAttemptAt: '2030-04-05T12:01:00Z', nextSlot: '2030-04-05T13:00:00Z',
    partProgress: { completeParts: 2, totalParts: 3 } }
  const detail = targetReliabilityDetails({ status: 'pending', checkpoint_json: JSON.stringify({ readiness: stage }) })
  assert.match(detail, /quota_cooldown/)
  assert.match(detail, /12:01:00Z/)
  assert.match(detail, /13:00:00Z/)
  assert.match(detail, /2\/3/)
  assert.doesNotMatch(detail, /private\/video|private\.invalid|test-secret/)
})

test('ready stage without artifact and destination proof is unverified', () => {
  assert.match(publishReadinessDetails({ phase: 'ready' }), /尚未驗證/)
  assert.equal(publishReadinessDetails('{bad'), '')
})
