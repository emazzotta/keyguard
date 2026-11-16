import { age } from './typage.js'

const PLAINTEXT_BYTES = 32
const REPORT_PATH = '/report'
const TOKEN_HEADER = 'X-Probe-Token'
const SLOW_STEP_MS = 15_000
const PLAIN_TIMEOUT_MS = 120_000
const PLAIN_CHALLENGE_BYTES = 32
const PLAIN_USER_ID_BYTES = 16
const ES256 = -7
const RS256 = -257
const KEY_NAME = `keyguard localhost probe (${new Date().toISOString().slice(0, 10)})`
const SLOW_HINT = 'Still waiting. Look for a "Windows Security" or passkey window behind the browser or in the ' +
  'taskbar. If there is none, press Send report, then reload this page and try the diagnostic button.'
const NO_PLATFORM_AUTHENTICATOR = 'This browser reports no platform authenticator: Windows Hello or Touch ID is ' +
  'not set up for it. Step 1 can then use only a security key or a phone, and may wait without a window.'

const $ = (id) => document.getElementById(id)
const token = new URLSearchParams(location.hash.slice(1)).get('token')
const plaintext = crypto.getRandomValues(new Uint8Array(PLAINTEXT_BYTES))
const outcomes = new Map()
const made = { identity: null, wrapped: null }

const sameBytes = (a, b) => a.length === b.length && a.every((byte, i) => byte === b[i])
const describe = (error) => `${error?.name ?? 'Error'}: ${error?.message ?? error}`
const settled = async (probe) => {
  try {
    return (await probe()) ?? null
  } catch (error) {
    return describe(error)
  }
}

const createPasskey = async () => {
  made.identity = await age.webauthn.createCredential({ keyName: KEY_NAME })
}

const wrap = async () => {
  const encrypter = new age.Encrypter()
  encrypter.addRecipient(new age.webauthn.WebAuthnRecipient({ identity: made.identity }))
  made.wrapped = await encrypter.encrypt(plaintext)
}

const unwrap = async () => {
  const decrypter = new age.Decrypter()
  decrypter.addIdentity(new age.webauthn.WebAuthnIdentity({ identity: made.identity }))
  if (!sameBytes(await decrypter.decrypt(made.wrapped), plaintext)) {
    throw new Error('the unwrapped bytes differ from the wrapped ones')
  }
}

const createPlainPasskey = async () => {
  const credential = await navigator.credentials.create({
    signal: AbortSignal.timeout(PLAIN_TIMEOUT_MS),
    publicKey: {
      rp: { name: 'Keyguard Passkey Probe' },
      user: {
        id: crypto.getRandomValues(new Uint8Array(PLAIN_USER_ID_BYTES)),
        name: `${KEY_NAME}, plain`,
        displayName: 'Keyguard Passkey Probe',
      },
      challenge: crypto.getRandomValues(new Uint8Array(PLAIN_CHALLENGE_BYTES)),
      pubKeyCredParams: [{ type: 'public-key', alg: ES256 }, { type: 'public-key', alg: RS256 }],
      authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
      extensions: { prf: {} },
    },
  })
  return {
    prfEnabled: credential.getClientExtensionResults().prf?.enabled ?? null,
    attachment: credential.authenticatorAttachment ?? null,
    transports: credential.response.getTransports?.() ?? [],
  }
}

const CHAIN = [
  { name: 'create', run: createPasskey },
  { name: 'wrap', run: wrap },
  { name: 'unwrap', run: unwrap },
]
const DIAGNOSTIC = { name: 'plain', run: createPlainPasskey }
const STEPS = [...CHAIN, DIAGNOSTIC]
const LAST_IN_CHAIN = CHAIN[CHAIN.length - 1]

const environment = Promise.all([
  settled(() => globalThis.PublicKeyCredential?.isUserVerifyingPlatformAuthenticatorAvailable?.()),
  settled(() => globalThis.PublicKeyCredential?.getClientCapabilities?.()),
  settled(() => navigator.userAgentData?.getHighEntropyValues(['platformVersion', 'fullVersionList', 'architecture'])),
]).then(([platformAuthenticator, capabilities, platform]) => ({
  secureContext: globalThis.isSecureContext,
  webauthn: 'PublicKeyCredential' in globalThis,
  platformAuthenticator,
  capabilities,
  platform,
}))

const buildReport = async () => ({
  label: $('label').value.trim(),
  browser: navigator.userAgent,
  origin: location.origin,
  environment: await environment,
  steps: STEPS.filter((step) => outcomes.has(step.name)).map((step) => ({ name: step.name, ...outcomes.get(step.name) })),
})

const setStatus = (text, kind = '') => {
  $('status').textContent = text
  $('status').className = kind
}

const sendReport = async () => {
  try {
    const response = await fetch(REPORT_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', [TOKEN_HEADER]: token ?? '' },
      body: JSON.stringify(await buildReport()),
    })
    if (!response.ok) throw new Error(`the probe answered ${response.status}`)
    setStatus('Report sent. Run again with another authenticator, or close this tab.', 'ok')
  } catch (error) {
    setStatus(`The report was not sent: ${describe(error)}`, 'err')
  }
}

const outcomeText = (outcome) => {
  if (!outcome) return ''
  if (outcome.pending) return 'waiting'
  if (!outcome.ok) return outcome.error
  return [`ok, ${outcome.ms} ms`, outcome.detail && JSON.stringify(outcome.detail)].filter(Boolean).join(', ')
}

const outcomeKind = (outcome) => (outcome?.ok === true ? 'ok' : outcome?.ok === false ? 'err' : '')

const isReady = (step) => {
  const position = CHAIN.indexOf(step)
  return position <= 0 || outcomes.get(CHAIN[position - 1].name)?.ok === true
}

const render = (busy) => {
  STEPS.forEach((step) => {
    const outcome = outcomes.get(step.name)
    $(step.name).disabled = busy || !isReady(step)
    $(`${step.name}-status`).textContent = outcomeText(outcome)
    $(`${step.name}-status`).className = outcomeKind(outcome)
  })
}

const forgetChainFrom = (step) => {
  const position = CHAIN.indexOf(step)
  if (position >= 0) CHAIN.slice(position).forEach((later) => outcomes.delete(later.name))
}

const attempt = async (step) => {
  const started = performance.now()
  const elapsed = () => Math.round(performance.now() - started)
  try {
    const detail = await step.run()
    return { ok: true, ms: elapsed(), ...(detail ? { detail } : {}) }
  } catch (error) {
    return { ok: false, ms: elapsed(), error: describe(error) }
  }
}

const runStep = async (step) => {
  forgetChainFrom(step)
  outcomes.set(step.name, { pending: true })
  render(true)
  setStatus('Waiting for the authenticator...')
  const slow = setTimeout(() => setStatus(SLOW_HINT, 'err'), SLOW_STEP_MS)
  const outcome = await attempt(step)
  clearTimeout(slow)
  outcomes.set(step.name, outcome)
  render(false)
  if (outcome.ok && CHAIN.includes(step) && step !== LAST_IN_CHAIN) return setStatus('Continue with the next step.')
  return sendReport()
}

STEPS.forEach((step) => $(step.name).addEventListener('click', () => runStep(step)))
$('send').addEventListener('click', sendReport)
environment.then((found) => {
  $('environment').textContent = JSON.stringify(found, null, 2)
  if (token && found.platformAuthenticator !== true) setStatus(NO_PLATFORM_AUTHENTICATOR, 'err')
})
render(false)
if (!token) setStatus('This address lacks the token the probe printed. Open the full address from the terminal.', 'err')
