async function json(res) {
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try { msg = (await res.json()).detail || msg } catch { /* ignore */ }
    const e = new Error(msg)
    // the gate watches for this to send an expired session back to the login screen
    e.status = res.status
    throw e
  }
  return res.json()
}

// credentials: the login cookie must ride along, including when the frontend is
// served from a different origin than the API
export const api = {
  get: (path) => fetch(path, { credentials: 'include' }).then(json),
  post: (path, body) =>
    fetch(path, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(json),
}

// Same credentials rule as the rest: deleting a project is an authenticated call,
// and without the cookie the server cannot tell whose project it is.
export const del = (path) =>
  fetch(path, { method: 'DELETE', credentials: 'include' }).then(json)

// Poll a job until done/error/cancelled. onUpdate({progress, message, id}) fires each tick.
export async function runJob(startPromise, onUpdate) {
  const { job_id } = await startPromise
  for (;;) {
    const job = await api.get(`/api/jobs/${job_id}`)
    onUpdate?.({ ...job, id: job_id })
    if (job.status === 'done') return job.result
    if (job.status === 'cancelled') throw cancelledError()
    if (job.status === 'error') throw new Error(job.error || 'Job failed')
    await new Promise((r) => setTimeout(r, 700))
  }
}

// A cancel is the user's own doing — callers show a neutral note, not an error.
export function cancelledError() {
  const e = new Error('Cancelled')
  e.cancelled = true
  return e
}

export const cancelJob = (jobId) => api.post(`/api/jobs/${jobId}/cancel`, {})

export function secToMMSS(s) {
  s = Math.max(0, Math.round(s))
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}:${String(sec).padStart(2, '0')}`
}

// Frame-level editing needs sub-second precision that secToMMSS rounds away.
export function secToClock(s, decimals = 2) {
  s = Math.max(0, s || 0)
  const m = Math.floor(s / 60)
  const rest = s - m * 60
  return `${m}:${rest.toFixed(decimals).padStart(decimals ? 3 + decimals : 2, '0')}`
}

export function mmssToSec(t) {
  if (t == null || t === '') return null
  const parts = String(t).trim().split(':')
  if (parts.some((p) => p === '' || isNaN(Number(p)))) return null
  if (parts.length === 3) return +parts[0] * 3600 + +parts[1] * 60 + +parts[2]
  if (parts.length === 2) return +parts[0] * 60 + +parts[1]
  return +parts[0]
}
