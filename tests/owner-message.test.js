import { describe, expect, test } from 'bun:test'
import * as owner from '../keyguard_passkey/pages/owner-message.js'
import { b64decode } from '../keyguard_passkey/pages/unlock-core.js'
import V from './unlock-core-vectors.json' with { type: 'json' }

const vector = V.owner_token
const CLAUDE = '212df38e-7c4b-4a51-9d0e-3f6b2a8c1e57'
const OPENCODE = `ses_${'a1B2'.repeat(7)}`
const message = (over = {}) => ({ text: 'yes, push', conversation: CLAUDE, box: 'macbook', title: 'Mazzotta-bot', ...over })
const fromB64url = (text) => b64decode(text.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - (text.length % 4)) % 4))
const verifiedByOwner = async (token) => {
  const [payload, signature] = token.split('.')
  const key = await crypto.subtle.importKey('raw', b64decode(vector.public_key_b64), { name: 'Ed25519' }, false, ['verify'])
  return crypto.subtle.verify({ name: 'Ed25519' }, key, fromB64url(signature), fromB64url(payload))
}

describe('signing an owner message', () => {
  /** @useCase docs/service.md scenario="The Token" */
  test('should sign the payload bytes into the token the client test vector holds', async () => {
    const token = await owner.signOwnerMessage(vector.private_key_pem, vector.message, vector.iat,
      fromB64url(vector.nonce_b64url))

    expect(token).toBe(vector.token)
  })

  /** @useCase docs/service.md scenario="The Token" */
  test('should write the payload in a fixed key order, expiring 600 seconds after it was approved', () => {
    expect(owner.tokenPayload(vector.message, vector.iat, fromB64url(vector.nonce_b64url))).toBe(vector.payload)
  })

  /** @useCase docs/service.md scenario="The Token" */
  test('should stamp a new nonce on each token, each verified by the owner key', async () => {
    const first = await owner.signOwnerMessage(vector.private_key_pem, vector.message, vector.iat)
    const second = await owner.signOwnerMessage(vector.private_key_pem, vector.message, vector.iat)

    expect(first === second).toBe(false)
    expect([await verifiedByOwner(first), await verifiedByOwner(second)]).toEqual([true, true])
  })

  /** @useCase docs/service.md scenario="The Token" */
  test('should keep the target title out of the token', async () => {
    const token = await owner.signOwnerMessage(vector.private_key_pem, { ...vector.message, title: 'Mazzotta-bot' },
      vector.iat, fromB64url(vector.nonce_b64url))

    expect(token).toBe(vector.token)
  })
})

describe('checking a message before it is shown', () => {
  /** @useCase docs/service.md scenario="The Request" */
  test.each([
    ['a message with line breaks', message({ text: 'yes, push\npersonal-infra' })],
    ['a message of 1000 characters', message({ text: 'x'.repeat(1000) })],
    ['a message of 1000 characters outside the basic plane', message({ text: '\u{1F600}'.repeat(1000) })],
    ['an opencode session', message({ conversation: OPENCODE })],
    ['an agy session', message({ conversation: `agy_${CLAUDE}` })],
  ])('should show %s', (_case, shown) => {
    expect(owner.messageProblem(shown)).toBeNull()
  })

  /** @useCase docs/service.md scenario="The Request" */
  test.each([
    ['an empty message', message({ text: '' })],
    ['a message over 1000 characters', message({ text: 'x'.repeat(1001) })],
    ['a tab', message({ text: 'tab\there' })],
    ['a carriage return', message({ text: 'a\rb' })],
    ['a bidi override', message({ text: 'bidi \u202e override' })],
    ['a zero-width space', message({ text: 'zero\u200bwidth' })],
    ['a variation selector', message({ text: 'yes\ufe0f' })],
    ['a supplementary variation selector', message({ text: 'yes\u{e0100}' })],
    ['a combining grapheme joiner', message({ text: 'yes\u034f' })],
    ['a Hangul filler', message({ text: 'yes \u3164 push' })],
    ['an unassigned default-ignorable code point', message({ text: 'yes\u2065' })],
    ['a line separator', message({ text: 'line\u2028separator' })],
    ['a private-use character', message({ text: 'private \ue000 use' })],
    ['a lone surrogate', message({ text: 'half \ud800 pair' })],
    ['a short id', message({ conversation: '212df38e' })],
    ['an upper-case UUID', message({ conversation: CLAUDE.toUpperCase() })],
    ['a box with a space', message({ box: 'mac book' })],
    ['no box', message({ box: undefined })],
  ])('should refuse %s, which it could not show whole', (_case, shown) => {
    expect(owner.messageProblem(shown)).not.toBeNull()
  })

  /** @useCase docs/service.md scenario="The Page" */
  test.each([
    [message(), 'Mazzotta-bot (212df38e) on macbook'],
    [message({ title: '' }), '212df38e on macbook'],
    [message({ conversation: OPENCODE, box: 'devbox' }), 'Mazzotta-bot (a1B2a1B2) on devbox'],
    [message({ conversation: `agy_${CLAUDE}` }), 'Mazzotta-bot (212df38e) on macbook'],
  ])('should name the target by title, short id and box', (shown, expected) => {
    expect(owner.addressee(shown)).toBe(expected)
  })
})
