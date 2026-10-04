#!/usr/bin/env python3
"""Build a standalone HTML review interface for ANY human-in-the-loop decision queue.

This stack is entering a long period of ratification and HITL, and every one of those tasks
is the same shape underneath: a queue of items, some text you have to read, a question with
a fixed set of answers, and a decision that has to come back to a store. Only the vocabulary
changes. So the interface is built once, here, and each task supplies a SPEC.

Nothing in this file knows what AI tagging is, what a TRL is, or what an OKR is. Adapters do
that - see scripts/ai-tagging/render_adjudication_ui.py for the first one.

WHAT IT GUARANTEES, so an adapter cannot ship a confusing screen:

  * EVERY OPTION CARRIES ITS DEFINITION, and it is refused without one. The definition sits
    against the radio it defines, always visible - not in a tooltip, not in a separate
    document, not in the reviewer's memory. A reviewer choosing between four words they have
    to remember the meaning of is a reviewer guessing.
  * THE TEXT YOU JUDGE FROM IS ON SCREEN, beside the question rather than behind a click,
    with any quoted evidence highlighted where it actually sits in the passage.
  * NOTHING IS POSTED ANYWHERE. One self-contained file, system fonts, no network. It has to
    open by double-clicking on a managed laptop.
  * PROGRESS SURVIVES THE TAB CLOSING. localStorage per batch, and re-opening resumes at the
    first undecided item.

The spec, as JSON or a dict:

    title      window/header text
    batch      stable id - keys localStorage and names the export
    question   the one question asked of every item
    options    [{code, label, definition}]  definition REQUIRED
    defer      optional {code, label, definition} for "come back to this"
    note       placeholder for the free-text line, or null to hide it
    purpose    "operational" (default) or "ground_truth". THE ANCHORING SWITCH.
               operational  — a proposed answer is pre-selected, so confirming costs one
                              keypress. Use when you are cleaning a record: no gold standard
                              is being minted, so agreeing cheaply is pure gain.
               ground_truth — NOTHING is pre-selected and item.current is refused, because the
                              human's own judgement is the thing being recorded. Showing a
                              reviewer the standing answer measures their agreeableness: FSO's
                              reviewers, shown their model's verdict, confirmed 78% of it and
                              agreement ran 88.3% where they confirmed against 28.8% where they
                              overturned. The model's EVIDENCE may still be shown -- anchoring
                              comes from seeing the answer, not the material.
    csv        THE CONTRACT — {header[], decision_column, correction_column, filename}
               `header` is the INPUT queue's own column list, in the input's own order. The
               export writes exactly those columns back, with the decision and correction
               cells filled in place, so the reviewer's file IS the file the task's existing
               importer already reads. No new loader, per task, ever. Refused without it.
    items      [{id, title, meta[], current, claim{}, body{}, row{}}]
               `row` is the item's FULL original CSV row, keyed by column name. It is what
               gets written back out; anything not decided is passed through untouched.

An item's `current` pre-selects an option, so confirming without changing it IS the
"leave it as it stands" decision and there is no second vocabulary to learn.
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import sys

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--paper:#EDEAE3;--ink:#1A1D1A;--soft:#5F645C;--rule:#C7C3B7;--hi:#F7F4EC;
  --go:#2F5D50;--warn:#8C3A2B;--mark:#F4DC9A;--tone-a:#544A7D;--tone-b:#7A6A3F}
@media (prefers-color-scheme:dark){:root{--paper:#15171A;--ink:#E8E5DC;--soft:#969C93;
  --rule:#31363A;--hi:#1D2023;--go:#7FBBA6;--warn:#E08E7B;--mark:#4C441E;
  --tone-a:#A99BD8;--tone-b:#C9B375}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--paper);color:var(--ink);display:flex;flex-direction:column;
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.rail{height:3px;background:var(--rule);flex:none}
.rail i{display:block;height:100%;background:var(--ink);width:0;transition:width .25s ease}
header{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex:none;
  padding:.75rem 1.4rem;border-bottom:1px solid var(--rule)}
header b{font-weight:650}
header span{color:var(--soft);font-variant-numeric:tabular-nums;font-size:.87rem}
main{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
main.solo{grid-template-columns:minmax(0,46rem);justify-content:center}
@media (max-width:1020px){main,main.solo{grid-template-columns:1fr;overflow:auto}}
.ask,.read{overflow:auto;padding:1.6rem 1.5rem 3.5rem}
.read{border-left:1px solid var(--rule);background:var(--hi)}
@media (max-width:1020px){.read{border-left:0;border-top:1px solid var(--rule)}}
.enter{animation:sl .2s ease}
@keyframes sl{from{opacity:0;transform:translateX(9px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.enter{animation:none}}
.meta{display:flex;gap:.55rem;flex-wrap:wrap;align-items:baseline;color:var(--soft);
  font-size:.84rem;font-variant-numeric:tabular-nums}
.chip{border:1px solid currentColor;border-radius:2px;padding:.04rem .36rem;font-size:.71rem;
  font-weight:600;letter-spacing:.03em}
.t-a{color:var(--tone-a)}.t-b{color:var(--tone-b)}
.mlab{font-style:normal;font-weight:400;opacity:.62;letter-spacing:.01em}
h1{font:600 1.32rem/1.3 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;margin:.3rem 0 0}
.blk{margin-top:1.6rem;border-top:1px solid var(--rule);padding-top:.5rem}
.blk>em{font-style:normal;color:var(--soft);font-size:.79rem;letter-spacing:.02em}
blockquote{margin:.85rem 0 0;padding-left:1rem;border-left:3px solid var(--ink);
  font:400 1.24rem/1.48 ui-serif,Georgia,"Iowan Old Style",serif;max-width:32em}
blockquote span{display:block}blockquote span+span{margin-top:.6rem}
.reads{margin:.95rem 0 0;max-width:40em}
.q{margin:.35rem 0 0;font-size:1.08rem;font-weight:600}
.opts{margin:.9rem 0 0;display:flex;flex-direction:column;gap:.15rem;max-width:36rem}
.opt{display:flex;gap:.7rem;align-items:flex-start;padding:.62rem .7rem;border-radius:4px;
  cursor:pointer;border:1px solid transparent}
.opt:hover{background:var(--paper)}
.opt:has(input:checked){border-color:var(--rule);background:var(--paper)}
.opt input{margin:.28rem 0 0;width:1.05rem;height:1.05rem;flex:none;accent-color:var(--go)}
.opt input:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.opt .lab{font-weight:650}
.opt .now{font-weight:400;color:var(--go);font-size:.8rem;margin-left:.45rem}
.opt .def{display:block;font-weight:400;color:var(--soft);font-size:.88rem;margin-top:.15rem;
  max-width:32em}
.go{margin-top:1.3rem;display:flex;align-items:center;gap:1.1rem;flex-wrap:wrap}
button{font:inherit;background:none;color:inherit;border:1px solid var(--rule);
  border-radius:3px;padding:.45rem .8rem;cursor:pointer}
button:hover{background:var(--hi)}
button:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
button.primary{background:var(--ink);color:var(--paper);border-color:var(--ink);
  font-weight:600;padding:.55rem 1.15rem}
button.primary:hover{background:var(--ink);opacity:.87}
.go a{color:var(--soft);cursor:pointer;text-decoration:underline;text-underline-offset:3px}
input[type=text]{font:inherit;width:100%;max-width:32rem;margin-top:1.1rem;padding:.45rem .55rem;
  background:transparent;color:inherit;border:0;border-bottom:1px solid var(--rule)}
input[type=text]:focus{outline:none;border-bottom-color:var(--ink)}
.read h2{font:600 .79rem/1 ui-sans-serif,sans-serif;color:var(--soft);margin:0 0 1rem;
  letter-spacing:.02em}
.body{white-space:pre-wrap;font:400 .97rem/1.7 ui-serif,Georgia,serif;max-width:40em}
mark{background:var(--mark);color:inherit;padding:.06em 0;border-radius:2px}
.tip{border-bottom:1px dotted var(--soft);cursor:help;position:relative}
.tip:hover::after,.tip:focus-visible::after{content:attr(data-tip);position:absolute;left:0;
  top:1.55em;z-index:9;width:max-content;max-width:24rem;background:var(--ink);
  color:var(--paper);padding:.5rem .65rem;border-radius:3px;white-space:normal;text-align:left;
  font:400 .82rem/1.45 ui-sans-serif,sans-serif;box-shadow:0 2px 12px rgba(0,0,0,.25)}
footer{flex:none;display:flex;justify-content:space-between;align-items:center;gap:1rem;
  padding:.55rem 1.4rem;border-top:1px solid var(--rule);font-size:.82rem;color:var(--soft)}
kbd{font:600 .7rem/1 ui-monospace,Consolas,monospace;border:1px solid var(--rule);
  border-radius:2px;padding:.16rem .3rem}
.done{padding:4rem 1rem;max-width:32em}
.done h2{font:600 1.5rem/1.3 ui-sans-serif,sans-serif;color:var(--ink);margin:0 0 .6rem}
.tally{font-variant-numeric:tabular-nums;color:var(--soft);margin:1.1rem 0 1.8rem;line-height:1.95}
</style></head><body>
<div class="rail"><i id="rail"></i></div>
<header><b>__TITLE__</b><span id="pos"></span></header>
<main id="main"><div class="ask" id="ask"></div><div class="read" id="read"></div></main>
<footer><span><kbd>1</kbd>–<kbd>9</kbd> choose &nbsp; <kbd>Enter</kbd> confirm &nbsp;
  <kbd>&larr;</kbd> back &nbsp; dotted terms explain themselves</span>
  <button id="dl">Download decisions</button></footer>
<script>
const S = __SPEC__, KEY = "hitl:" + S.batch;
let done = JSON.parse(localStorage.getItem(KEY) || "{}");
let i = S.items.findIndex(r => !done[r.id]); if (i < 0) i = S.items.length;
const $ = s => document.querySelector(s);
const save = () => localStorage.setItem(KEY, JSON.stringify(done));
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ALL = S.defer ? S.options.concat([S.defer]) : S.options;

function highlight(text, spans){
  let out = esc(text);
  (spans || []).slice().sort((a, b) => b.length - a.length).forEach(s => {
    const t = esc(String(s).trim());
    if (t.length < 12) return;
    out = out.split(t).join("" + t + "");
  });
  return out.split("").join("<mark>").split("").join("</mark>");
}

function render(){
  $("#rail").style.width = (100 * Object.keys(done).length / S.items.length) + "%";
  if (i >= S.items.length) return finish();
  const r = S.items[i], prev = done[r.id];
  const picked = (prev && prev.code) || r.current || "";
  $("#pos").textContent = `${i + 1} of ${S.items.length}  ·  ${Object.keys(done).length} decided`;
  $("#main").className = r.body ? "" : "solo";

  // A bare value is not information. `0.75` on its own asks the reviewer to guess whether it
  // is a confidence, a share or a score -- so a meta entry may carry a label, and it renders
  // as "confidence 0.75". Unlabelled entries still work for values that speak for themselves.
  const meta = (r.meta || []).map(m => {
    const lab = m.label ? `<i class="mlab">${esc(m.label)}</i> ` : "";
    return m.tip
      ? `<span class="chip t-${m.tone || "a"}">${lab}<span class="tip" data-tip="${esc(m.tip)}">${esc(m.text)}</span></span>`
      : `<span>${lab}${esc(m.text)}</span>`;
  }).join("");

  const claim = r.claim ? `<div class="blk"><em>${esc(r.claim.heading || "the case")}</em>
      ${(r.claim.quote || []).length ? "<blockquote>" +
        r.claim.quote.map(q => `<span>${esc(q)}</span>`).join("") + "</blockquote>" : ""}
      ${r.claim.reads ? `<p class="reads">${esc(r.claim.lead || "Read as")}
        <b class="tip" data-tip="${esc(r.claim.tip || "")}">${esc(r.claim.reads)}</b>.</p>` : ""}
    </div>` : "";

  const opts = ALL.map((o, n) => `<label class="opt"><input type="radio" name="v"
      value="${esc(o.code)}" ${o.code === picked ? "checked" : ""}>
      <span><span class="lab">${esc(o.label)}</span>${
        o.code === r.current ? '<span class="now">stands today</span>' : ""}
        <span class="def">${esc(o.definition)}</span></span></label>`).join("");

  // The item id used to be printed here as the first chip. It is an INTERNAL key -- often a
  // pipe-joined composite -- and putting it at the top of a reviewer's screen both wastes the
  // most valuable line on the page and makes the artifact look like a database dump rather
  // than a question. It is still reachable (hover the title) for "which row was that?".
  $("#ask").innerHTML = `<div class="enter"><div class="meta">${meta}</div>
    <h1 title="${esc(r.id)}">${esc(r.title || r.id)}</h1>${claim}
    <div class="blk"><em>your decision</em><p class="q">${esc(S.question)}</p>
      <div class="opts">${opts}</div>
      ${S.note ? `<input type="text" id="note" placeholder="${esc(S.note)}" autocomplete="off">` : ""}
      <div class="go"><button class="primary" id="next">Confirm and continue</button>
        ${i > 0 ? '<a id="back">back to the last one</a>' : ""}</div>
    </div></div>`;
  $("#read").innerHTML = r.body
    ? `<h2>${esc(r.body.heading || "the record")}</h2>
       <div class="body">${highlight(r.body.text || "", r.claim && r.claim.quote)}</div>` : "";

  if (prev && prev.note && $("#note")) $("#note").value = prev.note;
  const m = $("#read mark"); if (m) m.scrollIntoView({block: "center"});
  $("#next").onclick = commit;
  if ($("#back")) $("#back").onclick = () => { i--; render(); };
}

function commit(){
  const sel = document.querySelector('input[name=v]:checked');
  if (!sel){ document.querySelector(".opt input").focus(); return; }
  const r = S.items[i];
  done[r.id] = {code: sel.value, changed: sel.value !== (r.current || ""),
                note: ($("#note") || {}).value || "", at: new Date().toISOString()};
  save(); i++; render();
}

function finish(){
  const vals = Object.values(done);
  const kept = vals.filter(v => !v.changed && (!S.defer || v.code !== S.defer.code)).length;
  const chg = vals.filter(v => v.changed && (!S.defer || v.code !== S.defer.code)).length;
  const def = S.defer ? vals.filter(v => v.code === S.defer.code).length : 0;
  $("#main").className = "solo"; $("#read").innerHTML = "";
  $("#pos").textContent = `${vals.length} of ${S.items.length} decided`;
  $("#ask").innerHTML = `<div class="done"><h2>Queue clear</h2>
    <p>Every item has a decision. Download them and hand the file to the loader — nothing has
       been written to any system yet.</p>
    <div class="tally">${kept} left as they stood<br>${chg} changed<br>${def} deferred</div>
    <button class="primary" onclick="download()">Download decisions</button></div>`;
}

const q = s => '"' + String(s == null ? "" : s).replace(/"/g, '""') + '"';

// CRLF, and a UTF-8 BOM. Both are what PowerShell's Export-Csv writes and what Excel expects,
// and this file is a drop-in for exactly that round-trip -- so it should differ from the queue
// it replaces in its DECISION cells and nowhere else. The BOM also stops Excel mangling
// non-ASCII on open, which is how apostrophes and dashes get quietly rewritten before the file
// ever reaches the importer.
function saveCsv(name, rows){
  const text = "﻿" + rows.join("\r\n") + "\r\n";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], {type: "text/csv;charset=utf-8"}));
  a.download = name; a.click();
}

// THE WHOLE POINT: emit the INPUT CSV's own columns, in the input's own order, with the
// decision and correction columns filled in place. The reviewer's file is then the file the
// task's existing importer already reads -- "open it in Excel" replaced with no new loader.
//
// This used to emit a fixed id/decision/changed/note/decided_at shape that matched NO importer
// on this stack, which meant every task would still have needed its own loader -- exactly the
// duplication the tool exists to remove.
//
// Undecided rows are emitted too, with the decision cell left empty. Every importer here
// treats an empty decision as "skip", so a partial pass round-trips as a partial pass rather
// than silently dropping the rows nobody reached.
function download(){
  const C = S.csv || {};
  const head = C.header || [];
  const dCol = C.decision_column || "DECISION";
  const nCol = C.correction_column || "correction_notes";
  if (!head.length){ alert("This build has no CSV contract; cannot export."); return; }

  const lines = S.items.map(r => {
    const v = done[r.id];
    const row = Object.assign({}, r.row || {});
    if (v){
      row[dCol] = v.code;
      if (nCol in row) row[nCol] = v.note == null ? "" : v.note;
    }
    return head.map(k => q(row[k])).join(",");
  });
  saveCsv(C.filename || (S.batch + ".csv"), [head.map(q).join(",")].concat(lines));

  // Behavioural columns go to a SIDECAR keyed on the same id. They cannot ride in the queue
  // CSV without breaking the byte-compatibility above, and that compatibility is the thesis.
  const tHead = ["id", dCol, "decided_at", "changed", "ms_on_item", "radio_changes",
                 "revisits", "read_scrolled", "flagged", "flag_aspect", "flag_denied"];
  const tLines = S.items.filter(r => done[r.id]).map(r => {
    const v = done[r.id], t = v.telemetry || {};
    return [r.id, v.code, v.at, v.changed, t.ms_on_item, t.radio_changes, t.revisits,
            t.read_scrolled, v.flagged, v.flag_aspect, t.flag_denied].map(q).join(",");
  });
  saveCsv("telemetry_" + S.batch + ".csv", [tHead.map(q).join(",")].concat(tLines));
}
$("#dl").onclick = download;

addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" && e.target.type === "text"){
    if (e.key === "Enter") commit(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Enter"){ commit(); return; }
  if (e.key === "ArrowLeft"){ if (i > 0){ i--; render(); } return; }
  const n = parseInt(e.key, 10);
  if (n >= 1 && n <= ALL.length){
    const b = document.querySelectorAll('input[name=v]')[n - 1];
    if (b){ b.checked = true; b.focus(); }
  }
});
render();
</script></body></html>
"""


def build(spec: dict, out: pathlib.Path) -> pathlib.Path:
    """Validate the spec and write the file. Refuses rather than shipping a vague screen."""
    for key in ("title", "batch", "question", "options", "items"):
        if not spec.get(key):
            raise SystemExit("REFUSED: spec is missing %r." % key)
    for o in list(spec["options"]) + ([spec["defer"]] if spec.get("defer") else []):
        if not o.get("definition"):
            # The whole point. An option a reviewer has to remember the meaning of is an
            # option they will guess at, and a guess recorded as a ruling is worse than no
            # ruling. If there is no definition to show, the vocabulary is not ready.
            raise SystemExit("REFUSED: option %r has no definition. Every choice must carry "
                             "the words that define it, beside the choice." % o.get("code"))
    if len(spec["options"]) > 8:
        raise SystemExit("REFUSED: %d options. Past about eight this is a search problem, "
                         "not a decision, and a radio list is the wrong instrument."
                         % len(spec["options"]))

    # The CSV contract. The exported file has to be the file the task's own importer already
    # reads, so the header is the INPUT's header, in the input's order, and the decision and
    # correction columns must actually be in it. Getting this wrong is silent: the reviewer
    # does the work, the export looks fine, and the importer skips every row.
    csv_spec = spec.get("csv") or {}
    header = list(csv_spec.get("header") or [])
    if not header:
        raise SystemExit(
            "REFUSED: spec has no csv.header. Without the input queue's own column list the "
            "export cannot round-trip, and every task would need a new loader -- which is the "
            "duplication this tool exists to remove.")
    d_col = csv_spec.get("decision_column", "DECISION")
    n_col = csv_spec.get("correction_column", "correction_notes")
    if d_col not in header:
        raise SystemExit("REFUSED: decision column %r is not in csv.header. The importer reads "
                         "that column by name; an export without it round-trips as zero "
                         "decisions, silently. Header is: %s" % (d_col, ", ".join(header)))
    if n_col and n_col not in header:
        raise SystemExit("REFUSED: correction column %r is not in csv.header. An EDIT would "
                         "have nowhere to put its correction. Header is: %s"
                         % (n_col, ", ".join(header)))
    missing = [it.get("id") for it in spec["items"] if not it.get("row")]
    if missing:
        raise SystemExit("REFUSED: %d item(s) carry no `row`, so their original columns cannot "
                         "be written back. First: %r" % (len(missing), missing[0]))

    purpose = spec.get("purpose", "operational")
    if purpose not in ("operational", "ground_truth"):
        raise SystemExit("REFUSED: purpose %r is neither 'operational' nor 'ground_truth'. It "
                         "decides whether the standing answer is shown, which decides whether "
                         "the labels measure the record or the reviewer's agreeableness."
                         % purpose)
    if purpose == "ground_truth":
        anchored = [it.get("id") for it in spec["items"] if it.get("current")]
        if anchored:
            raise SystemExit(
                "REFUSED: purpose is 'ground_truth' but %d item(s) carry `current`, which "
                "pre-selects the standing answer. That is the FSO failure exactly -- their "
                "reviewers saw their model's verdict and confirmed 78%% of it. Drop `current`, "
                "or declare the task 'operational' if you are cleaning a record rather than "
                "minting a gold standard. First: %r" % (len(anchored), anchored[0]))
    else:
        # Not fatal -- some operational queues genuinely have no proposed answer -- but it means
        # every item costs a click instead of a keypress, and on a 300-row queue that is the
        # difference between a session someone finishes and one they abandon.
        if not any(it.get("current") for it in spec["items"]):
            print("  note: operational task with no `current` on any item -- nothing is "
                  "pre-selected, so agreeing costs a click rather than a keypress.")
    page = (TEMPLATE.replace("__TITLE__", html.escape(spec["title"]))
                    .replace("__SPEC__", json.dumps(spec, ensure_ascii=False)))
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(page, encoding="utf-8")
    except PermissionError:
        raise SystemExit("REFUSED: %s is open in another program." % out.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spec", required=True, help="JSON spec file, or - for stdin")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    raw = sys.stdin.read() if a.spec == "-" else pathlib.Path(a.spec).read_text(encoding="utf-8")
    spec = json.loads(raw)
    p = build(spec, pathlib.Path(a.out))
    print("  %d item(s), %d option(s) -> %s" % (len(spec["items"]), len(spec["options"]), p))
    print("  %.0f KB, self-contained. Double-click it; nothing is posted anywhere."
          % (p.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
