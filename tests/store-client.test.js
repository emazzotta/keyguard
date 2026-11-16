import { describe, expect, test } from 'bun:test'
import { retryingStale, StaleStore } from '../keyguard_passkey/pages/store-client.js'

describe('retryingStale', () => {
  /** @useCase docs/service.md */
  test('should try a write again once when the store moved under it', async () => {
    let attempts = 0

    const result = await retryingStale(async () => {
      attempts++
      if (attempts === 1) throw new StaleStore('moved')
      return 'written'
    })

    expect([result, attempts]).toEqual(['written', 2])
  })

  /** @useCase docs/service.md */
  test('should give up on the second stale store and pass any other error through at once', async () => {
    let attempts = 0

    const stale = retryingStale(async () => { attempts++; throw new StaleStore('moved') })

    await expect(stale).rejects.toBeInstanceOf(StaleStore)
    await expect(retryingStale(async () => { attempts++; throw new Error('refused') })).rejects.toThrow('refused')
    expect(attempts).toBe(3)
  })
})
