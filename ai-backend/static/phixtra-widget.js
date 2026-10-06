/*! PhiXtra chat box \u2014 paste-in version for any website (2026-10-05).
 *  <script src="https://chat.phixtra.com/widget.js" data-site="YOUR_WEBSITE_KEY" async></script>
 *  The website key is public and only works on the business's own website
 *  address. When the business's AI is off (PhiXtra Connect, 2026-10-06) the
 *  box still shows and the business's team answers from the PhiXtra Inbox;
 *  their replies appear here live while the visitor is on the page.
 */
(function () {
  "use strict";
  if (window.__phixtraWidget) { return; }
  window.__phixtraWidget = true;

  var script = document.currentScript || (function () {
    var all = document.getElementsByTagName("script");
    for (var i = all.length - 1; i >= 0; i--) { if ((all[i].src || "").indexOf("/widget.js") > -1 && all[i].getAttribute("data-site")) { return all[i]; } }
    return null;
  })();
  if (!script) { return; }
  var SITE = script.getAttribute("data-site") || "";
  var BASE = (script.src || "").replace(/\/widget\.js.*$/, "");
  if (!SITE || !BASE) { return; }

  function store(k, v) { try { if (v === undefined) { return localStorage.getItem(k); } localStorage.setItem(k, v); } catch (e) { return null; } }
  var SKEY = "phixtra_w_session_" + SITE;
  var sessionId = store(SKEY);
  if (!sessionId) { sessionId = "w-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); store(SKEY, sessionId); }

  fetch(BASE + "/widget/config?site=" + encodeURIComponent(SITE), { method: "GET" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (cfg) { if (cfg && cfg.enabled) { build(cfg); } })
    .catch(function () {});

  function build(cfg) {
    var color = /^#[0-9a-fA-F]{6}$/.test(cfg.color || "") ? cfg.color : "#0B1D40";
    var host = document.createElement("div");
    host.id = "phixtra-chat-root";
    host.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:2147483000;";
    document.body.appendChild(host);
    var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

    var css = [
      ":host{all:initial}",
      "*{box-sizing:border-box;font-family:Inter,system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif}",
      ".btn{width:60px;height:60px;border-radius:50%;border:0;background:" + color + ";color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 10px 30px rgba(11,29,64,.35)}",
      ".btn svg{width:28px;height:28px}",
      ".panel{position:absolute;right:0;bottom:76px;width:370px;max-width:calc(100vw - 32px);height:560px;max-height:calc(100vh - 120px);background:#fff;border-radius:18px;box-shadow:0 24px 60px rgba(11,29,64,.35);display:flex;flex-direction:column;overflow:hidden}",
      ".panel[hidden]{display:none}",
      ".top{background:" + color + ";color:#fff;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;gap:10px}",
      ".top b{font-size:15px;display:block}",
      ".top small{font-size:12px;opacity:.85}",
      ".x{background:transparent;border:0;color:#fff;cursor:pointer;width:36px;height:36px;border-radius:8px;font-size:20px;line-height:1}",
      ".x:hover{background:rgba(255,255,255,.12)}",
      ".msgs{flex:1;overflow-y:auto;padding:14px;background:#F4F6FA;display:flex;flex-direction:column;gap:10px}",
      ".m{max-width:85%;padding:9px 12px;border-radius:14px;font-size:14px;line-height:1.5;color:#111E2D;white-space:pre-wrap;word-wrap:break-word}",
      ".m.me{align-self:flex-end;background:" + color + ";color:#fff;border-bottom-right-radius:4px}",
      ".m.ai{align-self:flex-start;background:#fff;border:1px solid #D9E1EE;border-bottom-left-radius:4px}",
      ".m.ai a{color:" + color + ";font-weight:600}",
      ".m.typing{color:#4A5B78;font-style:italic}",
      ".m.sys{align-self:center;max-width:95%;background:#EEF2F8;border:1px dashed #C3CEDF;color:#4A5B78;font-size:12.5px;text-align:center}",
      ".who{align-self:flex-start;font-size:11.5px;color:#4A5B78;margin:0 0 -6px 4px}",
      ".dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3DD68C;margin-right:6px;vertical-align:middle}",
      ".btn{position:relative}",
      ".badge{position:absolute;top:2px;right:2px;width:14px;height:14px;border-radius:50%;background:#E5484D;border:2px solid #fff}",
      ".badge[hidden]{display:none}",
      ".cards{display:flex;flex-direction:column;gap:8px;align-self:flex-start;max-width:85%}",
      ".card{display:flex;gap:10px;background:#fff;border:1px solid #D9E1EE;border-radius:12px;padding:10px}",
      ".card img{width:64px;height:64px;object-fit:cover;border-radius:8px;flex:0 0 auto;background:#F4F6FA}",
      ".card b{font-size:13.5px;color:#111E2D;display:block}",
      ".card span{font-size:13px;color:#127A4A;font-weight:600;display:block;margin-top:2px}",
      ".card a{display:inline-block;margin-top:6px;font-size:12.5px;font-weight:700;color:#fff;background:" + color + ";padding:6px 10px;border-radius:8px;text-decoration:none}",
      ".form{align-self:stretch;background:#fff;border:1px solid #D9E1EE;border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px}",
      ".form p{margin:0;font-size:13.5px;color:#111E2D}",
      ".form input{width:100%;height:38px;border:1px solid #C3CEDF;border-radius:8px;padding:0 10px;font-size:14px}",
      ".form button{height:38px;border:0;border-radius:8px;background:" + color + ";color:#fff;font-weight:700;cursor:pointer}",
      ".bar{display:flex;gap:8px;padding:10px;border-top:1px solid #D9E1EE;background:#fff}",
      ".bar input{flex:1;min-width:0;height:44px;border:1px solid #C3CEDF;border-radius:12px;padding:0 12px;font-size:14px;color:#111E2D}",
      ".bar button{width:44px;height:44px;border:0;border-radius:12px;background:" + color + ";color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center}",
      ".bar button:disabled{opacity:.5;cursor:default}",
      ".bar svg{width:20px;height:20px}",
      ".foot{font-size:11px;color:#4A5B78;text-align:center;padding:0 0 8px;background:#fff}",
      "input:focus,button:focus-visible{outline:2px solid " + color + ";outline-offset:1px}",
      "@media (max-width:480px){.panel{position:fixed;right:8px;left:8px;bottom:86px;width:auto;height:calc(100vh - 110px)}}"
    ].join("");
    var style = document.createElement("style"); style.textContent = css; root.appendChild(style);

    var chatIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.4 8.4 0 0 1-12.4 7.4L3 21l2.1-5.4A8.4 8.4 0 1 1 21 11.5z"/></svg>';
    var sendIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>';

    var btn = el("button", "btn"); btn.type = "button"; btn.setAttribute("aria-label", "Chat with us"); btn.innerHTML = chatIcon;
    var badge = el("span", "badge"); badge.hidden = true; btn.appendChild(badge);
    var TEAM = cfg.mode === "team";
    var panel = el("div", "panel"); panel.hidden = true; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "Chat");
    var top = el("div", "top");
    var title = el("div"); var tb = el("b"); tb.textContent = cfg.title || "Chat with us"; var ts = el("small"); ts.textContent = cfg.subtitle || "We usually reply straight away";
    title.appendChild(tb); title.appendChild(ts);
    var x = el("button", "x"); x.type = "button"; x.setAttribute("aria-label", "Close chat"); x.textContent = "\u00d7";
    top.appendChild(title); top.appendChild(x);
    var msgs = el("div", "msgs"); msgs.setAttribute("aria-live", "polite");
    var bar = el("form", "bar");
    var input = el("input"); input.type = "text"; input.placeholder = "Type your message"; input.maxLength = 1000; input.setAttribute("aria-label", "Your message");
    var send = el("button"); send.type = "submit"; send.setAttribute("aria-label", "Send"); send.innerHTML = sendIcon;
    bar.appendChild(input); bar.appendChild(send);
    var foot = el("div", "foot"); foot.textContent = "Powered by PhiXtra AI";
    panel.appendChild(top); panel.appendChild(msgs); panel.appendChild(bar); panel.appendChild(foot);
    root.appendChild(panel); root.appendChild(btn);

    var greeted = false;
    function open() { panel.hidden = false; badge.hidden = true; if (!greeted) { greeted = true; addMsg(cfg.greeting || "Hello! How can I help you today?", "ai"); } msgs.scrollTop = msgs.scrollHeight; setTimeout(function () { input.focus(); }, 50); }
    btn.addEventListener("click", function () { if (panel.hidden) { open(); } else { panel.hidden = true; } });
    x.addEventListener("click", function () { panel.hidden = true; btn.focus(); });

    function el(tag, cls) { var e = document.createElement(tag); if (cls) { e.className = cls; } return e; }
    function esc(t) { return String(t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
    function format(t) {
      var s = esc(t);
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
      s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
      s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
      return s;
    }
    function addMsg(t, who) { var m = el("div", "m " + who); m.innerHTML = who === "ai" ? format(t) : esc(t); msgs.appendChild(m); msgs.scrollTop = msgs.scrollHeight; return m; }
    function addCards(list) {
      if (!list || !list.length) { return; }
      var wrap = el("div", "cards");
      list.slice(0, 6).forEach(function (p) {
        var c = el("div", "card");
        if (p.image_url) { var im = el("img"); im.src = p.image_url; im.alt = ""; im.onerror = function () { im.remove(); }; c.appendChild(im); }
        var t = el("div"); var n = el("b"); n.textContent = p.name || ""; t.appendChild(n);
        if (p.price) { var pr = el("span"); pr.textContent = p.price; t.appendChild(pr); }
        if (p.url) { var a = el("a"); a.href = p.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = "View product"; t.appendChild(a); }
        c.appendChild(t); wrap.appendChild(c);
      });
      msgs.appendChild(wrap); msgs.scrollTop = msgs.scrollHeight;
    }
    var DKEY = "phixtra_w_details_" + SITE;
    function contactForm() {
      if (store(DKEY) === sessionId) { return; }
      store(DKEY, sessionId);
      var f = el("form", "form");
      var p = el("p"); p.textContent = TEAM ? "So we can still reach you if you leave this page:" : "Leave your details and our team will get back to you."; f.appendChild(p);
      var n = el("input"); n.placeholder = "Your name"; n.setAttribute("aria-label", "Your name");
      var ph = el("input"); ph.placeholder = "Phone number"; ph.type = "tel"; ph.setAttribute("aria-label", "Phone number");
      var em = el("input"); em.placeholder = "Email address"; em.type = "email"; em.setAttribute("aria-label", "Email address");
      var b = el("button"); b.type = "submit"; b.textContent = "Send my details";
      [n, ph, em, b].forEach(function (i) { f.appendChild(i); });
      f.addEventListener("submit", function (e) {
        e.preventDefault();
        if (!ph.value.trim() && !em.value.trim()) { p.textContent = "Please add a phone number or an email address."; return; }
        b.disabled = true;
        fetch(BASE + "/widget/handoff-contact", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ site: SITE, session_id: sessionId, visitor_name: n.value.trim(), visitor_phone: ph.value.trim(), visitor_email: em.value.trim() }) })
          .then(function () {
            var nm = n.value.trim(), mail = em.value.trim(), tel = ph.value.trim();
            var note = (nm ? "Thanks, " + nm.split(" ")[0] + ". " : "Thank you. ") + "We'll reply here. If you've left by then, " +
              (mail ? "we'll email you at " + mail + "." : "we'll call you on " + tel + ".");
            f.replaceWith(addMsg(note, "sys"));
            startPolling();
          })
          .catch(function () { b.disabled = false; p.textContent = "That didn't send. Please try again."; });
      });
      msgs.appendChild(f); msgs.scrollTop = msgs.scrollHeight;
    }

    // \u2500\u2500 Live replies from the business's team (2026-10-06) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var lastId = 0, polling = false, pollTimer = null;
    function staffMsg(r) {
      var w = el("div", "who"); w.textContent = (r.by ? r.by + " \u00b7 " : "") + (cfg.title || "Our team"); msgs.appendChild(w);
      addMsg(r.text, "ai");
      if (r.by) { ts.innerHTML = '<span class="dot"></span>'; ts.appendChild(document.createTextNode(r.by + " is here")); }
    }
    function pollOnce(first) {
      return fetch(BASE + "/widget/poll?site=" + encodeURIComponent(SITE) + "&session_id=" + encodeURIComponent(sessionId) + "&after=" + lastId + (first ? "&history=1" : ""))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) { return false; }
          if (first && d.history && d.history.length) {
            greeted = true;
            d.history.forEach(function (h) { if (h.kind === "staff") { staffMsg(h); } else { addMsg(h.text, h.kind === "visitor" ? "me" : "ai"); } });
          }
          (d.replies || []).forEach(function (r) { staffMsg(r); });
          if ((d.replies || []).length && panel.hidden) { badge.hidden = false; }
          lastId = Math.max(lastId, d.last_id || 0);
          return !!((d.history && d.history.length) || lastId);
        }).catch(function () { return false; });
    }
    function tick() { pollTimer = null; pollOnce(false).then(function () { pollTimer = setTimeout(tick, panel.hidden ? 15000 : 4000); }); }
    function startPolling() { if (polling) { return; } polling = true; pollTimer = setTimeout(tick, 4000); }
    btn.addEventListener("click", function () { if (polling && !panel.hidden) { clearTimeout(pollTimer); tick(); } });
    pollOnce(true).then(function (had) { if (had) { startPolling(); } });

    async function tell(text) {
      try {
        var res = await fetch(BASE + "/widget/message", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ site: SITE, message: text, session_id: sessionId }) });
        var d = await res.json();
        if (!res.ok) { throw new Error(d.detail || "error"); }
        if (d.first) { addMsg("Thanks for your message. Someone from our team will reply here shortly.", "sys"); }
        if (!d.has_contact) { contactForm(); }
        startPolling();
      } catch (e) {
        addMsg((e && e.message && e.message !== "error") ? e.message : "Sorry, we couldn't send that. Please try again in a moment.", "sys");
      }
    }

    async function ask(text) {
      var typing = addMsg("Typing\u2026", "ai typing"), bubble = null, shown = "", timer = null;
      function draw() { timer = null; if (bubble) { bubble.innerHTML = format(shown); msgs.scrollTop = msgs.scrollHeight; } }
      try {
        var res = await fetch(BASE + "/widget/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ site: SITE, message: text, session_id: sessionId }) });
        if (!res.ok || !res.body) { throw new Error("http " + res.status); }
        var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
        while (true) {
          var ch = await reader.read(); if (ch.done) { break; }
          buf += dec.decode(ch.value, { stream: true }).replace(/\r\n/g, "\n");
          var cut;
          while ((cut = buf.indexOf("\n\n")) >= 0) {
            var block = buf.slice(0, cut); buf = buf.slice(cut + 2);
            var ev = "", raw = "";
            block.split("\n").forEach(function (l) { if (l.indexOf("event: ") === 0) { ev = l.slice(7); } else if (l.indexOf("data: ") === 0) { raw += l.slice(6); } });
            if (!ev || !raw) { continue; }
            var data = JSON.parse(raw);
            if (ev === "delta") {
              if (!bubble) { typing.remove(); bubble = addMsg("", "ai"); }
              shown += data; if (!timer) { timer = setTimeout(draw, 80); }
            } else if (ev === "done") {
              if (timer) { clearTimeout(timer); }
              if (data.session_id) { sessionId = data.session_id; store(SKEY, sessionId); }
              var final = data.reply || shown;
              if (data.quota_exceeded || !final) { final = final || "Thanks for your message. Our team will get back to you soon."; }
              if (!bubble) { typing.remove(); bubble = addMsg("", "ai"); }
              bubble.innerHTML = format(final);
              addCards(data.product_recommendations);
              if (data.handoff_triggered) { contactForm(); startPolling(); }
              return;
            } else if (ev === "error") {
              throw new Error(data.detail || "error");
            }
          }
        }
        if (bubble) { draw(); return; }
        throw new Error("empty");
      } catch (e) {
        typing.remove();
        if (bubble) { draw(); return; }
        addMsg("Sorry, we couldn't send that. Please try again in a moment.", "ai");
      }
    }

    bar.addEventListener("submit", function (e) {
      e.preventDefault();
      var t = input.value.trim(); if (!t) { return; }
      input.value = ""; addMsg(t, "me"); send.disabled = true;
      (TEAM ? tell(t) : ask(t)).then(function () { send.disabled = false; input.focus(); });
    });
  }
})();
