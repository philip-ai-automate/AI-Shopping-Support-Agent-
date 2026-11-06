/* Admin › Accounts / Inbox WhatsApp calls (2026-11-06). Needs #callBox (data-tid, data-name, data-ask)
   with #callMsg #callBtns #onCall #onCallState #muteBtn #endBtn #callAudio, and window.amPreview. */
(function(){
  var box = document.getElementById('callBox'); if (!box) return
  var tid = box.dataset.tid, msg = document.getElementById('callMsg'), btns = document.getElementById('callBtns')
  var onCall = document.getElementById('onCall'), stateEl = document.getElementById('onCallState')
  var ICE = [{ urls: 'stun:stun.l.google.com:19302' }], cur = null, pollT = null, timerT = null, checked = false
  function post(u, d){ return fetch(u, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body: JSON.stringify(d||{})})
    .then(function(r){ return r.json().catch(function(){ return {error:'Something went wrong ('+r.status+').'} }) }) }
  function btn(t, fn, main){ var b=document.createElement('button'); b.type='button'; b.className='btn btn-sm'+(main?' btn-primary':''); b.textContent=t; b.onclick=function(){fn(b)}; btns.appendChild(b); return b }
  function check(){
    if (checked) return; checked = true
    fetch('/admin/accounts/'+tid+'/call/check', {credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
      btns.innerHTML = ''
      if (!d.show){ msg.innerHTML = '<span class="ac-bad">Can\'t call: '+(d.why||'not available')+'</span>'; return }
      if (d.can_call){ msg.textContent = 'They agreed to WhatsApp calls. You\'re calling from '+d.number+'.'; btn('📞 WhatsApp call '+box.dataset.name, callOut, true) }
      else if (d.can_ask){ msg.textContent = 'WhatsApp needs their OK before a business can call. Send "Can we call you?" — when they accept, the WhatsApp call button appears here.'
        btn('Ask: can we call you?', function(b){
          amPreview({ title: 'Check before sending', text: box.dataset.ask, buttons: true,
            note: 'WhatsApp shows the customer Allow / Not now buttons. When they allow, the WhatsApp call button appears here.',
            send: function(){ post('/admin/accounts/'+tid+'/call/permission').then(function(r){
              document.getElementById('pv').hidden = true
              if (r.error){ alert(r.error) } else { b.disabled = true; b.textContent='✓ Asked'; msg.textContent='Asked. Refresh this page after they accept.' } }) } })
        }, true) }
      else { msg.textContent = 'You asked already. Waiting for them to accept (WhatsApp allows one request a day).' }
    }).catch(function(){ msg.textContent = 'Could not check calling right now.' })
  }
  function gather(pc){ return new Promise(function(res){ if (pc.iceGatheringState==='complete') return res(); var t=setTimeout(res,2500); pc.addEventListener('icegatheringstatechange', function(){ if(pc.iceGatheringState==='complete'){ clearTimeout(t); res() } }) }) }
  function fmt(s){ s=Math.max(0,s|0); return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0') }
  function done(note){
    clearInterval(pollT); clearInterval(timerT); pollT = timerT = null
    if (cur){ try{ cur.pc.close() }catch(e){} try{ cur.stream.getTracks().forEach(function(t){t.stop()}) }catch(e){} }
    cur = null; onCall.style.display='none'; btns.style.display='flex'
    msg.textContent = note || 'WhatsApp call ended.'
    if (window.amAfterCall) window.amAfterCall()
  }
  function callOut(){
    if (cur) return
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(stream){
      var pc = new RTCPeerConnection({iceServers: ICE})
      pc.ontrack = function(e){ document.getElementById('callAudio').srcObject = e.streams[0] || new MediaStream([e.track]) }
      stream.getTracks().forEach(function(t){ pc.addTrack(t, stream) })
      cur = {pc: pc, stream: stream, id: null, applied: false}
      btns.style.display='none'; onCall.style.display='block'; stateEl.textContent='Calling…'
      pc.createOffer({offerToReceiveAudio:true}).then(function(o){ return pc.setLocalDescription(o) }).then(function(){ return gather(pc) })
        .then(function(){ return post('/admin/accounts/'+tid+'/call/start', {sdp: pc.localDescription.sdp}) })
        .then(function(r){
          if (r.error){ done(); msg.innerHTML = '<span class="ac-bad">'+r.error+'</span>'; return }
          cur.id = r.id; stateEl.textContent = 'Ringing…'
          pollT = setInterval(function(){
            fetch('/admin/accounts/call/'+cur.id+'/status', {credentials:'same-origin', cache:'no-store'}).then(function(x){return x.json()}).then(function(s){
              if (!cur) return
              if (s.answer_sdp && !cur.applied){ cur.applied = true; cur.pc.setRemoteDescription({type:'answer', sdp: s.answer_sdp}).catch(function(){}) }
              if (s.status === 'answered' && !timerT){ var t0 = s.answered_at ? Date.parse(s.answered_at) : Date.now()
                timerT = setInterval(function(){ stateEl.textContent = 'On call · ' + fmt((Date.now()-t0)/1000) }, 1000) }
              if (s.ended) done(s.status === 'missed' ? 'No answer.' : 'WhatsApp call ended.')
            }).catch(function(){})
          }, 2000)
        })
    }).catch(function(){ alert('Your browser blocked the microphone. Allow it, then try again.') })
  }
  document.getElementById('endBtn').onclick = function(){ if (cur && cur.id) post('/admin/accounts/call/'+cur.id+'/end').then(function(){ done('WhatsApp call ended.') }); else done() }
  document.getElementById('muteBtn').onclick = function(e){ if (!cur) return; var on = cur.stream.getAudioTracks().some(function(t){return t.enabled}); cur.stream.getAudioTracks().forEach(function(t){ t.enabled = !on }); e.target.textContent = on ? '🎙 Unmute' : '🔇 Mute' }
  window.amCallCheck = check
})();
