import { messageProblem } from './owner-message.js'
import { ALL_MACHINES } from './pin-paste.js'
import * as core from './unlock-core.js'
import { storeClient, textFile } from './store-client.js'

const REQUEST_TAG_LENGTH = 4

const readPin = (key) => {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

const SERVICE_ANSWERS = [
  ['values', (outcome) => outcome.values],
  ['absent', (outcome) => ({ absent: outcome.absent })],
  ['names', (outcome) => ({ list: outcome.names })],
  ['written', (outcome) => ({ set: outcome.written[0] })],
  ['removed', (outcome) => ({ rm: outcome.removed })],
  ['token', (outcome) => ({ sign: outcome.token })],
]

export const serviceAnswer = (outcome) => {
  const found = SERVICE_ANSWERS.find(([key]) => Object.hasOwn(outcome, key))
  if (!found) throw new Error(`the service takes no answer of this kind (${Object.keys(outcome).join(', ')})`)
  return found[1](outcome)
}

export const freshnessProblem = (record, label, nowSeconds) => {
  const freshness = core.requestFreshness(record, nowSeconds)
  if (freshness === 'stale') {
    return `This request was signed more than ${core.REQUEST_MAX_AGE_SECONDS}s ago. An old signed request may be ` +
      `a replay, so it is not offered; ask again from the ${label}.`
  }
  return freshness === 'future' ? `This request is dated in the future. Check the clock of the ${label}, then ask again.` : null
}

const messageOf = (record) => (record.op === 'sign'
  ? { text: record.text, conversation: record.target, box: record.target_box, title: record.target_title ?? '' }
  : null)

const asRequest = (record) => ({
  op: record.op ?? 'get',
  names: record.vars,
  values: {},
  force: true,
  session: record.session ?? '',
  title: record.title ?? '',
  host: record.host,
  sealedValue: record.value,
  message: messageOf(record),
})

export const serviceTransport = (params, now = () => Date.now() / 1000) => {
  const store = storeClient((path, init) => fetch(path, init))
  const requestId = params.get('id')
  const requestPath = (suffix) => `/r/${encodeURIComponent(requestId)}/${suffix}`
  let requesters = {}
  let current = null
  const label = (host) => requesters[host]?.label ?? host
  const pinLink = (host) => `?${new URLSearchParams(requestId ? { id: requestId, pin: host } : { pin: host })}`
  const over = (text, extra = {}) => ({ kind: 'over', text, ...extra })
  const setup = (wanted) => {
    const hosts = Object.keys(requesters)
    const offered = hosts.length > 1 ? [...hosts, ALL_MACHINES] : hosts
    return { kind: 'setup', hosts, host: offered.includes(wanted) ? wanted : hosts[0] }
  }

  const review = async (record) => {
    if (record.answered) return over('This request is already answered. Nothing is left to approve here.', { tone: 'ok' })
    const verdict = await core.checkSignature(record, requesters, readPin)
    if (verdict === 'unknown-host') return over(`No key can be pinned for the host "${record.host}". Not approving.`)
    if (verdict === 'unpinned') return setup(record.host)
    const pinning = { pinLink: pinLink(record.host) }
    if (verdict === 'forged') {
      return over(`Signature check failed: not signed by the key pinned for the ${label(record.host)}. Not approving.`, pinning)
    }
    const problem = freshnessProblem(record, label(record.host), now()) ??
      (record.op === 'sign' ? messageProblem(messageOf(record)) : null)
    if (problem) return over(problem, pinning)
    current = record
    const age = Math.max(0, Math.round(now() - record.ts))
    const note = `Request ${requestId.slice(0, REQUEST_TAG_LENGTH)}, sent ${age}s ago.`
    return { kind: 'review', request: asRequest(record), note, status: 'Signature verified.', ...pinning }
  }

  return {
    ceremony: false,
    device: null,
    store,
    label,
    origin: () => `Your ${label(current.host)}`,
    asker: () => `the ${label(current.host)}`,
    loadRequest: async () => {
      requesters = (await store.fetchJson('/config')).requesters
      if (!requestId || params.has('pin')) return setup(params.get('pin'))
      const response = await fetch(requestPath('request'))
      if (!response.ok) return over(`Request not found (${response.status}). It may have expired.`)
      return review(await response.json())
    },
    pinned: (host) => readPin(core.pinKey(host)),
    pin: (host, key) => {
      localStorage.setItem(core.pinKey(host), key)
      return requestId ? `?${new URLSearchParams({ id: requestId })}` : null
    },
    storedEnrolment: () => core.storedEnrolment(),
    rememberEnrolment: (name, passkey) => core.rememberEnrolment(name, passkey),
    publishPasskey: async (meta) => {
      const stored = core.storedEnrolment()
      const path = core.unpublishedPasskey(meta, stored)
      if (path) await store.commit({ [path]: textFile(stored.passkey) })
    },
    checkRecipients: (index) => {
      const pinned = core.storedRecipients()
      const verdict = core.recipientCheck(index.recipients, pinned)
      if (verdict === 'changed') throw new core.RecipientsChanged(pinned, index.recipients)
      if (verdict === 'unpinned') {
        try {
          core.rememberRecipients(index.recipients)
        } catch {}
      }
    },
    proposedValues: async (request, remote) =>
      ({ [request.names[0]]: await core.openSealed(core.b64decode(request.sealedValue), remote) }),
    answer: async (outcome) => {
      const sealed = await core.sealResponse(serviceAnswer(outcome), current.eph_pubkey)
      const response = await fetch(requestPath('response'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ response: core.b64encode(sealed) }),
      })
      if (!response.ok) throw new Error(`the service did not take the answer (${response.status})`)
    },
  }
}
