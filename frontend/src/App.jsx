import { useEffect, useRef, useState } from 'react'
import { api, runJob, secToMMSS, mmssToSec } from './api'

const RATIOS = [
  { id: '9:16', label: '9:16 TikTok' },
  { id: '1:1', label: '1:1 Insta' },
  { id: '16:9', label: '16:9 YT' },
  { id: null, label: 'Original' },
]

const CAPTION_STYLES = ['TikTok Bold', 'Clean White', 'Yellow Pop', 'Neon']

const REFRAMES = [
  { id: 'smart', label: '🎯 Smart Crop', hint: 'AI keeps the speaker centered' },
  { id: 'fit', label: '🌫️ Fit + Blur', hint: 'Full video on blurred background' },
  { id: 'split', label: '⬆⬇ Split', hint: 'Speaker top, 2nd person / scene bottom' },
  { id: 'center', label: '▣ Center', hint: 'Plain center crop' },
]

export default function App() {
  // global
  const [job, setJob] = useState(null)
  const [toast, setToast] = useState(null)
  const [showSettings, setShowSettings] = useState(false)
  const [hasKey, setHasKey] = useState(false)
  const [keyPreview, setKeyPreview] = useState(null)
  const [packaged, setPackaged] = useState(false)
  const [multiUser, setMultiUser] = useState(false)
  const [proxy, setProxy] = useState('')
  const [cookiesBrowser, setCookiesBrowser] = useState('')
  const [cookiesFile, setCookiesFile] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const fileRef = useRef(null)

  // video
  const [url, setUrl] = useState('')
  const [qualities, setQualities] = useState([])
  const [quality, setQuality] = useState(null)
  const [video, setVideo] = useState(null)

  // shared options (sidebar)
  const [ratio, setRatio] = useState('9:16')
  const [reframeMode, setReframeMode] = useState('smart')
  const [captionsOn, setCaptionsOn] = useState(true)
  const [capStyle, setCapStyle] = useState('TikTok Bold')
  const [wordsPerLine, setWordsPerLine] = useState(4)
  const [numClips, setNumClips] = useState(6)
  const [lenRange, setLenRange] = useState([15, 60])

  // create
  const [tab, setTab] = useState('auto')
  const [mode, setMode] = useState('transcript')
  const [query, setQuery] = useState('')
  const [moments, setMoments] = useState([])
  const [selected, setSelected] = useState(new Set())

  // reel
  const [reelMode, setReelMode] = useState('teaser')
  const [reelTheme, setReelTheme] = useState('')
  const [reelDur, setReelDur] = useState(45)
  const [reelAnalysis, setReelAnalysis] = useState('transcript')

  // manual
  const [manualRows, setManualRows] = useState([{ name: 'clip_1', start: '', end: '' }])

  // results
  const [clips, setClips] = useState([])
  const [editing, setEditing] = useState(null)   // clip index being edited

  const busy = !!job
  const captionOpts = { enabled: captionsOn, style: capStyle, words_per_line: wordsPerLine }

  useEffect(() => {
    api.get('/api/config').then((c) => {
      setHasKey(c.has_key); setKeyPreview(c.key_preview); setPackaged(!!c.packaged)
      setMultiUser(!!c.multi_user)
      setProxy(c.proxy || ''); setCookiesBrowser(c.cookies_browser || ''); setCookiesFile(c.cookies_file || '')
    }).catch(() => {})
    api.get('/api/video').then((v) => { if (v.loaded) setVideo(v) }).catch(() => {})
    api.get('/api/clips').then((r) => setClips(r.clips || [])).catch(() => {})
  }, [])

  function err(text) { setToast({ text, ok: false }); setTimeout(() => setToast(null), 6000) }
  function ok(text) { setToast({ text, ok: true }); setTimeout(() => setToast(null), 3500) }

  async function exec(startPromise, after) {
    try {
      const result = await runJob(startPromise, (j) => setJob(j))
      after?.(result)
    } catch (e) {
      err(e.message)
    } finally {
      setJob(null)
    }
  }

  async function fetchQualities() {
    if (!url.trim()) return err('Enter a video URL first')
    try {
      setJob({ message: 'Fetching available qualities...', progress: 0.3 })
      const r = await api.get(`/api/qualities?url=${encodeURIComponent(url.trim())}`)
      setQualities(r.qualities || [])
      if (r.qualities?.length) setQuality(r.qualities[0])
    } catch (e) { err(e.message) } finally { setJob(null) }
  }

  function download() {
    if (!url.trim()) return err('Enter a video URL first')
    exec(api.post('/api/jobs/download', { url: url.trim(), quality }), (r) => {
      setVideo({ title: r.title, duration: r.duration, stream_url: `/api/video/stream?t=${Date.now()}` })
      setMoments([]); setClips([])
      ok(`Downloaded: ${r.title}`)
    })
  }

  // XHR, not fetch — fetch gives no upload progress, and these files are big.
  function uploadLocal(file) {
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    const xhr = new XMLHttpRequest()
    setJob({ message: `Loading ${file.name}...`, progress: 0 })

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return
      const frac = e.loaded / e.total
      setJob({
        message: frac < 1 ? `Loading ${file.name}... ${Math.round(frac * 100)}%` : 'Reading video...',
        progress: frac * 0.95,
      })
    }
    xhr.onload = () => {
      setJob(null)
      let body = {}
      try { body = JSON.parse(xhr.responseText) } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        setVideo({ title: body.title, duration: body.duration, stream_url: `/api/video/stream?t=${Date.now()}` })
        setMoments([]); setClips([]); setQualities([]); setUrl('')
        ok(`Loaded: ${body.title}`)
      } else {
        err(body.detail || `Upload failed (HTTP ${xhr.status})`)
      }
    }
    xhr.onerror = () => { setJob(null); err('Upload failed') }
    xhr.open('POST', '/api/video/upload')
    xhr.send(form)
  }

  function detect() {
    if (!hasKey) { setShowSettings(true); return err('Set your Gemini API key first') }
    exec(api.post('/api/jobs/detect', {
      mode, query, num_clips: numClips, min_len: lenRange[0], max_len: lenRange[1],
    }), (r) => {
      setMoments(r.moments || [])
      setSelected(new Set((r.moments || []).map((_, i) => i)))
      if (!r.moments?.length) err('No moments found — try a different prompt or mode')
    })
  }

  function cutSelected() {
    const chosen = moments.filter((_, i) => selected.has(i))
    if (!chosen.length) return err('Select at least one clip')
    exec(api.post('/api/jobs/cut', {
      clips: chosen.map((m, i) => ({
        name: m.hook_title || `clip_${i + 1}`,
        start_sec: m.start_sec, end_sec: m.end_sec, meta: m,
      })),
      ratio, reframe: reframeMode, captions: captionOpts,
    }), (r) => { setClips(r.clips); ok(`${r.clips.length} clips created!`) })
  }

  function makeReel() {
    if (!hasKey) { setShowSettings(true); return err('Set your Gemini API key first') }
    exec(api.post('/api/jobs/reel', {
      mode: reelMode, analysis: reelAnalysis, theme: reelTheme,
      target_duration: reelDur, ratio, reframe: reframeMode, captions: captionOpts,
    }), (r) => { setClips(r.clips); ok('Reel is ready!') })
  }

  async function addToDesktop() {
    try {
      await api.post('/api/create-shortcut', {})
      ok('Desktop shortcut ban gaya! ✅')
    } catch (e) {
      err(e.message)
    }
  }

  function cutManual() {
    const parsed = []
    for (const row of manualRows) {
      const s = mmssToSec(row.start), e = mmssToSec(row.end)
      if (s == null || e == null) continue
      if (e <= s) return err(`"${row.name}": end time must be after start time`)
      parsed.push({ name: row.name || `clip_${parsed.length + 1}`, start_sec: s, end_sec: e })
    }
    if (!parsed.length) return err('Enter valid start/end times (MM:SS) in at least one row')
    exec(api.post('/api/jobs/cut', { clips: parsed, ratio, reframe: reframeMode, captions: captionOpts }),
      (r) => { setClips(r.clips); ok(`${r.clips.length} clips created!`) })
  }

  return (
    <div className="layout">
      {toast && <div className={`toast ${toast.ok ? 'ok' : ''}`}>{toast.text}</div>}

      {/* ── SIDEBAR ── */}
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">🎬</div>
          <div>
            <h1>Soft Clipper</h1>
            <span className="tag">Long video → Viral clips</span>
          </div>
        </div>

        {/* the API key is only one of the things in here — proxy and cookies
            live in the same modal, and labelling the button after the key made
            them impossible to find */}
        <button className="btn sm" onClick={() => setShowSettings(true)}
                title="API key, download proxy and YouTube cookies">
          {hasKey ? '⚙️ Settings' : '⚠️ Set API Key'}
        </button>

        {packaged && (
          <button className="btn sm" onClick={addToDesktop} title="Create a shortcut on your Desktop for quick access">
            🖥️ Add to Desktop
          </button>
        )}

        <div className="sidebar-section">
          <span className="sec-title">🎯 Clip Settings</span>
          <div className="side-row">
            <span className="lbl-inline">Number of clips</span>
            <span className="side-val">{numClips}</span>
          </div>
          <input type="range" min={3} max={12} value={numClips} onChange={(e) => setNumClips(+e.target.value)} />
          <div className="side-row">
            <span className="lbl-inline">Min length</span>
            <span className="side-val">{lenRange[0]}s</span>
          </div>
          <input type="range" min={10} max={120} value={lenRange[0]}
            onChange={(e) => setLenRange([Math.min(+e.target.value, lenRange[1] - 5), lenRange[1]])} />
          <div className="side-row">
            <span className="lbl-inline">Max length</span>
            <span className="side-val">{lenRange[1] >= 60 ? `${Math.round(lenRange[1] / 60 * 10) / 10} min` : `${lenRange[1]}s`}</span>
          </div>
          <input type="range" min={20} max={900} value={lenRange[1]}
            onChange={(e) => setLenRange([lenRange[0], Math.max(+e.target.value, lenRange[0] + 5)])} />
        </div>

        <div className="sidebar-section">
          <span className="sec-title">📐 Aspect Ratio</span>
          <div className="pills">
            {RATIOS.map((r) => (
              <button key={r.label} className={`pill ${ratio === r.id ? 'active' : ''}`} onClick={() => setRatio(r.id)}>
                {r.label}
              </button>
            ))}
          </div>
        </div>

        <div className="sidebar-section">
          <span className="sec-title">🎥 Reframe Mode</span>
          <div className="reframe-list">
            {REFRAMES.map((r) => (
              <button key={r.id} className={`reframe-opt ${reframeMode === r.id ? 'active' : ''}`}
                onClick={() => setReframeMode(r.id)}>
                <span className="rf-label">{r.label}</span>
                <span className="rf-hint">{r.hint}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="sidebar-section">
          <span className="sec-title">💬 Captions</span>
          <div className="side-row">
            <span className="lbl-inline">Burn-in captions</span>
            <label className="switch">
              <input type="checkbox" checked={captionsOn} onChange={(e) => setCaptionsOn(e.target.checked)} />
              <span className="track" />
            </label>
          </div>
          {captionsOn && (
            <>
              <select className="select" value={capStyle} onChange={(e) => setCapStyle(e.target.value)}>
                {CAPTION_STYLES.map((s) => <option key={s}>{s}</option>)}
              </select>
              <div className="side-row">
                <span className="lbl-inline">Words per line</span>
                <span className="side-val">{wordsPerLine}</span>
              </div>
              <input type="range" min={2} max={8} value={wordsPerLine} onChange={(e) => setWordsPerLine(+e.target.value)} />
            </>
          )}
        </div>

        {(video || clips.length > 0) && (
          <div className="sidebar-section">
            <span className="sec-title">📊 Status</span>
            <div className="stat-tiles">
              <div className="stat-tile">
                <div className="v">{video ? secToMMSS(video.duration) : '—'}</div>
                <div className="k">Video</div>
              </div>
              <div className="stat-tile">
                <div className="v">{clips.length}</div>
                <div className="k">Clips</div>
              </div>
            </div>
          </div>
        )}
      </aside>

      {/* ── MAIN ── */}
      <main className="main">
        <div className="container">
          <header className="header">
            <div>
              <h1>Studio</h1>
              <div className="sub">Drop in a video, let AI find the viral moments, download TikTok-ready clips</div>
            </div>
            {video && <span className="badge">✓ Video loaded</span>}
          </header>

          {/* step 1: source */}
          <section className="card">
            <h2><span className="step-num">1</span> Video Source</h2>
            <div className="row">
              <div className="grow">
                <input
                  className="input"
                  placeholder="Paste a YouTube / TikTok / Facebook / Dailymotion link..."
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && download()}
                  disabled={busy}
                />
              </div>
              <button className="btn" onClick={fetchQualities} disabled={busy}>Qualities</button>
              {qualities.length > 0 && (
                <select className="select" style={{ width: 105 }} value={quality || ''} onChange={(e) => setQuality(e.target.value)}>
                  {qualities.map((q) => <option key={q} value={q}>{q}</option>)}
                </select>
              )}
              <button className="btn primary" onClick={download} disabled={busy}>⬇ Download</button>
            </div>

            <div
              className="dropzone mt"
              data-over={dragOver ? 'yes' : 'no'}
              onDragOver={(e) => { e.preventDefault(); if (!busy) setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault(); setDragOver(false)
                if (!busy) uploadLocal(e.dataTransfer.files?.[0])
              }}
              onClick={() => !busy && fileRef.current?.click()}
            >
              <div style={{ fontSize: 22 }}>📁</div>
              <div><b>Drop a video here</b> — or click to choose one</div>
              <div className="muted" style={{ fontSize: 13 }}>
                Already have the video on your PC? Use it directly — no link, no download.
              </div>
              <input
                ref={fileRef}
                type="file"
                accept="video/*"
                style={{ display: 'none' }}
                onChange={(e) => { uploadLocal(e.target.files?.[0]); e.target.value = '' }}
              />
            </div>

            {video && (
              <div className="mt">
                <div className="row mb" style={{ justifyContent: 'space-between' }}>
                  <strong>{video.title}</strong>
                  <span className="muted">{secToMMSS(video.duration)}</span>
                </div>
                <video className="player" src={video.stream_url || '/api/video/stream'} controls />
              </div>
            )}
          </section>

          {video && (
            <>
              {/* step 2: create */}
              <section className="card">
                <h2><span className="step-num">2</span> Create Clips</h2>

                <div className="tabs">
                  <button className={`tab ${tab === 'auto' ? 'active' : ''}`} onClick={() => setTab('auto')}>🤖 AI Auto-Detect</button>
                  <button className={`tab ${tab === 'reel' ? 'active' : ''}`} onClick={() => setTab('reel')}>🎞️ Teaser / Reel</button>
                  <button className={`tab ${tab === 'manual' ? 'active' : ''}`} onClick={() => setTab('manual')}>✂️ Manual</button>
                </div>

                {tab === 'auto' && (
                  <>
                    <div className="mb">
                      <label className="lbl">ANALYSIS MODE</label>
                      <div className="pills">
                        <button className={`pill ${mode === 'transcript' ? 'active' : ''}`} onClick={() => setMode('transcript')}>📝 Transcript (talking videos)</button>
                        <button className={`pill ${mode === 'visual' ? 'active' : ''}`} onClick={() => setMode('visual')}>👁️ Visual (songs / sports / gameplay)</button>
                      </div>
                    </div>
                    <div className="row">
                      <div className="grow">
                        <input className="input" placeholder='🔍 Looking for something specific? (optional) — e.g. "funny moments", "every goal"'
                          value={query} onChange={(e) => setQuery(e.target.value)} disabled={busy} />
                      </div>
                      <button className="btn primary" onClick={detect} disabled={busy}>✨ Detect Moments</button>
                    </div>

                    {moments.length > 0 && (
                      <div className="mt">
                        {moments.map((m, i) => (
                          <div key={i} className={`moment ${selected.has(i) ? 'selected' : ''}`}>
                            <input type="checkbox" className="checkbox" checked={selected.has(i)}
                              onChange={(e) => {
                                const next = new Set(selected)
                                e.target.checked ? next.add(i) : next.delete(i)
                                setSelected(next)
                              }} />
                            <div className="score-ring" style={{ '--pct': m.virality_score }}>
                              <span>{m.virality_score}</span>
                            </div>
                            <div className="info">
                              <div className="hook">{m.hook_title}</div>
                              <div className="times">{secToMMSS(m.start_sec)} → {secToMMSS(m.end_sec)} ({Math.round(m.end_sec - m.start_sec)}s)</div>
                              <div className="reason">{m.reason}</div>
                              {m.hashtags?.length > 0 && (
                                <div className="chips">{m.hashtags.map((h, j) => <span key={j} className="chip">{h}</span>)}</div>
                              )}
                            </div>
                          </div>
                        ))}
                        <button className="btn primary mt" style={{ width: '100%' }} onClick={cutSelected} disabled={busy}>
                          ✂️ Cut {selected.size} Selected Clips
                        </button>
                      </div>
                    )}
                  </>
                )}

                {tab === 'reel' && (
                  <>
                    <div className="grid2 mb">
                      <div>
                        <label className="lbl">TYPE</label>
                        <div className="pills">
                          <button className={`pill ${reelMode === 'teaser' ? 'active' : ''}`} onClick={() => setReelMode('teaser')}>🎬 Teaser</button>
                          <button className={`pill ${reelMode === 'highlight' ? 'active' : ''}`} onClick={() => setReelMode('highlight')}>⭐ Highlight Reel</button>
                        </div>
                      </div>
                      <div>
                        <label className="lbl">ANALYSIS</label>
                        <div className="pills">
                          <button className={`pill ${reelAnalysis === 'transcript' ? 'active' : ''}`} onClick={() => setReelAnalysis('transcript')}>📝 Transcript</button>
                          <button className={`pill ${reelAnalysis === 'visual' ? 'active' : ''}`} onClick={() => setReelAnalysis('visual')}>👁️ Visual</button>
                        </div>
                      </div>
                    </div>
                    <div className="grid2 mb">
                      {reelMode === 'highlight' ? (
                        <div>
                          <label className="lbl">THEME (OPTIONAL)</label>
                          <input className="input" placeholder='e.g. "only goals", "funny moments"' value={reelTheme}
                            onChange={(e) => setReelTheme(e.target.value)} />
                        </div>
                      ) : <div className="muted" style={{ alignSelf: 'end', paddingBottom: 10 }}>Teaser stitches the best hooks to build curiosity — never reveals the ending 🤫</div>}
                      <div>
                        <label className="lbl">TARGET LENGTH: {reelDur}s</label>
                        <input type="range" min={15} max={120} value={reelDur} onChange={(e) => setReelDur(+e.target.value)} />
                      </div>
                    </div>
                    <button className="btn primary" style={{ width: '100%' }} onClick={makeReel} disabled={busy}>
                      🎞️ Create {reelMode === 'teaser' ? 'Teaser' : 'Highlight Reel'}
                    </button>
                  </>
                )}

                {tab === 'manual' && (
                  <>
                    {manualRows.map((row, i) => (
                      <div key={i} className="row mb">
                        <div className="grow">
                          <input className="input" placeholder="Clip name" value={row.name}
                            onChange={(e) => setManualRows(manualRows.map((r, j) => j === i ? { ...r, name: e.target.value } : r))} />
                        </div>
                        <input className="input" style={{ width: 110 }} placeholder="Start 0:30" value={row.start}
                          onChange={(e) => setManualRows(manualRows.map((r, j) => j === i ? { ...r, start: e.target.value } : r))} />
                        <input className="input" style={{ width: 110 }} placeholder="End 1:45" value={row.end}
                          onChange={(e) => setManualRows(manualRows.map((r, j) => j === i ? { ...r, end: e.target.value } : r))} />
                        <button className="btn sm ghost" onClick={() => setManualRows(manualRows.filter((_, j) => j !== i))}
                          disabled={manualRows.length === 1}>✕</button>
                      </div>
                    ))}
                    <div className="row">
                      <button className="btn sm" onClick={() => setManualRows([...manualRows, { name: `clip_${manualRows.length + 1}`, start: '', end: '' }])}>
                        + Row
                      </button>
                      <div className="grow" />
                      <button className="btn primary" onClick={cutManual} disabled={busy}>✂️ Cut Clips</button>
                    </div>
                  </>
                )}
              </section>

              {/* step 3: results */}
              {clips.length > 0 && (
                <section className="card">
                  <h2 style={{ justifyContent: 'space-between' }}>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span className="step-num">3</span> Generated Clips ({clips.length})
                    </span>
                    <a className="btn sm" href="/api/clips/zip" download>📦 Download All (ZIP)</a>
                  </h2>
                  <div className="clips-grid">
                    {clips.map((c, i) => (
                      <div key={i} className="clip-card">
                        <video src={c.url} controls preload="metadata" />
                        <div className="body">
                          <div className="name">{c.name}</div>
                          <div className="size">{c.size_mb} MB</div>
                          {c.meta?.caption && <div className="caption-text">{c.meta.caption}</div>}
                          {c.meta?.hashtags?.length > 0 && (
                            <div className="chips">{c.meta.hashtags.slice(0, 4).map((h, j) => <span key={j} className="chip">{h}</span>)}</div>
                          )}
                          <div className="row mt">
                            <a className="btn sm grow" style={{ textAlign: 'center', textDecoration: 'none' }} href={c.url} download>⬇ Download</a>
                            {c.render && (
                              <button className="btn sm" title="Fix / edit this clip" onClick={() => setEditing(i)}>✏️</button>
                            )}
                            {(c.meta?.caption || c.meta?.hashtags?.length > 0) && (
                              <button className="btn sm" title="Copy caption + hashtags"
                                onClick={() => {
                                  navigator.clipboard.writeText(`${c.meta.caption || ''}\n\n${(c.meta.hashtags || []).join(' ')}`.trim())
                                  ok('Caption copied!')
                                }}>📋</button>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </main>

      {/* progress banner */}
      {job && (
        <div className="progress-banner">
          <div className="msg"><span className="spinner" /> {job.message}</div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${Math.max(4, (job.progress || 0) * 100)}%` }} />
          </div>
        </div>
      )}

      {/* settings modal */}
      {showSettings && (
        <SettingsModal
          hasKey={hasKey}
          keyPreview={keyPreview}
          multiUser={multiUser}
          initialProxy={proxy}
          initialCookiesBrowser={cookiesBrowser}
          initialCookiesFile={cookiesFile}
          onClose={() => setShowSettings(false)}
          onSaved={({ preview, proxy: savedProxy, cookiesBrowser: savedCB, cookiesFile: savedCF }) => {
            if (preview) setKeyPreview(preview)
            setHasKey(true)
            setProxy(savedProxy)
            setCookiesBrowser(savedCB)
            setCookiesFile(savedCF)
            setShowSettings(false)
            ok('Settings saved!')
          }}
          onError={err}
        />
      )}

      {/* edit clip modal */}
      {editing !== null && clips[editing] && (
        <EditModal
          clip={clips[editing]}
          index={editing}
          busy={busy}
          onClose={() => setEditing(null)}
          onManual={(payload) => {
            setEditing(null)
            exec(api.post('/api/jobs/edit', payload), (r) => { setClips(r.clips); ok('Clip re-rendered!') })
          }}
          onAi={(instruction) => {
            setEditing(null)
            exec(api.post('/api/jobs/ai_edit', { index: editing, instruction }), (r) => {
              setClips(r.clips)
              ok(r.explanation ? `✨ ${r.explanation}` : 'Clip fixed!')
            })
          }}
        />
      )}
    </div>
  )
}

function EditModal({ clip, index, busy, onClose, onManual, onAi }) {
  const r = clip.render
  const [tab, setTab] = useState('ai')
  const [instruction, setInstruction] = useState('')
  const [name, setName] = useState(clip.name)
  const [segments, setSegments] = useState(
    r.segments.map((s) => ({ start: secToMMSS(s.start_sec), end: secToMMSS(s.end_sec) }))
  )
  const [ratio, setRatio] = useState(r.ratio)
  const [reframe, setReframe] = useState(r.reframe || 'smart')
  const [capOn, setCapOn] = useState(!!r.captions?.enabled)
  const [capStyle, setCapStyle] = useState(r.captions?.style || 'TikTok Bold')
  const [words, setWords] = useState(r.captions?.words_per_line || 4)

  function applyManual() {
    const segs = []
    for (const s of segments) {
      const st = mmssToSec(s.start), en = mmssToSec(s.end)
      if (st == null || en == null || en <= st) continue
      segs.push({ start_sec: st, end_sec: en })
    }
    if (!segs.length) return
    onManual({
      index, name, segments: segs, ratio, reframe,
      captions: { enabled: capOn, style: capStyle, words_per_line: words },
    })
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 'min(560px, calc(100vw - 40px))' }} onClick={(e) => e.stopPropagation()}>
        <h3>✏️ Fix clip: {clip.name}</h3>

        <div className="tabs" style={{ marginTop: 12 }}>
          <button className={`tab ${tab === 'ai' ? 'active' : ''}`} onClick={() => setTab('ai')}>✨ AI Fix</button>
          <button className={`tab ${tab === 'manual' ? 'active' : ''}`} onClick={() => setTab('manual')}>🛠️ Manual</button>
        </div>

        {tab === 'ai' && (
          <>
            <p>Tell the AI what's wrong — it will adjust the clip and re-render it.</p>
            <textarea
              className="input" rows={3} autoFocus
              placeholder={'e.g. "start 5 seconds earlier"\n"the speaker is cut off, fix the framing"\n"clip is too long, keep only the main point"'}
              value={instruction} onChange={(e) => setInstruction(e.target.value)}
            />
            <div className="row mt" style={{ justifyContent: 'flex-end' }}>
              <button className="btn" onClick={onClose}>Cancel</button>
              <button className="btn primary" disabled={busy || !instruction.trim()} onClick={() => onAi(instruction.trim())}>
                ✨ Fix with AI
              </button>
            </div>
          </>
        )}

        {tab === 'manual' && (
          <>
            <label className="lbl">CLIP NAME</label>
            <input className="input mb" value={name} onChange={(e) => setName(e.target.value)} />

            <label className="lbl">SEGMENTS (source video time, MM:SS)</label>
            {segments.map((s, i) => (
              <div key={i} className="row mb">
                <input className="input" placeholder="Start" value={s.start}
                  onChange={(e) => setSegments(segments.map((x, j) => j === i ? { ...x, start: e.target.value } : x))} />
                <span className="muted">→</span>
                <input className="input" placeholder="End" value={s.end}
                  onChange={(e) => setSegments(segments.map((x, j) => j === i ? { ...x, end: e.target.value } : x))} />
                {segments.length > 1 && (
                  <button className="btn sm ghost" onClick={() => setSegments(segments.filter((_, j) => j !== i))}>✕</button>
                )}
              </div>
            ))}

            <div className="grid2 mb">
              <div>
                <label className="lbl">ASPECT RATIO</label>
                <select className="select" value={ratio ?? 'original'}
                  onChange={(e) => setRatio(e.target.value === 'original' ? null : e.target.value)}>
                  <option value="9:16">9:16 TikTok</option>
                  <option value="1:1">1:1 Insta</option>
                  <option value="16:9">16:9 YouTube</option>
                  <option value="original">Original</option>
                </select>
              </div>
              <div>
                <label className="lbl">REFRAME</label>
                <select className="select" value={reframe} onChange={(e) => setReframe(e.target.value)}>
                  <option value="smart">🎯 Smart Crop</option>
                  <option value="fit">🌫️ Fit + Blur</option>
                  <option value="split">⬆⬇ Split</option>
                  <option value="center">▣ Center</option>
                </select>
              </div>
            </div>

            <div className="row mb">
              <label className="switch">
                <input type="checkbox" checked={capOn} onChange={(e) => setCapOn(e.target.checked)} />
                <span className="track" />
              </label>
              <span className="muted">Captions</span>
              {capOn && (
                <>
                  <select className="select" style={{ width: 140 }} value={capStyle} onChange={(e) => setCapStyle(e.target.value)}>
                    {['TikTok Bold', 'Clean White', 'Yellow Pop', 'Neon'].map((s) => <option key={s}>{s}</option>)}
                  </select>
                  <input type="range" min={2} max={8} value={words} style={{ width: 80 }} onChange={(e) => setWords(+e.target.value)} />
                  <span className="muted">{words} w/line</span>
                </>
              )}
            </div>

            <div className="row" style={{ justifyContent: 'flex-end' }}>
              <button className="btn" onClick={onClose}>Cancel</button>
              <button className="btn primary" disabled={busy} onClick={applyManual}>🛠️ Re-render</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function SettingsModal({ hasKey, keyPreview, multiUser, initialProxy, initialCookiesBrowser, initialCookiesFile,
                        onClose, onSaved, onError }) {
  const [key, setKey] = useState('')
  const [proxy, setProxy] = useState(initialProxy || '')
  const [cookiesBrowser, setCookiesBrowser] = useState(initialCookiesBrowser || '')
  const [cookiesFile, setCookiesFile] = useState(initialCookiesFile || '')
  const inputRef = useRef(null)
  useEffect(() => inputRef.current?.focus(), [])

  async function save() {
    // key can be left blank to keep the existing one; must exist one way or another
    if (!key.trim() && !hasKey) return onError('Set your Gemini API key first')
    try {
      const body = {
        proxy: proxy.trim(),
        cookies_browser: cookiesBrowser.trim(),
        cookies_file: cookiesFile.trim(),
      }
      if (key.trim()) body.api_key = key.trim()
      await api.post('/api/config', body)
      onSaved({
        preview: key.trim() ? `...${key.trim().slice(-4)}` : null,
        proxy: proxy.trim(),
        cookiesBrowser: cookiesBrowser.trim(),
        cookiesFile: cookiesFile.trim(),
      })
    } catch (e) { onError(e.message) }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2 style={{ marginBottom: 4 }}>⚙️ Settings</h2>

        <h3 style={{ marginTop: 14 }}>🔑 Google Gemini API Key</h3>
        <p>
          Required for AI features (viral detection, transcription, teaser/reel).
          Get a free key at{' '}
          <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">aistudio.google.com</a>
          <br />{multiUser
            ? 'Your key is kept on the server for your account only, and is cleared when the app restarts.'
            : 'Your key is stored locally on your PC (config.json).'}
          {hasKey && <><br /><span style={{ opacity: 0.7 }}>
            A key is already saved{keyPreview ? ` (${keyPreview})` : ''} — leave blank to keep it.
          </span></>}
        </p>
        <input ref={inputRef} className="input mb" type="password" placeholder="AIza..." value={key}
          onChange={(e) => setKey(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && save()} />

        {/* Proxy and cookies are per-machine fixes: on the desktop the user's own
            ISP is what blocks YouTube. On the server every download leaves from
            one IP, so these belong to whoever runs it — showing them here would
            invite ten people to buy ten proxies, or to quietly break their own
            downloads with a bad one. */}
        {multiUser ? (
          <p style={{ marginTop: 18, opacity: 0.7, fontSize: 13 }}>
            Downloads are handled by the server, so proxy and cookie settings are
            configured there rather than per account.
          </p>
        ) : (<>
        <h3 style={{ marginTop: 18 }}>🌐 Download Proxy (optional)</h3>
        <p>
          If your ISP blocks YouTube/TikTok and downloads fail with a network error,
          put a proxy here to route downloads through it — no system-wide VPN needed.
          <br />Format: <code>http://host:port</code>, <code>socks5://host:port</code>, or with login{' '}
          <code>http://user:pass@host:port</code>. Leave blank to use your normal connection.
        </p>
        <input className="input mb" type="text" placeholder="socks5://127.0.0.1:1080" value={proxy}
          onChange={(e) => setProxy(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && save()} />

        <h3 style={{ marginTop: 18 }}>🍪 YouTube Cookies (fixes 403 errors)</h3>
        <p>
          If downloads fail with <b>HTTP Error 403</b>, YouTube is treating the app as a bot —
          common on shared VPN IPs. Pick a browser you're <b>signed in to YouTube</b> with, and
          downloads will look like your normal browsing.
          <br />Tip: Chrome 127+ encrypts its cookies, so <b>Firefox works best</b>. If your browser
          doesn't work, export a <code>cookies.txt</code> (via a "Get cookies.txt" extension) and use the box below.
          <br /><span style={{ opacity: 0.7 }}>Using a throwaway YouTube account is safer than your main one.</span>
        </p>
        <select className="input mb" value={cookiesBrowser} onChange={(e) => setCookiesBrowser(e.target.value)}>
          <option value="">Don't use browser cookies</option>
          <option value="firefox">Firefox (recommended)</option>
          <option value="chrome">Chrome</option>
          <option value="edge">Edge</option>
          <option value="brave">Brave</option>
          <option value="opera">Opera</option>
          <option value="vivaldi">Vivaldi</option>
          <option value="chromium">Chromium</option>
        </select>
        <input className="input mb" type="text" placeholder="Or full path to cookies.txt (overrides the browser above)"
          value={cookiesFile} onChange={(e) => setCookiesFile(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && save()} />
        </>)}

        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={save}>Save</button>
        </div>
      </div>
    </div>
  )
}
