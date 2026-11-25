import { b64decode, b64encode } from './unlock-core.js'

const TOKEN_VERSION = 1
export const TOKEN_LIFETIME_SECONDS = 600
const NONCE_BYTES = 16
const MAX_MESSAGE_CHARS = 1000
const SHORT_ID_CHARS = 8
const HIDDEN = /[\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}]/u
const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
const CONVERSATION = new RegExp(`^(?:${UUID}|ses_[0-9A-Za-z]{20,40}|agy_${UUID})$`)
const OPENCODE_PREFIX = 'ses_'
const AGY_PREFIX = 'agy_'
const BOX = /^[0-9A-Za-z-]{1,63}$/

const b64url = (bytes) => b64encode(bytes).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')

const pemBody = (pem) => b64decode(pem.replace(/-----(BEGIN|END) PRIVATE KEY-----/g, '').replace(/\s+/g, ''))

const isShowable = (text) => {
  const length = typeof text === 'string' ? [...text].length : 0
  return length > 0 && length <= MAX_MESSAGE_CHARS && !HIDDEN.test(text.replaceAll('\n', ''))
}

export const messageProblem = ({ text, conversation, box }) => {
  if (!isShowable(text)) {
    return `The message must be 1 to ${MAX_MESSAGE_CHARS} characters, with line breaks but no other hidden characters.`
  }
  if (typeof conversation !== 'string' || !CONVERSATION.test(conversation)) return 'The target is not a full conversation id.'
  if (typeof box !== 'string' || !BOX.test(box)) return 'The target box is not a box name.'
  return null
}

const shortId = (conversation) => {
  if (conversation.startsWith(OPENCODE_PREFIX)) return conversation.slice(-SHORT_ID_CHARS)
  const uuid = conversation.startsWith(AGY_PREFIX) ? conversation.slice(AGY_PREFIX.length) : conversation
  return uuid.slice(0, SHORT_ID_CHARS)
}

export const addressee = ({ conversation, box, title }) => {
  const short = shortId(conversation)
  return `${title ? `${title} (${short})` : short} on ${box}`
}

export const tokenPayload = ({ text, conversation, box }, issuedAt, nonce) => JSON.stringify({
  v: TOKEN_VERSION,
  text,
  to: { conversation, box },
  nonce: b64url(nonce),
  iat: issuedAt,
  exp: issuedAt + TOKEN_LIFETIME_SECONDS,
})

export const signOwnerMessage = async (pem, message, issuedAt,
  nonce = crypto.getRandomValues(new Uint8Array(NONCE_BYTES))) => {
  const key = await crypto.subtle.importKey('pkcs8', pemBody(pem), { name: 'Ed25519' }, false, ['sign'])
  const payload = new TextEncoder().encode(tokenPayload(message, issuedAt, nonce))
  const signature = new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, key, payload))
  return `${b64url(payload)}.${b64url(signature)}`
}
