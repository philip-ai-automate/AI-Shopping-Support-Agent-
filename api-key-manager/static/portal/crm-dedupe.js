/* crm-dedupe.js — warns when a typed name looks like an existing one, so
   users don't accidentally create duplicates ("Bello Fabric" vs
   "Bello Fabrics", or "VIP" vs "Vip Customer"). Shared by:
   - Companies: Add Company modal, "+ New Company…" box on Add/Edit Contact
     and the Contact detail Edit Profile panel (2026-09-09 CRM merge)
   - Tags: the "🏷️ Tags" page's "+ Create Tag" box, the Contacts list bulk
     "Add Tag" modal, and the Tags field (comma-separated) on the same three
     contact forms above (2026-09-09 tags unification) */
(function () {
  function normalize(s) {
    return (s || '').toLowerCase().trim().replace(/\s+/g, ' ');
  }
  function bigrams(s) {
    var out = [];
    for (var i = 0; i < s.length - 1; i++) out.push(s.substr(i, 2));
    return out;
  }
  // Dice's coefficient over character bigrams — cheap, no library, and
  // forgiving of small typos/pluralisation ("Bello Fabric" vs "Bello Fabrics").
  function similarity(a, b) {
    a = normalize(a); b = normalize(b);
    if (!a || !b) return 0;
    if (a === b) return 1;
    var ba = bigrams(a), bb = bigrams(b);
    if (!ba.length || !bb.length) return 0;
    var pool = bb.slice(), matches = 0;
    ba.forEach(function (g) {
      var idx = pool.indexOf(g);
      if (idx !== -1) { matches++; pool.splice(idx, 1); }
    });
    return (2 * matches) / (ba.length + bb.length);
  }

  function findHits(typedRaw, items) {
    var typed = normalize(typedRaw);
    if (typed.length < 3) return [];
    var hits = [];
    items.forEach(function (it) {
      if (normalize(it.name) === typed) return; // exact match — reused silently on save, no warning needed
      var s = similarity(typed, it.name);
      if (s >= 0.55) hits.push({ item: it, score: s });
    });
    hits.sort(function (a, b) { return b.score - a.score; });
    return hits.slice(0, 3);
  }

  function renderHits(warnBox, hits, labelText, onPick) {
    if (!hits.length) { warnBox.style.display = 'none'; warnBox.innerHTML = ''; return; }
    warnBox.innerHTML = '';
    var label = document.createElement('div');
    label.className = 'dedupe-label';
    label.textContent = labelText;
    warnBox.appendChild(label);
    hits.forEach(function (h) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dedupe-pill';
      btn.textContent = h.item.name;
      btn.addEventListener('click', function () {
        onPick(h.item);
        warnBox.style.display = 'none';
        warnBox.innerHTML = '';
      });
      warnBox.appendChild(btn);
    });
    warnBox.style.display = 'block';
  }

  // Reads {id, name} pairs out of a <select>'s options (skips blank/__new__).
  window.crmCompaniesFromSelect = function (selectEl) {
    var list = [];
    if (!selectEl) return list;
    Array.prototype.forEach.call(selectEl.options, function (opt) {
      if (opt.value && opt.value !== '__new__') list.push({ id: opt.value, name: opt.textContent });
    });
    return list;
  };

  // Wires a "similar X already exists" warning onto a text input that holds
  // ONE name (Add Company modal, "+ New Company…" box, "+ Create Tag", bulk
  // "Add Tag"). opts: { input, warnBox, getItems: fn()=>[{id,name,url?}],
  // onUse: fn(item), label: optional warning text }
  window.crmDedupeWatch = function (opts) {
    if (!opts.input || !opts.warnBox) return;
    var labelText = opts.label || 'Similar company already exists — did you mean:';
    var timer = null;
    opts.input.addEventListener('input', function () {
      clearTimeout(timer);
      var val = opts.input.value;
      timer = setTimeout(function () {
        var hits = findHits(val, opts.getItems ? (opts.getItems() || []) : (opts.getCompanies() || []));
        renderHits(opts.warnBox, hits, labelText, opts.onUse);
      }, 250);
    });
  };

  // Same idea, but for a COMMA-SEPARATED text field (the Tags field on
  // Add/Edit Contact and the Edit Profile panel) — only the segment
  // currently being typed (after the last comma) is checked, and picking a
  // suggestion replaces just that segment, keeping earlier tags intact.
  // opts: { input, warnBox, getItems: fn()=>[{id,name}], label: optional text }
  window.crmDedupeWatchCsv = function (opts) {
    if (!opts.input || !opts.warnBox) return;
    var labelText = opts.label || 'Similar tag already exists — did you mean:';
    var timer = null;
    opts.input.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { render(); }, 250);
    });

    function currentSegment() {
      var parts = opts.input.value.split(',');
      return parts[parts.length - 1].trim();
    }

    function render() {
      var hits = findHits(currentSegment(), opts.getItems() || []);
      renderHits(opts.warnBox, hits, labelText, function (item) {
        var parts = opts.input.value.split(',');
        parts[parts.length - 1] = ' ' + item.name;
        opts.input.value = parts.join(',').replace(/^\s+/, '');
        opts.input.dispatchEvent(new Event('input'));
      });
    }
  };

  // Duplicate-CONTACT check on the Add Contact form — phone (exact match,
  // since a real duplicate phone is a real duplicate contact) + name (fuzzy,
  // "did you mean" style, since two different people can share a name).
  // Never blocks saving — 2026-09-09, see All Contacts duplicate-prevention.
  // opts: { phoneInput, nameInput, phoneWarnBox, nameWarnBox,
  //         getContacts: fn()=>[{id,name,phone,url}] }
  window.crmDedupeWatchContact = function (opts) {
    if (!opts.getContacts) return;
    function normPhone(s) { return (s || '').replace(/\D/g, ''); }
    var timer = null;
    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(renderBoth, 250);
    }
    if (opts.phoneInput) opts.phoneInput.addEventListener('input', schedule);
    if (opts.nameInput) opts.nameInput.addEventListener('input', schedule);

    function renderBoth() {
      var contacts = opts.getContacts() || [];

      if (opts.phoneInput && opts.phoneWarnBox) {
        var typedPhone = normPhone(opts.phoneInput.value);
        var match = null;
        if (typedPhone.length >= 7) {
          for (var i = 0; i < contacts.length; i++) {
            if (contacts[i].phone && normPhone(contacts[i].phone) === typedPhone) { match = contacts[i]; break; }
          }
        }
        if (match) {
          opts.phoneWarnBox.innerHTML = '';
          var label = document.createElement('div');
          label.className = 'dedupe-label';
          label.textContent = 'A contact with this phone number already exists:';
          opts.phoneWarnBox.appendChild(label);
          var link = document.createElement('a');
          link.href = match.url;
          link.className = 'dedupe-pill';
          link.style.textDecoration = 'none';
          link.textContent = match.name + ' →';
          opts.phoneWarnBox.appendChild(link);
          opts.phoneWarnBox.style.display = 'block';
        } else {
          opts.phoneWarnBox.style.display = 'none';
          opts.phoneWarnBox.innerHTML = '';
        }
      }

      if (opts.nameInput && opts.nameWarnBox) {
        var hits = findHits(opts.nameInput.value, contacts);
        if (!hits.length) { opts.nameWarnBox.style.display = 'none'; opts.nameWarnBox.innerHTML = ''; return; }
        opts.nameWarnBox.innerHTML = '';
        var nlabel = document.createElement('div');
        nlabel.className = 'dedupe-label';
        nlabel.textContent = 'Similar contact(s) already exist — did you mean:';
        opts.nameWarnBox.appendChild(nlabel);
        hits.forEach(function (h) {
          var a = document.createElement('a');
          a.href = h.item.url;
          a.className = 'dedupe-pill';
          a.style.textDecoration = 'none';
          a.textContent = h.item.name + (h.item.phone ? ' (' + h.item.phone + ')' : '');
          opts.nameWarnBox.appendChild(a);
        });
        opts.nameWarnBox.style.display = 'block';
      }
    }
  };
})();
