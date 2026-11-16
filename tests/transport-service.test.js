import { describe, expect, test } from 'bun:test'
import { freshnessProblem, serviceAnswer } from '../keyguard_passkey/pages/transport-service.js'
import { REQUEST_MAX_AGE_SECONDS, REQUEST_MAX_SKEW_SECONDS } from '../keyguard_passkey/pages/unlock-core.js'

describe('serviceAnswer', () => {
  /** @useCase docs/service.md */
  test.each([
    ['released values as the name-to-value map', { values: { A: '1' } }, { A: '1' }],
    ['absent names', { absent: ['A'] }, { absent: ['A'] }],
    ['a listing', { names: ['A', 'B'] }, { list: ['A', 'B'] }],
    ['a write by the name it wrote', { written: ['A'], skipped: [], verified: 'A' }, { set: 'A' }],
    ['a removal', { removed: 'A' }, { rm: 'A' }],
  ])('should answer %s in the shape the client reads', (_case, outcome, expected) => {
    expect(serviceAnswer(outcome)).toEqual(expected)
  })

  /** @useCase docs/service.md */
  test('should refuse an outcome the service has no answer for', () => {
    expect(() => serviceAnswer({ refused: true })).toThrow('the service takes no answer of this kind (refused)')
  })
})

describe('freshnessProblem', () => {
  const NOW = 1_800_000_000

  /** @useCase docs/service.md */
  test('should offer a request signed moments ago', () => {
    expect(freshnessProblem({ ts: NOW - 10 }, 'dev box', NOW)).toBeNull()
  })

  /** @useCase docs/service.md */
  test('should refuse an old request as a possible replay and send the person back to the requester', () => {
    const problem = freshnessProblem({ ts: NOW - REQUEST_MAX_AGE_SECONDS - 1 }, 'dev box', NOW)

    expect([problem.includes('may be a replay'), problem.endsWith('ask again from the dev box.')]).toEqual([true, true])
  })

  /** @useCase docs/service.md */
  test("should blame the requester's clock for a request dated in the future", () => {
    expect(freshnessProblem({ ts: NOW + REQUEST_MAX_SKEW_SECONDS + 1 }, 'dev box', NOW))
      .toBe('This request is dated in the future. Check the clock of the dev box, then ask again.')
  })
})
