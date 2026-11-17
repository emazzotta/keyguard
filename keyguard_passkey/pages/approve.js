import * as core from './unlock-core.js'
import {
  $, answerAndClose, authenticatorProblem, capitalized, chip, describe, isCancelled, onSubmit,
  openTransport, passkeyRefused, reportLost, setStatus, withinAuthenticatorTime,
} from './page.js'
import { retryingStale } from './store-client.js'

const LISTING_SCOPE = 'every secret name'
const COPY = {
  get: {
    title: 'Approve a secret request',
    lead: (origin) => `${origin} asked for secrets. Check the names, then release them with your passkey.`,
    label: 'Secrets',
    button: 'Approve with passkey',
  },
  list: {
    title: 'Approve a name listing',
    lead: (origin) => `${origin} asks which secrets the store holds. Approving sends it the names only, never a value.`,
    label: 'Asks for',
    button: 'Send names with passkey',
  },
  set: {
    title: 'Approve a secret change',
    lead: (origin) => `${origin} proposes a value for these secrets. Approving writes them to the store with your ` +
      'passkey; the values never appear here.',
    label: 'Secrets',
    button: 'Write with passkey',
  },
  rm: {
    title: 'Approve a secret removal',
    lead: (origin) => `${origin} asks to remove this secret from the store. Approving deletes it with your passkey.`,
    label: 'Secret',
    button: 'Remove with passkey',
  },
}
const ANSWERED_PANELS = ['review', 'trust', 'editkey']

const transport = openTransport()
const { store } = transport
const lost = reportLost(transport)
let current = null
let pendingRecipients = null
let setupHost = null

const unlockRemote = async (meta) => {
  const chosen = core.approvalEnrolment(meta, transport.storedEnrolment())
  if (!chosen) {
    throw new Error(core.enrolmentNames(meta).length
      ? 'this browser has no enrolment of its own yet. Add this device at /enroll first'
      : 'nothing is enrolled yet. Enrol a passkey at /enroll first')
  }
  const passkey = chosen.passkey ?? (chosen.handle ? await store.fetchText(chosen.handle) : null)
  setStatus(passkey ? 'Waiting for the passkey\u2026' : `Waiting for the passkey. If asked, pick the one for "${chosen.name}".`,
    'busy')
  return core.unwrapRemote(await store.fetchFile(`identity-remote.${chosen.name}.age`), passkey)
}

const openIndex = async (remote) => {
  const index = core.checkFormat(await core.readIndex(await store.fetchFile(core.INDEX_FILE), remote))
  transport.checkRecipients(index)
  return index
}

const commitChange = (remote, change) => retryingStale(async () => {
  const { version } = await store.fetchMeta()
  const { files, deletes = [], ...result } = await change(await openIndex(remote))
  if (Object.keys(files).length || deletes.length) await store.commitAt(files, version, deletes)
  return result
})

const release = async (request, remote) =>
  ({ values: await core.resolveValues(request.names, remote, store.fetchFile, await openIndex(remote)) })

const listNames = async (_request, remote) => ({ names: core.listNames(await openIndex(remote)) })

const storeValues = async (request, remote) => {
  const values = await transport.proposedValues(request, remote)
  setStatus('Writing to the store\u2026', 'busy')
  const { written, skipped } = await commitChange(remote, (index) => core.planWrites(index, request.names, values, request.force))
  if (!written.length) return { written, skipped, verified: null }
  const [probe] = written
  const readBack = await core.resolveValues([probe], remote, store.fetchFile)
  if (readBack[probe] !== values[probe]) throw new Error(`${probe} did not read back as it was written`)
  return { written, skipped, verified: probe }
}

const removeValue = async (request, remote) => {
  const [name] = request.names
  await commitChange(remote, (index) => core.removeVariable(index, name))
  return { removed: name }
}

const FLOWS = { get: release, list: listNames, set: storeValues, rm: removeValue }

const writeSummary = ({ written, skipped }) => [
  written.length ? `Wrote ${written.join(', ')}.` : '',
  skipped.length ? `Kept ${skipped.join(', ')}, already stored.` : '',
].filter(Boolean).join(' ')

const DONE = {
  get: () => `Released. ${capitalized(transport.asker())} has the secrets.`,
  list: () => 'Sent the secret names, without values.',
  set: writeSummary,
  rm: ({ removed }) => `Removed ${removed}.`,
}

const finish = (outcome, text, kind) => {
  $('approve').disabled = true
  return answerAndClose(transport, outcome, text, kind, ANSWERED_PANELS)
}

const retry = (text) => {
  $('approve').disabled = false
  setStatus(text, 'err')
}

const settleFailure = (error) => {
  if (error instanceof core.AbsentNames) return finish({ absent: error.names }, `${error.message}.`, 'err')
  if (isCancelled(error)) return retry(passkeyRefused(error))
  if (error instanceof core.RecipientsChanged) {
    pendingRecipients = error.recipients
    $('trust').hidden = false
    return retry(`${capitalized(error.message)}.`)
  }
  if (!transport.ceremony) return retry(`Failed: ${describe(error)}.`)
  if (error instanceof core.NewerFormat) {
    return finish({ newer: { format: error.format, reader: error.reader } }, `${error.message}.`, 'err')
  }
  return finish({ error: describe(error) }, `Failed: ${describe(error)}.`, 'err')
}

const unlockAndRun = async (request) => {
  const meta = await store.fetchMeta()
  return { meta, outcome: await FLOWS[request.op](request, await unlockRemote(meta)) }
}

const approve = async (request) => {
  $('approve').disabled = true
  let ran
  try {
    ran = await withinAuthenticatorTime(unlockAndRun(request))
  } catch (error) {
    return settleFailure(error)
  }
  await finish(ran.outcome, DONE[request.op](ran.outcome), 'ok')
  await transport.publishPasskey(ran.meta).catch((error) => console.warn('passkey handle not published:', describe(error)))
}

const showCopy = (request) => {
  const copy = COPY[request.op]
  document.title = `${copy.title} \u00b7 Keyguard`
  $('title').textContent = copy.title
  $('lead').textContent = copy.lead(transport.origin())
  $('approve').textContent = copy.button
  $('names-label').textContent = copy.label
}

const showFacts = (request, note) => {
  $('names').replaceChildren(...(request.op === 'list' ? [LISTING_SCOPE] : request.names).map(chip))
  $('host').textContent = request.host ?? ''
  $('host-row').hidden = !request.host
  $('session-title').textContent = request.title
  $('session-id').textContent = request.session
  $('session-id').hidden = !request.session
  $('session-row').hidden = !request.title && !request.session
  $('age').textContent = note ?? ''
  $('age').hidden = !note
}

const showReview = async ({ request, note, status }) => {
  current = request
  showCopy(request)
  showFacts(request, note)
  $('review').hidden = false
  $('refuse').hidden = !transport.ceremony
  setStatus(status ?? '', status ? 'ok' : '')
  const problem = await authenticatorProblem()
  if (problem) return setStatus(problem, 'err')
  $('approve').disabled = false
  $('approve').focus()
}

const showPinTarget = (host) => {
  const label = transport.label(host)
  const pinned = transport.pinned(host)
  setupHost = host
  $('pubkey-label').textContent = `${capitalized(label)} signing key (base64)`
  $('pubkey').value = pinned ?? ''
  setStatus(pinned
    ? `This key is pinned. Replace it only if the key of the ${label} changed.`
    : `Pin the signing key of the ${label} once before approving.`)
}

const showSetup = ({ hosts, host }) => {
  if (!host) return setStatus('The service names no requesters, so there is no key to pin.', 'err')
  $('title').textContent = 'Pin a signing key'
  $('lead').textContent = 'Each request is checked against the key pinned here for the machine that sent it.'
  $('requester').replaceChildren(...hosts.map((id) => new Option(transport.label(id), id, false, id === host)))
  $('requester-row').hidden = hosts.length < 2
  showPinTarget(host)
  $('setup').hidden = false
}

const showOver = ({ title, text, tone = 'err' }) => {
  if (title) $('title').textContent = title
  setStatus(text, tone)
}

const start = async () => {
  const loaded = await transport.loadRequest()
  if (loaded.pinLink) {
    $('editkey').href = loaded.pinLink
    $('editkey').hidden = false
  }
  if (loaded.kind === 'setup') return showSetup(loaded)
  if (loaded.kind === 'over') return showOver(loaded)
  return showReview(loaded)
}

$('approve').addEventListener('click', () => current && approve(current).catch(lost))

$('refuse').addEventListener('click', () => finish({ refused: true }, 'Refused.', 'declined').catch(lost))

$('trust').addEventListener('click', () => {
  if (!pendingRecipients) return
  core.rememberRecipients(pendingRecipients)
  $('trust').hidden = true
  setStatus(`Trusted recipient set version ${pendingRecipients.version}. Approve again.`, 'ok')
  pendingRecipients = null
})

$('requester').addEventListener('change', () => showPinTarget($('requester').value))

onSubmit('setup', () => {
  try {
    core.decodeSigningKey($('pubkey').value)
  } catch (error) {
    return setStatus(`That is not a 32-byte base64 key: ${error.message}`, 'err')
  }
  let next
  try {
    next = transport.pin(setupHost, $('pubkey').value.trim())
  } catch {
    return setStatus('This browser blocks storage, so the key cannot be pinned.', 'err')
  }
  if (next) return location.replace(next)
  setStatus(`Pinned. Requests from the ${transport.label(setupHost)} are now checked against this key.`, 'ok')
})

start().catch((error) => setStatus(`Could not load the request: ${describe(error)}`, 'err'))
