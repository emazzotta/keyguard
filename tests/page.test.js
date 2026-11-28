import { describe, expect, mock, test } from 'bun:test'
import { passkeyRefused, retryAfterClick } from '../keyguard_passkey/pages/page.js'

const refusal = new DOMException('Origin is empty or not allowed.', 'NotAllowedError')
const notFocused = new DOMException('The document is not focused.', 'NotAllowedError')
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

describe('retryAfterClick', () => {
  const answers = (...outcomes) => {
    const queue = [...outcomes]
    return mock(async () => {
      const next = queue.shift()
      if (next instanceof Error) throw next
      return next
    })
  }

  /** @useCase README.md */
  test('should answer at once without a click when the browser shows the prompt', async () => {
    const request = answers('wrapped')
    const click = mock(async () => {})

    const result = await retryAfterClick(request, click)

    expect(result).toBe('wrapped')
    expect(request).toHaveBeenCalledTimes(1)
    expect(click).not.toHaveBeenCalled()
  })

  /** @useCase README.md */
  test('should ask for a click and show the prompt again when the browser refuses it', async () => {
    const request = answers(notFocused, 'wrapped')
    const click = mock(async () => {})

    const result = await retryAfterClick(request, click)

    expect(result).toBe('wrapped')
    expect(request).toHaveBeenCalledTimes(2)
    expect(click).toHaveBeenCalledWith(notFocused)
  })

  /** @useCase README.md */
  test('should give up when the browser refuses the prompt after the click too', async () => {
    const request = answers(notFocused, refusal)

    await expect(retryAfterClick(request, async () => {})).rejects.toBe(refusal)
    expect(request).toHaveBeenCalledTimes(2)
  })

  /** @useCase README.md */
  test('should fail without a click when the request fails for another reason', async () => {
    const broken = new TypeError('PRF extension not available')
    const click = mock(async () => {})

    await expect(retryAfterClick(answers(broken), click)).rejects.toBe(broken)
    expect(click).not.toHaveBeenCalled()
  })
})
