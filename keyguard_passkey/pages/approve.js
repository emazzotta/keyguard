import { addressee, signOwnerMessage } from './owner-message.js'
import * as core from './unlock-core.js'
import {
  $, answerAndClose, authenticatorProblem, capitalized, chip, describe, isCancelled, onSubmit,
  openTransport, passkeyRefused, reportLost, setStatus, STORAGE_BLOCKED, withinAuthenticatorTime,
} from './page.js'
import { ALL_MACHINES } from './pin-paste.js'
import { pastePinForm } from './pin-paste-form.js'
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
  sign: {
    title: 'Sign a message as you',
    lead: (origin) => `${origin} asks you to sign this message for one session. That session takes it as your own ` +
      'words, so read all of it.',
    label: 'Message',
    button: 'Sign with passkey',
  },
}
const ANSWERED_PANELS = ['review', 'trust', 'editkey']

const transport = openTransport()
const { store } = transport
const lost = reportLost(transport)
const pastePins = pastePinForm(transport)
let current = null
let pendingRecipients = null
let setupHost = null
let setupHosts = []

class EnrolmentUnchosen extends Error {}

const pickedEnrolment = (meta) => ($('enrolment-row').hidden ? null : core.namedEnrolment(meta, $('enrolment').value))

const offerEnrolments = (names) => {
  $('enrolment').replaceChildren(...names.map((name) => new Option(name, name)))
  $('enrolment-row').hidden = false
  return new EnrolmentUnchosen('This browser has not approved before. Pick the enrolment whose passkey this device ' +
    'holds, then approve again. Approvals here use it from then on.')
}

const unlockRemote = async (meta) => {
  const remembered = core.approvalEnrolment(meta, transport.storedEnrolment())
  const chosen = remembered ?? pickedEnrolment(meta)
  if (!chosen) {
    const names = core.enrolmentNames(meta)
    if (names.length) throw offerEnrolments(names)
    throw new Error('nothing is enrolled yet. Enrol a passkey at /enroll first')
  }
  const passkey = chosen.passkey ?? (chosen.handle ? await store.fetchText(chosen.handle) : null)
  setStatus(passkey ? 'Waiting for the passkey\u2026' : `Waiting for the passkey. If asked, pick the one for "${chosen.name}".`,
    'busy')
  const remote = await core.unwrapRemote(await store.fetchFile(`identity-remote.${chosen.name}.age`), passkey)
  if (!remembered) transport.rememberEnrolment(chosen.name, null)
  return remote
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

const signMessage = async (request, remote) => {
  if (!request.message) throw new Error('this request names no message to sign')
  const index = await openIndex(remote)
  const pem = await core.readOwnerKey(index, remote, store.fetchFile)
  return { token: await signOwnerMessage(pem, request.message, Math.floor(Date.now() / 1000)) }
}

const FLOWS = { get: release, list: listNames, set: storeValues, rm: removeValue, sign: signMessage }

const writeSummary = ({ written, skipped }) => [
  written.length ? `Wrote ${written.join(', ')}.` : '',
  skipped.length ? `Kept ${skipped.join(', ')}, already stored.` : '',
].filter(Boolean).join(' ')

const DONE = {
  get: () => `Released. ${capitalized(transport.asker())} has the secrets.`,
  list: () => 'Sent the secret names, without values.',
  set: writeSummary,
  rm: ({ removed }) => `Removed ${removed}.`,
  sign: () => `Signed. ${capitalized(transport.asker())} has the signed message.`,
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
  if (error instanceof EnrolmentUnchosen) return retry(error.message)
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

const showMessage = (message) => {
  $('message').textContent = message?.text ?? ''
  $('target').textContent = message ? addressee(message) : ''
  $('message-row').hidden = !message
  $('target-row').hidden = !message
  $('names-row').hidden = Boolean(message)
}

const showFacts = (request, note) => {
  showMessage(request.op === 'sign' ? request.message : null)
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
  setupHost = host
  const all = host === ALL_MACHINES
  $('title').textContent = all ? 'Pin signing keys' : 'Pin a signing key'
  for (const id of ['pubkey-label', 'pubkey']) $(id).hidden = all
  for (const id of ['pins-label', 'pins']) $(id).hidden = !all
  if (all) return pastePins.show(setupHosts)
  const label = transport.label(host)
  const pinned = transport.pinned(host)
  $('pin-rows').hidden = true
  $('held').hidden = true
  $('pin').textContent = 'Pin this key'
  $('pin').disabled = false
  $('pubkey-label').textContent = `${capitalized(label)} signing key (base64)`
  $('pubkey').value = pinned ?? ''
  setStatus(pinned
    ? `This key is pinned. Replace it only if the key of the ${label} changed.`
    : `Pin the signing key of the ${label} once before approving.`)
}

const showSetup = ({ hosts, host }) => {
  if (!host) return setStatus('The service names no requesters, so there is no key to pin.', 'err')
  $('lead').textContent = 'Each request is checked against the key pinned here for the machine that sent it.'
  setupHosts = hosts
  const all = hosts.length > 1 ? [new Option('All machines', ALL_MACHINES, false, host === ALL_MACHINES)] : []
  $('requester').replaceChildren(...hosts.map((id) => new Option(transport.label(id), id, false, id === host)), ...all)
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

const pinOne = () => {
  try {
    core.decodeSigningKey($('pubkey').value)
  } catch (error) {
    return setStatus(`That is not a 32-byte base64 key: ${error.message}`, 'err')
  }
  let next
  try {
    next = transport.pin(setupHost, $('pubkey').value.trim())
  } catch {
    return setStatus(STORAGE_BLOCKED, 'err')
  }
  if (next) return location.replace(next)
  setStatus(`Pinned. Requests from the ${transport.label(setupHost)} are now checked against this key.`, 'ok')
}

onSubmit('setup', () => (setupHost === ALL_MACHINES ? pastePins.pin() : pinOne()))

start().catch((error) => setStatus(`Could not load the request: ${describe(error)}`, 'err'))
