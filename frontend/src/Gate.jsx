import { useEffect, useState } from 'react'
import { api } from './api'
import App from './App.jsx'

/**
 * Decides whether to show the app or a login screen.
 *
 * The backend answers /api/me with multi_user:false when it runs as the desktop
 * build, and the gate steps out of the way entirely — that install has one user
 * and never had a login.
 */
export default function Gate() {
  const [state, setState] = useState({ loading: true })

  useEffect(() => {
    api.get('/api/me')
      .then((m) => setState({ loading: false, multiUser: m.multi_user, user: m.user }))
      // if we can't even ask, let the app load and surface its own errors
      .catch(() => setState({ loading: false, multiUser: false, user: 'local' }))
  }, [])

  if (state.loading) return <Splash text="Loading..." />
  if (state.multiUser && !state.user) {
    return <Login onDone={(user) => setState({ loading: false, multiUser: true, user })} />
  }
  return <App />
}

function Splash({ text }) {
  return (
    <div style={wrap}>
      <div style={{ color: '#8b8ba3' }}>{text}</div>
    </div>
  )
}

function Login({ onDone }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      const r = await api.post('/api/login', { username, password })
      onDone(r.user)
    } catch (err) {
      setError(err.message || 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={wrap}>
      <form onSubmit={submit} style={card}>
        <div style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.5px' }}>Soft Clipper</div>
        <div style={{ color: '#8b8ba3', fontSize: 14, marginTop: 6, marginBottom: 22 }}>
          Sign in to your dashboard
        </div>

        <input
          style={input} placeholder="Username" value={username} autoFocus
          autoComplete="username" onChange={(e) => setUsername(e.target.value)}
        />
        <input
          style={input} placeholder="Password" type="password" value={password}
          autoComplete="current-password" onChange={(e) => setPassword(e.target.value)}
        />

        {error && <div style={errorBox}>{error}</div>}

        <button style={button} disabled={busy || !username || !password}>
          {busy ? 'Signing in...' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

const wrap = {
  minHeight: '100vh', display: 'grid', placeItems: 'center',
  background: '#07070f', padding: 24,
}

const card = {
  width: '100%', maxWidth: 360, padding: 32,
  background: 'rgba(255,255,255,.04)', border: '1px solid rgba(255,255,255,.09)',
  borderRadius: 18, color: '#e8e8f2',
}

const input = {
  width: '100%', padding: '12px 14px', marginBottom: 12,
  background: 'rgba(0,0,0,.3)', border: '1px solid rgba(255,255,255,.12)',
  borderRadius: 10, color: '#e8e8f2', fontSize: 15, outline: 'none',
}

const button = {
  width: '100%', marginTop: 8, padding: '13px 20px', border: 'none', borderRadius: 12,
  background: 'linear-gradient(135deg,#8b5cf6 0%,#6366f1 50%,#22d3ee 100%)',
  color: '#fff', fontSize: 15, fontWeight: 700, cursor: 'pointer',
}

const errorBox = {
  background: 'rgba(239,68,68,.12)', border: '1px solid rgba(239,68,68,.3)',
  color: '#fca5a5', borderRadius: 10, padding: '10px 12px',
  fontSize: 13, marginBottom: 4,
}
