import { describe, expect, test } from 'bun:test'
import { age } from '../keyguard_passkey/pages/typage.js'
import * as core from '../keyguard_passkey/pages/unlock-core.js'
import V from './unlock-core-vectors.json' with { type: 'json' }

const text = (bytes) => new TextDecoder().decode(bytes)
const failure = async (work) => {
  try {
    await work()
  } catch (error) {
    return error
  }
  return null
}
const fakeStorage = () => {
  const items = new Map()
  return { getItem: (key) => items.get(key) ?? null, setItem: (key, value) => items.set(key, value), removeItem: (key) => items.delete(key) }
}
const fetchStoreFile = async (path) => core.b64decode(V.store[path])
const remoteDecrypter = new age.Decrypter()
remoteDecrypter.addIdentity(V.remote_identity)
const openPayload = async (b64) => JSON.parse(text(core.unpad(await remoteDecrypter.decrypt(core.b64decode(b64)))))
const stanzas = (b64) => (text(core.b64decode(b64).subarray(0, 2048)).match(/\n-> X25519 /g) ?? []).length
const index = await core.readIndex(core.b64decode(V.store['index.age']), V.remote_identity)
const everyRecipient = (someIndex) => new Set(Object.values(someIndex.recipients.tiers).flat()).size
const signingKey = async () => {
  const pair = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify'])
  const pubkey = core.b64encode(new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey)))
  const sign = async (fields) => ({
    ...fields,
    sig: core.b64encode(new Uint8Array(
      await crypto.subtle.sign({ name: 'Ed25519' }, pair.privateKey, core.canonicalRequestBytes(fields)))),
  })
  return { pubkey, sign }
}
const devboxKey = await signingKey()
const macKey = await signingKey()

describe('padding', () => {
  /** @useCase docs/service.md */
  test('should read a big-endian length prefix and drop the zero fill', () => {
    expect(text(core.unpad(new Uint8Array([0, 0, 0, 3, 65, 66, 67, 0, 0, 0])))).toBe('ABC')
  })

  /** @useCase docs/service.md */
  test('should reject padding shorter than its length prefix', () => {
    expect(() => core.unpad(new Uint8Array([0, 0, 255]))).toThrow()
  })

  /** @useCase docs/service.md */
  test('should pad like keyguard: length prefix, zero fill to 256 bytes', () => {
    const padded = core.pad(new TextEncoder().encode('{"name":"A","value":"b"}'))

    expect([padded.length, padded[3], text(core.unpad(padded))]).toEqual([256, 24, '{"name":"A","value":"b"}'])
  })

  /** @useCase docs/service.md */
  test('should serialize JSON with keys sorted at every depth', () => {
    expect(core.canonicalJson({ b: 1, a: { d: 2, c: [3, { f: 4, e: 5 }] } }))
      .toBe('{"a":{"c":[3,{"e":5,"f":4}],"d":2},"b":1}')
  })
})

describe('request signatures', () => {
  const plain = V.plain_request
  const plainKey = core.b64decode(plain.requester_pubkey_b64)
  const withCaller = V.caller_request
  const callerKey = core.b64decode(withCaller.requester_pubkey_b64)

  /** @useCase docs/service.md */
  test('should serialize canonical bytes identical to the client', () => {
    expect(core.b64encode(core.canonicalRequestBytes(plain.request))).toBe(plain.canonical_signed_bytes_b64)
  })

  /** @useCase docs/service.md */
  test('should verify a genuine client signature', async () => {
    expect(await core.verifyRequest(plain.request, plainKey)).toBe(true)
  })

  /** @useCase docs/service.md */
  test('should reject a tampered host', async () => {
    expect(await core.verifyRequest({ ...plain.request, host: 'evil' }, plainKey)).toBe(false)
  })

  /** @useCase docs/service.md */
  test('should reject a tampered variable list', async () => {
    expect(await core.verifyRequest({ ...plain.request, vars: ['ALL'] }, plainKey)).toBe(false)
  })

  /** @useCase docs/service.md */
  test('should serialize a non-ASCII session title byte for byte as the client signs it', () => {
    expect(core.b64encode(core.canonicalRequestBytes(withCaller.request))).toBe(withCaller.canonical_signed_bytes_b64)
  })

  /** @useCase docs/service.md */
  test('should verify a request signed with the asking session', async () => {
    expect(await core.verifyRequest(withCaller.request, callerKey)).toBe(true)
  })

  /** @useCase docs/service.md */
  test('should reject a tampered session title', async () => {
    expect(await core.verifyRequest({ ...withCaller.request, title: 'Harmless chore' }, callerKey)).toBe(false)
  })

  /** @useCase docs/service.md */
  test('should reject a session stripped from a request signed with one', async () => {
    expect(await core.verifyRequest({ ...withCaller.request, session: undefined }, callerKey)).toBe(false)
  })

  /** @useCase docs/service.md scenario="The Request" */
  test('should serialize a sign request byte for byte as the client signs it, its message included', async () => {
    const signing = V.sign_request

    expect(core.b64encode(core.canonicalRequestBytes(signing.request))).toBe(signing.canonical_signed_bytes_b64)
    expect(await core.verifyRequest(signing.request, core.b64decode(signing.requester_pubkey_b64))).toBe(true)
  })

  /** @useCase docs/service.md scenario="The Request" */
  test.each([['text', 'yes, push everything'], ['target', `ses_${'a'.repeat(26)}`], ['target_box', 'devbox'],
    ['target_title', 'Harmless chore']])('should reject a sign request whose %s was changed', async (field, value) => {
    const signing = V.sign_request

    expect(await core.verifyRequest({ ...signing.request, [field]: value }, core.b64decode(signing.requester_pubkey_b64)))
      .toBe(false)
  })

  /** @useCase docs/service.md */
  test('should read a null session from the service as no session', () => {
    const fromService = core.canonicalRequestBytes({ ...plain.request, session: null, title: null })

    expect(text(fromService)).toBe(text(core.canonicalRequestBytes(plain.request)))
  })

  /** @useCase docs/service.md */
  test('should accept a 32-byte pinned key', () => {
    expect(core.decodeSigningKey(plain.requester_pubkey_b64).length).toBe(32)
  })

  /** @useCase docs/service.md */
  test('should reject a wrong-length pinned key', () => {
    expect(() => core.decodeSigningKey(core.b64encode(new Uint8Array(16)))).toThrow()
  })

  /** @useCase docs/service.md */
  test.each([
    ['set', { op: 'set', value: 'c2VhbGVk' }, '{"eph_pubkey":"age1eph","host":"devbox","op":"set","ts":1,"value":"c2VhbGVk","vars":["A"]}'],
    ['list', { op: 'list', vars: [] }, '{"eph_pubkey":"age1eph","host":"devbox","op":"list","ts":1,"vars":[]}'],
    ['rm', { op: 'rm' }, '{"eph_pubkey":"age1eph","host":"devbox","op":"rm","ts":1,"vars":["A"]}'],
  ])('should sign a %s request over exactly the fields it carries, in canonical order', (_op, fields, expected) => {
    const request = { vars: ['A'], host: 'devbox', eph_pubkey: 'age1eph', ts: 1, ...fields }

    expect(text(core.canonicalRequestBytes(request))).toBe(expected)
  })
})

describe('checkSignature', () => {
  const requesters = { devbox: { label: 'dev box' }, mac: { label: 'Mac' } }
  const pins = (entries) => (key) => entries[key] ?? null
  const asking = (host) => ({ vars: ['A'], host, eph_pubkey: 'age1eph', ts: 1 })
  const bothPinned = pins({ 'keyguard.devbox_pubkey': devboxKey.pubkey, 'keyguard.mac_pubkey': macKey.pubkey })

  /** @useCase docs/service.md */
  test('should keep each key under the storage key existing browsers pinned it in', () => {
    expect([core.pinKey('devbox'), core.pinKey('mac')]).toEqual(['keyguard.devbox_pubkey', 'keyguard.mac_pubkey'])
  })

  /** @useCase docs/service.md */
  test('should verify a request against the key pinned for its host', async () => {
    expect(await core.checkSignature(await macKey.sign(asking('mac')), requesters, bothPinned)).toBe('verified')
  })

  /** @useCase docs/service.md */
  test("should refuse a request one requester's key signed in the name of another", async () => {
    expect(await core.checkSignature(await devboxKey.sign(asking('mac')), requesters, bothPinned)).toBe('forged')
  })

  /** @useCase docs/service.md */
  test('should ask for a key when only another requester has one pinned', async () => {
    const devboxOnly = pins({ 'keyguard.devbox_pubkey': devboxKey.pubkey })

    expect(await core.checkSignature(await macKey.sign(asking('mac')), requesters, devboxOnly)).toBe('unpinned')
  })

  /** @useCase docs/service.md */
  test.each(['laptop', '__proto__', 'constructor', ''])('should refuse the host "%s" the service names no requester for',
    async (host) => {
      expect(await core.checkSignature(await devboxKey.sign(asking(host)), requesters, bothPinned)).toBe('unknown-host')
    })
})

describe('reading values', () => {
  /** @useCase docs/service.md */
  test('should decrypt every requested value from a keyguard store, multi-line ones included', async () => {
    const values = await core.resolveValues(['JIRA_TOKEN', 'GITHUB_TOKEN', 'MULTILINE'], V.remote_identity, fetchStoreFile)

    expect(values).toEqual(V.expected)
  })

  /** @useCase docs/service.md */
  test('should name every requested variable the store lacks, and decrypt no value', async () => {
    const fetched = []

    const absent = await failure(() => core.resolveValues(['JIRA_TOKEN', 'NOPE', 'GONE'], V.remote_identity,
      (path) => { fetched.push(path); return fetchStoreFile(path) }))

    expect(absent).toBeInstanceOf(core.AbsentNames)
    expect([absent.names, absent.message, fetched]).toEqual([['NOPE', 'GONE'], 'NOPE, GONE are not in the store',
      ['index.age']])
  })

  /** @useCase docs/service.md scenario="The Owner Key" */
  test('should refuse to release the owner key without reading the store', async () => {
    const fetched = []

    const refused = await failure(() => core.resolveValues(['JIRA_TOKEN', core.OWNER_KEY_NAME], V.remote_identity,
      (path) => { fetched.push(path); return fetchStoreFile(path) }))

    expect([refused instanceof core.OwnerKeyRefused, fetched]).toEqual([true, []])
  })

  /** @useCase docs/service.md scenario="The Owner Key" */
  test('should read the owner key only to sign with it', async () => {
    const stored = await core.writeVariable(index, core.OWNER_KEY_NAME, V.owner_token.private_key_pem)
    const files = (path) => (path in stored.files ? core.b64decode(stored.files[path]) : fetchStoreFile(path))

    expect(await core.readOwnerKey(stored.index, V.remote_identity, files)).toBe(V.owner_token.private_key_pem)
  })

  /** @useCase docs/service.md scenario="The Owner Key" */
  test('should name the owner key absent when the store lacks it', async () => {
    const absent = await failure(() => core.readOwnerKey(index, V.remote_identity, fetchStoreFile))

    expect([absent instanceof core.AbsentNames, absent.names]).toEqual([true, [core.OWNER_KEY_NAME]])
  })

  /** @useCase docs/service.md */
  test('should seal the response to the ephemeral key', async () => {
    const ephemeral = await age.generateIdentity()
    const sealed = await core.sealResponse(V.expected, await age.identityToRecipient(ephemeral))
    const decrypter = new age.Decrypter()
    decrypter.addIdentity(ephemeral)

    expect(JSON.parse(text(await decrypter.decrypt(sealed)))).toEqual(V.expected)
  })

  /** @useCase docs/service.md */
  test('should derive the same variable file name as keyguard', async () => {
    expect(await core.variablePath(index.salt, 'JIRA_TOKEN')).toBe(index.entries.JIRA_TOKEN.file)
  })

  /** @useCase docs/service.md */
  test('should list every name of a store index in sorted order', () => {
    expect(core.listNames(index)).toEqual(['GITHUB_TOKEN', 'JIRA_TOKEN', 'MULTILINE'])
    expect(core.listNames({ entries: { B_TOKEN: {}, A_TOKEN: {} } })).toEqual(['A_TOKEN', 'B_TOKEN'])
  })

  /** @useCase docs/service.md */
  test('should open a value a requester sealed to the store key', async () => {
    const proposal = new age.Encrypter()
    proposal.addRecipient(await age.identityToRecipient(V.remote_identity))

    expect(await core.openSealed(await proposal.encrypt('proposed'), V.remote_identity)).toBe('proposed')
  })
})

describe('store format', () => {
  /** @useCase docs/service.md */
  test('should accept the store format this release reads', () => {
    expect(core.checkFormat(index)).toBe(index)
  })

  /** @useCase docs/service.md */
  test('should name the format and the reader a newer store needs', async () => {
    const newer = await failure(() => core.checkFormat({ ...index, version: core.STORE_FORMAT + 1, reader: '9.0.0' }))

    expect(newer).toBeInstanceOf(core.NewerFormat)
    expect([newer.format, newer.reader]).toEqual([core.STORE_FORMAT + 1, '9.0.0'])
  })

  /** @useCase docs/service.md */
  test('should start a new store with one high-tier recipient, a salt and no entries', () => {
    const salt = new Uint8Array(core.SALT_BYTES).fill(7)

    expect(core.newIndex('age1new', salt)).toEqual({
      version: core.STORE_FORMAT,
      salt: core.b64encode(salt),
      recipients: { version: 1, tiers: { high: ['age1new'], low: [] } },
      entries: {},
    })
  })

  /** @useCase docs/service.md */
  test('should seal a new index that the store key reads back', async () => {
    const identity = await age.generateIdentity()
    const fresh = core.newIndex(await age.identityToRecipient(identity))

    const reread = await core.readIndex(core.b64decode(await core.sealIndex(fresh)), identity)

    expect(reread).toEqual(fresh)
  })
})

describe('writing values', () => {
  /** @useCase docs/service.md */
  test('should put a new variable in the high tier, readable by the store key', async () => {
    const created = await core.writeVariable(index, 'NEW_TOKEN', 'new/value')
    const entry = created.index.entries.NEW_TOKEN

    expect([created.created, entry.tier, entry.file]).toEqual([true, 'high', await core.variablePath(index.salt, 'NEW_TOKEN')])
    expect(await openPayload(created.files[entry.file])).toEqual({ name: 'NEW_TOKEN', value: 'new/value' })
    expect(stanzas(created.files[entry.file])).toBe(index.recipients.tiers.high.length)
  })

  /** @useCase docs/service.md */
  test('should rewrite the index with the new entry, the same salt and recipients, sealed to every recipient', async () => {
    const created = await core.writeVariable(index, 'NEW_TOKEN', 'new/value')

    const rewritten = await core.readIndex(core.b64decode(created.files['index.age']), V.remote_identity)

    expect('NEW_TOKEN' in rewritten.entries).toBe(true)
    expect([rewritten.salt, core.canonicalJson(rewritten.recipients)]).toEqual([index.salt, core.canonicalJson(index.recipients)])
    expect(stanzas(created.files['index.age'])).toBe(everyRecipient(index))
  })

  /** @useCase docs/service.md */
  test('should overwrite an existing variable in place and leave the index alone', async () => {
    const replaced = await core.writeVariable(index, 'JIRA_TOKEN', 'rotated')

    expect([replaced.created, Object.keys(replaced.files)]).toEqual([false, [index.entries.JIRA_TOKEN.file]])
    expect((await openPayload(replaced.files[index.entries.JIRA_TOKEN.file])).value).toBe('rotated')
  })

  /** @useCase docs/service.md */
  test('should write every name in one plan, with one index naming them all', async () => {
    const plan = await core.planWrites(index, ['FIRST', 'SECOND'], { FIRST: 'one', SECOND: 'two' }, false)

    const rewritten = await core.readIndex(core.b64decode(plan.files['index.age']), V.remote_identity)

    expect([plan.written, plan.skipped]).toEqual([['FIRST', 'SECOND'], []])
    expect(Object.keys(rewritten.entries).filter((name) => ['FIRST', 'SECOND'].includes(name))).toEqual(['FIRST', 'SECOND'])
    expect(await openPayload(plan.files[rewritten.entries.SECOND.file])).toEqual({ name: 'SECOND', value: 'two' })
  })

  /** @useCase docs/service.md */
  test('should keep a stored value unless forced', async () => {
    const kept = await core.planWrites(index, ['JIRA_TOKEN', 'FRESH'], { JIRA_TOKEN: 'x', FRESH: 'y' }, false)
    const forced = await core.planWrites(index, ['JIRA_TOKEN'], { JIRA_TOKEN: 'x' }, true)

    expect([kept.written, kept.skipped]).toEqual([['FRESH'], ['JIRA_TOKEN']])
    expect([forced.written, Object.keys(forced.files)]).toEqual([['JIRA_TOKEN'], [index.entries.JIRA_TOKEN.file]])
  })

  /** @useCase docs/service.md scenario="The Owner Key" */
  test.each([true, false])('should refuse to write the owner key, forced or not (%s)', async (force) => {
    const refused = await failure(() => core.planWrites(index, ['FRESH', core.OWNER_KEY_NAME],
      { FRESH: 'x', [core.OWNER_KEY_NAME]: 'pem' }, force))

    expect(refused).toBeInstanceOf(core.OwnerKeyRefused)
  })

  /** @useCase docs/service.md */
  test('should plan no file when every name is kept', async () => {
    const plan = await core.planWrites(index, ['JIRA_TOKEN'], { JIRA_TOKEN: 'x' }, false)

    expect([plan.files, plan.index]).toEqual([{}, index])
  })
})

describe('removing values', () => {
  /** @useCase docs/service.md */
  test('should delete the variable file and drop only its entry from an index sealed to every recipient', async () => {
    const removal = await core.removeVariable(index, 'JIRA_TOKEN')

    const shrunk = await core.readIndex(core.b64decode(removal.files['index.age']), V.remote_identity)

    expect(removal.deletes).toEqual([index.entries.JIRA_TOKEN.file])
    expect([Object.keys(shrunk.entries).sort(), shrunk.salt]).toEqual([['GITHUB_TOKEN', 'MULTILINE'], index.salt])
    expect(stanzas(removal.files['index.age'])).toBe(everyRecipient(index))
  })

  /** @useCase docs/service.md scenario="The Owner Key" */
  test('should refuse to remove the owner key', async () => {
    const stored = await core.writeVariable(index, core.OWNER_KEY_NAME, 'pem')

    expect(await failure(() => core.removeVariable(stored.index, core.OWNER_KEY_NAME))).toBeInstanceOf(core.OwnerKeyRefused)
  })

  /** @useCase docs/service.md */
  test.each(['NOPE', 'constructor'])('should refuse to remove "%s", which the store does not hold', async (name) => {
    const absent = await failure(() => core.removeVariable(index, name))

    expect(absent).toBeInstanceOf(core.AbsentNames)
    expect(absent.message).toBe(`${name} is not in the store`)
  })
})

describe('recipient pinning', () => {
  /** @useCase docs/service.md */
  test('should accept a recipient set equal to the pinned one', () => {
    expect(core.recipientCheck(index.recipients, index.recipients)).toBe('match')
  })

  /** @useCase docs/service.md */
  test('should report a recipient set nobody pinned yet', () => {
    expect(core.recipientCheck(index.recipients, null)).toBe('unpinned')
  })

  /** @useCase docs/service.md */
  test('should flag a new version or an extra recipient at the same version', () => {
    const bumped = { ...index.recipients, version: index.recipients.version + 1 }
    const extra = { ...index.recipients, tiers: { ...index.recipients.tiers, high: [...index.recipients.tiers.high, 'age1evil'] } }

    expect([core.recipientCheck(bumped, index.recipients), core.recipientCheck(extra, index.recipients)])
      .toEqual(['changed', 'changed'])
  })

  /** @useCase docs/service.md */
  test('should remember the pinned recipient set', () => {
    const storage = fakeStorage()

    core.rememberRecipients(index.recipients, storage)

    expect(core.recipientCheck(index.recipients, core.storedRecipients(storage))).toBe('match')
  })

  /** @useCase docs/service.md */
  test('should name both versions when the recipient set changed', () => {
    const changed = new core.RecipientsChanged(index.recipients, { ...index.recipients, version: 9 })

    expect([changed.recipients.version, changed.message.includes(`pinned version ${index.recipients.version}, now 9`)])
      .toEqual([9, true])
  })
})

describe('freshness', () => {
  const NOW = 1_800_000_000

  /** @useCase docs/service.md */
  test.each([
    ['signed moments ago', NOW - 10, 'fresh'],
    ['right at the age limit', NOW - core.REQUEST_MAX_AGE_SECONDS, 'fresh'],
    ['older than the limit, which may be a replay', NOW - core.REQUEST_MAX_AGE_SECONDS - 1, 'stale'],
    ['dated beyond the allowed clock skew', NOW + core.REQUEST_MAX_SKEW_SECONDS + 1, 'future'],
  ])('should judge a request %s', (_case, ts, expected) => {
    expect(core.requestFreshness({ ts }, NOW)).toBe(expected)
  })
})

describe('enrolments', () => {
  const handles = {
    files: {
      'identity-remote.iphone.age': 'h', 'identity-remote.iphone.passkey': 'h',
      'identity-remote.macbook.age': 'h', 'identity-remote.paper.age': 'h',
    },
  }

  /** @useCase docs/service.md */
  test('should list passkey enrolments by name, without the paper backup', () => {
    const meta = { version: 7, files: { 'index.age': 'h', 'identity-remote.paper.age': 'h', 'identity-remote.iphone.age': 'h',
      'identity-remote.ipad.age': 'h', [`vars/${'a'.repeat(64)}.age`]: 'h' } }

    expect([core.enrolmentNames(meta), core.enrolmentNames({ version: 0, files: {} })]).toEqual([['ipad', 'iphone'], []])
  })

  /** @useCase docs/service.md */
  test('should remember which enrolment and passkey this browser uses', () => {
    const storage = fakeStorage()

    core.rememberEnrolment('ipad', 'AGE-PLUGIN-FIDO2PRF-1TEST', storage)

    expect(core.storedEnrolment(storage)).toEqual({ name: 'ipad', passkey: 'AGE-PLUGIN-FIDO2PRF-1TEST' })
  })

  /** @useCase docs/service.md */
  test('should remember a picked enrolment without a passkey, dropping the one stored before', () => {
    const storage = fakeStorage()
    core.rememberEnrolment('ipad', 'AGE-PLUGIN-FIDO2PRF-1TEST', storage)

    core.rememberEnrolment('macbook', null, storage)

    expect(core.storedEnrolment(storage)).toEqual({ name: 'macbook', passkey: null })
  })

  /** @useCase docs/service.md */
  test.each([
    ['with its published handle', 'iphone', { name: 'iphone', passkey: null, handle: 'identity-remote.iphone.passkey' }],
    ['without a handle where none is published', 'macbook', { name: 'macbook', passkey: null, handle: null }],
    ['nothing for a name the store does not hold', 'ipad', null],
    ['nothing for the paper backup', 'paper', null],
  ])('should name an enrolment the user picked %s', (_case, name, expected) => {
    expect(core.namedEnrolment(handles, name)).toEqual(expected)
  })

  /** @useCase docs/service.md */
  test('should treat blocked storage as no stored enrolment', () => {
    const blocked = { getItem: () => { throw new Error('SecurityError') } }

    expect(core.storedEnrolment(blocked)).toEqual({ name: null, passkey: null })
  })

  /** @useCase docs/service.md */
  test('should date the passkey name so a replaced passkey stays tellable apart', () => {
    expect(core.passkeyName('iphone', new Date('2026-09-25T18:00:00Z'))).toBe('Keyguard (iphone, 2026-09-25)')
  })

  /** @useCase docs/service.md */
  test.each([
    ['iphone on an iPhone', 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148', 5, 'iphone'],
    ['ipad on an iPad, which reports a Mac user agent with touch', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 5, 'ipad'],
    ['macbook on a Mac', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 0, 'macbook'],
    ['nothing for an unknown device', 'Mozilla/5.0 (X11; Linux x86_64)', 0, ''],
  ])('should suggest %s', (_case, userAgent, touchPoints, expected) => {
    expect(core.guessDeviceName(userAgent, touchPoints)).toBe(expected)
  })

  /** @useCase docs/service.md */
  test('should name the published passkey handle after its enrolment', () => {
    expect(core.passkeyPath('ipad')).toBe('identity-remote.ipad.passkey')
  })

  /** @useCase docs/service.md */
  test.each([
    ['publish a browser passkey the store lacks', { name: 'macbook', passkey: 'P' }, 'identity-remote.macbook.passkey'],
    ['not republish a passkey the store has', { name: 'iphone', passkey: 'P' }, null],
    ['not publish for an enrolment the store no longer holds', { name: 'ipad', passkey: 'P' }, null],
    ['not publish without a stored passkey', { name: 'macbook', passkey: null }, null],
  ])('should %s', (_case, stored, expected) => {
    expect(core.unpublishedPasskey(handles, stored)).toBe(expected)
  })

  /** @useCase docs/service.md */
  test("should unlock with this browser's own passkey first, then a published handle", () => {
    expect(core.unlockSource(handles, { name: 'macbook', passkey: 'P' })).toEqual({ name: 'macbook', passkey: 'P' })
    expect(core.unlockSource(handles, { name: null, passkey: null }))
      .toEqual({ name: 'iphone', passkey: null, handle: 'identity-remote.iphone.passkey' })
  })

  /** @useCase docs/service.md */
  test('should leave the unlock choice to the user when nothing is pinned or published', () => {
    expect(core.unlockSource({ files: { 'identity-remote.iphone.age': 'h' } }, { name: 'iphone', passkey: null })).toBeNull()
  })

  /** @useCase docs/service.md */
  test.each([
    ['the pinned passkey of this browser', handles, { name: 'macbook', passkey: 'P' }, { name: 'macbook', passkey: 'P' }],
    ['a legacy browser through its published handle', handles, { name: 'iphone', passkey: null },
      { name: 'iphone', passkey: null, handle: 'identity-remote.iphone.passkey' }],
    ['the only enrolment', { files: { 'identity-remote.iphone.age': 'h' } }, { name: null, passkey: null },
      { name: 'iphone', passkey: null, handle: null }],
    ['nothing when it would have to guess between several', handles, { name: null, passkey: null }, null],
  ])('should approve with %s', (_case, meta, stored, expected) => {
    expect(core.approvalEnrolment(meta, stored)).toEqual(expected)
  })
})
