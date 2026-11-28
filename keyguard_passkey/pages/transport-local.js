import { storeClient } from './store-client.js'

const TOKEN_HEADER = 'X-Keyguard-Token'
const DEVICE = 'local'

export const localTransport = (token) => {
  const call = (path, init = {}) => fetch(path, { ...init, headers: { ...init.headers, [TOKEN_HEADER]: token } })
  const store = storeClient(call)
  return {
    ceremony: true,
    device: DEVICE,
    store,
    origin: () => 'A command on this machine',
    asker: () => 'the command',
    loadRequest: async () => {
      try {
        return { kind: 'review', request: await store.fetchJson('/request') }
      } catch (error) {
        return { kind: 'over', title: 'This approval is over', text: `Run the command again to get a new address (${error?.message ?? error}).` }
      }
    },
    storedEnrolment: () => ({ name: DEVICE, passkey: null }),
    rememberEnrolment: () => {},
    publishPasskey: async () => {},
    checkRecipients: () => {},
    proposedValues: async (request) => request.values,
    answer: async (outcome) => {
      const response = await call('/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(outcome),
      })
      if (!response.ok) throw new Error(`the command did not take the answer (${response.status})`)
    },
  }
}
