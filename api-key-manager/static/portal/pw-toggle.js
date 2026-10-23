/* Eye button on every password / key box: tap to show what was typed or
   filled in by Chrome or a password manager, tap again to hide. Boxes that
   already have their own eye button (sign-up pages) are left alone. */
(function(){
  var EYE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_OFF = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

  function add(inp){
    if (inp.dataset.pwEye) return;
    inp.dataset.pwEye = '1';
    var parent = inp.parentNode;
    if (!parent || parent.querySelector('button[aria-label*="assword"]')) return;

    var cs = getComputedStyle(inp);
    var inline = (cs.display === 'inline-block' || cs.display === 'inline') &&
                 inp.offsetWidth > 0 && inp.offsetWidth < parent.clientWidth - 4;
    var wrap = document.createElement('span');
    wrap.style.cssText = 'position:relative;display:' + (inline ? 'inline-block' : 'block') +
                         ';flex:' + cs.flex + ';min-width:0';
    parent.insertBefore(wrap, inp);
    wrap.appendChild(inp);
    if (!inline) inp.style.width = '100%';
    inp.style.paddingRight = (parseFloat(cs.paddingRight) || 0) + 30 + 'px';

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Show password');
    btn.title = 'Show';
    btn.innerHTML = EYE;
    btn.style.cssText = 'position:absolute;right:6px;top:50%;transform:translateY(-50%);' +
      'background:none;border:0;padding:4px;margin:0;cursor:pointer;color:#64748B;' +
      'display:flex;align-items:center;line-height:0;box-shadow:none;width:auto;height:auto';
    btn.addEventListener('click', function(){
      var show = inp.type === 'password';
      inp.type = show ? 'text' : 'password';
      btn.innerHTML = show ? EYE_OFF : EYE;
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      btn.title = show ? 'Hide' : 'Show';
    });
    wrap.appendChild(btn);

    // Hide again when the form is sent, so the browser still offers to save it.
    if (inp.form) inp.form.addEventListener('submit', function(){
      if (inp.type === 'text') { inp.type = 'password'; btn.innerHTML = EYE; }
    });
  }

  function scan(root){
    (root.querySelectorAll ? root : document).querySelectorAll('input[type="password"]').forEach(add);
  }
  function start(){
    scan(document);
    new MutationObserver(function(muts){
      muts.forEach(function(m){ m.addedNodes.forEach(function(n){
        if (n.nodeType !== 1) return;
        if (n.matches && n.matches('input[type="password"]')) add(n); else scan(n);
      }); });
    }).observe(document.body, {childList: true, subtree: true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
