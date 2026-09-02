# /ui Live-Morph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein einziger Auto-Update-Mechanismus ersetzt vier ganzseitige Meta-Refreshes und zwei Fragment-Poller in der `/ui`-Konsole, sodass Scrollposition, Sortierung, Filtereingabe, Formularinhalte und laufende Medienwiedergabe bei jedem Update erhalten bleiben.

**Architecture:** `_page()` gibt statt `<meta http-equiv="refresh">` ein `<main data-live="N">` aus. Ein global eingebundener JS-Block `_LIVE_JS` pollt dieselbe URL, parst die Antwort mit `DOMParser` und *morpht* das neue `<main>` in das bestehende — Knoten werden schlüsselbasiert wiederverwendet statt ersetzt. Weil der Scroll-Container nie ausgetauscht wird, überlebt der gesamte Interaktionszustand ohne Serialisierung.

**Tech Stack:** Python 3 / Starlette (server-rendertes HTML in `admin.py`), Vanilla-ES5-JS in Python-String-Konstanten, keine externen Abhängigkeiten. Tests: stdlib `unittest`; JS-Syntaxprüfung über `node --check`; reale Verifikation über headless Chromium per CDP.

**Spec:** `docs/superpowers/specs/2026-09-02-ui-live-morph-design.md`

## Global Constraints

- **Keine neuen Abhängigkeiten.** Weder `requirements.txt` noch npm-Pakete. Die Konsole ist bewusst dependency-frei; `node` und Chromium sind reine Entwicklungswerkzeuge und dürfen nicht zur Laufzeitvoraussetzung werden.
- **Kein Build-Schritt.** Das JS bleibt als Python-String-Konstante in `admin.py`, im Stil der bestehenden Blöcke `_SCROLL_JS`, `_SORT_JS`, `_FILTER_JS`. Keine Auslagerung nach `static/`.
- **ES5-Syntax.** Kein `let`/`const`/Arrow-Functions/Template-Literals — die bestehenden Blöcke sind durchgängig ES5, und ein Syntaxfehler in einem eingebetteten Blob fällt zur Laufzeit lautlos aus (das Skript läuft einfach nicht).
- **Der Python-Interpreter für alle Kommandos ist `/home/dev/projekte/llm-gateway/venv/bin/python`** — absolut, weil der Worktree kein eigenes `venv/` hat (es ist gitignored und liegt nur im Haupt-Checkout).
- **`admin.py` importiert niemals `main`.** Alle Callbacks kommen injiziert über `admin.bind(...)`. Diese Eigenschaft darf keine Änderung verletzen.
- **Compile-Gate vor jedem Commit:** `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py`
- **Chromium für CDP-Aufgaben:** `/home/dev/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`
- **Signatur bleibt:** `_page(title, body, active="", refresh=None, nologin=False, subnav="")` behält Parameterliste und Bedeutung. Die vier Aufrufer mit `refresh=…` werden in Phase 1 **nicht** angefasst.

---

## File Structure

| Datei | Verantwortung | Änderung |
|---|---|---|
| `admin.py` | die gesamte `/ui`-Konsole; enthält `_page()` und alle JS-Konstanten | modifiziert (alle Tasks außer 5) |
| `test_admin_live.py` | Server-Kontrakt von `_page()` plus Syntaxprüfung aller eingebetteten JS-Blöcke | neu (Task 1) |
| `docs/superpowers/specs/2026-09-02-ui-live-morph-design.md` | das Design | nur Lektüre |
| `CLAUDE.md`, `README.md` | Architekturbeschreibung der Konsole | modifiziert (Task 8) |

`admin.py` ist mit 5690 Zeilen groß, aber die Aufteilung ist im Projekt bewusst so gewählt („Eleven self-contained Python files hold everything"). Dieser Plan folgt dem Bestand und legt keine neue Datei für die Konsole an; die einzige neue Datei ist die Testdatei, entsprechend dem vorhandenen Muster (`test_prune_branch.py`, `test_ratelimit_headers.py` — stdlib `unittest`, für Logik, die lautlos statt laut versagt).

---

### Task 1: Server-Kontrakt — `_page()` gibt `data-live` statt Meta-Refresh aus

**Files:**
- Create: `test_admin_live.py`
- Modify: `admin.py` (Funktion `_page`, aktuell Zeile 349-356)

**Interfaces:**
- Consumes: nichts.
- Produces: `_page(..., refresh=N)` rendert `<main data-live="N">…</main>` und **kein** `<meta http-equiv="refresh">`. `_page(..., refresh=None)` rendert `<main>` ohne `data-live`. Task 2 baut den Poller auf diesen Kontrakt.

- [ ] **Step 1: Testdatei mit den fehlschlagenden Tests anlegen**

Erstelle `test_admin_live.py`:

```python
"""The console's live-update contract.

Two failure modes are silent, which is why they are tested here rather than
eyeballed: a `_page()` that stops emitting `data-live` leaves every auto-updating
view frozen with no error anywhere, and a syntax error inside one of the JS blobs
embedded as a Python string simply means the script never runs — the page renders
fine and nothing in the log says otherwise.
"""
import re
import shutil
import subprocess
import tempfile
import unittest

import admin


class PageLiveAttr(unittest.TestCase):
    def test_refresh_emits_data_live_on_main(self):
        html = admin._page("T", "<p>x</p>", "dashboard", refresh=4)
        self.assertIn('<main data-live="4">', html)

    def test_refresh_no_longer_emits_meta_refresh(self):
        html = admin._page("T", "<p>x</p>", "dashboard", refresh=4)
        self.assertNotIn("http-equiv=\"refresh\"", html)

    def test_no_refresh_emits_plain_main(self):
        # Asserted on the tag, not on the whole page: _LIVE_JS mentions "data-live"
        # in its own source, so a substring check over the document would be a lie.
        html = admin._page("T", "<p>x</p>", "dashboard")
        self.assertIn("<main>", html)
        self.assertNotIn("<main data-live", html)

    def test_body_is_inside_main(self):
        html = admin._page("T", "<p>marker</p>", "dashboard", refresh=2)
        m = re.search(r"<main[^>]*>(.*)</main>", html, re.S)
        self.assertIsNotNone(m, "page must have a <main> element")
        self.assertIn("<p>marker</p>", m.group(1))


class EmbeddedScriptsParse(unittest.TestCase):
    """Every plain inline <script> the console emits must be valid ES5.

    `type=`-carrying scripts (model-viewer's module, the three.js importmap) are
    skipped: they are not plain scripts and `node --check` would reject them for
    reasons that say nothing about our code.
    """

    def test_inline_scripts_are_syntactically_valid(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        html = admin._page("T", "<p>x</p>", "dashboard", refresh=4)
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(blocks, "expected at least one inline script in the page")
        for i, src in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                fh.write(src)
                path = fh.name
            p = subprocess.run(["node", "--check", path],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0,
                             f"inline script #{i} is not valid JS:\n{p.stderr}")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
```

Erwartet: `test_refresh_emits_data_live_on_main` und `test_refresh_no_longer_emits_meta_refresh` schlagen FEHL (heute steht das Meta-Tag im `<head>` und `<main>` trägt kein Attribut). `test_no_refresh_emits_plain_main`, `test_body_is_inside_main` und `test_inline_scripts_are_syntactically_valid` sind bereits GRÜN — sie sichern den Bestand ab.

- [ ] **Step 3: `_page()` umstellen**

In `admin.py` diese Fassung:

```python
def _page(title: str, body: str, active: str = "", refresh: Optional[int] = None,
          nologin: bool = False, subnav: str = "") -> str:
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    head = "" if nologin else _nav(active)        # login page renders without the nav
    # subnav (see SUBTABS) renders as a second header row — outside <main>, so it
    # never scrolls and sits flush under the tabs.
    return (f'<!doctype html><html><head><meta charset="utf-8">{meta}<title>{_esc(title)} · Gateway</title>'
            f"<style>{_CSS}</style></head><body>{head}{subnav}<main>{body}</main>{_SCROLL_JS}{_SORT_JS}</body></html>")
```

ersetzen durch:

```python
def _page(title: str, body: str, active: str = "", refresh: Optional[int] = None,
          nologin: bool = False, subnav: str = "") -> str:
    # `refresh` no longer reloads the page. It marks <main> as live and _LIVE_JS
    # polls this same URL and MORPHS the new <main> into the old one — the container
    # is never replaced, so scroll position, sort order, a half-typed filter, an
    # open form, playing video and the model-viewer's camera all survive an update.
    # A response without data-live stops the poller, which is exactly what dropping
    # the meta tag used to mean.
    live = f' data-live="{int(refresh)}"' if refresh else ""
    head = "" if nologin else _nav(active)        # login page renders without the nav
    # subnav (see SUBTABS) renders as a second header row — outside <main>, so it
    # never scrolls and sits flush under the tabs.
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)} · Gateway</title>'
            f"<style>{_CSS}</style></head><body>{head}{subnav}<main{live}>{body}</main>"
            f"{_SCROLL_JS}{_SORT_JS}</body></html>")
```

- [ ] **Step 4: Tests laufen lassen, alle grün**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
```

Erwartet: 5 Tests, alle PASS.

- [ ] **Step 5: Compile-Gate und Commit**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py
git add test_admin_live.py admin.py
git commit -m "ui: _page marks <main> live instead of emitting a meta refresh"
```

---

### Task 2: `_LIVE_JS` — der Poller und der Morph

**Files:**
- Modify: `admin.py` (neue Konstante `_LIVE_JS` unmittelbar nach `_SORT_JS`; Einbindung in `_page`)
- Test: `test_admin_live.py` (zwei zusätzliche Tests)

**Interfaces:**
- Consumes: `<main data-live="N">` aus Task 1.
- Produces: `window.gwLiveHooks` — ein Array von Funktionen ohne Argumente, das nach jedem erfolgreichen Morph in Reihenfolge abgearbeitet wird. Task 3 registriert dort. Ein Fehler in einem Hook darf den Poller nicht anhalten. Ausserdem das Attribut `data-live-skip`, das einen beliebigen Teilbaum vom Morph ausnimmt.

- [ ] **Step 1: Die zwei fehlschlagenden Tests ergänzen**

In `test_admin_live.py` diese Klasse **vor** `if __name__ == "__main__":` anhängen:

```python
class LiveScriptPresence(unittest.TestCase):
    def test_live_js_is_always_embedded(self):
        # Embedded unconditionally, like _SORT_JS: the script disables itself when
        # <main> carries no data-live, so a static page pays nothing for it.
        # Compared against the constant itself rather than a keyword — _SORT_JS also
        # mentions gwLiveHooks, so a keyword check would pass with _LIVE_JS missing.
        for kwargs in ({"refresh": 4}, {}):
            html = admin._page("T", "<p>x</p>", "dashboard", **kwargs)
            self.assertIn(admin._LIVE_JS, html, f"_LIVE_JS missing for {kwargs}")

    def test_live_js_declares_the_hook_array(self):
        self.assertIn("window.gwLiveHooks", admin._LIVE_JS)
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
```

Erwartet: beide neuen Tests FEHLEN fehl — `AttributeError: module 'admin' has no attribute '_LIVE_JS'` beziehungsweise der `assertIn` schlägt fehl.

- [ ] **Step 3: `_LIVE_JS` implementieren**

Dies ist der Algorithmus in lesbarer Form. Er ist **eins zu eins** in eine Python-String-Konstante zu übertragen, im Stil von `_SORT_JS` (eine Klammer-verkettete Folge von Stringliteralen, ES5, keine Zeilenkommentare im JS — sie überleben die Verkettung nicht):

```js
(function () {
  var main = document.querySelector('main');
  if (!main) return;
  window.gwLiveHooks = window.gwLiveHooks || [];
  var base = parseInt(main.getAttribute('data-live') || '0', 10) * 1000;
  if (!(base > 0)) return;

  var MEDIA = {IMG: 1, VIDEO: 1, AUDIO: 1, IFRAME: 1, SOURCE: 1, 'MODEL-VIEWER': 1};
  var FORM = {INPUT: 1, TEXTAREA: 1, SELECT: 1};
  var seq = 0;

  function keyOf(n) {
    if (n.nodeType !== 1) return null;
    return n.id || n.getAttribute('data-k') || null;
  }

  function dirty(e) {
    if (!FORM[e.tagName]) return false;
    if (document.activeElement === e) return true;
    if (e.tagName === 'SELECT') {
      var sel = e.querySelector('option[selected]');
      var def = sel ? sel.value : (e.options[0] ? e.options[0].value : '');
      return e.value !== def;
    }
    if (e.type === 'checkbox' || e.type === 'radio') return e.checked !== e.defaultChecked;
    return e.value !== e.defaultValue;
  }

  function frozen(e) {
    return e.tagName === 'SCRIPT' || e.hasAttribute('data-live-skip') || dirty(e);
  }

  function adopt(n) {
    var c = document.importNode(n, true);
    if (c.nodeType === 1) {
      var s = c.querySelectorAll ? c.querySelectorAll('script') : [];
      for (var i = 0; i < s.length; i++) s[i].parentNode.removeChild(s[i]);
      if (c.tagName === 'SCRIPT') return document.createComment('gw-script-skipped');
    }
    return c;
  }

  function syncAttrs(o, n) {
    if (MEDIA[o.tagName] && o.getAttribute('src') === n.getAttribute('src')) return;
    var i, a;
    for (i = n.attributes.length - 1; i >= 0; i--) {
      a = n.attributes[i];
      if (o.tagName === 'DETAILS' && a.name === 'open') continue;
      if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value);
    }
    for (i = o.attributes.length - 1; i >= 0; i--) {
      a = o.attributes[i];
      if (o.tagName === 'DETAILS' && a.name === 'open') continue;
      if (!n.hasAttribute(a.name)) o.removeAttribute(a.name);
    }
    if (o.tagName === 'INPUT') {
      if (o.type === 'checkbox' || o.type === 'radio') o.checked = n.hasAttribute('checked');
      else o.value = n.getAttribute('value') || '';
    } else if (o.tagName === 'TEXTAREA') {
      o.value = n.textContent;
    }
  }

  function same(o, n) {
    if (o.nodeType !== n.nodeType) return false;
    if (o.nodeType !== 1) return true;
    if (o.tagName !== n.tagName) return false;
    var ko = keyOf(o), kn = keyOf(n);
    if (ko || kn) return ko === kn;
    return true;
  }

  function morph(o, n) {
    if (o.nodeType === 3 || o.nodeType === 8) {
      if (o.data !== n.data) o.data = n.data;
      return;
    }
    if (o.nodeType !== 1) return;
    if (frozen(o)) return;
    syncAttrs(o, n);
    if (MEDIA[o.tagName]) return;
    reconcile(o, n);
  }

  function reconcile(o, n) {
    var pool = {}, unkeyed = [], c, k, i;
    for (c = o.firstChild; c; c = c.nextSibling) {
      k = keyOf(c);
      if (k) pool['#' + k] = c; else unkeyed.push(c);
    }
    var out = [], ui = 0, m;
    for (c = n.firstChild; c; c = c.nextSibling) {
      k = keyOf(c);
      m = null;
      if (k) {
        m = pool['#' + k] || null;
        if (m) pool['#' + k] = null;
      } else {
        while (ui < unkeyed.length && !same(unkeyed[ui], c)) ui++;
        if (ui < unkeyed.length) { m = unkeyed[ui]; ui++; }
      }
      if (m) { morph(m, c); out.push(m); }
      else out.push(adopt(c));
    }
    var stamp = ++seq, nx;
    for (i = 0; i < out.length; i++) out[i].__gwLive = stamp;
    c = o.firstChild;
    while (c) {
      nx = c.nextSibling;
      if (c.__gwLive !== stamp) o.removeChild(c);
      c = nx;
    }
    var cur = o.firstChild;
    for (i = 0; i < out.length; i++) {
      if (cur === out[i]) cur = cur.nextSibling;
      else o.insertBefore(out[i], cur);
    }
  }

  var wait = base, timer = null, catchUp = false;

  function schedule(ms) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(tick, ms);
  }

  function stop() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function tick() {
    if (document.hidden) { catchUp = true; schedule(wait); return; }
    fetch(location.href, {cache: 'no-store', credentials: 'same-origin'})
      .then(function (r) {
        if (r.redirected && new URL(r.url).pathname !== location.pathname) {
          stop();
          location.href = r.url;
          return null;
        }
        if (!r.ok) { wait = Math.min(wait * 2, 30000); schedule(wait); return null; }
        return r.text();
      })
      .then(function (html) {
        if (html === null || html === undefined) return;
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var nm = doc.querySelector('main');
        if (!nm) { stop(); return; }
        try {
          morph(main, nm);
        } catch (e) {
          main.replaceChildren.apply(main, [].slice.call(nm.childNodes).map(adopt));
        }
        for (var i = 0; i < window.gwLiveHooks.length; i++) {
          try { window.gwLiveHooks[i](); } catch (e) {}
        }
        var next = parseInt(main.getAttribute('data-live') || '0', 10) * 1000;
        if (!(next > 0)) { stop(); return; }
        wait = base = next;
        schedule(wait);
      })
      .catch(function () { wait = Math.min(wait * 2, 30000); schedule(wait); });
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && catchUp && timer) { catchUp = false; schedule(0); }
  });

  schedule(wait);
})();
```

Zwei Punkte, die beim Übertragen leicht verlorengehen und dann lautlos schaden:

1. `morph(main, nm)` synchronisiert auch die Attribute von `<main>` selbst — daher liest Schritt „next" das Attribut vom **alten** `main`, nicht vom geparsten. Das ist Absicht und darf nicht auf `nm.getAttribute` umgeschrieben werden.
2. `adopt()` entfernt Skripte aus neu eingefügten Teilbäumen. Ohne das würde ein per `importNode` geklontes `<script>` beim Einfügen ausgeführt und etwa `_JOB_TICK`s `setInterval` ein zweites Mal starten.

Die Konstante steht direkt nach `_SORT_JS` und trägt diesen erklärenden Kopf:

```python
# One auto-update mechanism for the whole console. `_page(refresh=N)` marks <main>
# with data-live=N; this poller re-fetches the SAME url and morphs the response's
# <main> into the live one instead of reloading the page. Nodes are matched by id or
# data-k (falling back to position+tag), so a table that gains a row keeps every
# other row's identity — which is what preserves scroll, sort order, a focused
# filter input, an open form, playing media and the model-viewer's camera.
# Five things are deliberately never touched: <script> (a re-inserted _JOB_TICK
# would double its setInterval), [data-live-skip] subtrees, form controls that are
# focused or dirty, media whose src is unchanged, and <details open> (user state the
# server knows nothing about). A response without data-live stops the poller — the
# same signal the meta tag's absence used to carry.
```

- [ ] **Step 4: In `_page()` einbinden**

`{_SCROLL_JS}{_SORT_JS}` in `_page()` zu `{_SCROLL_JS}{_SORT_JS}{_LIVE_JS}` ergänzen. Die Reihenfolge ist wesentlich: `_LIVE_JS` liest `window.gwLiveHooks` zwar defensiv (`|| []`), aber `_SORT_JS` muss vor dem ersten Tick registriert haben.

- [ ] **Step 5: Tests laufen lassen, alle grün**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
```

Erwartet: 7 Tests, alle PASS. `test_inline_scripts_are_syntactically_valid` prüft den neuen Blob mit — schlägt er fehl, steckt ein Syntaxfehler in der Übertragung nach Python.

- [ ] **Step 6: Compile-Gate und Commit**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py
git add admin.py test_admin_live.py
git commit -m "ui: _LIVE_JS polls and morphs <main> in place"
```

---

### Task 3: Post-Morph-Hooks für Sortierung und Filter

**Files:**
- Modify: `admin.py` (`_SORT_JS`, aktuell Zeile 314-346; `_FILTER_JS`, aktuell Zeile 4682-4699)
- Test: `test_admin_live.py`

**Interfaces:**
- Consumes: `window.gwLiveHooks` aus Task 2.
- Produces: nichts für spätere Tasks.

**Warum das nötig ist:** Der Server liefert Tabellenzeilen stets in Einfügereihenfolge. Ohne Hook würde der Morph eine vom Nutzer angeklickte Sortierung bei jedem Takt zurückdrehen, und neu eingemorphte Zeilen würden einen aktiven Filter ignorieren.

- [ ] **Step 1: Fehlschlagenden Test ergänzen**

In `test_admin_live.py` an `LiveScriptPresence` anhängen:

```python
    def test_sort_and_filter_register_post_morph_hooks(self):
        # The server always renders insertion order and the morph reuses nodes, so
        # without these hooks a clicked sort order is undone on every tick and rows
        # morphed in fresh ignore an active filter.
        self.assertIn("gwLiveHooks.push", admin._SORT_JS)
        self.assertIn("gwLiveHooks.push", admin._FILTER_JS)
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live.LiveScriptPresence -v
```

Erwartet: `test_sort_and_filter_register_post_morph_hooks` FEHLGESCHLAGEN.

- [ ] **Step 3: Hook in `_SORT_JS` registrieren**

`_SORT_JS` endet heute mit:

```
"var s={};try{s=JSON.parse(sessionStorage.getItem(k)||'{}');}catch(e){}"
"if(s.idx!=null)sortIt(tbl,s.idx,s.dir||1);});"
"})();</script>"
```

Die vorletzte Zeile bleibt; danach — noch innerhalb der IIFE, damit `sortIt` und die Tabellenliste im Closure erreichbar sind — wird die erneute Anwendung registriert:

```
"var s={};try{s=JSON.parse(sessionStorage.getItem(k)||'{}');}catch(e){}"
"if(s.idx!=null)sortIt(tbl,s.idx,s.dir||1);});"
"window.gwLiveHooks=window.gwLiveHooks||[];"
"window.gwLiveHooks.push(function(){"
"[].slice.call(document.querySelectorAll('table.sortable')).forEach(function(tbl,i){"
"var hdr=tbl.rows[0];if(!hdr)return;var s={};"
"try{s=JSON.parse(sessionStorage.getItem('sort:'+key(tbl,i))||'{}');}catch(e){}"
"if(s.idx!=null)sortIt(tbl,s.idx,s.dir||1);});});"
"})();</script>"
```

Der Hook liest den Zustand erneut aus `sessionStorage`, statt ihn in einer Variablen zu halten — das ist bereits die Quelle der Wahrheit und bleibt es (siehe Task 4: die Persistenz wird nicht entfernt).

- [ ] **Step 4: Hook in `_FILTER_JS` registrieren**

`_FILTER_JS` endet heute mit:

```
"i.addEventListener('blur',save);window.addEventListener('beforeunload',save);}"
"})();</script>"
```

Ergänzen (der `beforeunload`-Save fällt erst in Task 4):

```
"i.addEventListener('blur',save);window.addEventListener('beforeunload',save);}"
"window.gwLiveHooks=window.gwLiveHooks||[];"
"window.gwLiveHooks.push(function(){if(window.sfRun)window.sfRun();});"
"})();</script>"
```

- [ ] **Step 5: Tests laufen lassen, alle grün**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
```

Erwartet: 9 Tests, alle PASS — insbesondere bleibt `test_inline_scripts_are_syntactically_valid` grün. (Der Lauf enthält beide in diesem Task ergänzten Tests: den Hook-Test und den unten beschriebenen `test_page_level_script_constants_are_valid`.)

Achtung: `_FILTER_JS` steckt nicht in `_page()`, sondern wird von einzelnen Seiten in den Body gehängt. Der Syntaxtest erfasst ihn deshalb nicht automatisch. Ergänze in `EmbeddedScriptsParse` eine zweite Methode:

```python
    def test_page_level_script_constants_are_valid(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        for name in ("_SCROLL_JS", "_SORT_JS", "_LIVE_JS", "_FILTER_JS", "_JOB_TICK"):
            blob = getattr(admin, name)
            for src in re.findall(r"<script>(.*?)</script>", blob, re.S):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                    fh.write(src)
                    path = fh.name
                p = subprocess.run(["node", "--check", path],
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, f"{name} is not valid JS:\n{p.stderr}")
```

- [ ] **Step 6: Compile-Gate und Commit**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py
git add admin.py test_admin_live.py
git commit -m "ui: re-apply sort and filter after each live morph"
```

---

### Task 4: Die Reload-Krücken entfernen

**Files:**
- Modify: `admin.py` (`_FILTER_JS`, `_SCROLL_JS`)
- Test: `test_admin_live.py`

**Interfaces:**
- Consumes: der laufende Morph aus Task 2.
- Produces: nichts.

**Was entfernt wird und was ausdrücklich bleibt.** Entfernt wird ausschließlich, was den Vollreload kompensiert: in `_FILTER_JS` der Fokus-und-Caret-Restore samt 15-Sekunden-Fenster und der `beforeunload`-Save, in `_SCROLL_JS` die Restaurierung von `<main>.scrollTop`. **Erhalten bleiben** die Sortier- und Filterpersistenz in `sessionStorage` sowie der `.col`-Scroll mit dem `|master`-Schlüssel — beide bedienen echte Navigation (Tab-Wechsel, Formular-POST, Durchklicken einer Master/Detail-Liste) und haben mit Auto-Refresh nichts zu tun.

- [ ] **Step 1: Fehlschlagenden Test ergänzen**

An `LiveScriptPresence` anhängen:

```python
    def test_filter_no_longer_restores_focus_and_caret(self):
        # Reload compensation only: the morph never replaces a focused input, so the
        # caret needs no rescuing. The filter TEXT persistence stays — it serves real
        # navigation, which the morph does not cover.
        self.assertNotIn("setSelectionRange", admin._FILTER_JS)
        self.assertNotIn("beforeunload", admin._FILTER_JS)
        self.assertIn("sessionStorage.setItem", admin._FILTER_JS)

    def test_scroll_keeps_master_column_but_drops_main_restore(self):
        # <main> is never replaced any more, so its scrollTop needs no restoring.
        # The .col/|master key survives: it is a master/detail navigation feature.
        self.assertIn("|master", admin._SCROLL_JS)
        self.assertNotIn("'|main'", admin._SCROLL_JS)
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live.LiveScriptPresence -v
```

Erwartet: beide neuen Tests FEHLGESCHLAGEN.

- [ ] **Step 3: `_FILTER_JS` zurückbauen**

Die neue Fassung — Filtertext bleibt persistent, Fokus/Caret/`beforeunload` fallen weg:

```python
# Type-to-filter over any `table.filterable`. The typed text persists per view in
# sessionStorage so it survives real navigation; the caret does NOT need saving any
# more — the live morph never replaces a focused input (see _LIVE_JS).
_FILTER_JS = ("<script>(function(){var K='flt:'+location.pathname+location.search;"
              "function el(){return document.getElementById('sf');}"
              "function save(){var i=el();if(!i)return;try{sessionStorage.setItem(K,"
              "JSON.stringify({v:i.value}));}catch(e){}}"
              "window.sfRun=function(){var i=el();if(!i)return;"
              "var q=(i.value||'').toLowerCase();"
              "document.querySelectorAll('.filterable tr').forEach(function(r){"
              "if(r.getElementsByTagName('th').length)return;"
              "r.style.display=r.textContent.toLowerCase().indexOf(q)>-1?'':'none';});save();};"
              "var i=el();if(i){var s=null;"
              "try{s=JSON.parse(sessionStorage.getItem(K)||'null');}catch(e){}"
              "if(s&&s.v){i.value=s.v;window.sfRun();}}"
              "window.gwLiveHooks=window.gwLiveHooks||[];"
              "window.gwLiveHooks.push(function(){if(window.sfRun)window.sfRun();});"
              "})();</script>")
```

- [ ] **Step 4: `_SCROLL_JS` zurückbauen**

Die neue Fassung — nur noch die `.col`-Spalten, `<main>` fällt weg:

```python
# Scroll memory for the master/detail COLUMNS only (`.col`). <main> needs none: the
# live morph never replaces it, so its scroll position is simply never lost. The
# first column's key deliberately omits the query string, so the list keeps its
# position while you click through its items.
_SCROLL_JS = ("<script>(function(){"
              "var q=location.search.replace(/([?&])saved=[^&]*&?/,'$1').replace(/[?&]$/,'');"
              "var b='scr:'+location.pathname;"
              "function t(){var o=[];"
              "[].slice.call(document.querySelectorAll('.col')).forEach(function(e,j){"
              "o.push([e,j===0?b+'|master':b+q+'|c'+j]);});return o;}"
              "try{t().forEach(function(p){var v=sessionStorage.getItem(p[1]);"
              "if(v!=null)p[0].scrollTop=+v;});}catch(e){}"
              "var d=false;function save(){if(d)return;d=true;requestAnimationFrame(function(){d=false;"
              "try{t().forEach(function(p){sessionStorage.setItem(p[1],p[0].scrollTop);});}catch(e){}});}"
              "t().forEach(function(p){p[0].addEventListener('scroll',save);});"
              "window.addEventListener('beforeunload',save);"
              "})();</script>")
```

- [ ] **Step 5: Tests laufen lassen, alle grün**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
```

Erwartet: 11 Tests, alle PASS.

- [ ] **Step 6: Compile-Gate und Commit**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py
git add admin.py test_admin_live.py
git commit -m "ui: drop the state-restore hacks the full-page reload needed"
```

---

### Task 5: Reale Verifikation von Phase 1 im Browser

**Files:**
- Create: `$CLAUDE_JOB_DIR/tmp/livecheck.py` (Wegwerf-Skript, wird **nicht** committet)
- Modify: keine

**Interfaces:**
- Consumes: die Tasks 1-4.
- Produces: ein Messprotokoll; keine Codeartefakte.

**Warum überhaupt:** Der Morph ist JS und lässt sich mit den Bordmitteln dieses Projekts nicht als Unit-Test fahren (kein jsdom, keine npm-Abhängigkeiten erlaubt). Ohne echtes Rendering bleibt jede Aussage über das Verhalten Vermutung.

- [ ] **Step 1: Isolierte Testinstanz aufsetzen**

Alle DB-Pfade sind `config.yaml`-Knöpfe und `CONFIG_PATH` ist relativ zum cwd, deshalb genügt ein Scratch-Verzeichnis mit Symlinks auf den Worktree-Code:

```bash
INST=$CLAUDE_JOB_DIR/tmp/inst
rm -rf "$INST" && mkdir -p "$INST"
cd /home/dev/projekte/llm-gateway/.claude/worktrees/ui-live-morph
for f in *.py; do ln -s "$PWD/$f" "$INST/$f"; done
ln -s "$PWD/static" "$INST/static"
```

`config.yaml` in `$INST` schreiben — **eigene** DB-Pfade, kein `api_key`, keine `users`, damit `/ui` ohne Login offen ist (`_ui_locked()` ist genau dann falsch):

```yaml
api_key: ""
backends: []
jobs:
  enabled: true
  db_path: jobs.db
  blob_dir: jobs
stats:
  enabled: true
  db_path: stats.db
  blob_dir: calls
```

> **Auf keinen Fall** auf die echten `jobs.db`/`store.db` des Repos zeigen: `jobs.init()` ruft `reconcile_orphans()` und markiert jeden laufenden Job als „interrupted by process restart". Eine Testinstanz auf der echten DB zerschießt also produktive Jobs.

Der Store wird direkt geseedet (`store.init(...)` plus `upsert_backend`/`upsert`); ein Media-Alias aus `sample_comfyui_workflows/` genügt.

- [ ] **Step 1b: ComfyUI-Stub für einen dauerhaft laufenden Job**

Die Media-Jobs-Liste und die Job-Detailseite sind nur live, solange ein Job `queued`/`running` ist. Ein stdlib-`ThreadingHTTPServer`-Stub liefert das ohne GPU: `/object_info` gibt eine Checkpoint-Combo zurück (wird zu `models`), `/queue` antwortet `{"queue_running":[],"queue_pending":[]}` (sonst greift der Executor-Watchdog), und `/prompt` **verzögert** die Antwort um mehrere Minuten — dadurch bleibt der Job `running`, solange gemessen wird.

Das Dashboard (`refresh=4`) ist immer live und braucht den Stub nicht; die Prüfpunkte 3 und 4 hängen dagegen an der Jobs-Liste, weil nur dort `#sf` und `table.sortable` gerendert werden.

Server starten:

```bash
cd $CLAUDE_JOB_DIR/tmp/inst && \
  /home/dev/projekte/llm-gateway/venv/bin/uvicorn main:app --host 127.0.0.1 --port 4099
```

- [ ] **Step 2: Chromium headless starten**

```bash
/home/dev/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome \
  --headless=new --no-sandbox --remote-debugging-port=9333 \
  --user-data-dir=$CLAUDE_JOB_DIR/tmp/chrome-profile
```

Das Profilverzeichnis **vor jedem Lauf löschen** — ein restaurierter `sessionStorage` hat in diesem Projekt schon einmal Messwerte verfälscht und einen funktionierenden Fix als kaputt erscheinen lassen.

- [ ] **Step 3: Die sechs Prüfpunkte messen**

Steuerung per CDP über `websockets` aus dem venv. Auf `/ui/dashboard` (dauerhaft live, `data-live="4"`) und auf `/ui/jobs?sub=media` prüfen:

1. **Scroll bleibt:** `main.scrollTop` auf 300 setzen, drei Takte abwarten (13 s), erneut lesen → muss 300 sein.
2. **Kein Dokumentwechsel:** vor dem Warten `window.__gen = 1` setzen, danach `typeof window.__gen` abfragen → muss `'number'` sein. Ein Vollreload hätte die Markierung gelöscht. Das ist der eigentliche Kernbeweis.
3. **Filter behält Fokus und Cursor:** in `#sf` „a" tippen, fokussieren, `selectionStart` merken, zwei Takte warten → `document.activeElement.id === 'sf'`, Wert und `selectionStart` unverändert.
4. **Sortierung bleibt:** eine Spaltenüberschrift klicken, Zeilenreihenfolge als Array merken, zwei Takte warten → identische Reihenfolge.
5. **Poller stoppt:** eine Seite mit `refresh=None` laden (etwa `/ui/statistic`) → `main` hat kein `data-live`, und über 10 s entstehen keine weiteren Requests (per CDP `Network.requestWillBeSent` zählen).
6. **DOM-Identität überlebt:** ein Element in `main` markieren (`document.querySelector('main table').__probe = 7`), zwei Takte warten, erneut lesen → muss 7 sein, was beweist, dass der Knoten wiederverwendet und nicht ersetzt wurde.

- [ ] **Step 4: Messprotokoll festhalten**

Alle sechs Punkte mit Ist-Werten notieren. Schlägt einer fehl, ist das ein Befund für Task 2, kein Grund weiterzugehen — Phase 2 setzt auf einem funktionierenden Morph auf.

- [ ] **Step 5: Aufräumen**

Chromium und Uvicorn beenden, Testinstanz löschen. Es gibt nichts zu committen; das Skript bleibt Wegwerfware unter `$CLAUDE_JOB_DIR/tmp`.

---

### Task 6: Phase 2a — Media Playground auf den Morph umstellen

**Files:**
- Modify: `admin.py` (`_PG_POLL_JS` entfällt, aktuell Zeile 3458-3466; `_playground_body`, Zeile 3469-3479; `playground_page`, Zeile 3548-3557; `playground_status`, Zeile 3666-3671; Routen-Registrierung von `/ui/playground/status/{job_id}`)
- Test: `test_admin_live.py`

**Interfaces:**
- Consumes: der Morph aus Task 2, insbesondere die Dirty-Input-Regel.
- Produces: nichts für spätere Tasks.

**Was hier passiert:** Der Playground pollte bisher nur die Ergebnisspalte über einen eigenen Endpunkt, damit das Formular beim Bearbeiten nicht neu geladen wird. Genau das leistet der Morph jetzt für die ganze Seite, also fällt der Sonderweg weg.

- [ ] **Step 1: Fehlschlagenden Test ergänzen**

An `LiveScriptPresence` anhängen:

```python
    def test_playground_uses_the_shared_live_mechanism(self):
        # The result column had its own poller because a full-page reload would have
        # wiped the form mid-edit. The morph's dirty-input rule covers that now.
        self.assertFalse(hasattr(admin, "_PG_POLL_JS"),
                         "_PG_POLL_JS should be gone — the live morph replaces it")
        self.assertFalse(hasattr(admin, "playground_status"),
                         "the result-fragment route should be gone")
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live.LiveScriptPresence -v
```

Erwartet: `test_playground_uses_the_shared_live_mechanism` FEHLGESCHLAGEN.

- [ ] **Step 3: `_PG_POLL_JS` und den Fragment-Endpunkt entfernen**

- Die Konstante `_PG_POLL_JS` samt Kommentarblock löschen.
- Die Funktion `playground_status` löschen.
- In `register(app)` die Zeile mit `add_api_route("/ui/playground/status/{job_id}", …)` löschen.
- In `_playground_body` den Parameter `poll_job` und die daraus gebaute `data-poll-job`-Auszeichnung entfernen; der `model-viewer`-Kommentar wird umgeschrieben, weil sein Grund entfällt:

```python
def _playground_body(aliases: list, vals: dict, cand: Optional[dict], result_html: str,
                     oi: Optional[dict] = None, kept: Optional[set] = None) -> str:
    # model-viewer loads with the page and stays loaded: _LIVE_JS never re-inserts a
    # <script>, so a viewer arriving through a live update upgrades against the
    # definition that is already there.
    return (f'<script type="module" src="{_MODELVIEWER_SRC}"></script>'
            f'<div class="cols"><div class="col">{_playground_form(aliases, vals, cand, oi, kept)}</div>'
            f'<div class="col"><div id="resultcol">{result_html}</div></div></div>')
```

- [ ] **Step 4: `playground_page` auf `refresh` umstellen**

Der Block, der heute `poll_job` berechnet und `_page(...)` ohne `refresh` aufruft, wird zu:

```python
    job_id = qp.get("job", "")
    refresh = None
    if job_id:
        result_html, refresh = _job_result_html(job_id, jobs.get(job_id))
    else:
        result_html = "<h2>Result</h2><p class='hint'>Generate to see the result here.</p>"
    wf = (cand.get("workflow_json") if cand else {}) or {}
    oi = await _object_info(cand.get("backend", ""), wf, cand.get("mapping")) if cand else {}
    kept = set(_pg_images.get(_session_user(request) or "default", {}).keys())
    return HTMLResponse(_page("Media Playground",
                              _playground_body(aliases, vals, cand, result_html, oi, kept),
                              "playground", refresh=refresh,
                              subnav=_subnav("playground", "media")))
```

`_job_result_html` liefert das Refresh-Intervall bereits als zweiten Rückgabewert und braucht keine Änderung. Der `data-jobdone`-Marker, den nur der alte Poller las, wird von `playground_status` mit gelöscht — in `_job_result_html` selbst steht er nicht.

Prüfe die übrigen `_playground_body`-Aufrufer (rund um Zeile 3534, 3621) und entferne dort das `poll_job`-Argument, falls gesetzt.

- [ ] **Step 5: Tests laufen lassen und Seite rendern**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
grep -n "_PG_POLL_JS\|playground/status\|poll_job\|data-jobdone" admin.py
```

Erwartet: 12 Tests PASS, und `grep` findet **keine** Treffer mehr.

- [ ] **Step 6: Compile-Gate und Commit**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py
git add admin.py test_admin_live.py
git commit -m "ui: media playground uses the shared live morph, not its own poller"
```

---

### Task 7: Phase 2b — Voice-Upload auf den Morph umstellen

**Files:**
- Modify: `admin.py` (`_VU_POLL_JS`, Zeile 3859-3866; `playground_page`s Voice-Zweig, Zeile 3519-3529; `voice_upload`, Zeile 3899-3901; `voice_upload_status`, Zeile 3903-3910; Routen-Registrierung; `_vu_fragment`s `data-vudone`-Marker)
- Test: `test_admin_live.py`

**Interfaces:**
- Consumes: der Morph aus Task 2.
- Produces: nichts.

**Was hier passiert:** Der Upload-Poller lud die Seite nach Abschluss per `location.replace('…&vu=done')` komplett neu, nur damit die Bibliothekstabelle den neuen Eintrag zeigt. Mit dem Morph aktualisiert sich diese Tabelle im selben Takt mit; der `vu=done`-Sonderweg entfällt vollständig.

- [ ] **Step 1: Fehlschlagenden Test ergänzen**

An `LiveScriptPresence` anhängen:

```python
    def test_voice_upload_uses_the_shared_live_mechanism(self):
        # The old poller ended in location.replace(...&vu=done) purely so the library
        # table would show the new entry. The morph updates that table in the same tick.
        self.assertFalse(hasattr(admin, "_VU_POLL_JS"),
                         "_VU_POLL_JS should be gone — the live morph replaces it")
        self.assertFalse(hasattr(admin, "voice_upload_status"),
                         "the upload-progress fragment route should be gone")
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag bestätigen**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live.LiveScriptPresence -v
```

Erwartet: `test_voice_upload_uses_the_shared_live_mechanism` FEHLGESCHLAGEN.

- [ ] **Step 3: Poller und Fragment-Endpunkt entfernen**

- Konstante `_VU_POLL_JS` löschen.
- Funktion `voice_upload_status` löschen.
- In `register(app)` die Route `/ui/playground/voice-upload-status` löschen.
- In `_vu_fragment` das abschließende `"<span data-vudone hidden></span>"` entfernen — es war ausschließlich das Stoppsignal für den alten Poller. Der Rückgabewert endet dann mit `return head + rows + tail`.

- [ ] **Step 4: Den Voice-Zweig auf `refresh` umstellen**

Der Zweig in `playground_page` wird zu:

```python
    if sub == "voice":
        vals = {k: qp.get(k, "") for k in _VOICEPLAY_KEYS}
        prog = _voice_upload_prog.get(_session_user(request) or "default")
        # A running upload makes the page live at 1s; the final tick renders the
        # finished checklist AND the library table with the new entry, so the old
        # location.replace(...&vu=done) reload has nothing left to do.
        vu_refresh = 1 if (prog and not prog.get("done")) else None
        if prog:
            result_html = _vu_fragment(prog)
        else:
            result_html = "<h2>Result</h2><p class='hint'>Synthesize to hear the result here.</p>"
        return HTMLResponse(_page("Voice", _voiceplay_body(vals, result_html), "playground",
                                  refresh=vu_refresh, subnav=_subnav("playground", "voice")))
```

- [ ] **Step 5: `voice_upload` auf denselben Weg bringen**

Der Rückgabewert am Ende von `voice_upload` wird zu:

```python
    asyncio.create_task(_run())
    return HTMLResponse(_page("Voice", _voiceplay_body(vals, _vu_fragment(prog)),
                              "playground", refresh=1,
                              subnav=_subnav("playground", "voice")))
```

- [ ] **Step 6: Tests laufen lassen und Rückstände suchen**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_admin_live -v
grep -n "_VU_POLL_JS\|voice-upload-status\|data-vudone\|vu=done" admin.py
```

Erwartet: 13 Tests PASS, `grep` ohne Treffer.

- [ ] **Step 7: Compile-Gate und Commit**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py
git add admin.py test_admin_live.py
git commit -m "ui: voice upload progress uses the shared live morph"
```

---

### Task 8: Dokumentation und Abschlussverifikation

**Files:**
- Modify: `CLAUDE.md` (Abschnitt `admin.py` in der Architekturliste), `README.md` (Beschreibung der Konsole)
- Test: der volle Testlauf plus eine zweite CDP-Runde

**Interfaces:**
- Consumes: Tasks 1-7.
- Produces: nichts.

- [ ] **Step 1: `CLAUDE.md` ergänzen**

Im `admin.py`-Absatz der Architekturliste nach dem Satz über `SUBTABS`/`_with_subnav()` einfügen:

```
  **Auto-update is one mechanism, not six** (`_LIVE_JS`): `_page(refresh=N)` marks
  `<main data-live="N">` and a global poller re-fetches the SAME url and MORPHS the
  response's `<main>` into the live one — nodes matched by `id`/`data-k`, falling
  back to position+tag. Nothing is ever reloaded, so scroll, sort order, a focused
  filter, an open form, playing media and the model-viewer camera survive an update;
  a response without `data-live` stops the poller (what the meta tag's absence used
  to mean). Never touched: `<script>` (a re-inserted `_JOB_TICK` would double its
  `setInterval`), `[data-live-skip]` subtrees, focused/dirty form controls, media
  with an unchanged `src`, and `<details open>`. Post-morph hooks in
  `window.gwLiveHooks` re-apply sort and filter, because the server always renders
  insertion order. This replaced four `<meta http-equiv="refresh">` pages
  (Dashboard, Media Jobs, Job detail, Backends) and the two hand-rolled fragment
  pollers (media playground result column, voice-upload progress).
```

- [ ] **Step 2: `README.md` angleichen**

Jede Stelle, die der Konsole ganzseitiges Auto-Refresh zuschreibt, auf den neuen Mechanismus umschreiben. Finden mit:

```bash
grep -n -i "refresh\|auto-update\|reload" README.md
```

- [ ] **Step 3: Voller Testlauf**

```bash
cd /home/dev/projekte/llm-gateway/.claude/worktrees/ui-live-morph
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest discover -p 'test_*.py' -v
```

Erwartet: alle Tests des Projekts PASS — auch `test_anthropic_bridge`, `test_prune_branch`, `test_ratelimit_headers`, `test_scheduler`, `test_chain_export_node`. Keiner davon berührt `admin.py`; ein Fehlschlag dort wäre ein Zeichen für einen versehentlichen Import-Nebeneffekt.

- [ ] **Step 4: Zweite CDP-Runde für Phase 2**

Testinstanz wie in Task 5. Zusätzlich prüfen:

1. `/ui/playground?sub=media` mit laufendem Job: in ein Formularfeld tippen, zwei Takte warten → Eingabe unverändert, Fokus erhalten, Ergebnisspalte hat sich aktualisiert.
2. Ein GLB-Ergebnis im `model-viewer` drehen, einen Takt warten → Kameraposition unverändert (`model-viewer` besitzt `getCameraOrbit()`).
3. `/ui/job/<id>` eines laufenden Jobs: `window.__gen` markieren, drei Takte warten → Markierung überlebt, Status hat sich aktualisiert.
4. Nach Jobabschluss: `data-live` verschwindet, keine weiteren Requests.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: describe the single live-morph auto-update mechanism"
```

---

## Verifikationsübersicht

| Was | Wie | Wann |
|---|---|---|
| `_page()`-Kontrakt (`data-live` statt Meta-Tag) | `test_admin_live.py`, stdlib unittest | Task 1, danach in jedem Lauf |
| JS-Syntax aller eingebetteten Blöcke | `node --check` aus dem Test heraus | Task 1/3, danach in jedem Lauf |
| Hook-Registrierung, entfernte Krücken, entfernte Poller | `test_admin_live.py` | Tasks 3, 4, 6, 7 |
| Morph-Verhalten im echten DOM | headless Chromium per CDP, 6 Prüfpunkte | Task 5 (Phase 1), Task 8 (Phase 2) |
| Keine Syntaxfehler in Python | `py_compile admin.py` | vor jedem Commit |
| Keine Regression in den übrigen Modulen | `unittest discover` | Task 8 |
