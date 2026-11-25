import { decodeSigningKey } from './unlock-core.js'

export const ALL_MACHINES = '*'
const LINE_FIELDS = 2
const HINT_CHARS = 8

const decodedOrNull = (text) => {
  try {
    return decodeSigningKey(text)
  } catch {
    return null
  }
}

const sameBytes = (left, right) => left.length === right.length && left.every((byte, index) => byte === right[index])

const comparedWithPin = (key, pinned) => {
  if (pinned === null) return { state: 'new' }
  const held = decodedOrNull(pinned)
  return held && sameBytes(held, decodeSigningKey(key)) ? { state: 'pinned' } : { state: 'differs', pinned }
}

const labelOf = (machines, id) => (Object.hasOwn(machines, id) ? machines[id].label : null)

const keyRow = (line, id, key, machines) => {
  const label = labelOf(machines, id)
  if (label === null) return { line, id, label, state: 'unknown' }
  if (!decodedOrNull(key)) return { line, id, label, state: 'bad-key' }
  return { line, id, label, key, ...comparedWithPin(key, machines[id].pinned) }
}

const pastedLines = (text) => text.split(/\r?\n/)
  .map((raw, index) => ({ line: index + 1, text: raw.trim() }))
  .filter(({ text: trimmed }) => trimmed !== '')
  .map((entry) => ({ ...entry, fields: entry.text.split(/\s+/) }))

export const readPinPaste = (text, machines) => {
  const lines = pastedLines(text)
  const ids = lines.filter(({ fields }) => fields.length === LINE_FIELDS).map(({ fields }) => fields[0])
  const repeated = new Set(ids.filter((id, index) => ids.indexOf(id) !== index))
  return lines.map(({ line, text: lineText, fields }) => {
    if (fields.length !== LINE_FIELDS) return { line, state: 'unreadable', text: lineText }
    const [id, key] = fields
    if (repeated.has(id)) return { line, id, label: labelOf(machines, id), state: 'twice' }
    return keyRow(line, id, key, machines)
  })
}

export const toPin = (rows, replacing) =>
  rows.filter(({ id, state }) => state === 'new' || (state === 'differs' && replacing.has(id)))

export const keyHint = (key) => `${key.slice(0, HINT_CHARS)}\u2026${key.slice(-HINT_CHARS)}`
