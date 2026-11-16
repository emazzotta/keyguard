import { localTransport } from './transport-local.js'
import { serviceTransport } from './transport-service.js'

export const CLOSE_DELAY_MS = 1_000
const AUTHENTICATOR_TIMEOUT_MS = 120_000
const NO_AUTHENTICATOR = 'This browser reports no authenticator: set up Windows Hello or Touch ID, or use a browser ' +
  'that can reach a phone.'
const NO_PRF = 'This browser cannot derive a key from a passkey (no PRF support). Update it or use another browser.'
export const PASSKEY_CANCELLED = 'The passkey prompt was cancelled or timed out. Try again. A browser inside a chat ' +
  'or mail app cannot use passkeys: open the link in your own browser.'

export const $ = (id) => document.getElementById(id)

export const describe = (error) => `${error?.name ?? 'Error'}: ${error?.message ?? error}`

export const capitalized = (text) => text.charAt(0).toUpperCase() + text.slice(1)

export const setStatus = (text, kind = '') => {
  $('status').textContent = text
  $('status').className = kind
}

export const chip = (text) => {
  const item = document.createElement('li')
  item.textContent = text
  return item
}

export const isCancelled = (error) => error?.name === 'NotAllowedError'

export const onSubmit = (id, handler) => $(id).addEventListener('submit', (event) => {
  event.preventDefault()
  handler()
})

export const answerAndClose = async (transport, outcome, text, kind, panels) => {
  $('refuse').hidden = true
  await transport.answer(outcome)
  for (const id of panels) $(id).hidden = true
  setStatus(`${text} You can close this tab.`, kind)
  setTimeout(() => window.close(), CLOSE_DELAY_MS)
}

export const reportLost = (transport) => (error) =>
  setStatus(`${capitalized(transport.asker())} did not get the answer (${describe(error)}).`, 'err')

export const withinAuthenticatorTime = (work) => {
  let timer
  const timeout = new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error('no authenticator answered within 2 minutes')), AUTHENTICATOR_TIMEOUT_MS)
  })
  return Promise.race([work, timeout]).finally(() => clearTimeout(timer))
}

export const authenticatorProblem = async () => {
  const credentials = globalThis.PublicKeyCredential
  if (!credentials) return NO_AUTHENTICATOR
  const [platform, capabilities] = await Promise.all([
    credentials.isUserVerifyingPlatformAuthenticatorAvailable?.().catch(() => null),
    credentials.getClientCapabilities?.().catch(() => null),
  ])
  if (capabilities?.['extension:prf'] === false) return NO_PRF
  return platform === false && capabilities?.hybridTransport === false ? NO_AUTHENTICATOR : null
}

export const openTransport = () => {
  const token = new URLSearchParams(location.hash.slice(1)).get('token')
  return token ? localTransport(token) : serviceTransport(new URLSearchParams(location.search))
}
