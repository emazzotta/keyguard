import { describe, expect, test } from 'bun:test'
import { passkeyRefused } from '../keyguard_passkey/pages/page.js'

const refusal = new DOMException('Origin is empty or not allowed.', 'NotAllowedError')
const browserCreate = Math.max
const extensionCreate = async function create(options) {
  return options
}

describe('passkeyRefused', () => {
  /** @useCase README.md */
  test('should name the reason the browser or extension gave', () => {
    expect(passkeyRefused(refusal, browserCreate))
      .toEndWith('Reason given: NotAllowedError: Origin is empty or not allowed.')
  })

  /** @useCase README.md */
  test('should point at an in-app browser when the browser itself answers passkey requests', () => {
    const text = passkeyRefused(refusal, browserCreate)
    expect(text).toContain('A browser inside a chat or mail app cannot use passkeys')
    expect(text).not.toContain('KeePassXC-Browser')
  })

  /** @useCase README.md */
  test('should point at the extension when one has replaced the passkey request', () => {
    const text = passkeyRefused(refusal, extensionCreate)
    expect(text).toContain('A browser extension takes the passkey requests on this page')
    expect(text).not.toContain('chat or mail app')
  })

  /** @useCase README.md */
  test('should keep the in-app browser hint when the browser has no passkey requests at all', () => {
    expect(passkeyRefused(refusal, undefined)).toContain('A browser inside a chat or mail app cannot use passkeys')
  })
})
