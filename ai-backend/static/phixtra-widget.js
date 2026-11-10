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
  // Chat link (2026-10-29): data-mode="page" fills the page (chat.phixtra.com/c/<name>),
  // opens straight away and shows data-suggestions as questions to tap.
  var PAGE = script.getAttribute("data-mode") === "page";
  var TOPPX = parseInt(script.getAttribute("data-top") || "0", 10) || 0;
  var GREET = script.getAttribute("data-greeting") || "";
  var SUGG = []; try { SUGG = JSON.parse(script.getAttribute("data-suggestions") || "[]") || []; } catch (e) { SUGG = []; }
  var BASE = (script.src || "").replace(/\/widget\.js.*$/, "");
  // Quote list (2026-10-31, PhiXtra Quotations): the portal takes website quote requests.
  var PORTAL = script.getAttribute("data-portal") || "https://portal.phixtra.com";
  if (!SITE || !BASE) { return; }

  function store(k, v) { try { if (v === undefined) { return localStorage.getItem(k); } localStorage.setItem(k, v); } catch (e) { return null; } }
  var SKEY = "phixtra_w_session_" + SITE;
  // "Try it yourself" demo: the page names the visitor's own private chat.
  var FIXSID = script.getAttribute("data-session") || "";
  // Logged-in Shopify customer (2026-11-02, Shopify app Part 3): sent by the
  // PhiXtra chat app embed, signed by the store, checked by PhiXtra.
  var VSIG = script.getAttribute("data-visitor-sig") || "";
  var VISITOR = VSIG ? { name: script.getAttribute("data-visitor-name") || "", email: script.getAttribute("data-visitor-email") || "",
                         company: script.getAttribute("data-visitor-company") || "",
                         location: script.getAttribute("data-visitor-location") || "" } : null;
  function withVisitor(o) { if (VISITOR) { o.visitor = VISITOR; o.visitor_sig = VSIG; } return o; }
  if (FIXSID) { SKEY += "_" + FIXSID; store(SKEY, FIXSID); }
  var sessionId = store(SKEY) || FIXSID;
  if (!sessionId) { sessionId = "w-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); store(SKEY, sessionId); }

  fetch(BASE + "/widget/config?site=" + encodeURIComponent(SITE) + "&lang=" + encodeURIComponent(navigator.language || ""), { method: "GET" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (cfg) { if (cfg && cfg.enabled) { build(cfg); } })
    .catch(function () {});

  function build(cfg) {
    // Customise chat box Phase 2 (9 Nov 2026): the business can hide the chat on phones.
    if (!PAGE && cfg.phones === false && window.matchMedia && window.matchMedia("(max-width: 767px)").matches) { return; }
    var LEFT = !PAGE && cfg.position === "left";
    // All six positions (10 Nov 2026): bottom / middle / top, right or left.
    var VPOS = PAGE ? "bottom" : (cfg.vpos === "middle" || cfg.vpos === "top" ? cfg.vpos : "bottom");
    var HEX = /^#[0-9a-fA-F]{6}$/;
    var agentText = HEX.test(cfg.agent_text || "") ? cfg.agent_text : null;
    var visitorText = HEX.test(cfg.visitor_text || "") ? cfg.visitor_text : null;
    var visitorSize = (cfg.visitor_size >= 10 && cfg.visitor_size <= 24) ? cfg.visitor_size : null;
    if (!SUGG.length && cfg.questions && cfg.questions.length) { SUGG = cfg.questions.slice(0, 4); }
    var color = /^#[0-9a-fA-F]{6}$/.test(cfg.color || "") ? cfg.color : "#0B1D40";
    // Customise chat box (9 Nov 2026): text on the business's colour stays readable.
    var fg = (function (h) { var f = function (i) { var v = parseInt(h.substr(i, 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return (0.2126 * f(1) + 0.7152 * f(3) + 0.0722 * f(5)) > 0.42 ? "#111111" : "#fff"; })(color);
    var FONTS = { Inter: "Inter,system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif", Roboto: "Roboto,system-ui,Arial,sans-serif",
                  Lato: "Lato,system-ui,Arial,sans-serif", Merriweather: "Merriweather,Georgia,serif" };
    var font = FONTS.Inter;
    if (cfg.font === "store") { try { font = getComputedStyle(document.body).fontFamily || font; } catch (e) { /* keep Inter */ } }
    else if (FONTS[cfg.font]) {
      font = FONTS[cfg.font];
      if (cfg.font !== "Inter") { var fl = document.createElement("link"); fl.rel = "stylesheet"; fl.href = "https://fonts.googleapis.com/css2?family=" + cfg.font + ":wght@400;700&display=swap"; document.head.appendChild(fl); }
    }
    var LARGE = cfg.size === "large";
    var AV = cfg.avatar || "none";
    var host = document.createElement("div");
    host.id = "phixtra-chat-root";
    host.style.cssText = PAGE ? "position:fixed;left:0;right:0;bottom:0;top:" + TOPPX + "px;z-index:2147483000;"
                              : "position:fixed;" + (LEFT ? "left" : "right") + ":20px;" +
                                (VPOS === "top" ? "top:20px;" : VPOS === "middle" ? "top:calc(50% - 30px);" : "bottom:20px;") +
                                "z-index:2147483000;";
    // Many store themes (e.g. Shopify's Dawn: div:empty{display:none}) hide
    // empty boxes; ours looks empty because its content is in a shadow root
    // (2026-11-02). Keep it shown whatever the page's styles say.
    host.style.setProperty("display", "block", "important");
    host.style.setProperty("visibility", "visible", "important");
    document.body.appendChild(host);
    var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

    var css = [
      ":host{all:initial}",
      "*{box-sizing:border-box;font-family:" + font.replace(/"/g, "'") + "}",
      ".btn{width:60px;height:60px;border-radius:50%;border:0;background:" + color + ";color:" + fg + ";cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 10px 30px rgba(11,29,64,.35)}",
      ".btn svg{width:28px;height:28px}",
      ".panel{position:absolute;" + (LEFT ? "left" : "right") + ":0;bottom:76px;width:" + (LARGE ? 440 : 370) + "px;max-width:calc(100vw - 32px);height:" + (LARGE ? 640 : 560) + "px;max-height:calc(100vh - 120px);background:#fff;border-radius:18px;box-shadow:0 24px 60px rgba(11,29,64,.35);display:flex;flex-direction:column;overflow:hidden}",
      ".panel[hidden]{display:none}",
      // Top: the window opens below the button. Middle: beside it, from the bottom of the screen.
      (VPOS === "top" ? ".panel{bottom:auto;top:76px}" : ""),
      (VPOS === "middle" ? ".panel{position:fixed;bottom:20px;" + (LEFT ? "left" : "right") + ":96px;max-width:calc(100vw - 120px)}" : ""),
      ".top{background:" + color + ";color:" + fg + ";padding:14px 16px;display:flex;align-items:center;justify-content:space-between;gap:10px}",
      ".top b{font-size:15px;display:block}",
      ".top small{font-size:12px;opacity:.85}",
      ".x{background:transparent;border:0;color:" + fg + ";cursor:pointer;width:36px;height:36px;border-radius:8px;font-size:20px;line-height:1}",
      ".x:hover{background:rgba(255,255,255,.12)}",
      ".msgs{flex:1;overflow-y:auto;padding:14px;background:#F4F6FA;display:flex;flex-direction:column;gap:10px}",
      ".m{max-width:85%;padding:9px 12px;border-radius:14px;font-size:14px;line-height:1.5;color:#111E2D;white-space:pre-wrap;word-wrap:break-word}",
      ".m.me{align-self:flex-end;background:" + color + ";color:" + (visitorText || fg) + ";border-bottom-right-radius:4px" + (visitorSize ? ";font-size:" + visitorSize + "px" : "") + "}",
      ".m.ai{align-self:flex-start;background:#fff;border:1px solid #D9E1EE;border-bottom-left-radius:4px" + (agentText ? ";color:" + agentText : "") + "}",
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
      /* photo gallery on a product card (2026-10-25) */
      ".card.g{flex-direction:column;gap:8px}",
      ".gal{position:relative}",
      ".gal img.big{width:100%;height:200px;object-fit:cover;border-radius:10px;display:block;background:#F4F6FA}",
      ".gal button.nav{position:absolute;top:78px;width:40px;height:40px;border-radius:50%;border:0;background:#fff;color:#0B1D40;font-size:20px;line-height:40px;cursor:pointer;box-shadow:0 1px 4px rgba(3,12,24,.25);padding:0}",
      ".gal button.prev{left:6px}.gal button.next{right:6px}",
      ".gal .cnt{position:absolute;right:8px;bottom:8px;background:rgba(11,29,64,.85);color:#fff;font-size:11px;font-weight:700;border-radius:99px;padding:2px 8px}",
      ".gal .lab{position:absolute;left:8px;bottom:8px;background:rgba(255,255,255,.92);color:#111E2D;font-size:11px;font-weight:700;border-radius:99px;padding:2px 8px}",
      ".thumbs{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px}",
      ".thumbs button{border:2px solid transparent;border-radius:7px;padding:0;background:none;cursor:pointer;flex:0 0 auto}",
      ".thumbs button.on{border-color:#0B1D40}",
      ".thumbs img{width:40px;height:40px;object-fit:cover;border-radius:5px;display:block}",
      ".card b{font-size:13.5px;color:#111E2D;display:block}",
      ".card span{font-size:13px;color:#127A4A;font-weight:600;display:block;margin-top:2px}",
      ".card a{display:inline-block;margin-top:6px;font-size:12.5px;font-weight:700;color:" + fg + ";background:" + color + ";padding:6px 10px;border-radius:8px;text-decoration:none}",
      ".form{align-self:stretch;background:#fff;border:1px solid #D9E1EE;border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px}",
      ".form p{margin:0;font-size:13.5px;color:#111E2D}",
      ".form input{width:100%;height:38px;border:1px solid #C3CEDF;border-radius:8px;padding:0 10px;font-size:14px}",
      ".form button{height:38px;border:0;border-radius:8px;background:" + color + ";color:" + fg + ";font-weight:700;cursor:pointer}",
      ".bar{display:flex;gap:8px;padding:10px;border-top:1px solid #D9E1EE;background:#fff}",
      ".bar input{flex:1;min-width:0;height:44px;border:1px solid #C3CEDF;border-radius:12px;padding:0 12px;font-size:14px;color:#111E2D}",
      ".bar button{width:44px;height:44px;border:0;border-radius:12px;background:" + color + ";color:" + fg + ";cursor:pointer;display:flex;align-items:center;justify-content:center}",
      ".bar button:disabled{opacity:.5;cursor:default}",
      ".bar svg{width:20px;height:20px}",
      ".foot{font-size:11px;color:#4A5B78;text-align:center;padding:0 0 8px;background:#fff}",
      "input:focus,button:focus-visible{outline:2px solid " + color + ";outline-offset:1px}",
      "@media (max-width:480px){.panel{position:fixed;right:8px;left:8px;bottom:86px;width:auto;height:calc(100vh - 110px)}}",
      // Phones: whatever the button's height, the window fills the screen above the bottom edge.
      (VPOS !== "bottom" ? "@media (max-width:480px){.panel{top:auto;bottom:12px;height:calc(100vh - 40px);max-width:none}}" : ""),
      ".sugg{display:flex;flex-direction:column;align-items:flex-start;gap:7px}",
      ".sugg button{border:1px solid #C3CEDF;background:#fff;color:#0B1D40;border-radius:999px;padding:8px 12px;font-size:13.5px;font-weight:600;cursor:pointer;text-align:left;max-width:100%}",
      ".sugg button:hover{border-color:" + color + "}",
      /* quote list (2026-10-31) */
      ".qbar{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 14px;background:#fff;border-bottom:1px solid #D9E1EE}",
      ".qbar[hidden]{display:none}",
      ".qbar button{border:1.5px solid " + color + ";background:#fff;color:" + color + ";border-radius:999px;padding:6px 12px;font-size:13px;font-weight:700;cursor:pointer}",
      ".qbar span{font-size:12px;color:#4A5B78}",
      ".qv{flex:1;overflow-y:auto;padding:12px 14px;background:#fff;display:flex;flex-direction:column;gap:10px;font-size:13.5px;color:#111E2D}",
      ".qv[hidden],.msgs[hidden],.bar[hidden]{display:none}",
      ".qv input,.qv textarea{width:100%;border:1px solid #C3CEDF;border-radius:8px;padding:8px 10px;font-size:14px;color:#111E2D}",
      ".qv label{display:flex;flex-direction:column;gap:3px;font-size:12.5px;font-weight:600}",
      ".qres{display:flex;flex-direction:column;gap:6px}",
      ".qres button,.qrow{display:flex;justify-content:space-between;align-items:center;gap:8px;border:1px solid #D9E1EE;background:#F8FAFD;border-radius:10px;padding:8px 10px;font-size:13px;text-align:left;cursor:pointer;color:#111E2D}",
      ".qrow{cursor:default;background:#fff}",
      ".qres button span:last-child{white-space:nowrap;font-weight:700;color:" + color + "}",
      ".qrow input{width:70px;padding:5px 6px}",
      ".qrow .rm{border:0;background:none;color:#B42318;font-size:12px;text-decoration:underline;cursor:pointer;padding:0}",
      ".qest{display:flex;justify-content:space-between;background:#F4F6FA;border-radius:8px;padding:8px 10px;font-size:12.5px}",
      ".qest b{font-size:15px}",
      ".qgo{height:42px;border:0;border-radius:10px;background:" + color + ";color:" + fg + ";font-weight:700;font-size:14px;cursor:pointer}",
      ".qgo:disabled{opacity:.6}",
      ".qerr{color:#B42318;font-size:13px;margin:0}",
      ".qok{background:#E6F4EC;color:#067647;border-radius:10px;padding:12px;font-weight:600;margin:0}",
      ".qhp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}",
      ".qmuted{color:#4A5B78;font-size:12.5px;margin:0}",
      ".btn.w{width:auto;padding:0 20px 0 16px;border-radius:999px;gap:8px;font-size:15px;font-weight:700;white-space:nowrap}",
      ".btn.w svg{width:22px;height:22px}",
      /* Customise chat box (9 Nov 2026): logo, avatar, emojis and stickers */
      ".lg{width:36px;height:36px;border-radius:50%;object-fit:cover;flex:0 0 auto;background:rgba(255,255,255,.2)}",
      ".tl{display:flex;align-items:center;gap:10px;min-width:0}",
      ".row{display:flex;gap:8px;align-items:flex-end;align-self:flex-start;max-width:92%}",
      ".row .m{max-width:100%}",
      ".av{width:28px;height:28px;border-radius:50%;flex:0 0 auto;display:flex;align-items:center;justify-content:center;font-size:11.5px;font-weight:700;overflow:hidden;background:" + color + ";color:" + fg + "}",
      ".av img{width:100%;height:100%;object-fit:cover}.av svg{width:17px;height:17px}",
      ".eb{width:36px!important;background:transparent!important;color:#111E2D!important;font-size:20px;padding:0}",
      ".pk{border-top:1px solid #D9E1EE;background:#fff;padding:10px 12px;display:flex;flex-direction:column;gap:8px}",
      ".pk[hidden]{display:none}",
      ".pk .e{display:flex;flex-wrap:wrap;gap:2px}.pk .e button{border:0;background:none;font-size:21px;cursor:pointer;padding:3px;border-radius:6px}",
      ".pk .e button:hover{background:#F4F6FA}",
      ".pk .s{display:flex;flex-wrap:wrap;gap:6px}.pk .s button{border:0;cursor:pointer;font-size:12.5px;font-weight:700;padding:6px 10px;border-radius:12px;background:#FFF4D6;color:#5A4300}",
      ".m.stk{background:#FFF4D6!important;color:#5A4300!important;font-weight:700;border:0}"
    ].join("") + (PAGE ? ":host{display:block;width:100%;height:100%}.btn,.x{display:none!important}" +
      ".panel{position:absolute;inset:0;width:auto;max-width:none;height:auto;max-height:none;border-radius:0;box-shadow:none}" +
      "@media (min-width:700px){.panel{left:50%;right:auto;width:520px;transform:translateX(-50%);top:20px;bottom:20px;border-radius:18px;box-shadow:0 20px 60px rgba(11,29,64,.18)}}" +
      "@media (max-width:480px){.panel{position:absolute;left:0;right:0;bottom:0;top:0;width:auto;height:auto}}" : "");
    var style = document.createElement("style"); style.textContent = css; root.appendChild(style);

    var chatIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.4 8.4 0 0 1-12.4 7.4L3 21l2.1-5.4A8.4 8.4 0 1 1 21 11.5z"/></svg>';
    var sendIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>';

    var btn = el("button", "btn"); btn.type = "button"; btn.setAttribute("aria-label", cfg.button_words || "Chat with us"); btn.innerHTML = chatIcon;
    if (cfg.button_words) { btn.className = "btn w"; var bwl = el("span"); bwl.textContent = cfg.button_words; btn.appendChild(bwl); }
    var badge = el("span", "badge"); badge.hidden = true; btn.appendChild(badge);
    var TEAM = cfg.mode === "team";
    var panel = el("div", "panel"); panel.hidden = true; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "Chat");
    var top = el("div", "top");
    var title = el("div"); var tb = el("b"); tb.textContent = cfg.title || "Chat with us"; var ts = el("small"); ts.textContent = cfg.subtitle || "We usually reply straight away";
    title.appendChild(tb); title.appendChild(ts);
    // Business hours (Phase 3, 10 Nov 2026): green dot when open, grey + when the team is back when closed.
    var CLOSED = cfg.hours && cfg.open === false && cfg.closed ? cfg.closed : null;
    if (cfg.hours) {
      ts.textContent = ""; var hd = el("span", "dot"); if (CLOSED) { hd.style.background = "#A0A9B8"; }
      ts.appendChild(hd); ts.appendChild(document.createTextNode(CLOSED ? CLOSED.closed_sub : (cfg.subtitle || "We usually reply straight away")));
    }
    if (cfg.logo) { var tl = el("div", "tl"), lgi = el("img", "lg"); lgi.src = cfg.logo; lgi.alt = ""; lgi.onerror = function () { lgi.remove(); }; tl.appendChild(lgi); tl.appendChild(title); title = tl; }
    var x = el("button", "x"); x.type = "button"; x.setAttribute("aria-label", "Close chat"); x.textContent = "\u00d7";
    top.appendChild(title); top.appendChild(x);
    var msgs = el("div", "msgs"); msgs.setAttribute("aria-live", "polite");
    var bar = el("form", "bar");
    var input = el("input"); input.type = "text"; input.placeholder = "Type your message"; input.maxLength = 1000; input.setAttribute("aria-label", "Your message");
    var send = el("button"); send.type = "submit"; send.setAttribute("aria-label", "Send"); send.innerHTML = sendIcon;
    var STICKERS = ["Thanks!", "Great \ud83d\udc4d", "Deal \ud83e\udd1d", "Hello \ud83d\udc4b"];
    var pick = null;
    if (cfg.emojis) {
      var eb = el("button", "eb"); eb.type = "button"; eb.setAttribute("aria-label", "Emojis and stickers"); eb.textContent = "\ud83d\ude42";
      pick = el("div", "pk"); pick.hidden = true;
      var er = el("div", "e");
      ["\ud83d\ude00", "\ud83d\ude0a", "\ud83d\udc4d", "\ud83d\ude4f", "\ud83c\udf89", "\u2764\ufe0f", "\ud83d\udc4c", "\ud83e\udd1d", "\ud83d\ude02", "\ud83d\ude2e"].forEach(function (e) {
        var b = el("button"); b.type = "button"; b.textContent = e; b.setAttribute("aria-label", "Add " + e);
        b.addEventListener("click", function () { input.value += e; input.focus(); }); er.appendChild(b);
      });
      var sr = el("div", "s");
      STICKERS.forEach(function (t) { var b = el("button"); b.type = "button"; b.textContent = t; b.addEventListener("click", function () { pick.hidden = true; sendText(t); }); sr.appendChild(b); });
      pick.appendChild(er); pick.appendChild(sr);
      eb.addEventListener("click", function () { pick.hidden = !pick.hidden; });
      bar.appendChild(eb);
    }
    bar.appendChild(input); bar.appendChild(send);
    var foot = el("div", "foot"); foot.textContent = "Powered by PhiXtra AI";
    panel.appendChild(top); panel.appendChild(msgs); if (pick) { panel.appendChild(pick); } panel.appendChild(bar); panel.appendChild(foot);
    root.appendChild(panel); root.appendChild(btn);
    setupQuote(panel, top, msgs, bar);

    var greeted = false;
    function open() { panel.hidden = false; badge.hidden = true; if (!greeted) { greeted = true; addMsg(GREET || cfg.greeting || "Hello! How can I help you today?", "ai"); showSugg();
        if (CLOSED && TEAM) { addMsg(CLOSED.closed_msg, "sys"); contactForm(CLOSED.closed_form); } } msgs.scrollTop = msgs.scrollHeight; setTimeout(function () { input.focus(); }, 50); }
    var suggBox = null;
    function showSugg() {
      if (!SUGG.length || suggBox) { return; }
      suggBox = el("div", "sugg");
      SUGG.slice(0, 6).forEach(function (q) {
        var b = el("button"); b.type = "button"; b.textContent = String(q);
        b.addEventListener("click", function () { sendText(String(q).replace(/^\S+\s/, function (m) { return /[\u{1F1E6}-\u{1F1FF}]/u.test(m) ? "" : m; })); });
        suggBox.appendChild(b);
      });
      msgs.appendChild(suggBox);
    }
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
    var BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01"/></svg>';
    function avatar(staff) {
      var a = el("div", "av");
      if (staff) { a.textContent = staff.split(/\s+/).map(function (w) { return w.charAt(0); }).join("").slice(0, 2).toUpperCase(); return a; }
      var src = AV === "upload" ? cfg.avatar_img : AV === "logo" ? cfg.logo : null;
      if (src) { var im = el("img"); im.src = src; im.alt = ""; a.appendChild(im); }
      else if (AV === "bot") { a.innerHTML = BOT; }
      else { a.textContent = String(cfg.title || "C").charAt(0).toUpperCase(); }
      return a;
    }
    function addMsg(t, who, staff) {
      var m = el("div", "m " + who); m.innerHTML = who.indexOf("ai") === 0 ? format(t) : esc(t);
      if (who === "me" && STICKERS.indexOf(t) > -1) { m.className = "m me stk"; }
      if (who.indexOf("ai") === 0 && (AV !== "none" || staff)) {
        var row = el("div", "row"); row.appendChild(avatar(staff)); row.appendChild(m); msgs.appendChild(row);
        m.remove = function () { row.parentNode && row.parentNode.removeChild(row); };
      } else { msgs.appendChild(m); }
      msgs.scrollTop = msgs.scrollHeight; return m;
    }
    function addCards(list) {
      if (!list || !list.length) { return; }
      var wrap = el("div", "cards");
      list.slice(0, 6).forEach(function (p) {
        var c = el("div", "card");
        var imgs = (p.images || []).filter(function (x) { return x && x.url; });
        if (imgs.length > 1) { c.className = "card g"; c.appendChild(gallery(imgs, p.name || "")); }
        else if (p.image_url) { var im = el("img"); im.src = p.image_url; im.alt = ""; im.onerror = function () { im.remove(); }; c.appendChild(im); }
        var t = el("div"); var n = el("b"); n.textContent = p.name || ""; t.appendChild(n);
        if (p.price) { var pr = el("span"); pr.textContent = p.price; t.appendChild(pr); }
        if (p.url) { var a = el("a"); a.href = p.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = "View product"; t.appendChild(a); }
        c.appendChild(t); wrap.appendChild(c);
      });
      msgs.appendChild(wrap); msgs.scrollTop = msgs.scrollHeight;
    }
    // Several photos of one product: arrows, small pictures, swipe (2026-10-25).
    function gallery(imgs, name) {
      var g = el("div"), box = el("div", "gal"), big = el("img", "big"), cnt = el("span", "cnt"), lab = el("span", "lab");
      var th = el("div", "thumbs"), i = 0, x0 = null;
      function show(n) {
        i = (n + imgs.length) % imgs.length;
        big.src = imgs[i].url; big.alt = name + (imgs[i].label ? " · " + imgs[i].label : "") + " (photo " + (i + 1) + " of " + imgs.length + ")";
        cnt.textContent = (i + 1) + " / " + imgs.length;
        lab.textContent = imgs[i].label || ""; lab.hidden = !imgs[i].label;
        Array.prototype.forEach.call(th.children, function (b, k) { b.className = k === i ? "on" : ""; b.setAttribute("aria-current", k === i ? "true" : "false"); });
      }
      var prev = el("button", "nav prev"), next = el("button", "nav next");
      prev.type = next.type = "button"; prev.textContent = "\u2039"; next.textContent = "\u203A";
      prev.setAttribute("aria-label", "Previous photo"); next.setAttribute("aria-label", "Next photo");
      prev.addEventListener("click", function () { show(i - 1); }); next.addEventListener("click", function () { show(i + 1); });
      box.addEventListener("touchstart", function (e) { x0 = e.touches[0].clientX; }, { passive: true });
      box.addEventListener("touchend", function (e) { if (x0 === null) { return; } var dx = e.changedTouches[0].clientX - x0; x0 = null; if (Math.abs(dx) > 35) { show(i + (dx < 0 ? 1 : -1)); } });
      imgs.forEach(function (im, k) {
        var b = el("button"); b.type = "button"; b.setAttribute("aria-label", "Photo " + (k + 1) + (im.label ? ": " + im.label : ""));
        var t = el("img"); t.src = im.thumb || im.url; t.alt = ""; b.appendChild(t);
        b.addEventListener("click", function () { show(k); }); th.appendChild(b);
      });
      box.appendChild(big); box.appendChild(prev); box.appendChild(next); box.appendChild(lab); box.appendChild(cnt);
      g.appendChild(box); g.appendChild(th); show(0);
      return g;
    }
    var DKEY = "phixtra_w_details_" + SITE;
    function contactForm(prompt) {
      if (store(DKEY) === sessionId) { return; }
      store(DKEY, sessionId);
      var f = el("form", "form");
      var p = el("p"); p.textContent = prompt || (TEAM ? "So we can still reach you if you leave this page:" : "Leave your details and our team will get back to you."); f.appendChild(p);
      var n = el("input"); n.placeholder = "Your name"; n.setAttribute("aria-label", "Your name");
      var ph = el("input"); ph.placeholder = "Phone number"; ph.type = "tel"; ph.setAttribute("aria-label", "Phone number");
      var em = el("input"); em.placeholder = "Email address"; em.type = "email"; em.setAttribute("aria-label", "Email address");
      if (VISITOR) { n.value = VISITOR.name; em.value = VISITOR.email; }
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
      addMsg(r.text, "ai", r.by || (cfg.title || "Team"));
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
          body: JSON.stringify(withVisitor({ site: SITE, message: text, session_id: sessionId })) });
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
          body: JSON.stringify(withVisitor({ site: SITE, message: text, session_id: sessionId })) });
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
              if (data.handoff_triggered) { if (CLOSED) { addMsg(CLOSED.closed_msg, "sys"); } contactForm(CLOSED ? CLOSED.closed_form : null); startPolling(); }
              else if (PAGE) { startPolling(); }  // chat link page: the team can step in any time
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

    function sendText(t) {
      t = (t || "").trim(); if (!t || send.disabled) { return; }
      if (suggBox) { suggBox.remove(); suggBox = null; SUGG = []; }
      input.value = ""; addMsg(t, "me"); send.disabled = true;
      (TEAM ? tell(t) : ask(t)).then(function () { send.disabled = false; input.focus(); });
    }
    bar.addEventListener("submit", function (e) { e.preventDefault(); sendText(input.value); });
    if (PAGE) { open(); }

    // ── Request a quote (2026-10-31): a quote list inside the chat box, when
    // the business has switched it on in PhiXtra (Quotations › Quotation settings).
    function setupQuote(panel, top, msgs, bar) {
      fetch(PORTAL + "/public/quote/config?site=" + encodeURIComponent(SITE))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (q) { if (q && q.enabled) { buildQuote(q); } })
        .catch(function () {});
      function buildQuote(q) {
        var QKEY = "phixtra_w_quote_" + SITE, list = [];
        try { list = JSON.parse(localStorage.getItem(QKEY) || "[]") || []; } catch (e) { list = []; }
        function keep() { try { localStorage.setItem(QKEY, JSON.stringify(list)); } catch (e) { /* private mode */ } }
        var showPrices = q.prices === "list_estimate", showEst = q.prices !== "none";
        function money(v) { return (q.prefix || "") + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
        var qbar = el("div", "qbar");
        var qb = el("button"); qb.type = "button"; qbar.appendChild(el("span")).textContent = "Need prices for several products?"; qbar.appendChild(qb);
        var qv = el("div", "qv"); qv.hidden = true;
        panel.insertBefore(qbar, msgs); panel.insertBefore(qv, msgs);
        var sent = null, inQuote = false;
        function label() { qb.textContent = inQuote ? "\u2190 Back to chat" : "Request a quote" + (list.length ? " (" + list.length + ")" : ""); }
        label();
        qb.addEventListener("click", function () { inQuote = !inQuote; qv.hidden = !inQuote; msgs.hidden = inQuote; bar.hidden = inQuote; label(); if (inQuote) { draw(); } });
        function txt(tag, cls, t) { var e = el(tag, cls); e.textContent = t; return e; }
        function draw() {
          qv.innerHTML = "";
          if (sent) {
            qv.appendChild(txt("p", "qok", sent));
            var b = txt("button", "qgo", "Back to chat"); b.type = "button";
            b.addEventListener("click", function () { sent = null; qb.click(); }); qv.appendChild(b); return;
          }
          var sl = el("label"); sl.appendChild(document.createTextNode("Find a product"));
          var s = el("input"); s.type = "search"; s.placeholder = "e.g. paper cups"; s.maxLength = 80; sl.appendChild(s); qv.appendChild(sl);
          var res = el("div", "qres"); qv.appendChild(res);
          var timer = null;
          function search() {
            fetch(PORTAL + "/public/quote/products?site=" + encodeURIComponent(SITE) + "&q=" + encodeURIComponent(s.value.trim()))
              .then(function (r) { return r.ok ? r.json() : { items: [] }; })
              .then(function (j) {
                res.innerHTML = "";
                (j.items || []).slice(0, 8).forEach(function (p) {
                  var b = el("button"); b.type = "button";
                  b.appendChild(txt("span", "", p.name));
                  b.appendChild(txt("span", "", (showPrices && p.price !== null ? money(p.price) + " \u00b7 " : "") + "+ Add"));
                  b.addEventListener("click", function () {
                    var f = list.filter(function (l) { return l.id === p.id; })[0];
                    if (f) { f.qty += 1; } else { list.push({ id: p.id, name: p.name, qty: 1, price: p.price }); }
                    keep(); label(); draw();
                  });
                  res.appendChild(b);
                });
              }).catch(function () {});
          }
          s.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(function () { if (s.value.trim() || !list.length) { search(); } else { res.innerHTML = ""; } }, 250); });
          if (!list.length) { search(); }   // a first look at the products; once the list has some, results come from searching
          if (!list.length) { qv.appendChild(txt("p", "qmuted", "Add the products you need, then send one request.")); return; }
          qv.appendChild(txt("b", "", "Your quote list"));
          var total = 0, priced = false;
          list.forEach(function (l, i) {
            var row = el("div", "qrow"); row.appendChild(txt("span", "", l.name));
            var right = el("span");
            var n = el("input"); n.type = "number"; n.min = "1"; n.step = "any"; n.value = l.qty; n.setAttribute("aria-label", "Quantity of " + l.name);
            n.addEventListener("change", function () { l.qty = Math.max(1, parseFloat(n.value) || 1); keep(); draw(); });
            var rm = txt("button", "rm", "Remove"); rm.type = "button";
            rm.addEventListener("click", function () { list.splice(i, 1); keep(); label(); draw(); });
            right.appendChild(n); right.appendChild(rm); row.appendChild(right); qv.appendChild(row);
            if (l.price !== null && l.price !== undefined) { total += l.price * l.qty; priced = true; }
          });
          if (showEst && priced) {
            var est = el("div", "qest"); est.appendChild(txt("span", "", "Estimate at list prices, before delivery and tax")); est.appendChild(txt("b", "", money(total))); qv.appendChild(est);
          } else { qv.appendChild(txt("p", "qmuted", "Prices come in your quotation.")); }
          var f = el("form"); f.noValidate = true; f.style.cssText = "display:flex;flex-direction:column;gap:8px";
          [["company", "Company", "text"], ["name", "Your name", "text"], ["email", "Work email", "email"], ["phone", "Phone or WhatsApp (optional)", "tel"],
           ["deliver_to", "Deliver to (city, country)", "text"]].forEach(function (d) {
            var lb = el("label"); lb.appendChild(document.createTextNode(d[1])); var inp = el("input"); inp.name = d[0]; inp.type = d[2]; lb.appendChild(inp); f.appendChild(lb);
          });
          var nl = el("label"); nl.appendChild(document.createTextNode("Notes (any language)")); var ta = el("textarea"); ta.name = "notes"; ta.rows = 2; nl.appendChild(ta); f.appendChild(nl);
          var hp = el("input"); hp.name = "website"; hp.className = "qhp"; hp.tabIndex = -1; hp.setAttribute("aria-hidden", "true"); f.appendChild(hp);
          var err = el("p", "qerr"); err.setAttribute("role", "alert"); f.appendChild(err);
          var go = txt("button", "qgo", "Send quote request"); go.type = "submit"; f.appendChild(go);
          f.addEventListener("submit", function (e) {
            e.preventDefault();
            var v = function (k) { return (f.elements[k].value || "").trim(); };
            if (!v("name") && !v("company")) { err.textContent = "Please add your name or company."; return; }
            if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v("email"))) { err.textContent = "Please add a valid email so we can send your quotation."; return; }
            err.textContent = ""; go.disabled = true; go.textContent = "Sending\u2026";
            fetch(PORTAL + "/public/quote/submit", { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ site: SITE, lines: list.map(function (l) { return { product_id: l.id, name: l.name, qty: l.qty }; }),
                company: v("company"), name: v("name"), email: v("email"), phone: v("phone"), deliver_to: v("deliver_to"),
                notes: v("notes"), hp: f.elements.website.value, lang: navigator.language || "en", page: location.href }) })
              .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
              .then(function (x) {
                if (x.ok && x.j.ok) { sent = x.j.message || "Thank you! Your quote request has been sent."; list = []; keep(); label(); draw(); }
                else { err.textContent = (x.j && x.j.error) || "Sorry, something went wrong. Please try again."; go.disabled = false; go.textContent = "Send quote request"; }
              })
              .catch(function () { err.textContent = "Sorry, we couldn't send it. Please try again."; go.disabled = false; go.textContent = "Send quote request"; });
          });
          qv.appendChild(f);
        }
      }
    }
  }
})();
