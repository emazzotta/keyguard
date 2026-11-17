import { age } from './typage.js'
import * as core from './unlock-core.js'
import {
  $, answerAndClose, authenticatorProblem, describe, isCancelled, onSubmit, openTransport, passkeyRefused,
  reportLost, setStatus, withinAuthenticatorTime,
} from './page.js'
import { textFile } from './store-client.js'

const DEVICE_NAME = /^[a-z0-9]+$/
const MIN_PASSPHRASE_LENGTH = 12
const KEEP_PAPER = 'The passphrase opens the store when every passkey is gone. Keep it somewhere safe, away from ' +
  'this device.'
const NEW_KEY_WARNING = 'The store has a key already. This makes a new one, which the tool that manages the store ' +
  'must then add as a recipient. To approve from one more device, use Add this device instead.'
const MINTED_PANELS = ['mint', 'switch']

const transport = openTransport()
const { store } = transport
const lost = reportLost(transport)
let enrolments = []
let autoSource = null

const nameProblem = (name) =>
  DEVICE_NAME.test(name) && name !== core.PAPER ? null : 'Pick a name of lowercase letters and digits, other than "paper".'

const passphraseProblem = () => {
  if ($('pass').value.length < MIN_PASSPHRASE_LENGTH) return `The passphrase needs at least ${MIN_PASSPHRASE_LENGTH} characters.`
  return $('pass').value === $('pass2').value ? null : 'The passphrases do not match.'
}

const hasIndex = (meta) => Object.hasOwn(meta.files ?? {}, core.INDEX_FILE)

const underPassphrase = async (remote, passphrase) => {
  const sealer = new age.Encrypter()
  sealer.setPassphrase(passphrase)
  return sealer.encrypt(remote)
}

const mint = async (device) => {
  setStatus('Creating the passkey\u2026', 'busy')
  const passkey = await age.webauthn.createCredential({ keyName: core.passkeyName(device) })
  const remote = await age.generateIdentity()
  const recipient = await age.identityToRecipient(remote)
  setStatus('Wrapping the store key under the passkey and the paper passphrase\u2026', 'busy')
  const files = {
    [`identity-remote.${device}.age`]: core.b64encode(await core.wrapRemote(remote, passkey)),
    [core.passkeyPath(device)]: textFile(passkey),
    [`identity-remote.${core.PAPER}.age`]: core.b64encode(await underPassphrase(remote, $('pass').value)),
  }
  const creating = !hasIndex(await store.fetchMeta())
  if (creating) files[core.INDEX_FILE] = await core.sealIndex(core.newIndex(recipient))
  setStatus('Saving to the store\u2026', 'busy')
  await store.commit(files)
  transport.rememberEnrolment(device, passkey)
  return { recipient, creating }
}

const showMinted = ({ recipient, creating }) => {
  for (const id of MINTED_PANELS) $(id).hidden = true
  $('result-lead').textContent = creating
    ? 'Requesters seal the values they propose to this public key of the store:'
    : 'Add this public key as a recipient of the store with the tool that manages it:'
  $('pubkey').textContent = recipient
  $('result').hidden = false
  setStatus(creating ? 'Store created.' : 'Enrolled.', 'ok')
}

const settleMintFailure = (error) => {
  if (isCancelled(error) || !transport.ceremony) {
    $('enrol').disabled = false
    return setStatus(isCancelled(error) ? passkeyRefused(error) : `Failed: ${describe(error)}.`, 'err')
  }
  return answerAndClose(transport, { error: describe(error) }, `Failed: ${describe(error)}.`, 'err', ['mint'])
}

const enrol = async () => {
  const device = transport.device ?? $('device').value.trim()
  const problem = nameProblem(device) ?? passphraseProblem()
  if (problem) return setStatus(problem, 'err')
  $('enrol').disabled = true
  let minted
  try {
    minted = await withinAuthenticatorTime(mint(device))
  } catch (error) {
    return settleMintFailure(error)
  }
  if (!transport.ceremony) return showMinted(minted)
  return answerAndClose(transport, { created: true }, 'Store created. Keep the paper passphrase somewhere safe.', 'ok', ['mint'])
}

const showSourceChoice = () => {
  const stored = transport.storedEnrolment()
  $('source').replaceChildren(...enrolments.map((name) => new Option(name, name, false, name === stored.name)))
  $('source-row').hidden = enrolments.length < 2
}

const explainUnlockFailure = (error, source, pinned) => {
  if (pinned) {
    autoSource = null
    showSourceChoice()
    return setStatus(`Could not unlock with the passkey for "${source}" (${describe(error)}). Try again, or pick ` +
      'another enrolment to unlock with.', 'err')
  }
  if (/no identity matched/.test(error?.message ?? '')) {
    return setStatus(`That passkey does not open "${source}". Try again and pick the passkey named for "${source}".`, 'err')
  }
  return setStatus(`Could not unlock the store key: ${describe(error)}`, 'err')
}

const unlockWithSource = async () => {
  const source = autoSource ?? { name: $('source').value, passkey: null, handle: null }
  const passkey = source.passkey ?? (source.handle ? await store.fetchText(source.handle) : null)
  setStatus(passkey
    ? `Waiting for the passkey for "${source.name}"\u2026`
    : `Waiting for a passkey. If asked, pick the one for "${source.name}".`, 'busy')
  try {
    return await core.unwrapRemote(await store.fetchFile(`identity-remote.${source.name}.age`), passkey)
  } catch (error) {
    explainUnlockFailure(error, source.name, Boolean(passkey))
    return null
  }
}

const addPasskey = async (name, replacing) => {
  const remote = await unlockWithSource()
  if (!remote) return
  const keyName = core.passkeyName(name)
  setStatus('Creating a passkey on this device\u2026', 'busy')
  const passkey = await age.webauthn.createCredential({ keyName })
  setStatus('Wrapping the store key under the new passkey\u2026', 'busy')
  const wrapped = await core.wrapRemote(remote, passkey)
  setStatus('Saving to the store\u2026', 'busy')
  await store.commit({ [`identity-remote.${name}.age`]: core.b64encode(wrapped), [core.passkeyPath(name)]: textFile(passkey) })
  transport.rememberEnrolment(name, passkey)
  $('add').hidden = true
  setStatus(`Added "${name}". Approvals in this browser now use the passkey "${keyName}".` +
    (replacing ? ' You can delete the passkey it replaces from your passkey manager.' : ''), 'ok')
}

const addDevice = async () => {
  const name = $('name').value.trim()
  const problem = nameProblem(name)
  if (problem) return setStatus(problem, 'err')
  const replacing = enrolments.includes(name)
  if (replacing && !$('replace').checked) {
    $('replace-row').hidden = false
    return setStatus(`"${name}" is already enrolled. Tick Replace to wrap it under a passkey made on this device, ` +
      'or pick another name.', 'err')
  }
  $('add-device').disabled = true
  try {
    await addPasskey(name, replacing)
  } catch (error) {
    setStatus(isCancelled(error) ? passkeyRefused(error) : `Failed: ${describe(error)}.`, 'err')
  } finally {
    $('add-device').disabled = false
  }
}

const showAdd = (meta, guess) => {
  $('title').textContent = 'Add this device'
  $('lead').textContent = 'Gives this device a passkey of its own for the store key, so approving here never ' +
    'depends on a passkey synced from another device.'
  $('name').value = guess
  autoSource = core.unlockSource(meta, transport.storedEnrolment())
  if (!autoSource) showSourceChoice()
  $('add').hidden = false
}

const showMint = (meta, guess) => {
  $('title').textContent = 'Enrol a passkey'
  $('lead').textContent = "Creates the store key, wraps it under a passkey made on this device and under a paper " +
    'passphrase, and stores both.'
  $('device').value = guess
  $('mint-note').textContent = hasIndex(meta) ? NEW_KEY_WARNING : KEEP_PAPER
  $('switch').hidden = !enrolments.length
  $('mint').hidden = false
}

const showCeremony = () => {
  $('title').textContent = 'Create the store'
  $('lead').textContent = "Creates the store's key, wraps it under a passkey made on this device and under a paper " +
    'passphrase, and stores both.'
  $('enrol').textContent = 'Create the store'
  $('device-row').hidden = true
  $('mint-note').textContent = KEEP_PAPER
  $('mint').hidden = false
  $('refuse').hidden = false
}

const startCeremony = async () => {
  const loaded = await transport.loadRequest()
  if (loaded.kind === 'over') {
    $('title').textContent = loaded.title
    setStatus(loaded.text, 'err')
    return false
  }
  showCeremony()
  return true
}

const startService = async () => {
  const meta = await store.fetchMeta()
  enrolments = core.enrolmentNames(meta)
  transport.publishPasskey(meta).catch((error) => console.warn('passkey handle not published:', describe(error)))
  const guess = core.guessDeviceName(navigator.userAgent, navigator.maxTouchPoints)
  if (enrolments.length && !new URLSearchParams(location.search).has('new')) showAdd(meta, guess)
  else showMint(meta, guess)
  return true
}

const start = async () => {
  if (!await (transport.ceremony ? startCeremony() : startService())) return
  const problem = await authenticatorProblem()
  if (problem) return setStatus(problem, 'err')
  $('enrol').disabled = false
  $('add-device').disabled = false
}

onSubmit('mint', () => enrol().catch(lost))
onSubmit('add', addDevice)
$('refuse').addEventListener('click', () => answerAndClose(transport, { refused: true }, 'Cancelled.', 'declined', ['mint']).catch(lost))

start().catch((error) => setStatus(`Could not read the store: ${describe(error)}`, 'err'))
