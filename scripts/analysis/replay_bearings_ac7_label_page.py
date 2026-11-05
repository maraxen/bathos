"""Write a self-contained page for the owner to label the AC-7 readings, and save labels.json from it.

Prints only counts, so the person running it does not see the readings. Every entry needs an explicit
choice, so a forgotten entry stays unlabelled and the scorer's n_unlabelled catches it.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

logger = logging.getLogger("replay_bearings_ac7_label_page")

ARMS = ("colliculix", "tev_design", "plegadx")
READERS = ("reader1", "reader2", "reader3")
PURPOSES = {
    "A": "cascade router",
    "B": "praxia crystallize/remelt cost gate",
    "C": "MTT/rhizome program",
    "D": "SWE-bench recon line",
    "E": "contamination-immune methodology paper",
    "F": "semantic compiler vision",
    "G": "interpretability mandate",
}

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AC-7 Labels</title>
<style>
:root{--bg:#fbfaf7;--fg:#22211e;--muted:#6a675f;--line:#dcd8cf;--card:#fff;--accent:#2f5d8a;--warn:#9a4a12}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a18;--fg:#ece9e2;--muted:#a29e94;--line:#3a3833;--card:#24231f;--accent:#8db8e0;--warn:#e0a070}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px 16px 96px}
h1{font-size:1.5rem;margin:.5rem 0}h2{font-size:1.25rem;margin:2rem 0 .25rem}h3{font-size:1rem;margin:1.25rem 0 .5rem;color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:8px 0}
.card.done{border-left:4px solid var(--accent)}
.kind{font-size:.8rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.opts{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0}
label{cursor:pointer}
input[type=text]{width:100%;max-width:420px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
details{font-size:.85rem;color:var(--muted);margin-top:6px;overflow-wrap:anywhere}
.ag{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:.9rem;margin-top:6px}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
button{font:inherit;padding:6px 12px;border:1px solid var(--accent);background:transparent;color:var(--accent);border-radius:6px;cursor:pointer}
#left{color:var(--warn)}
code{font-size:.9em}
</style></head><body><main>
<h1>AC-7 labels</h1>
<p>Label every entry. Choose <b>Project-level purpose</b> and name it if the entry shows a purpose of the whole project; use the same name when two entries show the same purpose, because a name counts once per reader. Choose <b>Sub-line</b> if it is part of something larger or the record does not support it. Choose <b>Too vague</b> if nobody could disagree with it.</p>
<p>Pre-registered: on tev_design, the k=4 vs k=9 episode and the 260714 target swap are claim-scale slips, so entries that report them as separate purposes are sub-lines.</p>
<p>On colliculix, also tick which of the diagnosed purposes A to G an entry corresponds to.</p>
<div id="root"></div>
</main>
<div class="bar"><span id="left"></span><button id="save">Download labels.json</button><span class="muted">Save it as <code>~/scratch/ac7/labels.json</code></span></div>
<script>
const DATA=__DATA__;const AG=__PURPOSES__;const KEY="ac7-labels-v1";
let state={};try{state=JSON.parse(localStorage.getItem(KEY))||{}}catch(e){}
const persist=()=>{try{localStorage.setItem(KEY,JSON.stringify(state))}catch(e){}};
const root=document.getElementById("root");
const el=(t,p={},...k)=>{const e=document.createElement(t);Object.assign(e,p);k.forEach(c=>e.append(c));return e};
const total=DATA.flatMap(r=>r.entries).length;
function refresh(){const n=DATA.flatMap(r=>r.entries.map(x=>state[r.key+"|"+x.id])).filter(s=>s&&s.choice&&(s.choice!=="project"||(s.identity||"").trim())).length;
document.getElementById("left").textContent=(total-n)?(total-n)+" of "+total+" entries still need a label":"All "+total+" entries labelled";}
const arms=[...new Set(DATA.map(r=>r.arm))];
for(const arm of arms){root.append(el("h2",{textContent:arm}));
const list=el("datalist",{id:"names-"+arm});root.append(list);
const names=()=>{list.replaceChildren(...[...new Set(DATA.filter(r=>r.arm===arm).flatMap(r=>r.entries.map(x=>(state[r.key+"|"+x.id]||{}).identity)).filter(Boolean))].map(v=>el("option",{value:v})))};names();
for(const r of DATA.filter(x=>x.arm===arm)){root.append(el("h3",{textContent:r.reader}));
for(const x of r.entries){const k=r.key+"|"+x.id;const s=state[k]||(state[k]={});
const card=el("div",{className:"card"});const mark=()=>card.classList.toggle("done",!!s.choice);
card.append(el("div",{className:"kind",textContent:x.kind+" "+x.id}),el("div",{textContent:x.text}));
const opts=el("div",{className:"opts"});const name=el("input",{type:"text",placeholder:"Name this purpose",value:s.identity||""});name.setAttribute("list","names-"+arm);name.hidden=s.choice!=="project";
name.oninput=()=>{s.identity=name.value;persist();refresh()};name.onchange=names;
for(const [v,t] of [["project","Project-level purpose"],["sub","Sub-line or unsupported"],["generic","Too vague"]]){const i=el("input",{type:"radio",name:k,checked:s.choice===v});
i.onchange=()=>{s.choice=v;name.hidden=v!=="project";persist();mark();refresh()};opts.append(el("label",{},i," "+t));}
card.append(opts,name);
if(arm==="colliculix"){const ag=el("div",{className:"ag"});s.maps=s.maps||[];for(const [c,t] of Object.entries(AG)){const i=el("input",{type:"checkbox",checked:s.maps.includes(c)});i.onchange=()=>{s.maps=i.checked?[...s.maps,c]:s.maps.filter(m=>m!==c);persist()};ag.append(el("label",{},i," "+c+": "+t));}card.append(ag);}
card.append(el("details",{},el("summary",{textContent:"Citations ("+x.citations.length+")"}),...x.citations.map(c=>el("div",{textContent:typeof c==="string"?c:JSON.stringify(c)}))));
mark();root.append(card);}}}
document.getElementById("save").onclick=()=>{const out={};for(const r of DATA){const o=out[r.key]={};for(const x of r.entries){const s=state[r.key+"|"+x.id]||{};if(!s.choice||(s.choice==="project"&&!(s.identity||"").trim()))continue;
o[x.id]={identity:s.choice==="project"?s.identity.trim():null,generic:s.choice==="generic",maps_to:[...(s.maps||[])].sort()};}}
const a=el("a",{href:URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:"application/json"})),download:"labels.json"});a.click();};
refresh();
</script></body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data = []
    for arm in ARMS:
        for reader in READERS:
            reading = json.loads((args.work_dir / "readings" / arm / f"{reader}.json").read_text(encoding="utf-8"))
            entries = [
                {"id": p["id"], "kind": "purpose", "text": p["text"], "citations": p["citations"]}
                for p in reading["purposes"]
            ]
            entries += [
                {"id": f"open:{i}", "kind": "open line", "text": o["text"], "citations": o["citations"]}
                for i, o in enumerate(reading.get("open_lines", []))
            ]
            data.append({"key": f"{arm}/{reader}", "arm": arm, "reader": reader, "entries": entries})

    # "</" cannot appear inside the inline script, or a reading's text could close it.
    embed = lambda obj: json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")  # noqa: E731
    html = PAGE.replace("__DATA__", embed(data)).replace("__PURPOSES__", embed(PURPOSES))
    out = args.work_dir / "label.html"
    out.write_text(html, encoding="utf-8")
    logger.info("wrote %s: %d readings, %d entries", out, len(data), sum(len(r["entries"]) for r in data))


if __name__ == "__main__":
    main()
