/* Label picker (2026-09-28) — the chips + "+ Add label" control used on the
   contact page (About section) and in the Add/Edit Contact forms. Picks from
   the business's existing labels (the same list as the Labels page) or
   creates a new one, instead of typing comma-separated text.

   LabelPicker.mount(el, {
     labels:   ["Lagos", "VIP"],                  // labels already on the contact
     all:      [{name: "Lagos", count: 41}, ...], // every label the business has
     onChange: function (labels) { ... },         // called after add/remove
     hiddenInput: <input>                         // optional: kept as "a, b" (tags_csv)
   })
   Returns {get(), set(labels)}. */
(function () {
  if (window.LabelPicker) return;

  var css = [
    '.lp{display:flex;flex-wrap:wrap;gap:6px;align-items:center;position:relative}',
    '.lp-chip{display:inline-flex;align-items:center;gap:4px;background:#f1f5f9;border:1px solid #e4e7ec;border-radius:999px;padding:2px 4px 2px 9px;font-size:12px;font-weight:600;color:#101828}',
    '.lp-chip button{border:0;background:none;color:#667085;cursor:pointer;font-size:14px;line-height:1;padding:0 3px}',
    '.lp-chip button:hover{color:#d92d20}',
    '.lp-add{border:1px dashed #b8c0cc;background:#fff;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;color:#2563eb;cursor:pointer;font-family:inherit}',
    '.lp-pop{position:absolute;z-index:9500;top:calc(100% + 6px);left:0;width:250px;background:#fff;border:1px solid #e4e7ec;border-radius:12px;box-shadow:0 12px 32px rgba(16,24,40,.16);padding:8px}',
    '.lp-pop input{width:100%;box-sizing:border-box;border:1px solid #e4e7ec;border-radius:8px;padding:7px 9px;font:inherit;font-size:13px}',
    '.lp-pop ul{list-style:none;margin:6px 0 0;padding:0;max-height:200px;overflow:auto}',
    '.lp-pop li button{width:100%;text-align:left;background:none;border:0;padding:7px 8px;border-radius:7px;font:inherit;font-size:13px;cursor:pointer;display:flex;justify-content:space-between;gap:8px;color:#101828}',
    '.lp-pop li button:hover,.lp-pop li button:focus{background:#f2f4f7;outline:none}',
    '.lp-pop .lp-count{color:#667085;font-size:11px;white-space:nowrap}',
    '.lp-pop .lp-create{color:#2563eb;font-weight:600}',
    '.lp-pop .lp-none{color:#667085;font-size:12px;padding:6px 8px}'
  ].join('');
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  function closeAll() { document.querySelectorAll('.lp-pop').forEach(function (p) { p.remove(); }); }
  document.addEventListener('click', closeAll);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeAll(); });

  function mount(el, opts) {
    var labels = (opts.labels || []).slice();
    var all = (opts.all || []).map(function (a) { return typeof a === 'string' ? {name: a} : a; });

    function has(name) {
      return labels.some(function (l) { return l.toLowerCase() === name.toLowerCase(); });
    }
    function changed() {
      if (opts.hiddenInput) opts.hiddenInput.value = labels.join(', ');
      render();
      if (opts.onChange) opts.onChange(labels.slice());
    }
    function render() {
      el.classList.add('lp');
      el.innerHTML = '';
      labels.forEach(function (l) {
        var c = document.createElement('span'); c.className = 'lp-chip'; c.textContent = l;
        var x = document.createElement('button'); x.type = 'button'; x.textContent = '×';
        x.setAttribute('aria-label', 'Remove label ' + l); x.title = 'Remove';
        x.onclick = function (e) { e.stopPropagation(); labels = labels.filter(function (v) { return v !== l; }); changed(); };
        c.appendChild(x); el.appendChild(c);
      });
      var add = document.createElement('button'); add.type = 'button'; add.className = 'lp-add';
      add.textContent = '+ Add label';
      add.onclick = function (e) { e.stopPropagation(); open(); };
      el.appendChild(add);
    }
    function open() {
      closeAll();
      var pop = document.createElement('div'); pop.className = 'lp-pop';
      pop.onclick = function (e) { e.stopPropagation(); };
      var inp = document.createElement('input'); inp.type = 'text';
      inp.placeholder = 'Search or create a label'; inp.setAttribute('aria-label', 'Search labels');
      inp.maxLength = 50;
      var ul = document.createElement('ul');
      function pick(name) {
        name = name.trim().slice(0, 50);
        if (!name || has(name)) { closeAll(); return; }
        if (!all.some(function (a) { return a.name.toLowerCase() === name.toLowerCase(); })) all.push({name: name, count: 0});
        labels.push(name); closeAll(); changed();
      }
      function draw() {
        var q = inp.value.trim(); ul.innerHTML = '';
        var hits = all.filter(function (a) { return !has(a.name) && a.name.toLowerCase().indexOf(q.toLowerCase()) !== -1; });
        hits.slice(0, 40).forEach(function (a) {
          var li = document.createElement('li'); var b = document.createElement('button'); b.type = 'button';
          var n = document.createElement('span'); n.textContent = a.name; b.appendChild(n);
          if (a.count != null) {
            var cnt = document.createElement('span'); cnt.className = 'lp-count';
            cnt.textContent = a.count + (a.count === 1 ? ' contact' : ' contacts'); b.appendChild(cnt);
          }
          b.onclick = function () { pick(a.name); };
          li.appendChild(b); ul.appendChild(li);
        });
        var exact = all.some(function (a) { return a.name.toLowerCase() === q.toLowerCase(); });
        if (q && !exact) {
          var li = document.createElement('li'); var b = document.createElement('button'); b.type = 'button';
          b.className = 'lp-create'; b.textContent = '+ Create label "' + q + '"';
          b.onclick = function () { pick(q); };
          li.appendChild(b); ul.appendChild(li);
        }
        if (!ul.children.length) {
          var none = document.createElement('li'); none.className = 'lp-none';
          none.textContent = all.length ? 'All your labels are already on this contact.' : 'Type a name to create your first label.';
          ul.appendChild(none);
        }
      }
      inp.oninput = draw;
      inp.onkeydown = function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          var first = ul.querySelector('button');
          if (first) first.click();
        }
      };
      draw();
      pop.appendChild(inp); pop.appendChild(ul); el.appendChild(pop);
      // Keep the list on screen: open it leftwards if it would run off the right edge.
      var r = pop.getBoundingClientRect();
      if (r.right > window.innerWidth - 8) { pop.style.left = 'auto'; pop.style.right = '0'; }
      // preventScroll: focusing must never scroll a clipped card sideways.
      try { inp.focus({preventScroll: true}); } catch (e) { inp.focus(); }
    }

    if (opts.hiddenInput) opts.hiddenInput.value = labels.join(', ');
    render();
    return {
      get: function () { return labels.slice(); },
      set: function (list) { labels = (list || []).slice(); if (opts.hiddenInput) opts.hiddenInput.value = labels.join(', '); render(); }
    };
  }

  window.LabelPicker = {mount: mount};
})();
