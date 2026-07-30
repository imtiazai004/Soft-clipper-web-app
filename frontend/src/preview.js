/** Drawing a clip the way it will be exported, on a 2D canvas.
 *
 *  The preview used to be the source video with draggable chips floating over
 *  it. The chips said *where* a caption would go and could say nothing about
 *  what it would look like — not the typeface, not the size, not the colour,
 *  not even the crop the clip would actually be. Everything you chose in the
 *  sidebar was a guess you paid for in render minutes.
 *
 *  Plain canvas rather than a WebGL library, deliberately. Everything drawn here
 *  is a video frame, some rectangles and some text; a renderer built for
 *  thousands of sprites buys nothing and costs a dependency the whole app then
 *  carries. This module has none.
 *
 *  **The rule that matters more than any of it:** the preview may only draw what
 *  the render will draw. Positions and sizes come from the same numbers ffmpeg
 *  is given, in the same coordinate space, and the caption chunking is not done
 *  here at all — it is computed by core/captions.py and fetched. Where the
 *  canvas genuinely cannot match libass, the UI says so out loud rather than
 *  quietly lying.
 */

/** 9:16, 1:1 and 16:9 as the renderer defines them — core/video.py TARGETS. */
export const TARGETS = {
  '9:16': [1080, 1920],
  '1:1': [1080, 1080],
  '16:9': [1920, 1080],
}

/**
 * The source rectangle a reframe mode will take.
 *
 * Mirrors core/video.py: `center` takes the largest centred rectangle of the
 * target shape, `manual` takes the placed window, `fit` takes the whole frame
 * and letterboxes it over a blur. `smart` cannot be mirrored — it depends on
 * where OpenCV finds a face, which is not knowable in the browser — so it is
 * shown as the centre crop with the UI saying that is what it is.
 */
export function sourceRect(mode, sw, sh, ratio, crop) {
  const [tw, th] = TARGETS[ratio] || TARGETS['9:16']
  const targetAR = tw / th

  if (mode === 'fit') return { x: 0, y: 0, w: sw, h: sh, letterbox: true }

  if (mode === 'manual' && crop) {
    const zoom = Math.min(4, Math.max(1, crop.zoom ?? 1))
    let cw = Math.min(sw, sh * targetAR) / zoom
    let ch = cw / targetAR
    if (ch > sh) { ch = sh; cw = ch * targetAR }
    const cx = Math.min(1, Math.max(0, crop.cx ?? 0.5))
    const cy = Math.min(1, Math.max(0, crop.cy ?? 0.5))
    return {
      x: Math.min(Math.max(cx * sw - cw / 2, 0), Math.max(0, sw - cw)),
      y: Math.min(Math.max(cy * sh - ch / 2, 0), Math.max(0, sh - ch)),
      w: cw, h: ch,
    }
  }

  // centre, and the stand-in for smart
  let cw = Math.min(sw, sh * targetAR)
  let ch = Math.min(sh, cw / targetAR)
  return { x: (sw - cw) / 2, y: (sh - ch) / 2, w: cw, h: ch }
}

/** The caption chunk showing at this moment, or null. */
export function lineAt(lines, t) {
  for (const line of lines || []) {
    if (t >= line.start && t < line.end) return line
  }
  return null
}

/**
 * Which word of a chunk is being spoken.
 *
 * Split by character length, which is what core/captions.py does — the word
 * timings inside a segment are an approximation there too, and matching the
 * approximation is the point.
 */
export function wordAt(line, t) {
  if (!line?.words?.length) return -1
  const total = line.words.reduce((n, w) => n + w.length, 0) || 1
  const span = line.end - line.start
  let at = line.start
  for (let i = 0; i < line.words.length; i++) {
    const next = at + span * (line.words[i].length / total)
    if (t < next) return i
    at = next
  }
  return line.words.length - 1
}

/** Wrap to the width available, in canvas pixels. */
function wrap(ctx, text, maxWidth) {
  const words = text.split(' ')
  const lines = []
  let current = ''
  for (const word of words) {
    const candidate = current ? `${current} ${word}` : word
    if (ctx.measureText(candidate).width > maxWidth && current) {
      lines.push(current)
      current = word
    } else {
      current = candidate
    }
  }
  if (current) lines.push(current)
  return lines
}

function outlined(ctx, text, x, y, fill, stroke, width) {
  if (width > 0) {
    ctx.lineWidth = width
    ctx.strokeStyle = stroke
    ctx.lineJoin = 'round'
    ctx.miterLimit = 2
    ctx.strokeText(text, x, y)
  }
  ctx.fillStyle = fill
  ctx.fillText(text, x, y)
}

/**
 * Draw one frame.
 *
 * `scene` is everything the renderer would be given. Nothing is computed here
 * that the backend already computed — the caption lines and their metrics
 * arrive ready, and this only places them.
 */
export function drawFrame(ctx, scene) {
  const { video, ratio, reframe, crop, canvasW, canvasH, time } = scene
  ctx.clearRect(0, 0, canvasW, canvasH)
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, canvasW, canvasH)

  // ── the picture ─────────────────────────────────────────────────────────
  if (video && video.videoWidth) {
    const rect = sourceRect(reframe, video.videoWidth, video.videoHeight, ratio, crop)
    if (rect.letterbox) {
      // Fit + blur: the background is the frame blown up and blurred, the
      // foreground is the whole frame letterboxed over it.
      ctx.save()
      ctx.filter = 'blur(18px) brightness(0.92)'
      const coverScale = Math.max(canvasW / video.videoWidth, canvasH / video.videoHeight)
      const bw = video.videoWidth * coverScale
      const bh = video.videoHeight * coverScale
      ctx.drawImage(video, (canvasW - bw) / 2, (canvasH - bh) / 2, bw, bh)
      ctx.restore()

      const fitScale = Math.min(canvasW / video.videoWidth, canvasH / video.videoHeight)
      const fw = video.videoWidth * fitScale
      const fh = video.videoHeight * fitScale
      ctx.drawImage(video, (canvasW - fw) / 2, (canvasH - fh) / 2, fw, fh)
    } else {
      ctx.drawImage(video, rect.x, rect.y, rect.w, rect.h, 0, 0, canvasW, canvasH)
    }
  }

  // ── captions ────────────────────────────────────────────────────────────
  const m = scene.metrics
  const line = scene.captionsOn ? lineAt(scene.lines, time) : null
  if (line && m) {
    // The ASS coordinate space is play_x by play_y and ffmpeg scales it to the
    // output, so one PlayRes unit is canvasH / play_y canvas pixels. This is the
    // whole reason a caption lands in the same place here as in the export.
    const unit = canvasH / m.play_y
    const size = m.fontsize * unit
    ctx.font = `700 ${size}px ${JSON.stringify(m.family)}, sans-serif`
    ctx.textAlign = 'center'

    const text = m.rtl ? line.text : line.text.toUpperCase()
    const maxWidth = canvasW - 20 * unit
    const rows = wrap(ctx, text, maxWidth)
    const lineHeight = size * 1.15

    // Bottom-centre on the style's own margin, unless someone dragged it.
    let cx = canvasW / 2
    let baseline = canvasH - m.margin_v * unit
    if (scene.capPos) {
      cx = scene.capPos.x * canvasW
      baseline = scene.capPos.y * canvasH + ((rows.length - 1) * lineHeight) / 2
    }
    ctx.textBaseline = 'alphabetic'

    rows.forEach((row, i) => {
      const y = baseline - (rows.length - 1 - i) * lineHeight
      if (m.boxed) {
        const w = ctx.measureText(row).width
        ctx.fillStyle = m.outline
        ctx.globalAlpha = 0.75
        ctx.fillRect(cx - w / 2 - size * 0.25, y - size * 0.92, w + size * 0.5, size * 1.18)
        ctx.globalAlpha = 1
        ctx.fillStyle = m.colour
        ctx.fillText(row, cx, y)
      } else {
        outlined(ctx, row, cx, y, m.colour, m.outline, size * 0.11)
      }
    })

    // The spoken word, when word-by-word highlighting is on. Drawn only on a
    // single unwrapped row: once the text has been broken across lines, working
    // out which row a word landed on means measuring every word again, and
    // getting that subtly wrong is worse than not colouring it.
    if (scene.highlight && rows.length === 1) {
      const at = wordAt(line, time)
      const words = m.rtl ? line.words : line.words.map((w) => w.toUpperCase())
      if (at >= 0 && words[at]) {
        const full = ctx.measureText(rows[0]).width
        const before = ctx.measureText(words.slice(0, at).join(' ') + (at ? ' ' : '')).width
        const left = cx - full / 2 + before
        ctx.textAlign = 'left'
        outlined(ctx, words[at], left, baseline, m.highlight, m.outline, size * 0.11)
        ctx.textAlign = 'center'
      }
    }
  }

  // ── headline ────────────────────────────────────────────────────────────
  if (scene.headline?.enabled && (scene.headline.text || '').trim()) {
    const unit = canvasH / (m?.play_y || 288)
    const size = (scene.headline.size || 20) * unit
    ctx.font = `700 ${size}px sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    const top = scene.headline.position !== 'bottom'
    const y = top ? 30 * unit + size / 2 : canvasH - 95 * unit
    const rows = wrap(ctx, scene.headline.text, canvasW - 28 * unit).slice(0, 3)
    rows.forEach((row, i) => {
      const ry = y + i * size * 1.2
      if (scene.headline.style === 'box') {
        const w = ctx.measureText(row).width
        ctx.fillStyle = 'rgba(0,0,0,0.9)'
        ctx.fillRect(canvasW / 2 - w / 2 - size * 0.4, ry - size * 0.7, w + size * 0.8, size * 1.4)
        ctx.fillStyle = '#fff'
        ctx.fillText(row, canvasW / 2, ry)
      } else {
        outlined(ctx, row, canvasW / 2, ry, '#fff', '#000', size * 0.12)
      }
    })
  }

  // ── text overlays ───────────────────────────────────────────────────────
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  for (const ov of scene.overlays || []) {
    if (!(ov.text || '').trim()) continue
    const size = (ov.size || 24) * (canvasH / (m?.play_y || 288))
    ctx.font = `700 ${size}px sans-serif`
    outlined(ctx, ov.text, ov.x * canvasW, ov.y * canvasH, ov.color || '#fff', '#000', size * 0.11)
  }

  // ── the logo ────────────────────────────────────────────────────────────
  const logo = scene.brand
  if (logo?.enabled && scene.logoImage?.complete && scene.logoImage.naturalWidth) {
    const w = canvasW * (logo.scale_pct / 100)
    const h = w * (scene.logoImage.naturalHeight / scene.logoImage.naturalWidth)
    // (W-w)*x, the same expression core/brand.py hands to ffmpeg.
    ctx.globalAlpha = logo.opacity ?? 0.85
    ctx.drawImage(scene.logoImage, (canvasW - w) * (logo.x ?? 0.96), (canvasH - h) * (logo.y ?? 0.04), w, h)
    ctx.globalAlpha = 1
  }
}
