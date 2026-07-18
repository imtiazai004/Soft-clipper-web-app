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

// Poll a job until done/error. onUpdate({progress, message}) fires on each tick.
export async function runJob(startPromise, onUpdate) {
  const { job_id } = await startPromise
  for (;;) {
    const job = await api.get(`/api/jobs/${job_id}`)
    onUpdate?.(job)
    if (job.status === 'done') return job.result
    if (job.status === 'error') throw new Error(job.error || 'Job failed')
    await new Promise((r) => setTimeout(r, 700))
  }
}

export function secToMMSS(s) {
  s = Math.max(0, Math.round(s))
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}:${String(sec).padStart(2, '0')}`
}

export function mmssToSec(t) {
  if (t == null || t === '') return null
  const parts = String(t).trim().split(':')
  if (parts.some((p) => p === '' || isNaN(Number(p)))) return null
  if (parts.length === 3) return +parts[0] * 3600 + +parts[1] * 60 + +parts[2]
  if (parts.length === 2) return +parts[0] * 60 + +parts[1]
  return +parts[0]
}
