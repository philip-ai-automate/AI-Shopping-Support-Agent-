/* KeyLand WhatsApp calls (2026-10-22), from the main portal's wa-calls.js.
   Loaded on every KeyLand page. Polls /estate/calls/live; a buyer's call rings
   here for the lead's agent and the managers (everyone if unassigned) and the
   first to press Answer takes it. Audio runs in the browser (WebRTC): Meta sends
   an offer, the answer goes back through KeyLand (estate_calls.py). Incoming only. */
(function () {
  'use strict'
  if (window.__klCalls) return
  window.__klCalls = true

  var POLL_MS = 3000, ICE = [{ urls: 'stun:stun.l.google.com:19302' }]
  var shown = {}, active = null, timerT = null, pollT = null, ringCtx = null, ringT = null, titleT = null, baseTitle = document.title

  // ── Styles ──
  var css = document.createElement('style')
  css.textContent = [
    '#phxCalls{position:fixed;right:max(16px,env(safe-area-inset-right,0px));bottom:calc(16px + env(safe-area-inset-bottom,0px));z-index:9999;display:flex;flex-direction:column;gap:10px;width:min(340px,calc(100vw - 32px));font-family:Inter,system-ui,sans-serif}',
    '.phx-call{background:#fff;border:1.5px solid #0B1D40;border-radius:14px;padding:14px;box-shadow:0 10px 30px rgba(3,12,24,.22);display:flex;flex-direction:column;gap:10px;color:#111E2D}',
    '.phx-call.taken{border-color:#E1E7F0;box-shadow:0 4px 14px rgba(3,12,24,.12)}',
    '.phx-t{display:flex;gap:10px;align-items:center}',
    '.phx-av{width:42px;height:42px;border-radius:50%;background:#0B1D40;color:#fff;display:grid;place-items:center;font-weight:700;flex:none}',
    '.phx-call b{display:block;font-size:14.5px}.phx-call small{display:block;color:#5A6B85;font-size:12.5px}',
    '.phx-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#067647;margin-right:6px;animation:phxp 1s infinite}',
    '@keyframes phxp{50%{opacity:.25}}@media (prefers-reduced-motion:reduce){.phx-dot{animation:none}}',
    '.phx-acts{display:flex;gap:8px;flex-wrap:wrap}',
    '.phx-b{font:inherit;font-size:13.5px;font-weight:700;padding:9px 14px;border-radius:10px;border:1.5px solid #E1E7F0;background:#fff;color:#111E2D;cursor:pointer}',
    '.phx-b.go{background:#067647;border-color:#067647;color:#fff}.phx-b.no{background:#B42318;border-color:#B42318;color:#fff}',
    '.phx-b:disabled{opacity:.55;cursor:default}',
    '.phx-err{font-size:12.5px;color:#B42318}',
    '.phx-timer{font-variant-numeric:tabular-nums}',
    '.phx-chatbar{font-size:12px;font-weight:700;padding:4px 9px;border-radius:7px;background:#ECFDF3;color:#067647;white-space:nowrap}',
  ].join('\n')
  document.head.appendChild(css)
  var box = document.createElement('div'); box.id = 'phxCalls'; box.setAttribute('aria-live', 'polite')
  document.addEventListener('DOMContentLoaded', function () { document.body.appendChild(box) })
  if (document.body) document.body.appendChild(box)

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] }) }
  function ini(n) { var p = String(n || '?').replace(/^\+/, '').split(/\s+/); return ((p[0] || '')[0] || '?').toUpperCase() + ((p[1] || '')[0] || '').toUpperCase() }
  function fmt(s) { s = Math.max(0, s | 0); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0') }
  function post(url, data) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) })
      .then(function (r) { return r.json().catch(function () { return { error: 'Something went wrong (' + r.status + ').' } }) })
  }

  // ── Ringtone (WebAudio; browsers allow it once the page has been clicked) ──
  function ringStart() {
    if (ringT) return
    try {
      ringCtx = ringCtx || new (window.AudioContext || window.webkitAudioContext)()
      var beep = function () {
        [0, 0.45].forEach(function (off) {
          var o = ringCtx.createOscillator(), g = ringCtx.createGain(), t = ringCtx.currentTime + off
          o.frequency.value = 440; o.connect(g); g.connect(ringCtx.destination)
          g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.18, t + 0.03); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.38)
          o.start(t); o.stop(t + 0.4)
        })
      }
      beep(); ringT = setInterval(beep, 2500)
    } catch (e) {}
    if (!titleT) titleT = setInterval(function () { document.title = document.title === baseTitle ? '📞 Incoming WhatsApp call' : baseTitle }, 1000)
  }
  function ringStop() {
    clearInterval(ringT); ringT = null
    clearInterval(titleT); titleT = null; document.title = baseTitle
  }

  // ── Cards ──
  function card(id) {
    var el = document.getElementById('phx-c-' + id)
    if (!el) { el = document.createElement('div'); el.className = 'phx-call'; el.id = 'phx-c-' + id; box.appendChild(el) }
    return el
  }
  function drop(id, later) {
    var el = document.getElementById('phx-c-' + id)
    if (!el) return
    if (later) setTimeout(function () { el.remove() }, later); else el.remove()
    delete shown[id]
  }
  function ringingCard(c) {
    var el = card(c.id)
    if (el.dataset.state === 'ringing') return
    el.dataset.state = 'ringing'; el.className = 'phx-call'
    el.innerHTML = '<div class="phx-t"><span class="phx-av">' + esc(ini(c.name)) + '</span><span><b><span class="phx-dot"></span>' + esc(c.name) + ' is calling</b>' +
      '<small>WhatsApp · ' + esc(c.phone) + (c.readiness ? ' · ' + esc(c.readiness.charAt(0).toUpperCase() + c.readiness.slice(1)) + ' buyer' : '') + '</small></span></div>' +
      '<div class="phx-acts"><button class="phx-b go" type="button" data-a="ans">📞 Answer</button><button class="phx-b" type="button" data-a="skip">Not me</button>' +
      leadBtn(c) + '</div><div class="phx-err" hidden></div>'
    el.querySelector('[data-a=ans]').onclick = function () { answer(c, el) }
    wireLead(el, c)
    el.querySelector('[data-a=skip]').onclick = function () { el.dataset.skipped = '1'; el.innerHTML = '<small>You passed. It keeps ringing for the others.</small>'; drop(c.id, 3000); maybeStopRing() }
  }
  function takenCard(c) {
    var el = document.getElementById('phx-c-' + c.id)
    if (!el || el.dataset.state === 'taken' || el.dataset.state === 'mine') return
    el.dataset.state = 'taken'; el.className = 'phx-call taken'
    el.innerHTML = c.by ? '<b>Answered by ' + esc(c.by) + '</b><small>' + esc(c.name) + ' · the call stopped ringing for you.</small>'
      : '<b>Missed call from ' + esc(c.name) + '</b><small>It\'s on the lead and in Needs attention for a call-back.</small>'
    drop(c.id, 5000)
  }
  function maybeStopRing() {
    var any = Array.prototype.some.call(box.children, function (el) { return el.dataset.state === 'ringing' })
    if (!any) ringStop()
  }
  function err(el, msg) { var e = el.querySelector('.phx-err'); if (e) { e.textContent = msg; e.hidden = !msg } }

  // ── WebRTC ──
  function gather(pc) {
    return new Promise(function (res) {
      if (pc.iceGatheringState === 'complete') return res()
      var t = setTimeout(res, 3000)
      pc.addEventListener('icegatheringstatechange', function () { if (pc.iceGatheringState === 'complete') { clearTimeout(t); res() } })
    })
  }
  function newPc(stream) {
    var pc = new RTCPeerConnection({ iceServers: ICE })
    var audio = document.createElement('audio'); audio.autoplay = true; audio.hidden = true; document.body.appendChild(audio)
    pc.ontrack = function (e) { audio.srcObject = e.streams[0] || new MediaStream([e.track]) }
    stream.getTracks().forEach(function (t) { pc.addTrack(t, stream) })
    return { pc: pc, audio: audio, stream: stream }
  }
  function cleanup() {
    if (!active) return
    try { active.pc.close() } catch (e) {}
    try { active.stream.getTracks().forEach(function (t) { t.stop() }) } catch (e) {}
    try { active.audio.remove() } catch (e) {}
    clearInterval(timerT); timerT = null
    var id = active.id; active = null
    var el = document.getElementById('phx-c-' + id)
    if (el) { el.dataset.state = 'done'; el.className = 'phx-call taken'; el.innerHTML = '<b>Call ended</b><small>Saved on the lead\'s Interaction Log.</small>'; drop(id, 4000) }
    chatBar()
  }
  // "Add as lead" for callers who aren't in Leads yet; "Lead ↗" once they are.
  function leadBtn(c) {
    if (!c || !c.lead_id) return ''
    return '<a class="phx-b" href="/estate/leads/' + c.lead_id + '?brief=1" target="_blank" rel="noopener">🧠 AI brief ↗</a>'
  }
  function wireLead() {}
  function onCallCard(id, name, answeredAt, c) {
    var el = card(id); el.dataset.state = 'mine'; el.className = 'phx-call'
    el.innerHTML = '<div class="phx-t"><span class="phx-av">' + esc(ini(name)) + '</span><span><b>On a call with ' + esc(name) + '</b><small class="phx-timer">' + (answeredAt ? '00:00' : 'Connecting…') + '</small></span></div>' +
      '<div class="phx-acts"><button class="phx-b" type="button" data-a="mute">🎙 Mute</button><button class="phx-b no" type="button" data-a="end">Hang up</button>' + leadBtn(c) + '</div><div class="phx-err" hidden></div>'
    wireLead(el, c)
    el.querySelector('[data-a=end]').onclick = function () { post('/estate/calls/' + id + '/end').then(cleanup) }
    el.querySelector('[data-a=mute]').onclick = function (ev) {
      if (!active) return
      var on = active.stream.getAudioTracks().some(function (t) { return t.enabled })
      active.stream.getAudioTracks().forEach(function (t) { t.enabled = !on })
      ev.target.textContent = on ? '🎙 Unmute' : '🎙 Mute'
    }
    return el
  }
  function startTimer(el, fromIso) {
    clearInterval(timerT)
    var t0 = fromIso ? new Date(fromIso).getTime() : Date.now()
    var tick = function () { var x = el.querySelector('.phx-timer'); if (x) x.textContent = fmt((Date.now() - t0) / 1000) }
    tick(); timerT = setInterval(tick, 1000)
  }

  function answer(c, el) {
    if (active) { err(el, 'Finish your current call first.'); return }
    var btn = el.querySelector('[data-a=ans]'); btn.disabled = true; btn.textContent = 'Connecting…'
    post('/estate/calls/' + c.id + '/claim').then(function (d) {
      if (d.error) { el.innerHTML = '<b>' + esc(d.error) + '</b>'; el.className = 'phx-call taken'; drop(c.id, 4000); maybeStopRing(); return }
      ringStop()
      return navigator.mediaDevices.getUserMedia({ audio: true }).catch(function () { return null }).then(function (stream) {
        if (!stream) {
          post('/estate/calls/' + c.id + '/release')
          err(el, 'Your browser blocked the microphone. Allow it (the 🔒 or 🎙 icon by the address bar), then answer again.')
          btn.disabled = false; btn.textContent = '📞 Answer'; el.dataset.state = ''; return
        }
        var s = newPc(stream); active = { id: c.id, pc: s.pc, audio: s.audio, stream: stream, dir: 'in' }
        return s.pc.setRemoteDescription({ type: 'offer', sdp: d.offer })
          .then(function () { return s.pc.createAnswer() })
          .then(function (a) { return s.pc.setLocalDescription(a) })
          .then(function () { return gather(s.pc) })
          .then(function () { return post('/estate/calls/' + c.id + '/accept', { sdp: s.pc.localDescription.sdp }) })
          .then(function (r) {
            if (r.error) { cleanup(); var e2 = card(c.id); e2.innerHTML = '<b>Couldn\'t connect</b><small>' + esc(r.error) + '</small>'; drop(c.id, 8000); return }
            startTimer(onCallCard(c.id, c.name, true, c), null); chatBar()
          })
      })
    }).catch(function () { err(el, 'Could not reach the server.'); btn.disabled = false; btn.textContent = '📞 Answer' })
  }

  var lastLive = null
  function chatBar() {
    var slot = document.getElementById('waCallSlot'); if (!slot) return
    var phone = '+' + (slot.dataset.phone || '').replace(/\D/g, '')
    var old = slot.querySelector('.phx-chatbar'); if (old) old.remove()
    if (!lastLive) return
    var live = (lastLive.mine && lastLive.mine.phone === phone && ['answered', 'answering'].indexOf(lastLive.mine.status) >= 0) ? 'You\'re on a call'
      : ((lastLive.taken || []).filter(function (t) { return t.phone === phone && t.status === 'answered' })[0] || {}).by
    if (!live) return
    var s = document.createElement('span'); s.className = 'phx-chatbar'
    s.textContent = live === 'You\'re on a call' ? '📞 ' + live : '📞 On a call with ' + live
    slot.prepend(s)
  }

  // ── Poll ──
  function poll() {
    fetch('/estate/calls/live', { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null }).then(function (d) {
      if (!d || !d.enabled) { if (d && !d.enabled) clearInterval(pollT); return }
      lastLive = d
      var ringIds = {}
      ;(d.ringing || []).forEach(function (c) {
        ringIds[c.id] = 1
        var el = document.getElementById('phx-c-' + c.id)
        if (el && (el.dataset.skipped || el.dataset.state === 'mine')) return
        shown[c.id] = c; ringingCard(c)
      })
      ;(d.taken || []).forEach(function (c) { if (shown[c.id] && !ringIds[c.id]) takenCard(c) })
      Object.keys(shown).forEach(function (id) {
        var el = document.getElementById('phx-c-' + id)
        if (!ringIds[id] && el && el.dataset.state === 'ringing') { el.innerHTML = '<b>Call ended</b><small>The customer hung up or nobody answered in time.</small>'; el.className = 'phx-call taken'; el.dataset.state = 'gone'; drop(id, 4000) }
      })
      if (Object.keys(ringIds).some(function (id) { var el = document.getElementById('phx-c-' + id); return el && el.dataset.state === 'ringing' })) ringStart(); else ringStop()
      // my own call
      if (active) {
        var m = d.mine
        if (!m || m.id !== active.id) { if (active.id !== 'out' && (!m || m.id !== active.id)) cleanup() }
        else if (active.dir === 'out') {
          if (m.answer_sdp && !active.applied) { active.applied = true; active.pc.setRemoteDescription({ type: 'answer', sdp: m.answer_sdp }).catch(function () {}) }
          var el2 = document.getElementById('phx-c-' + m.id)
          if (m.status === 'answered' && el2 && !timerT) startTimer(el2, m.answered_at)
        }
      }
      chatBar()
    }).catch(function () {})
  }
  pollT = setInterval(poll, POLL_MS); poll()
})()
