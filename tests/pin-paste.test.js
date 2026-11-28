import { describe, expect, test } from 'bun:test'
import { keyHint, readPinPaste, toPin } from '../keyguard_passkey/pages/pin-paste.js'
import { b64encode } from '../keyguard_passkey/pages/unlock-core.js'

const KEY_A = b64encode(new Uint8Array(32).fill(1))
const KEY_B = b64encode(new Uint8Array(32).fill(2))
const machines = (pins = {}) => ({
  mac: { label: 'Mac', pinned: pins.mac ?? null },
  devbox: { label: 'dev box', pinned: pins.devbox ?? null },
})
const states = (rows) => rows.map(({ line, state }) => [line, state])

describe('reading pasted pin lines', () => {
  /** @useCase docs/service.md scenario="First Setup" */
  test('should offer a key for a machine that has none pinned', () => {
    expect(readPinPaste(`mac ${KEY_A}`, machines())).toEqual([
      { line: 1, id: 'mac', label: 'Mac', key: KEY_A, state: 'new' },
    ])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should see the pinned key in a line that spells it without padding', () => {
    const pasted = `mac ${KEY_A.replace(/=+$/, '')}`

    expect(states(readPinPaste(pasted, machines({ mac: KEY_A })))).toEqual([[1, 'pinned']])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should name the key pinned now when a line brings another one', () => {
    expect(readPinPaste(`mac ${KEY_A}`, machines({ mac: KEY_B }))).toEqual([
      { line: 1, id: 'mac', label: 'Mac', key: KEY_A, pinned: KEY_B, state: 'differs' },
    ])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should treat an unreadable pinned value as another key', () => {
    expect(states(readPinPaste(`mac ${KEY_A}`, machines({ mac: 'garbage' })))).toEqual([[1, 'differs']])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test.each(['nobody', 'constructor', '__proto__', 'hasOwnProperty'])(
    'should flag the id %s that no machine has', (id) => {
      expect(readPinPaste(`${id} ${KEY_A}`, machines())).toEqual([{ line: 1, id, label: null, state: 'unknown' }])
    })

  /** @useCase docs/service.md scenario="First Setup" */
  test.each(['c2hvcnQ=', 'not*base64', `${KEY_A}AAAA`])('should flag the key %s that is not 32 bytes in base64', (key) => {
    expect(readPinPaste(`mac ${key}`, machines())).toEqual([{ line: 1, id: 'mac', label: 'Mac', state: 'bad-key' }])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test.each(['mac', `mac ${KEY_A} extra`, `| mac | ${KEY_A} |`])('should flag the line %s that is not an id and a key',
    (text) => {
      expect(readPinPaste(text, machines())).toEqual([{ line: 1, state: 'unreadable', text }])
    })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should pin neither line of an id that two lines name', () => {
    expect(states(readPinPaste(`mac ${KEY_A}\ndevbox ${KEY_B}\nmac ${KEY_B}`, machines()))).toEqual([
      [1, 'twice'], [2, 'new'], [3, 'twice'],
    ])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should skip blank lines and read past tabs, padding spaces and Windows line ends', () => {
    const pasted = `\r\n  mac\t${KEY_A}  \r\n\r\ndevbox  ${KEY_B}\r\n`

    expect(readPinPaste(pasted, machines()).map(({ line, id, key }) => [line, id, key])).toEqual([
      [2, 'mac', KEY_A], [4, 'devbox', KEY_B],
    ])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should read nothing from an empty box', () => {
    expect(readPinPaste('  \n', machines())).toEqual([])
  })
})

describe('choosing what one tap pins', () => {
  const rows = readPinPaste(`mac ${KEY_A}\ndevbox ${KEY_B}\nnobody ${KEY_A}`, machines({ mac: KEY_B }))

  /** @useCase docs/service.md scenario="First Setup" */
  test('should pin the new keys and leave a differing pin alone', () => {
    expect(toPin(rows, new Set()).map(({ id }) => id)).toEqual(['devbox'])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should replace a differing pin once its line is ticked', () => {
    expect(toPin(rows, new Set(['mac'])).map(({ id }) => id)).toEqual(['mac', 'devbox'])
  })

  /** @useCase docs/service.md scenario="First Setup" */
  test('should pin nothing for a ticked id whose line is flagged', () => {
    expect(toPin(rows, new Set(['nobody'])).map(({ id }) => id)).toEqual(['devbox'])
  })
})

describe('showing a key', () => {
  /** @useCase docs/service.md scenario="First Setup" */
  test('should show the first and last 8 characters', () => {
    expect(keyHint('p8n6K6/+27yVAza8fTxOAWUQZZ3G9NnJGfSgybU603Q=')).toBe('p8n6K6/+\u2026ybU603Q=')
  })
})
