import * as core from './unlock-core.js'

const STALE_STORE = 412
const MAX_WRITE_ATTEMPTS = 2

export class StaleStore extends Error {}

export const textFile = (text) => core.b64encode(new TextEncoder().encode(text))

export const retryingStale = async (write) => {
  for (let attempt = 1; ; attempt++) {
    try {
      return await write()
    } catch (error) {
      if (!(error instanceof StaleStore) || attempt === MAX_WRITE_ATTEMPTS) throw error
    }
  }
}

export const storeClient = (call) => {
  const fetchJson = async (path) => {
    const response = await call(path)
    if (!response.ok) throw new Error(`${path} returned ${response.status}`)
    return response.json()
  }
  const fetchFile = async (path) => {
    const response = await call(`/store/${path}`)
    if (!response.ok) throw new Error(`the store has no ${path} (${response.status})`)
    return new Uint8Array(await response.arrayBuffer())
  }
  const fetchMeta = () => fetchJson('/store/meta')
  const commitAt = async (files, version, deletes = []) => {
    const response = await call('/store/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'If-Match': String(version) },
      body: JSON.stringify({ files, delete: deletes }),
    })
    if (response.status === STALE_STORE) throw new StaleStore('the store changed while writing')
    if (!response.ok) throw new Error(`the store refused the write (${response.status})`)
  }
  return {
    fetchJson,
    fetchFile,
    fetchMeta,
    commitAt,
    fetchText: async (path) => new TextDecoder().decode(await fetchFile(path)),
    commit: (files, deletes = []) => retryingStale(async () => commitAt(files, (await fetchMeta()).version, deletes)),
  }
}
