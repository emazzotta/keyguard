// Imported on first use: a request is shown and verified with WebCrypto alone, before this bundle loads.
const loadAge = async () => (await import('./typage.js')).age

export const b64encode = (bytes) => {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

export const b64decode = (text) => {
  const binary = atob(text)
  const out = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i)
  return out
}

export const unpad = (bytes) => {
  if (bytes.length < 4) throw new Error('corrupt padding')
  const length = ((bytes[0] << 24) | (bytes[1] << 16) | (bytes[2] << 8) | bytes[3]) >>> 0
  const end = 4 + length
  if (end > bytes.length) throw new Error('corrupt padding')
  return bytes.subarray(4, end)
}

// Sorted as the client's sort_keys serialisation signs them; a request signs only the fields it carries.
const SIGNED_FIELDS = ['eph_pubkey', 'host', 'op', 'session', 'title', 'ts', 'value', 'vars']

const isPresent = (value) => value !== undefined && value !== null

export const canonicalRequestBytes = (request) =>
  new TextEncoder().encode(JSON.stringify(Object.fromEntries(
    SIGNED_FIELDS.filter((field) => isPresent(request[field])).map((field) => [field, request[field]]))))

export const decodeSigningKey = (text) => {
  const raw = b64decode(text.trim())
  if (raw.length !== 32) throw new Error(`expected a 32-byte Ed25519 key, got ${raw.length} bytes`)
  return raw
}

export const REQUEST_MAX_AGE_SECONDS = 360
export const REQUEST_MAX_SKEW_SECONDS = 60

export const requestFreshness = (request, nowSeconds) => {
  const age = nowSeconds - request.ts
  if (age > REQUEST_MAX_AGE_SECONDS) return 'stale'
  if (age < -REQUEST_MAX_SKEW_SECONDS) return 'future'
  return 'fresh'
}

export const verifyRequest = async (request, rawPubkey) => {
  const key = await crypto.subtle.importKey('raw', rawPubkey, { name: 'Ed25519' }, false, ['verify'])
  return crypto.subtle.verify({ name: 'Ed25519' }, key, b64decode(request.sig), canonicalRequestBytes(request))
}

export const pinKey = (host) => `keyguard.${host}_pubkey`

// The host is signed too, so a key pinned for one requester never vouches for a request in another's name.
export const checkSignature = async (request, requesters, readPin) => {
  if (!Object.hasOwn(requesters, request.host)) return 'unknown-host'
  const pinned = readPin(pinKey(request.host))
  if (!pinned) return 'unpinned'
  return (await verifyRequest(request, decodeSigningKey(pinned))) ? 'verified' : 'forged'
}

const decryptStoreJson = async (ageBytes, remoteIdentity) => {
  const age = await loadAge()
  const decrypter = new age.Decrypter()
  decrypter.addIdentity(remoteIdentity)
  return JSON.parse(new TextDecoder().decode(unpad(await decrypter.decrypt(ageBytes))))
}

export const INDEX_FILE = 'index.age'
export const STORE_FORMAT = 1

export const readIndex = (indexBytes, remoteIdentity) => decryptStoreJson(indexBytes, remoteIdentity)

export class NewerFormat extends Error {
  constructor (format, reader) {
    super(`The store has format ${format}; this release reads up to format ${STORE_FORMAT}`)
    this.format = format
    this.reader = reader
  }
}

export const checkFormat = (index) => {
  const format = index.version ?? STORE_FORMAT
  if (format > STORE_FORMAT) throw new NewerFormat(format, index.reader)
  return index
}

export class AbsentNames extends Error {
  constructor (names) {
    super(`${names.join(', ')} ${names.length === 1 ? 'is' : 'are'} not in the store`)
    this.names = names
  }
}

const requirePresent = (index, names) => {
  const absent = names.filter((name) => !Object.hasOwn(index.entries, name))
  if (absent.length) throw new AbsentNames(absent)
}

export const listNames = (index) => Object.keys(index.entries).sort()

export const resolveValues = async (names, remoteIdentity, fetchStoreFile, readAlready = null) => {
  const index = readAlready ?? await readIndex(await fetchStoreFile(INDEX_FILE), remoteIdentity)
  requirePresent(index, names)
  const values = {}
  for (const name of names) {
    const entry = index.entries[name]
    const payload = await decryptStoreJson(await fetchStoreFile(entry.file), remoteIdentity)
    if (payload.name !== name) throw new Error(`payload name mismatch for ${name}`)
    values[name] = payload.value
  }
  return values
}

export const sealResponse = async (values, ephRecipient) => {
  const age = await loadAge()
  const encrypter = new age.Encrypter()
  encrypter.addRecipient(ephRecipient)
  return encrypter.encrypt(new TextEncoder().encode(JSON.stringify(values)))
}

const PAD_BLOCK = 256
const LENGTH_PREFIX_BYTES = 4
const SALT_BYTES = 32
export const NEW_VARIABLE_TIER = 'high'

export const pad = (bytes) => {
  const out = new Uint8Array(Math.ceil((bytes.length + LENGTH_PREFIX_BYTES) / PAD_BLOCK) * PAD_BLOCK)
  new DataView(out.buffer).setUint32(0, bytes.length)
  out.set(bytes, LENGTH_PREFIX_BYTES)
  return out
}

const sortKeys = (value) => {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortKeys(value[key])]))
  }
  return value
}

export const canonicalJson = (value) => JSON.stringify(sortKeys(value))

const hex = (bytes) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')

export const variablePath = async (saltBase64, name) => {
  const salt = b64decode(saltBase64)
  const nameBytes = new TextEncoder().encode(name)
  const input = new Uint8Array(salt.length + nameBytes.length)
  input.set(salt)
  input.set(nameBytes, salt.length)
  return `vars/${hex(new Uint8Array(await crypto.subtle.digest('SHA-256', input)))}.age`
}

const sealTo = async (recipients, bytes) => {
  if (!recipients.length) throw new Error('refusing to seal to no recipients')
  const age = await loadAge()
  const encrypter = new age.Encrypter()
  for (const recipient of recipients) encrypter.addRecipient(recipient)
  return encrypter.encrypt(bytes)
}

const tierRecipients = (index, tier) => [...(index.recipients.tiers[tier] ?? [])].sort()
const everyRecipient = (index) => [...new Set(Object.values(index.recipients.tiers).flat())].sort()
const sealedJson = async (recipients, value) =>
  b64encode(await sealTo(recipients, pad(new TextEncoder().encode(canonicalJson(value)))))

export const sealIndex = (index) => sealedJson(everyRecipient(index), index)

export const newIndex = (recipient, salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES))) => ({
  version: STORE_FORMAT,
  salt: b64encode(salt),
  recipients: { version: 1, tiers: { high: [recipient], low: [] } },
  entries: {},
})

export const writeVariable = async (index, name, value) => {
  const existing = index.entries[name]
  const entry = existing ?? { file: await variablePath(index.salt, name), tier: NEW_VARIABLE_TIER }
  const files = { [entry.file]: await sealedJson(tierRecipients(index, entry.tier), { name, value }) }
  if (existing) return { files, created: false, index }
  const updated = { ...index, entries: { ...index.entries, [name]: entry } }
  files[INDEX_FILE] = await sealIndex(updated)
  return { files, created: true, index: updated }
}

export const planWrites = async (index, names, values, force) => {
  let current = index
  const files = {}
  const written = []
  const skipped = []
  for (const name of names) {
    if (Object.hasOwn(current.entries, name) && !force) {
      skipped.push(name)
      continue
    }
    const change = await writeVariable(current, name, values[name])
    Object.assign(files, change.files)
    current = change.index
    written.push(name)
  }
  return { files, written, skipped, index: current }
}

export const removeVariable = async (index, name) => {
  requirePresent(index, [name])
  const entry = index.entries[name]
  const entries = Object.fromEntries(Object.entries(index.entries).filter(([key]) => key !== name))
  const updated = { ...index, entries }
  return { files: { [INDEX_FILE]: await sealIndex(updated) }, deletes: [entry.file], index: updated }
}

export const openSealed = async (sealedBytes, identity) => {
  const age = await loadAge()
  const decrypter = new age.Decrypter()
  decrypter.addIdentity(identity)
  return decrypter.decrypt(sealedBytes, 'text')
}

const RECIPIENTS_KEY = 'keyguard.recipients'

export const recipientCheck = (current, pinned) => {
  if (!pinned) return 'unpinned'
  return canonicalJson(current) === canonicalJson(pinned) ? 'match' : 'changed'
}

export class RecipientsChanged extends Error {
  constructor (pinned, current) {
    super(`the store's recipient set changed (pinned version ${pinned.version}, now ${current.version}). If you ` +
      'just added or removed a recipient, trust the new set, then approve again')
    this.recipients = current
  }
}

export const storedRecipients = (storage = globalThis.localStorage) => {
  try {
    return JSON.parse(storage.getItem(RECIPIENTS_KEY) ?? 'null')
  } catch {
    return null
  }
}

export const rememberRecipients = (recipients, storage = globalThis.localStorage) =>
  storage.setItem(RECIPIENTS_KEY, canonicalJson(recipients))

export const PAPER = 'paper'
const ENROLMENT_FILE = /^identity-remote\.([a-z0-9]+)\.age$/
const DEVICE_KEY = 'keyguard.device'
const PASSKEY_KEY = 'keyguard.passkey'

export const enrolmentNames = (meta) =>
  Object.keys(meta?.files ?? {})
    .map((path) => ENROLMENT_FILE.exec(path)?.[1])
    .filter((name) => name && name !== PAPER)
    .sort()

export const passkeyPath = (name) => `identity-remote.${name}.passkey`

const published = (meta, name) => passkeyPath(name) in (meta?.files ?? {})

export const unpublishedPasskey = (meta, stored) =>
  stored.name && stored.passkey && enrolmentNames(meta).includes(stored.name) && !published(meta, stored.name)
    ? passkeyPath(stored.name)
    : null

export const unlockSource = (meta, stored) => {
  const names = enrolmentNames(meta)
  if (stored.passkey && names.includes(stored.name)) return { name: stored.name, passkey: stored.passkey }
  const name = names.find((candidate) => published(meta, candidate))
  return name ? { name, passkey: null, handle: passkeyPath(name) } : null
}

export const approvalEnrolment = (meta, stored) => {
  const names = enrolmentNames(meta)
  const name = names.includes(stored.name) ? stored.name : names.length === 1 ? names[0] : null
  if (!name) return null
  if (stored.passkey && name === stored.name) return { name, passkey: stored.passkey }
  return { name, passkey: null, handle: published(meta, name) ? passkeyPath(name) : null }
}

export const storedEnrolment = (storage = globalThis.localStorage) => {
  try {
    return { name: storage.getItem(DEVICE_KEY), passkey: storage.getItem(PASSKEY_KEY) }
  } catch {
    return { name: null, passkey: null }
  }
}

export const rememberEnrolment = (name, passkey, storage = globalThis.localStorage) => {
  storage.setItem(DEVICE_KEY, name)
  storage.setItem(PASSKEY_KEY, passkey)
}

export const passkeyName = (name, date = new Date()) => `Keyguard (${name}, ${date.toISOString().slice(0, 10)})`

export const guessDeviceName = (userAgent, maxTouchPoints = 0) => {
  if (/iPhone/.test(userAgent)) return 'iphone'
  if (/iPad/.test(userAgent) || (/Macintosh/.test(userAgent) && maxTouchPoints > 1)) return 'ipad'
  if (/Macintosh/.test(userAgent)) return 'macbook'
  return ''
}

const pinned = (passkey) => (passkey ? { identity: passkey } : undefined)

export const wrapRemote = async (remote, passkey) => {
  const age = await loadAge()
  const encrypter = new age.Encrypter()
  encrypter.addRecipient(new age.webauthn.WebAuthnRecipient(pinned(passkey)))
  return encrypter.encrypt(remote)
}

export const unwrapRemote = async (wrappedBytes, passkey) => {
  const age = await loadAge()
  const decrypter = new age.Decrypter()
  decrypter.addIdentity(new age.webauthn.WebAuthnIdentity(pinned(passkey)))
  return decrypter.decrypt(wrappedBytes, 'text')
}
