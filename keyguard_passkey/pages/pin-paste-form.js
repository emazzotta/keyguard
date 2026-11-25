import { $, setStatus, STORAGE_BLOCKED } from './page.js'
import { keyHint, readPinPaste, toPin } from './pin-paste.js'

const NOTES = {
  new: () => 'New: the tap pins it.',
  pinned: () => 'Already pinned.',
  differs: ({ pinned }) => `Another key is pinned: ${keyHint(pinned)}.`,
  unknown: () => 'No machine has this id. Not pinned.',
  'bad-key': () => 'The key is not 32 bytes in base64. Not pinned.',
  unreadable: () => 'Not an id and a key. Not pinned.',
  twice: () => 'Another line names this id too. Neither is pinned.',
}
const UNFLAGGED_STATES = new Set(['new', 'pinned', 'differs'])

const counted = (count, noun) => `${count} ${noun}${count === 1 ? '' : 's'}`

const textElement = (tag, text) => {
  const element = document.createElement(tag)
  element.textContent = text
  return element
}

export const pastePinForm = (transport) => {
  let hosts = []
  let rows = []
  const replacing = new Set()

  const showPinButton = () => {
    const count = toPin(rows, replacing).length
    $('pin').textContent = `Pin ${counted(count, 'key')}`
    $('pin').disabled = count === 0
  }

  const replaceBox = (id) => {
    const box = document.createElement('input')
    box.type = 'checkbox'
    box.addEventListener('change', () => {
      if (box.checked) replacing.add(id)
      else replacing.delete(id)
      showPinButton()
    })
    const label = textElement('label', 'Replace it')
    label.className = 'check'
    label.prepend(box)
    return label
  }

  const item = (row) => {
    const shown = document.createElement('li')
    shown.dataset.state = row.state
    if (row.id) shown.dataset.id = row.id
    shown.classList.toggle('flagged', !UNFLAGGED_STATES.has(row.state))
    shown.append(textElement('strong', row.label ? `${row.label} (${row.id})` : (row.id ?? `Line ${row.line}`)))
    const text = row.key ? keyHint(row.key) : row.text
    if (text) shown.append(textElement('code', text))
    shown.append(textElement('p', NOTES[row.state](row)))
    if (row.state === 'differs') shown.append(replaceBox(row.id))
    return shown
  }

  const showPasted = () => {
    const machines = Object.fromEntries(hosts.map((id) =>
      [id, { label: transport.label(id), pinned: transport.pinned(id) }]))
    rows = readPinPaste($('pins').value, machines)
    $('pin-rows').replaceChildren(...rows.map(item))
    $('pin-rows').hidden = rows.length === 0
    showPinButton()
  }

  const showBack = (next) => {
    $('back').hidden = !next
    if (next) $('back').href = next
  }

  const keylessAsker = () => {
    const host = transport.askingHost()
    return host && !transport.pinned(host) ? host : null
  }

  const pinnedText = (chosen, unpinned, keyless) => {
    const names = chosen.map(({ id }) => transport.label(id)).join(', ')
    const left = unpinned ? ` ${counted(unpinned, 'line')} not pinned, as marked.` : ''
    const waits = keyless ? ` The request waits for a key for the ${transport.label(keyless)}.` : ''
    return `Pinned ${counted(chosen.length, 'key')}: ${names}.${left}${waits}`
  }

  $('pins').addEventListener('input', () => {
    replacing.clear()
    showPasted()
  })

  return {
    show: (requesters) => {
      hosts = requesters
      // Empty by design: keys from the service, the link or the pins held would let the service choose whom to trust.
      $('pins').value = ''
      replacing.clear()
      showPasted()
      showBack(null)
      setStatus('Paste one line for each machine. Nothing is pinned before you tap.')
    },
    pin: () => {
      const chosen = toPin(rows, replacing)
      const unpinned = rows.filter((row) => row.state !== 'pinned' && !chosen.includes(row)).length
      let next
      try {
        for (const { id, key } of chosen) next = transport.pin(id, key)
      } catch {
        return setStatus(STORAGE_BLOCKED, 'err')
      }
      const keyless = next ? keylessAsker() : null
      if (next && unpinned === 0 && !keyless) return location.replace(next)
      replacing.clear()
      showPasted()
      setStatus(pinnedText(chosen, unpinned, keyless), 'ok')
      showBack(next)
    },
  }
}
