import { describe, expect, it } from 'vitest'
import { calendarEntityEvents } from '../calendarEvents'

const target = (id, status, scheduleAt = '2026-11-26T10:00:00') => ({ id, status, scheduleAt, fileRef: 'campaign_post:9', accountName: `Account ${id}`, accountId: id })
const entity = (status, targets) => ({ entityId: 'mg-1', status, scheduledAt: '2026-11-26T10:00:00',
  posts: [{ id: 9, platform: 'twitter', profileId: 7, draft: { message: 'Destination copy' } }],
  jobs: [{ id: 3, platform: 'twitter', profileId: 7, status, targets }],
})

describe('calendar destination visibility', () => {
  it.each(['failed', 'cancelled'])('retains a future pending sibling when the aggregate is %s', (status) => {
    const records = [entity(status, [target(1, status), target(2, 'pending')])]
    const pending = calendarEntityEvents(records, { month: '2026-11' })
    expect(pending.map((event) => event.targetId)).toEqual([2])
    expect(pending[0]).toMatchObject({ targetStatus: 'pending', jobId: 3, title: 'Destination copy' })
    expect(calendarEntityEvents(records, { month: '2026-11', showAll: true }).map((event) => event.targetId)).toEqual([1, 2])
  })

  it('retains pending, running, retrying, delivery-wait and action-needed destinations', () => {
    const statuses = ['pending', 'running', 'retrying', 'awaiting_delivery', 'blocked', 'needs_review']
    const records = [entity('failed', statuses.map((status, index) => target(index + 1, status)))]
    expect(calendarEntityEvents(records).map((event) => event.targetStatus)).toEqual(statuses)
  })

  it('does not invent an aggregate fallback after every destination is filtered out', () => {
    expect(calendarEntityEvents([entity('failed', [target(1, 'failed')])])).toEqual([])
    expect(calendarEntityEvents([entity('cancelled', [target(1, 'cancelled')])])).toEqual([])
  })

  it('applies month and destination filters after expansion rather than to sibling groups', () => {
    const records = [entity('failed', [target(1, 'failed'), target(2, 'pending'), target(3, 'pending', '2026-12-01T10:00:00')])]
    expect(calendarEntityEvents(records, { month: '2026-11', platforms: ['twitter'], profileIds: [7], accountIds: [2] }).map((event) => event.targetId)).toEqual([2])
    expect(calendarEntityEvents(records, { platforms: ['facebook'] })).toEqual([])
  })

  it('expands a legacy entity with jobs/targets but no campaign posts', () => {
    const record = { entityId: 'job-7', status: 'awaiting_delivery', posts: [], jobs: [{ id: 7, platform: 'twitter', title: 'Legacy upload',
      targets: [{ id: 10, status: 'awaiting_delivery', accountName: 'Legacy account', scheduleAt: '2026-11-26T12:00:00' }],
    }] }
    expect(calendarEntityEvents([record])[0]).toMatchObject({ eventId: 'job-7:7:10', targetStatus: 'awaiting_delivery', title: 'Legacy upload', destinationLabel: 'twitter · Legacy account' })
  })
})
