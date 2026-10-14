#!/usr/bin/env python3
# Review page for the pictures the agents found for new devices (new/results, new/images). Every
# picture goes through the same normalization as pictures.py: white background, captions and
# badges cropped, frame stripped, diagonal products levelled, margins trimmed, 960 px webp. The
# card shows the found picture next to the result. Per device: accept, reject, "довернуть" (level
# the product, a box shot at an angle too), "отменить доворот" (no auto rotation), "не отбеливать"
# (keep a light gray background), a crop frame and white-out boxes on the found picture.
# Decisions go to new/decisions.json at once. An accepted picture is written to <out>/<gtin>.webp
# right away and rewritten when its marks change, and gets "picture" in registry.json; a rejected
# or undecided one has neither.
# pictures.py find --again queues the rejected ones for another search.
# Results are kept in new/cache/ until pictures.py or the source changes; a background pass fills
# it at start, --prepare fills it on all cores and exits.
# Usage (from dali_pictures): python3 scripts/review.py [--port 8790] [--out new/accepted] [--prepare]

import argparse
import hashlib
import json
import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pictures import (  # pylint: disable=import-error  # resolved from the script's own directory
    NEW,
    REDOWNLOAD_FLAGS,
    ROOT,
    load_registry,
    oriented_size,
    process_file,
    save_registry,
)
from PIL import Image

SMALL = 600
CACHE = NEW / "cache"
DECISIONS = NEW / "decisions.json"
# Results on disk stay valid while the normalization code is the same.
CODE = hashlib.sha1((Path(__file__).parent / "pictures.py").read_bytes()).hexdigest()

PAGE = r"""<!doctype html><meta charset=utf-8><title>Новые картинки DALI</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#d9dde3;--accent:#2563eb;
--ok:#15803d;--ok-bg:#e3f5e8;--no:#dc2626;--no-bg:#fde8e8;--warn:#b45309}
*{box-sizing:border-box}
body{margin:0;font:13px/1.35 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);
padding:8px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
.tabs{display:flex;flex-wrap:wrap;gap:4px}
button{font:inherit;border:1px solid var(--line);background:var(--card);border-radius:6px;padding:4px 10px;cursor:pointer;color:var(--fg)}
.tabs button.on{background:var(--fg);color:var(--card);border-color:var(--fg)}
.tabs b{font-variant-numeric:tabular-nums}
.pager{display:flex;gap:6px;align-items:center;margin-left:auto}
input[type=search]{font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:6px}
.help{color:var(--muted);width:100%;font-size:12px}
main{padding:12px 16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:10px}
.card{background:var(--card);border:2px solid var(--line);border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:6px;min-width:0}
.card.sel{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.card.c-accept{background:var(--ok-bg)}.card.c-reject{background:var(--no-bg)}
.pics{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.pic{display:flex;flex-direction:column;gap:2px;min-width:0}
.pic div{aspect-ratio:3/2;display:flex;align-items:center;justify-content:center;border-radius:4px;background:#fff;
outline:1px solid var(--line);overflow:hidden;cursor:zoom-in}
.pic img{max-width:100%;max-height:100%}
.pic span{font-size:11px;color:var(--muted)}
.pic span.small{color:var(--warn);font-weight:600}
.missing{color:var(--muted);cursor:default!important}
.meta{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline;min-width:0}
.meta .name{font-weight:600;overflow-wrap:anywhere}
.meta code{color:var(--muted);font-size:12px}
.chip{font-size:11px;padding:1px 6px;border-radius:9px;background:#eef0f3;color:#374151}
.chip.w{background:var(--warn);color:#fff}.chip.f{background:var(--accent);color:#fff}
.note{font-size:12px;color:var(--muted);max-height:3.9em;overflow:auto}
.acts{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.acts button.on.accept{background:var(--ok);border-color:var(--ok);color:#fff}
.acts button.on.reject{background:var(--no);border-color:var(--no);color:#fff}
.acts button.on.f{background:var(--accent);border-color:var(--accent);color:#fff}
.acts input{flex:1;min-width:120px;font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:6px}
#zoom{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;z-index:10;padding:24px;gap:16px;align-items:center;justify-content:center;cursor:zoom-out}
#zoom.on{display:flex}
#zoom div{flex:1;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:#fff}
#zoom img{max-width:100%;max-height:calc(100% - 30px);background:#fff}
.empty{grid-column:1/-1;color:var(--muted);padding:40px;text-align:center}
#frame{position:fixed;inset:0;background:rgba(20,24,32,.92);display:none;z-index:11;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:16px;color:#fff}
#frame.on{display:flex}
#fwrap{position:relative;overflow:hidden;cursor:crosshair;user-select:none;line-height:0;background:#fff}
#fwrap img{max-width:92vw;max-height:78vh;display:block;pointer-events:none}
#fcrop{position:absolute;outline:2px solid #3b82f6;box-shadow:0 0 0 9999px rgba(0,0,0,.5);pointer-events:none}
.ferase{position:absolute;background:rgba(255,255,255,.85);outline:2px solid #ef4444;pointer-events:none}
#frame .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:center}
#frame button{padding:5px 12px;border-color:#fff3;background:#fff1;color:#fff}
#frame button.save{background:#2563eb;border-color:#2563eb}
</style>
<header>
  <div class=tabs id=tabs></div>
  <input type=search id=q placeholder="бренд, модель, GTIN">
  <button id=acceptall title="Принять все картинки этой вкладки, по которым ещё нет решения">Принять все без решения</button>
  <div class=pager><button id=prev>←</button><span id=pageinfo></span><button id=next>→</button></div>
  <div class=help>Слева найденная картинка, справа результат автообработки, он же пойдёт в базу. Клавиши: ←/→ или J/K выбор,
  A принять, X отклонить, T довернуть, B отменить доворот, W не отбеливать фон, F рамка обрезки, пробел крупно, Esc закрыть.
  Принятая картинка сразу пишется в каталог результатов. «Принять все» учитывает вкладку и поиск.</div>
</header>
<main id=grid></main>
<div id=zoom><div><img id=zs><span>найденная</span></div><div><img id=zr><span>результат</span></div></div>
<div id=frame>
  <div id=ftitle></div>
  <div id=fwrap><img id=fimg></div>
  <div class=bar><span>Тяни мышкой рамку обрезки. С Shift — прямоугольник, который закрасить белым.</span></div>
  <div class=bar><button class=save id=fsave>Сохранить (Enter)</button><button id=fundo>Убрать последнюю закраску (Z)</button>
    <button id=fclear>Сбросить всё (Backspace)</button><button id=fcancel>Отмена (Esc)</button></div>
</div>
<script>
const PER_PAGE = 40;
const FLAGS = {background:'не белый фон', 'background-light':'светлый фон', small:'мелкий исходник', 'small-product':'мелкое изделие',
  cropped:'обрезано', 'cropped-after-turn':'обрезано после поворота', frame:'снята рамка', diagonal:'доворот', 'rotated-manual':'доворот вручную',
  'manual-crop':'рамка', plain:'доворот отменён', whitened:'фон отбелён', 'extreme-ratio':'очень вытянутая', 'trim-failed':'не обрезалось', error:'ошибка'};
const WARN = new Set(__WARN__);
let items = [], dec = {}, tab = 'todo', page = 0, sel = 0, query = '', pageList = [], pages = 1;
const results = {};  // gtin -> {key, url, flags, out}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);
const warned = (x) => (results[x.g]?.flags ?? []).some((f) => WARN.has(f)) || x.small;
const TABS = [
  ['todo', 'Без решения', (x) => x.s && !dec[x.g]?.status],
  ['warn', 'С предупреждениями', (x) => x.s && !dec[x.g]?.status && warned(x)],
  ['accept', 'Принято', (x) => dec[x.g]?.status === 'accept'],
  ['reject', 'Отклонено', (x) => dec[x.g]?.status === 'reject'],
  ['edit', 'Поворот или рамка', (x) => { const d = dec[x.g]; return d && (d.rotate || d.crop || d.erase || d.plain || d.keep); }],
  ['none', 'Не найдено', (x) => !x.s],
  ['all', 'Все', () => true],
];

function filtered() {
  const test = TABS.find(([k]) => k === tab)[2];
  const q = query.toLowerCase();
  return items.filter((x) => test(x) && (!q || `${x.b} ${x.p} ${x.g}`.toLowerCase().includes(q)));
}
function refresh() {
  const list = filtered();
  pages = Math.max(1, Math.ceil(list.length / PER_PAGE));
  page = Math.max(0, Math.min(page, pages - 1));
  pageList = list.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  sel = Math.min(sel, Math.max(0, pageList.length - 1));
  render();
  pageList.forEach(load);
}
// The marks that change the result; the preview is recomputed when they change.
const marks = (g) => { const d = dec[g] ?? {}; return { rotate: d.rotate, crop: d.crop, erase: d.erase, plain: d.plain, keep: d.keep }; };
const keyOf = (g) => JSON.stringify(marks(g));
async function load(x) {
  if (!x.s) return;
  const key = keyOf(x.g);
  if (results[x.g]?.key === key) return;
  results[x.g] = { key, pending: true };
  const res = await fetch(`/api/result/${x.g}`, { method: 'POST', body: key });
  const r = await res.json();
  if (results[x.g].key !== key) return;
  results[x.g] = { key, ...r, url: `/result/${x.g}.webp?k=${encodeURIComponent(key)}` };
  render();
}
function render() {
  document.getElementById('tabs').innerHTML = TABS.map(([k, n, t]) =>
    `<button data-tab=${k} class="${tab === k ? 'on' : ''}">${n} <b>${items.filter(t).length}</b></button>`).join('');
  document.getElementById('pageinfo').textContent = `${page + 1} / ${pages}`;
  document.getElementById('grid').innerHTML = pageList.length ? pageList.map((x, i) => {
    const d = dec[x.g] ?? {}, r = results[x.g];
    const flags = (r?.flags ?? []).map((f) => `<span class="chip ${WARN.has(f) ? 'w' : ''}">${esc(FLAGS[f] ?? f)}</span>`).join('');
    const chips = flags + (x.exact === false ? '<span class="chip w">другая модель серии</span>' : '')
      + (r?.error ? `<span class="chip w">${esc(r.error)}</span>` : '');
    const src = x.s
      ? `<div data-zoom><img loading=lazy src="${esc(x.s)}"></div><span class="${x.small ? 'small' : ''}">найдена ${x.ss.join('×')}${x.small ? ', мелкая' : ''}</span>`
      : `<div class=missing>картинки нет (${esc(x.st)})</div><span>&nbsp;</span>`;
    const out = !x.s ? '<div class=missing></div><span>&nbsp;</span>'
      : r?.url ? `<div data-zoom><img src="${r.url}"></div><span>результат ${(r.out ?? []).join('×')}</span>`
      : `<div class=missing>${r?.error ? 'ошибка' : 'обработка…'}</div><span>&nbsp;</span>`;
    return `<article class="card ${d.status ? 'c-' + d.status : ''} ${i === sel ? 'sel' : ''}" data-i=${i}>
      <div class=pics><div class=pic>${src}</div><div class=pic>${out}</div></div>
      <div class=meta><span class=name>${esc(x.b)} ${esc(x.p)}</span><code>${x.g}</code>${chips}</div>
      ${x.note ? `<div class=note>${esc(x.note)}</div>` : ''}
      <div class=acts>
        <button class="accept ${d.status === 'accept' ? 'on' : ''}" data-act=accept ${x.s ? '' : 'disabled'}>Принять</button>
        <button class="reject ${d.status === 'reject' ? 'on' : ''}" data-act=reject>Отклонить</button>
        <button class="f ${d.rotate ? 'on' : ''}" data-act=rotate title="Выровнять наклонное изделие (T)">Довернуть</button>
        <button class="f ${d.plain ? 'on' : ''}" data-act=plain title="Отменить автоматический доворот (B)">Отменить доворот</button>
        <button class="f ${d.keep ? 'on' : ''}" data-act=keep title="Оставить светло-серый фон как есть (W)">Не отбеливать</button>
        <button data-act=frame ${x.s ? '' : 'disabled'}>Рамка${d.crop || d.erase ? ' ✓' : ''}</button>
        <input data-comment placeholder="комментарий, при отклонении: почему" value="${esc(d.comment)}">
        ${x.page ? `<a href="${esc(x.page)}" target=_blank rel=noreferrer>страница</a>` : ''}
      </div></article>`;
  }).join('') : '<div class=empty>Здесь пусто</div>';
}

async function save(x, patch) {
  const d = { ...dec[x.g], ...patch };
  for (const k of Object.keys(d)) if (d[k] === undefined || d[k] === 0 || d[k] === false) delete d[k];
  if (Object.keys(d).length) dec[x.g] = d; else delete dec[x.g];
  render();
  const res = await fetch('/api/decision', { method: 'POST', body: JSON.stringify({ gtin: x.g, decision: dec[x.g] ?? null }) });
  if (!res.ok) alert('Не сохранилось: ' + res.status + ' ' + await res.text());
  load(x);
}
async function act(x, a) {
  const d = dec[x.g] ?? {};
  if (a === 'frame') return openFrame(x);
  if (a === 'accept' || a === 'reject') {
    if (a === 'accept' && !x.s) return;
    return save(x, { status: d.status === a ? undefined : a });
  }
  if (a === 'rotate') return save(x, { rotate: !d.rotate });
  if (a === 'plain') return save(x, { plain: !d.plain });
  if (a === 'keep') return save(x, { keep: !d.keep });
}

function zoom(x) {
  document.getElementById('zs').src = x.s || '';
  document.getElementById('zr').src = results[x.g]?.url || '';
  document.getElementById('zoom').classList.add('on');
}

// Crop frame on the found picture. Boxes are fractions [left, top, right, bottom].
let fItem = null, fCrop = null, fErase = [], fDrag = null;
const fwrap = document.getElementById('fwrap');
function openFrame(x) {
  if (!x.s) return;
  fItem = x; fCrop = dec[x.g]?.crop ?? null; fErase = [...(dec[x.g]?.erase ?? [])];
  document.getElementById('ftitle').textContent = `${x.b} ${x.p} · ${x.g}`;
  document.getElementById('fimg').src = x.s;
  document.getElementById('frame').classList.add('on');
  drawFrame();
}
function closeFrame() { document.getElementById('frame').classList.remove('on'); fItem = null; }
const boxStyle = (b) => `left:${b[0] * 100}%;top:${b[1] * 100}%;width:${(b[2] - b[0]) * 100}%;height:${(b[3] - b[1]) * 100}%`;
function drawFrame() {
  fwrap.querySelectorAll('#fcrop,.ferase').forEach((el) => el.remove());
  const live = fDrag && { box: norm(fDrag.a, fDrag.b), erase: fDrag.erase };
  const crop = live && !live.erase ? live.box : fCrop;
  const erase = live?.erase ? [...fErase, live.box] : fErase;
  for (const b of erase) fwrap.insertAdjacentHTML('beforeend', `<div class=ferase style="${boxStyle(b)}"></div>`);
  if (crop) fwrap.insertAdjacentHTML('beforeend', `<div id=fcrop style="${boxStyle(crop)}"></div>`);
}
function point(e) {
  const r = document.getElementById('fimg').getBoundingClientRect();
  const c = (v) => Math.min(1, Math.max(0, v));
  return [c((e.clientX - r.left) / r.width), c((e.clientY - r.top) / r.height)];
}
function norm(a, b) {
  const r4 = (v) => Math.round(v * 10000) / 10000;
  return [r4(Math.min(a[0], b[0])), r4(Math.min(a[1], b[1])), r4(Math.max(a[0], b[0])), r4(Math.max(a[1], b[1]))];
}
fwrap.addEventListener('mousedown', (e) => { e.preventDefault(); fDrag = { a: point(e), b: point(e), erase: e.shiftKey }; });
window.addEventListener('mousemove', (e) => { if (fDrag) { fDrag.b = point(e); drawFrame(); } });
window.addEventListener('mouseup', () => {
  if (!fDrag) return;
  const box = norm(fDrag.a, fDrag.b);
  if (box[2] - box[0] > 0.01 && box[3] - box[1] > 0.01) { if (fDrag.erase) fErase.push(box); else fCrop = box; }
  fDrag = null; drawFrame();
});
async function saveFrame() {
  const x = fItem;
  closeFrame();
  await save(x, { crop: fCrop ?? undefined, erase: fErase.length ? fErase : undefined });
}
document.getElementById('fsave').onclick = saveFrame;
document.getElementById('fcancel').onclick = closeFrame;
document.getElementById('fundo').onclick = () => { fErase.pop(); drawFrame(); };
document.getElementById('fclear').onclick = () => { fCrop = null; fErase = []; drawFrame(); };

document.addEventListener('click', (e) => {
  const t = e.target;
  if (t.closest('#frame')) return;
  if (t.closest('#zoom')) return document.getElementById('zoom').classList.remove('on');
  if (t.dataset.tab) { tab = t.dataset.tab; page = 0; sel = 0; return refresh(); }
  const card = t.closest('.card');
  if (!card || t.tagName === 'A') return;
  const x = pageList[+card.dataset.i];
  sel = +card.dataset.i;
  if (t.closest('[data-zoom]')) return zoom(x);
  if (t.dataset.act) return act(x, t.dataset.act);
  // Only the highlight moves: a re-render would drop the text selection.
  document.querySelectorAll('.card.sel').forEach((c) => c.classList.remove('sel'));
  card.classList.add('sel');
});
document.addEventListener('change', (e) => {
  if (e.target.dataset.comment === undefined) return;
  const x = pageList[+e.target.closest('.card').dataset.i];
  save(x, { comment: e.target.value || undefined });
});
document.getElementById('acceptall').onclick = async (e) => {
  const gtins = filtered().filter((x) => x.s && !dec[x.g]?.status).map((x) => x.g);
  if (!gtins.length) return alert('Здесь нет картинок без решения');
  if (!confirm(`Принять ${gtins.length} шт.? Каждая будет обработана и записана в каталог результатов.`)) return;
  const b = e.target;
  b.disabled = true; b.textContent = `Принимаю ${gtins.length}…`;
  try {
    const res = await fetch('/api/accept-all', { method: 'POST', body: JSON.stringify(gtins) });
    if (!res.ok) return alert('Не сохранилось: ' + res.status + ' ' + await res.text());
    const r = await res.json();
    dec = r.decisions;
    refresh();
    if (r.failed.length) alert(`Не обработались и остались без решения: ${r.failed.join(', ')}`);
  } finally {
    b.disabled = false; b.textContent = 'Принять все без решения';
  }
};
document.getElementById('q').addEventListener('input', (e) => { query = e.target.value.trim(); page = 0; sel = 0; refresh(); });
document.getElementById('prev').onclick = () => { page--; sel = 0; refresh(); window.scrollTo(0, 0); };
document.getElementById('next').onclick = () => { page++; sel = 0; refresh(); window.scrollTo(0, 0); };
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (fItem) {
    if (k === 'enter') saveFrame();
    else if (k === 'escape') closeFrame();
    else if (k === 'backspace') { fCrop = null; fErase = []; drawFrame(); }
    else if (k === 'z' || k === 'я') { fErase.pop(); drawFrame(); }
    else return;
    return e.preventDefault();
  }
  const x = pageList[sel];
  if (k === 'escape') return document.getElementById('zoom').classList.remove('on');
  if (k === 'arrowright' || k === 'j' || k === 'о') sel = Math.min(sel + 1, pageList.length - 1);
  else if (k === 'arrowleft' || k === 'k' || k === 'л') sel = Math.max(sel - 1, 0);
  else if (!x) return;
  else if (k === 'a' || k === 'ф') return act(x, 'accept');
  else if (k === 'x' || k === 'ч') return act(x, 'reject');
  else if (k === 't' || k === 'е') return act(x, 'rotate');
  else if (k === 'b' || k === 'и') return act(x, 'plain');
  else if (k === 'w' || k === 'ц') return act(x, 'keep');
  else if (k === 'f' || k === 'а') return openFrame(x);
  else if (k === ' ') { e.preventDefault(); return zoom(x); }
  else return;
  e.preventDefault();
  render();
  document.querySelector('.card.sel')?.scrollIntoView({ block: 'nearest' });
});

(async () => {
  const data = await (await fetch('/api/items')).json();
  items = data.items; dec = data.decisions;
  refresh();
})();
</script>
"""


def load_items():
    out = []
    for f in sorted((NEW / "results").glob("*.json")):
        r = json.loads(f.read_text())
        gtin = r["gtin"]
        found = sorted((NEW / "images").glob(f"{gtin}.*")) if r.get("status") == "ok" else []
        item = {
            "g": gtin,
            "b": r.get("brand") or "",
            "p": r.get("product") or "",
            "st": r.get("status"),
            "note": r.get("note", ""),
            "page": r.get("page"),
            "exact": r.get("exact_model"),
            "s": None,
        }
        if found:
            item["s"] = "/" + str(found[0].relative_to(ROOT))
            with Image.open(found[0]) as img:
                item["ss"] = list(oriented_size(img))
            item["small"] = max(item["ss"]) < SMALL
        out.append(item)
    out.sort(key=lambda x: (x["b"].lower(), x["g"]))
    return out


def to_mark(marks):
    """Review marks in the form pictures.process_file takes."""
    mark = {k: marks[k] for k in ("crop", "erase") if marks.get(k)}
    # "Довернуть" here levels any product, not only a long one.
    if marks.get("rotate"):
        mark["level"] = True
    if marks.get("plain"):
        mark["choice"] = "bad"
    if marks.get("keep"):
        mark["keep_background"] = True
    return mark


def run(file, marks):
    try:
        r = process_file(file, mark=to_mark(marks))
        return {"flags": r["flags"], "out": r["out"], "buffer": r["buffer"]}
    except Exception as e:  # pylint: disable=broad-exception-caught
        # a broken download must not kill the page
        return {"flags": ["error"], "error": str(e)}


class Store:
    """Decisions in new/decisions.json and the processed results, keyed by GTIN."""

    def __init__(self, items, pool, out):
        self.out = out
        self.lock = threading.Lock()
        self.decisions = json.loads(DECISIONS.read_text()) if DECISIONS.exists() else {}
        self.sources = {x["g"]: ROOT / x["s"][1:] for x in items if x["s"]}
        self.pool = pool
        self.cache = {}
        self.order = [x["g"] for x in items if x["s"]]
        CACHE.mkdir(parents=True, exist_ok=True)

    def result(self, gtin, marks):
        return self._result_job(gtin, marks)()

    def warm_up(self):
        """One result at a time, so the page always has a free worker."""
        for gtin in self.order:
            with self.lock:
                marks = self.decisions.get(gtin, {})
            self.result(gtin, marks)

    def prepare(self):
        with self.lock:
            marks = {g: self.decisions.get(g, {}) for g in self.order}
        jobs = [self._result_job(g, m) for g, m in marks.items()]
        for job in jobs:
            job()

    def cached_buffer(self, gtin):
        with self.lock:
            hit = self.cache.get(gtin)
        r = hit and hit[1]
        return r.get("buffer") if isinstance(r, dict) else None

    def accept_all(self, gtins):
        """Accepts the pictures among `gtins` that have no decision; returns the ones that failed."""
        with self.lock:
            todo = [g for g in gtins if g in self.sources and not self.decisions.get(g, {}).get("status")]
            accepted = {g: {**self.decisions.get(g, {}), "status": "accept"} for g in todo}
            self.decisions.update(accepted)
            self._save_decisions()
        jobs = {g: self._result_job(g, d) for g, d in accepted.items()}
        failed = []
        self.out.mkdir(parents=True, exist_ok=True)
        for g, job in jobs.items():
            r = job()
            if "buffer" in r:
                (self.out / f"{g}.webp").write_bytes(r["buffer"])
            else:
                failed.append(g)
        with self.lock:
            for g in failed:
                self.decisions.get(g, {}).pop("status", None)
                if not self.decisions.get(g, True):
                    del self.decisions[g]
            self._save_decisions()
            known = load_registry()
            for g in todo:
                if g not in failed:
                    known[g] = {"status": "picture"}
            save_registry(known)
        return failed

    def decide(self, gtin, decision):
        with self.lock:
            if decision:
                self.decisions[gtin] = decision
            else:
                self.decisions.pop(gtin, None)
            self._save_decisions()
        # The output holds only accepted pictures, processed with the current marks.
        file = self.out / f"{gtin}.webp"
        accepted = decision and decision.get("status") == "accept"
        if accepted:
            r = self.result(gtin, decision)
            if "buffer" not in r:
                raise ValueError(r.get("error", "no result"))
            self.out.mkdir(parents=True, exist_ok=True)
            file.write_bytes(r["buffer"])
        else:
            file.unlink(missing_ok=True)
        with self.lock:
            known = load_registry()
            if accepted:
                known[gtin] = {"status": "picture"}
            elif known.get(gtin, {}).get("status") == "picture":
                del known[gtin]
            save_registry(known)

    # --- Private ---

    def _save_decisions(self):
        tmp = DECISIONS.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.decisions, ensure_ascii=False, indent=1, sort_keys=True))
        os.replace(tmp, DECISIONS)

    def _result_job(self, gtin, marks):
        """Starts processing unless cached; the returned callable waits for the result."""
        st = self.sources[gtin].stat()
        key = json.dumps(
            {"code": CODE, "source": [st.st_size, st.st_mtime_ns], **to_mark(marks)}, sort_keys=True
        )
        with self.lock:
            hit = self.cache.get(gtin)
            if not hit or hit[0] != key:
                hit = (key, self._load(gtin, key) or self.pool.submit(run, self.sources[gtin], marks))
                self.cache[gtin] = hit
        entry = hit[1]
        if not isinstance(entry, Future):
            return lambda: entry

        def wait():
            r = entry.result()
            with self.lock:
                if self.cache.get(gtin) is hit:
                    self.cache[gtin] = (key, r)
                    self._dump(gtin, key, r)
            return r

        return wait

    def _load(self, gtin, key):
        try:
            meta = json.loads((CACHE / f"{gtin}.json").read_text())
            if meta.pop("key") != key:
                return None
            if "error" not in meta:
                meta["buffer"] = (CACHE / f"{gtin}.webp").read_bytes()
            return meta
        except (OSError, ValueError, KeyError):
            return None

    def _dump(self, gtin, key, r):
        if "buffer" in r:
            (CACHE / f"{gtin}.webp").write_bytes(r["buffer"])
        meta = {k: v for k, v in r.items() if k != "buffer"}
        (CACHE / f"{gtin}.json").write_text(json.dumps({"key": key, **meta}))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, items, store, **kwargs):
        self.items = items
        self.store = store
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _send(self, body, content_type, status=200):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        return self.rfile.read(int(self.headers["Content-Length"]))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(
                PAGE.replace(
                    "__WARN__", json.dumps(REDOWNLOAD_FLAGS + ("error", "extreme-ratio", "whitened"))
                ),
                "text/html; charset=utf-8",
            )
        if path == "/api/items":
            with self.store.lock:
                body = {"items": self.items, "decisions": self.store.decisions}
            return self._send(json.dumps(body, ensure_ascii=False), "application/json")
        if path.startswith("/result/"):
            # The page asks for the result right after /api/result computed it, so it is cached.
            gtin = Path(path).stem
            buffer = self.store.cached_buffer(gtin)
            if not buffer:
                return self.send_error(404)
            return self._send(buffer, "image/webp")
        return super().do_GET()

    def do_POST(self):  # pylint: disable=invalid-name
        path = urlparse(self.path).path
        if path.startswith("/api/result/"):
            gtin = path.rsplit("/", 1)[1]
            if gtin not in self.store.sources:
                return self.send_error(404)
            r = self.store.result(gtin, json.loads(self._body()))
            return self._send(json.dumps({k: v for k, v in r.items() if k != "buffer"}), "application/json")
        if path == "/api/decision":
            body = json.loads(self._body())
            try:
                self.store.decide(body["gtin"], body["decision"])
            except Exception as e:  # pylint: disable=broad-exception-caught
                # reported to the page, which shows it
                return self._send(str(e), "text/plain; charset=utf-8", 500)
            return self._send("{}", "application/json")
        if path == "/api/accept-all":
            failed = self.store.accept_all(json.loads(self._body()))
            with self.store.lock:
                body = {"decisions": self.store.decisions, "failed": failed}
                return self._send(json.dumps(body, ensure_ascii=False), "application/json")
        return self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--jobs", type=int, help="2 for the page, all cores for --prepare")
    parser.add_argument("--prepare", action="store_true", help="fill the result cache and exit")
    parser.add_argument("--out", type=lambda p: Path(p).resolve(), default=NEW / "accepted")
    args = parser.parse_args()
    items = load_items()
    with ProcessPoolExecutor(args.jobs or (os.cpu_count() if args.prepare else 2)) as pool:
        store = Store(items, pool, args.out)
        if args.prepare:
            store.prepare()
            print(f"{len(store.order)} results in {CACHE}")
        else:
            threading.Thread(target=store.warm_up, daemon=True).start()
            server = ThreadingHTTPServer(("0.0.0.0", args.port), partial(Handler, items=items, store=store))
            print(
                f"http://localhost:{args.port}/  {len(items)} devices, {len(store.sources)} with a picture,"
                f" decisions -> {DECISIONS}"
            )
            server.serve_forever()


if __name__ == "__main__":
    main()
