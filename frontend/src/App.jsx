import { useEffect, useRef, useState } from 'react'
import { api, cancelJob, runJob, secToMMSS, secToClock, mmssToSec } from './api'

const RATIOS = [
  { id: '9:16', label: '9:16 TikTok' },
  { id: '1:1', label: '1:1 Insta' },
  { id: '16:9', label: '16:9 YT' },
  { id: null, label: 'Original' },
]

const CAPTION_STYLES = ['TikTok Bold', 'Clean White', 'Yellow Pop', 'Neon', 'Bounce', 'Boxed']

// Mirrors core/silence.py LENGTHS
const SPLIT_LENGTHS = [30, 45, 60, 90, 120]

const REFRAMES = [
  { id: 'smart', label: '🎯 Smart Crop', hint: 'AI keeps the speaker centered' },
  { id: 'fit', label: '🌫️ Fit + Blur', hint: 'Full video on blurred background' },
  { id: 'split', label: '⬆⬇ Split', hint: 'Speaker top, 2nd person / scene bottom' },
  { id: 'center', label: '▣ Center', hint: 'Plain center crop' },
  { id: 'manual', label: '✋ Manual Frame', hint: 'Place the crop yourself in the editor' },
]

// aspect ratio as width/height, for drawing the crop box over the source video
const RATIO_AR = { '9:16': 9 / 16, '1:1': 1, '16:9': 16 / 9 }

const FRAME = 1 / 30   // one frame step at 30fps — fine for trimming by eye

const clamp01 = (v) => Math.max(0, Math.min(1, v))

// Look presets — labels for the editor, plus a CSS approximation of what ffmpeg
// bakes on export. The keys must match core/effects.py LOOKS; the CSS is only a
// live preview, so it's close, not pixel-exact.
const LOOKS = [
  { id: 'none', label: 'None', css: 'none' },
  { id: 'warm', label: '🔥 Warm', css: 'sepia(.2) saturate(1.12) hue-rotate(-8deg)' },
  { id: 'cold', label: '❄️ Cold', css: 'saturate(1.06) hue-rotate(10deg) brightness(1.02)' },
  { id: 'vintage', label: '📼 Vintage', css: 'sepia(.38) contrast(.95) saturate(.82)' },
  { id: 'bw', label: '⬛ B&W', css: 'grayscale(1)' },
  { id: 'cinematic', label: '🎬 Cinematic', css: 'contrast(1.1) saturate(.9) brightness(.98)' },
  { id: 'vivid', label: '⚡ Vivid', css: 'saturate(1.4) contrast(1.06)' },
]

const DEFAULT_EFFECTS = {
  mirror: false, brightness: 0, contrast: 1, saturation: 1, look: 'none', speed: 1,
}

// Text-overlay colours. Keys match core/captions.py OVERLAY_COLORS; the hex here
// is just the live-preview swatch, close to what libass burns.
const OVERLAY_COLORS = {
  white: '#ffffff', black: '#000000', yellow: '#ffe600', red: '#ff2d2d',
  green: '#22c55e', blue: '#3c6bff', pink: '#ff69b4', cyan: '#00e5ff',
}
const overlayCss = (c) => (c && c.startsWith('#') ? c : OVERLAY_COLORS[c] || '#ffffff')

const newOverlay = () => ({
  id: (crypto.randomUUID?.() || String(Math.random())).slice(0, 8),
  text: 'Text', x: 0.5, y: 0.35, size: 22, color: 'white',
})

// The CSS `filter` string that mirrors the baked effects for live preview.
// eq brightness is additive (-.5...5) but CSS brightness() is multiplicative,
// so map it to 1 + brightness; the rest line up directly.
function effectsToCss(e) {
  const look = LOOKS.find((l) => l.id === e.look)?.css
  const parts = []
  if (look && look !== 'none') parts.push(look)
  if (e.brightness) parts.push(`brightness(${(1 + e.brightness).toFixed(3)})`)
  if (e.contrast !== 1) parts.push(`contrast(${e.contrast})`)
  if (e.saturation !== 1) parts.push(`saturate(${e.saturation})`)
  return parts.length ? parts.join(' ') : 'none'
}

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
  const mainVideoRef = useRef(null)

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
  const [capHighlight, setCapHighlight] = useState(false)
  const [wordsPerLine, setWordsPerLine] = useState(4)
  const [headlineOn, setHeadlineOn] = useState(false)
  const [headlineText, setHeadlineText] = useState('')
  const [headlineStyle, setHeadlineStyle] = useState('box')
  const [headlinePos, setHeadlinePos] = useState('top')
  const [headlineSize, setHeadlineSize] = useState(20)
  const [crop, setCrop] = useState({ cx: 0.5, cy: 0.5, zoom: 1 })
  // look effects + text overlays applied to EVERY generated clip (edit once here,
  // fine-tune any single clip later in its own editor)
  const [effects, setEffects] = useState({ ...DEFAULT_EFFECTS })
  const setFx = (patch) => setEffects((e) => ({ ...e, ...patch }))
  const [overlays, setOverlays] = useState([])
  const [styleOpen, setStyleOpen] = useState(false)
  const moveOverlay = (id, x, y) =>
    setOverlays((list) => list.map((o) => (o.id === id ? { ...o, x, y } : o)))
  const [numClips, setNumClips] = useState(6)
  const [lenRange, setLenRange] = useState([15, 60])

  // create
  const [tab, setTab] = useState('auto')
  const [mode, setMode] = useState('transcript')
  const [splitLen, setSplitLen] = useState(60)
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
  // one-line summary of the active global look, shown on the collapsed panel
  const styleSummary = [
    effects.look !== 'none' && LOOKS.find((l) => l.id === effects.look)?.label,
    effects.mirror && 'Mirror',
    effects.speed !== 1 && `${effects.speed}×`,
    (effects.brightness !== 0 || effects.contrast !== 1 || effects.saturation !== 1) && 'Adjusted',
    overlays.length > 0 && `${overlays.length} text`,
  ].filter(Boolean).join(' · ')
  const captionOpts = { enabled: captionsOn, style: capStyle, words_per_line: wordsPerLine, highlight: capHighlight }
  const headlineOpts = {
    enabled: headlineOn, text: headlineText, style: headlineStyle,
    position: headlinePos, size: headlineSize,
  }
  // every render request carries the same look settings, so all clips share the
  // global style; the per-clip editor overrides any of these for one clip
  const lookOpts = {
    ratio, reframe: reframeMode, captions: captionOpts, headline: headlineOpts, crop,
    effects,
    overlays: overlays.map(({ text, x, y, size, color }) => ({ text, x, y, size, color })),
  }

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
      if (e.cancelled) ok('Stopped — nothing was created')
      else err(e.message)
    } finally {
      setJob(null)
    }
  }

  // Stop whatever is running: an upload is aborted here, a server job is asked
  // to stop and then ends on its own (runJob sees the 'cancelled' status).
  async function cancelCurrent() {
    if (!job) return
    if (job.xhr) {
      job.xhr.abort()
      setJob(null)
      ok('Stopped — nothing was created')
      return
    }
    if (!job.id) return
    setJob({ ...job, message: 'Cancelling...' })
    try { await cancelJob(job.id) } catch { /* job may have just finished */ }
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
    // xhr rides along in the job so the Cancel button can abort the upload
    setJob({ message: `Loading ${file.name}...`, progress: 0, xhr })

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return
      const frac = e.loaded / e.total
      setJob({
        message: frac < 1 ? `Loading ${file.name}... ${Math.round(frac * 100)}%` : 'Reading video...',
        progress: frac * 0.95,
        xhr,
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
    xhr.onabort = () => setJob(null)     // cancelCurrent() already told the user
    xhr.open('POST', '/api/video/upload')
    xhr.send(form)
  }

  // Fixed-length splitting deliberately skips the API-key check — this is the
  // one path that works with no AI account at all.
  function split() {
    exec(api.post('/api/jobs/split', { length: splitLen }), (r) => {
      setMoments(r.moments || [])
      setSelected(new Set((r.moments || []).map((_, i) => i)))
      if (!r.moments?.length) err('Could not split this video — try a shorter clip length')
    })
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
      ...lookOpts,
    }), (r) => { setClips(r.clips); ok(`${r.clips.length} clips created!`) })
  }

  function makeReel() {
    if (!hasKey) { setShowSettings(true); return err('Set your Gemini API key first') }
    exec(api.post('/api/jobs/reel', {
      mode: reelMode, analysis: reelAnalysis, theme: reelTheme,
      target_duration: reelDur, ...lookOpts,
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
    exec(api.post('/api/jobs/cut', { clips: parsed, ...lookOpts }),
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
              <label className="side-row" style={{ cursor: 'pointer' }}>
                <span className="lbl-inline">Highlight each word</span>
                <input type="checkbox" checked={capHighlight}
                  onChange={(e) => setCapHighlight(e.target.checked)} />
              </label>
            </>
          )}
        </div>

        <div className="sidebar-section">
          <span className="sec-title">🏷️ Headline</span>
          <div className="side-row">
            <span className="lbl-inline">Title on video</span>
            <label className="switch">
              <input type="checkbox" checked={headlineOn} onChange={(e) => setHeadlineOn(e.target.checked)} />
              <span className="track" />
            </label>
          </div>
          {headlineOn && (
            <>
              <input
                className="input" placeholder="Custom headline (blank = AI title)"
                value={headlineText} onChange={(e) => setHeadlineText(e.target.value)}
              />
              <div className="hint">
                Leave it blank and each clip gets its own AI hook title. Type here to force the same
                headline on every clip.
              </div>
              <div className="pills">
                <button className={`pill ${headlineStyle === 'box' ? 'active' : ''}`} onClick={() => setHeadlineStyle('box')}>▬ Box</button>
                <button className={`pill ${headlineStyle === 'plain' ? 'active' : ''}`} onClick={() => setHeadlineStyle('plain')}>A Plain</button>
              </div>
              <div className="pills">
                <button className={`pill ${headlinePos === 'top' ? 'active' : ''}`} onClick={() => setHeadlinePos('top')}>⬆ Top</button>
                <button className={`pill ${headlinePos === 'bottom' ? 'active' : ''}`} onClick={() => setHeadlinePos('bottom')}>⬇ Bottom</button>
              </div>
              <div className="side-row">
                <span className="lbl-inline">Text size</span>
                <span className="side-val">{headlineSize}</span>
              </div>
              <input type="range" min={12} max={32} value={headlineSize} onChange={(e) => setHeadlineSize(+e.target.value)} />
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
                <FrameStage
                  videoRef={mainVideoRef}
                  src={video.stream_url || '/api/video/stream'}
                  ratio={ratio}
                  crop={crop}
                  setCrop={setCrop}
                  headline={headlineOpts}
                  headlineFallback="Your clip headline"
                  manual={reframeMode === 'manual'}
                  effects={effects}
                  overlays={overlays}
                  moveOverlay={moveOverlay}
                />
                {reframeMode === 'manual' && (
                  <div className="hint mt">
                    ✋ Manual frame: drag inside the video to place the crop window, zoom below.
                    Every clip is cut with this framing — per-clip tweaks live in ✏️ Edit.
                  </div>
                )}
                {reframeMode === 'manual' && RATIO_AR[ratio] && (
                  <ZoomSlider crop={crop} setCrop={setCrop} />
                )}

                {/* set the look ONCE for every clip; each clip can still be tweaked
                    on its own later with ✏️ Edit */}
                <details className="style-all mt" open={styleOpen}
                  onToggle={(e) => setStyleOpen(e.target.open)}>
                  <summary>🎨 Style all clips {styleSummary && <span className="muted">— {styleSummary}</span>}</summary>
                  <div className="hint mb">
                    These apply to <b>every clip</b> you generate. Speed up, add a look,
                    mirror, and drop a watermark / text on the video above — then create your clips.
                  </div>
                  <EffectsPanel effects={effects} setFx={setFx} />
                  <OverlaysPanel overlays={overlays} setOverlays={setOverlays} />
                </details>
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
                        <button className={`pill ${mode === 'split' ? 'active' : ''}`} onClick={() => setMode('split')}>✂️ Fixed length (no AI)</button>
                      </div>
                    </div>

                    {mode === 'split' ? (
                      <div className="row">
                        <div className="grow">
                          <label className="lbl">CLIP LENGTH</label>
                          <div className="pills">
                            {SPLIT_LENGTHS.map((s) => (
                              <button key={s} className={`pill ${splitLen === s ? 'active' : ''}`}
                                onClick={() => setSplitLen(s)}>{s}s</button>
                            ))}
                          </div>
                          <span className="muted small">
                            Cuts the whole video into {splitLen}-second clips, each ending on a pause.
                            No API key needed.
                          </span>
                        </div>
                        <button className="btn primary" onClick={split} disabled={busy}>✂️ Split Video</button>
                      </div>
                    ) : (
                      <div className="row">
                        <div className="grow">
                          <input className="input" placeholder='🔍 Looking for something specific? (optional) — e.g. "funny moments", "every goal"'
                            value={query} onChange={(e) => setQuery(e.target.value)} disabled={busy} />
                        </div>
                        <button className="btn primary" onClick={detect} disabled={busy}>✨ Detect Moments</button>
                      </div>
                    )}

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
          <div className="msg">
            <span className="spinner" /> {job.message}
            {(job.id || job.xhr) && (
              <button
                className="btn sm danger cancel-btn"
                onClick={cancelCurrent}
                disabled={job.cancelled}
              >
                {job.cancelled ? 'Stopping...' : '✕ Cancel'}
              </button>
            )}
          </div>
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
          duration={video?.duration || 0}
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

/** Source video with a draggable crop window and a live headline preview.
 *
 *  The box is drawn from the same numbers ffmpeg will crop with, so what you
 *  frame here is what the clip renders — no guessing between UI and output.
 */
/** A draggable text overlay shown on the stage. Position is stored 0..1 of the
 *  reference frame (the crop box when manual framing is on, else the whole
 *  video), so what you drag here is where ffmpeg burns it. */
function OverlayChip({ ov, onMove }) {
  const dragging = useRef(false)
  function move(e) {
    const rect = e.currentTarget.offsetParent?.getBoundingClientRect()
    if (!rect) return
    onMove(ov.id, clamp01((e.clientX - rect.left) / rect.width),
      clamp01((e.clientY - rect.top) / rect.height))
  }
  return (
    <div className="ov-chip" title="Drag to move"
      style={{
        left: `${ov.x * 100}%`, top: `${ov.y * 100}%`,
        color: overlayCss(ov.color), fontSize: `${(ov.size / 288) * 100}cqh`,
      }}
      onPointerDown={(e) => {
        e.stopPropagation()
        e.currentTarget.setPointerCapture(e.pointerId)
        dragging.current = true
        move(e)
      }}
      onPointerMove={(e) => { if (dragging.current) { e.stopPropagation(); move(e) } }}
      onPointerUp={() => { dragging.current = false }}
      onPointerCancel={() => { dragging.current = false }}
    >{ov.text || 'Text'}</div>
  )
}

function FrameStage({ videoRef, src, ratio, crop, setCrop, headline, headlineFallback,
                      manual, effects, overlays, moveOverlay, onTimeUpdate, onReady }) {
  const [dims, setDims] = useState({ w: 0, h: 0 })
  const stageRef = useRef(null)
  const dragging = useRef(false)
  const ar = RATIO_AR[ratio]
  const active = manual && !!ar && dims.w > 0 && dims.h > 0

  let fracW = 1, fracH = 1
  if (active) {
    const wide = Math.min(dims.w, dims.h * ar) / Math.max(1, crop.zoom || 1)
    const high = Math.min(dims.h, wide / ar)
    fracW = Math.min(1, (high * ar) / dims.w)
    fracH = Math.min(1, high / dims.h)
  }
  const left = Math.max(0, Math.min(crop.cx - fracW / 2, 1 - fracW))
  const top = Math.max(0, Math.min(crop.cy - fracH / 2, 1 - fracH))

  function moveTo(e) {
    const rect = stageRef.current?.getBoundingClientRect()
    if (!rect) return
    setCrop({
      ...crop,
      cx: clamp01((e.clientX - rect.left) / rect.width),
      cy: clamp01((e.clientY - rect.top) / rect.height),
    })
  }

  const headText = (headline?.text || '').trim() || headlineFallback || ''
  const headlineEl = headline?.enabled && headText && (
    <div className={`hl-preview ${headline.style} ${headline.position}`}
      style={{ fontSize: `${(headline.size / 288) * 100}cqh` }}>
      <span>{headText}</span>
    </div>
  )
  // headline + overlays share the same reference box so both preview where they burn
  const burnEls = (
    <>
      {headlineEl}
      {(overlays || []).map((ov) => (
        <OverlayChip key={ov.id} ov={ov} onMove={moveOverlay} />
      ))}
    </>
  )

  return (
    <div className="stage" ref={stageRef}
      style={{ aspectRatio: dims.w ? `${dims.w} / ${dims.h}` : '16 / 9' }}>
      <video
        ref={videoRef} src={src} className="stage-video" controls={!active}
        // live look preview: filters + mirror flip. Applied to the video only,
        // so the headline overlay stays upright and readable, exactly as ffmpeg
        // burns it after the flip.
        style={{
          filter: effects ? effectsToCss(effects) : 'none',
          transform: effects?.mirror ? 'scaleX(-1)' : 'none',
        }}
        onLoadedMetadata={(e) => {
          setDims({ w: e.target.videoWidth, h: e.target.videoHeight })
          onReady?.(e)
        }}
        onTimeUpdate={onTimeUpdate}
      />
      {!active && burnEls}
      {active && (
        <div
          className="crop-layer"
          onPointerDown={(e) => {
            e.currentTarget.setPointerCapture(e.pointerId)
            dragging.current = true
            moveTo(e)
          }}
          onPointerMove={(e) => dragging.current && moveTo(e)}
          onPointerUp={() => { dragging.current = false }}
          onPointerCancel={() => { dragging.current = false }}
        >
          <div className="crop-box" style={{
            left: `${left * 100}%`, top: `${top * 100}%`,
            width: `${fracW * 100}%`, height: `${fracH * 100}%`,
          }}>
            {burnEls}
          </div>
        </div>
      )}
    </div>
  )
}

/** Look effects: mirror, colour adjust, preset looks, speed. Everything here
 *  previews live on the stage video (CSS + playbackRate) and is baked by ffmpeg
 *  only on Re-render, so dragging a slider costs the server nothing. */
function EffectsPanel({ effects, setFx }) {
  const changed =
    effects.mirror || effects.look !== 'none' || effects.speed !== 1 ||
    effects.brightness !== 0 || effects.contrast !== 1 || effects.saturation !== 1

  return (
    <>
      <label className="lbl" style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span>EFFECTS</span>
        {changed && (
          <button className="btn sm ghost" onClick={() => setFx({ ...DEFAULT_EFFECTS })}>Reset all</button>
        )}
      </label>

      <div className="row mb" style={{ flexWrap: 'wrap', gap: 8 }}>
        <button className={`pill ${effects.mirror ? 'active' : ''}`}
          onClick={() => setFx({ mirror: !effects.mirror })}>🪞 Mirror</button>
        <div className="grow" style={{ minWidth: 130, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="muted" style={{ minWidth: 46 }}>Speed</span>
          <input type="range" min={0.5} max={2} step={0.05} value={effects.speed}
            onChange={(e) => setFx({ speed: +e.target.value })} />
          <span className="side-val">{effects.speed.toFixed(2)}×</span>
        </div>
      </div>

      <div className="look-grid mb">
        {LOOKS.map((l) => (
          <button key={l.id} className={`look-chip ${effects.look === l.id ? 'active' : ''}`}
            onClick={() => setFx({ look: l.id })}>{l.label}</button>
        ))}
      </div>

      <div className="adjust-grid mb">
        <div className="adjust-row">
          <span className="muted">Brightness</span>
          <input type="range" min={-0.5} max={0.5} step={0.02} value={effects.brightness}
            onChange={(e) => setFx({ brightness: +e.target.value })} />
        </div>
        <div className="adjust-row">
          <span className="muted">Contrast</span>
          <input type="range" min={0.5} max={2} step={0.02} value={effects.contrast}
            onChange={(e) => setFx({ contrast: +e.target.value })} />
        </div>
        <div className="adjust-row">
          <span className="muted">Saturation</span>
          <input type="range" min={0} max={3} step={0.02} value={effects.saturation}
            onChange={(e) => setFx({ saturation: +e.target.value })} />
        </div>
      </div>
    </>
  )
}

/** Text overlays / stickers: add lines of text, colour and size them, and drag
 *  them on the stage above. Previewed live; burned by ffmpeg on Re-render. */
function OverlaysPanel({ overlays, setOverlays }) {
  const update = (id, patch) =>
    setOverlays(overlays.map((o) => (o.id === id ? { ...o, ...patch } : o)))
  const remove = (id) => setOverlays(overlays.filter((o) => o.id !== id))

  return (
    <>
      <label className="lbl" style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span>TEXT OVERLAYS</span>
        <button className="btn sm" onClick={() => setOverlays([...overlays, newOverlay()])}>+ Text</button>
      </label>

      {overlays.length === 0 && (
        <div className="hint mb">Add text or emoji, then drag it on the video above to place it.</div>
      )}

      {overlays.map((ov) => (
        <div key={ov.id} className="ov-row mb">
          <input className="input" placeholder="Text / emoji" value={ov.text}
            onChange={(e) => update(ov.id, { text: e.target.value })} />
          <select className="select" style={{ width: 96 }} value={ov.color}
            onChange={(e) => update(ov.id, { color: e.target.value })}>
            {Object.keys(OVERLAY_COLORS).map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <input type="range" min={10} max={48} value={ov.size} style={{ width: 74 }}
            title="Size" onChange={(e) => update(ov.id, { size: +e.target.value })} />
          <button className="btn sm ghost" title="Remove" onClick={() => remove(ov.id)}>✕</button>
        </div>
      ))}
    </>
  )
}

function ZoomSlider({ crop, setCrop }) {
  return (
    <div className="row mb">
      <span className="muted" style={{ minWidth: 54 }}>Zoom</span>
      <input
        type="range" min={1} max={3} step={0.05} value={crop.zoom}
        onChange={(e) => setCrop({ ...crop, zoom: +e.target.value })}
      />
      <span className="side-val">{crop.zoom.toFixed(2)}×</span>
      <button className="btn sm ghost" onClick={() => setCrop({ cx: 0.5, cy: 0.5, zoom: 1 })}>Reset</button>
    </div>
  )
}

function EditModal({ clip, index, busy, duration, onClose, onManual, onAi }) {
  const r = clip.render
  const [tab, setTab] = useState('ai')
  const [instruction, setInstruction] = useState('')
  const [name, setName] = useState(clip.name)
  const [segments, setSegments] = useState(
    r.segments.map((s) => ({ start: secToClock(s.start_sec), end: secToClock(s.end_sec) }))
  )
  const [active, setActive] = useState(0)          // segment the player is editing
  const [preview, setPreview] = useState('source') // source (edit) | clip (result)
  const [ratio, setRatio] = useState(r.ratio)
  const [reframe, setReframe] = useState(r.reframe || 'smart')
  const [capOn, setCapOn] = useState(!!r.captions?.enabled)
  const [capStyle, setCapStyle] = useState(r.captions?.style || 'TikTok Bold')
  const [capHi, setCapHi] = useState(!!r.captions?.highlight)
  const [words, setWords] = useState(r.captions?.words_per_line || 4)
  const [head, setHead] = useState({
    enabled: !!r.headline?.enabled,
    text: r.headline?.text || '',
    style: r.headline?.style || 'box',
    position: r.headline?.position || 'top',
    size: r.headline?.size || 20,
  })
  const [crop, setCrop] = useState({
    cx: r.crop?.cx ?? 0.5, cy: r.crop?.cy ?? 0.5, zoom: r.crop?.zoom ?? 1,
  })
  const [effects, setEffects] = useState({ ...DEFAULT_EFFECTS, ...(r.effects || {}) })
  const setFx = (patch) => setEffects((e) => ({ ...e, ...patch }))
  const [overlays, setOverlays] = useState(
    (r.overlays || []).map((o) => ({ ...newOverlay(), ...o }))
  )
  const moveOverlay = (id, x, y) =>
    setOverlays((list) => list.map((o) => (o.id === id ? { ...o, x, y } : o)))

  const videoRef = useRef(null)
  const stopAt = useRef(null)                      // pause point for segment preview
  const [now, setNow] = useState(0)
  const aiTitle = clip.meta?.hook_title || clip.name.replace(/_/g, ' ')

  // preview speed by actually playing the source faster/slower
  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = effects.speed
  }, [effects.speed])

  function seek(t) {
    const v = videoRef.current
    if (!v) return
    const max = duration || v.duration || t
    v.currentTime = Math.max(0, Math.min(t, max))
    setNow(v.currentTime)
  }

  function step(delta) {
    const v = videoRef.current
    if (!v) return
    v.pause()
    stopAt.current = null
    seek(v.currentTime + delta)
  }

  function setSeg(i, key, value) {
    setSegments(segments.map((s, j) => (j === i ? { ...s, [key]: value } : s)))
  }

  /** Write the player's exact position into a segment edge — this is the
   *  frame-accurate trim: park on the frame you want, then claim it. */
  function grab(i, key) {
    setSeg(i, key, secToClock(videoRef.current?.currentTime || 0))
    setActive(i)
  }

  function playSegment(i) {
    const st = mmssToSec(segments[i].start), en = mmssToSec(segments[i].end)
    if (st == null || en == null) return
    setActive(i)
    stopAt.current = en
    seek(st)
    videoRef.current?.play()
  }

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
      captions: { enabled: capOn, style: capStyle, words_per_line: words, highlight: capHi },
      headline: head,
      crop,
      effects,
      overlays: overlays.map(({ text, x, y, size, color }) => ({ text, x, y, size, color })),
    })
  }

  const activeSeg = segments[active] || segments[0]

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal wide" onClick={(e) => e.stopPropagation()}>
        <h3 style={{ justifyContent: 'space-between' }}>
          <span>✏️ Edit clip: {clip.name}</span>
          {/* Source = the frame you cut & style against (live preview). Result =
              the actual rendered clip, to check the baked output. */}
          <span className="seg-toggle">
            <button className={`tab ${preview === 'source' ? 'active' : ''}`}
              onClick={() => setPreview('source')}>✂️ Source</button>
            <button className={`tab ${preview === 'clip' ? 'active' : ''}`}
              onClick={() => setPreview('clip')}>▶ Result</button>
          </span>
        </h3>

        {preview === 'clip' ? (
          <div className="stage">
            <video src={clip.url} className="stage-video" controls autoPlay />
          </div>
        ) : (
        /* live source preview — the frame you are actually cutting */
        <FrameStage
          videoRef={videoRef}
          src="/api/video/stream"
          ratio={ratio}
          crop={crop}
          setCrop={setCrop}
          headline={head}
          headlineFallback={aiTitle}
          manual={reframe === 'manual'}
          effects={effects}
          overlays={overlays}
          moveOverlay={moveOverlay}
          // open on the clip's own first frame, not on the start of the source
          onReady={() => seek(mmssToSec(segments[0]?.start) || 0)}
          onTimeUpdate={(e) => {
            const t = e.target.currentTime
            setNow(t)
            if (stopAt.current != null && t >= stopAt.current) {
              e.target.pause()
              stopAt.current = null
            }
          }}
        />
        )}

        {preview === 'source' && (<>
        <div className="frame-bar">
          <button className="btn sm" title="Back 1 second" onClick={() => step(-1)}>⏪ 1s</button>
          <button className="btn sm" title="Back 1 frame" onClick={() => step(-FRAME)}>◀ frame</button>
          <button className="btn sm" onClick={() => {
            const v = videoRef.current
            if (!v) return
            if (v.paused) { stopAt.current = null; v.play() } else v.pause()
          }}>⏯ Play</button>
          <button className="btn sm" title="Forward 1 frame" onClick={() => step(FRAME)}>frame ▶</button>
          <button className="btn sm" title="Forward 1 second" onClick={() => step(1)}>1s ⏩</button>
          <span className="timecode">{secToClock(now)}</span>
        </div>

        {duration > 0 && (
          <input
            className="scrubber" type="range" min={0} max={duration} step={0.01} value={Math.min(now, duration)}
            onChange={(e) => { stopAt.current = null; seek(+e.target.value) }}
          />
        )}
        </>
        )}

        {preview === 'source' && activeSeg && (
          <div className="hint">
            Editing segment {active + 1} — park the player on a frame, then hit
            <b> ⤓ Start</b> or <b>⤓ End</b> to snap that edge to it.
          </div>
        )}

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

            <label className="lbl">SEGMENTS (source video time, M:SS.ss)</label>
            {segments.map((s, i) => (
              <div key={i} className={`seg-row ${i === active ? 'active' : ''}`} onClick={() => setActive(i)}>
                <input className="input" placeholder="Start" value={s.start}
                  onChange={(e) => setSeg(i, 'start', e.target.value)} />
                <button className="btn sm" title="Use the player's current frame as the start"
                  onClick={() => grab(i, 'start')}>⤓ Start</button>
                <span className="muted">→</span>
                <input className="input" placeholder="End" value={s.end}
                  onChange={(e) => setSeg(i, 'end', e.target.value)} />
                <button className="btn sm" title="Use the player's current frame as the end"
                  onClick={() => grab(i, 'end')}>⤓ End</button>
                <button className="btn sm" title="Preview this segment" onClick={() => playSegment(i)}>▶</button>
                {segments.length > 1 && (
                  <button className="btn sm ghost" onClick={() => {
                    setSegments(segments.filter((_, j) => j !== i))
                    setActive(0)
                  }}>✕</button>
                )}
              </div>
            ))}
            <button className="btn sm mb" onClick={() => {
              const last = segments[segments.length - 1]
              setSegments([...segments, { start: last?.end || '0:00.00', end: '' }])
              setActive(segments.length)
            }}>+ Segment</button>

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
                  <option value="manual">✋ Manual Frame</option>
                </select>
              </div>
            </div>

            {reframe === 'manual' && (
              RATIO_AR[ratio] ? (
                <>
                  <div className="hint mb">
                    Drag inside the video above to move the crop window — what's inside the box is
                    what the clip shows.
                  </div>
                  <ZoomSlider crop={crop} setCrop={setCrop} />
                </>
              ) : (
                <div className="hint mb">Manual framing needs a fixed aspect ratio — pick 9:16, 1:1 or 16:9 above.</div>
              )
            )}

            <EffectsPanel effects={effects} setFx={setFx} />

            <OverlaysPanel overlays={overlays} setOverlays={setOverlays} />

            <label className="lbl">HEADLINE</label>
            <div className="row mb">
              <label className="switch">
                <input type="checkbox" checked={head.enabled}
                  onChange={(e) => setHead({ ...head, enabled: e.target.checked })} />
                <span className="track" />
              </label>
              <span className="muted">Title on video</span>
              {head.enabled && (
                <>
                  <select className="select" style={{ width: 110 }} value={head.style}
                    onChange={(e) => setHead({ ...head, style: e.target.value })}>
                    <option value="box">▬ Box</option>
                    <option value="plain">A Plain</option>
                  </select>
                  <select className="select" style={{ width: 110 }} value={head.position}
                    onChange={(e) => setHead({ ...head, position: e.target.value })}>
                    <option value="top">⬆ Top</option>
                    <option value="bottom">⬇ Bottom</option>
                  </select>
                  <input type="range" min={12} max={32} value={head.size} style={{ width: 80 }}
                    onChange={(e) => setHead({ ...head, size: +e.target.value })} />
                </>
              )}
            </div>
            {head.enabled && (
              <input className="input mb" placeholder={`Blank = AI title: "${aiTitle}"`}
                value={head.text} onChange={(e) => setHead({ ...head, text: e.target.value })} />
            )}

            <div className="row mb">
              <label className="switch">
                <input type="checkbox" checked={capOn} onChange={(e) => setCapOn(e.target.checked)} />
                <span className="track" />
              </label>
              <span className="muted">Captions</span>
              {capOn && (
                <>
                  <select className="select" style={{ width: 140 }} value={capStyle} onChange={(e) => setCapStyle(e.target.value)}>
                    {CAPTION_STYLES.map((s) => <option key={s}>{s}</option>)}
                  </select>
                  <input type="range" min={2} max={8} value={words} style={{ width: 80 }} onChange={(e) => setWords(+e.target.value)} />
                  <span className="muted">{words} w/line</span>
                  <label className="muted" style={{ cursor: 'pointer' }}>
                    <input type="checkbox" checked={capHi} onChange={(e) => setCapHi(e.target.checked)} /> highlight
                  </label>
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
            ? 'Your key is saved on the server under your account only — other users cannot see it.'
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
