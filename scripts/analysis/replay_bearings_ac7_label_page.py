"""Build the page the owner uses to label the AC-7 readings, and export the saved labels for the scorer.

Build: writes a page for publishing as a private artifact, whose labels save to the artifact's database.
Export: turns the documents read back from that database into ``labels.json``.

Prints only counts. The page never shows per-reader purpose counts or the outcome rule's numbers, so the
owner cannot see the score form while labelling. Every entry needs an explicit choice, so a forgotten entry
stays unlabelled and the scorer's n_unlabelled catches it.
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

PAGE = r"""<title>AC-7 Labels</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Newsreader:opsz,wght@6..72,400;6..72,500&display=swap">
<style>
/* Layout: one entry at a time in a single reading column, with a progress bar that stays in view. */
:root{--bg:#f3f5f6;--card:#fff;--fg:#1c2529;--muted:#5a6a72;--line:#d3dbdf;--accent:#0b6b75;--accent-soft:#dcedef;--warn:#9a4a12;
--read:"Newsreader",Georgia,serif;--ui:"IBM Plex Sans",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#14191c;--card:#1d2428;--fg:#e6ecee;--muted:#9dadb5;--line:#34404a;--accent:#5cc3ce;--accent-soft:#17383d;--warn:#e7a673;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#14191c;--card:#1d2428;--fg:#e6ecee;--muted:#9dadb5;--line:#34404a;--accent:#5cc3ce;--accent-soft:#17383d;--warn:#e7a673;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:16px/1.5 var(--ui)}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--card)}
.wrap{max-width:760px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.top{position:sticky;top:env(safe-area-inset-top,0px);background:var(--card);border-bottom:1px solid var(--line);z-index:5}
.top .wrap{display:flex;flex-wrap:wrap;gap:4px 16px;align-items:center;padding-block:10px}
h1{font-size:1rem;font-weight:600;margin:0}
#count{font-variant-numeric:tabular-nums;color:var(--muted);font-size:.9rem}
#status{font-size:.85rem;color:var(--muted);margin-left:auto}
#status.bad{color:var(--warn)}
.bar{width:100%;height:4px;background:var(--line);border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);width:0}
main{padding-block:20px 56px}
.where{font-size:.9rem;color:var(--muted);margin:0 0 8px}
.where b{color:var(--fg);font-weight:600}
#text{font:400 1.3rem/1.45 var(--read);margin:0;overflow-wrap:anywhere}
details{margin:10px 0 0;font-size:.85rem;color:var(--muted)}
summary{cursor:pointer}
#cites div{font:.8rem/1.45 var(--mono);margin-top:6px;overflow-wrap:anywhere}
.note{font-size:.9rem;color:var(--warn);margin:12px 0 0}
.choices{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:20px}
button{font:inherit;color:inherit;cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.choice{text-align:left;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-width:0}
.choice b{display:block;font-weight:600}
.choice span{display:block;font-size:.85rem;color:var(--muted)}
.choice[aria-pressed="true"],.tick[aria-pressed="true"],.chip[aria-pressed="true"]{background:var(--accent-soft);border-color:var(--accent)}
kbd{font:500 .75rem var(--mono);border:1px solid var(--line);border-radius:4px;padding:0 5px;color:var(--muted);background:var(--bg)}
.panel{margin-top:16px}
.panel label,.panel h2{display:block;font-size:.9rem;font-weight:600;margin:0 0 6px}
input[type=text]{width:100%;padding:8px 10px;font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px}
.chips,.ticks{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip,.tick{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:3px 9px;font-size:.9rem;text-align:left;overflow-wrap:anywhere;min-width:0}
.hint{font-size:.85rem;color:var(--muted);margin:6px 0 0}
.hint.warn{color:var(--warn)}
.nav{display:flex;flex-wrap:wrap;gap:8px;margin-top:24px;align-items:center}
.btn{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:7px 14px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--card);font-weight:600}
.right{margin-left:auto}
#overview h2{font-size:1.05rem;margin:28px 0 8px}
#overview h3{font-size:.9rem;font-weight:500;color:var(--muted);margin:10px 0 4px}
.map{display:flex;flex-wrap:wrap;gap:4px}
.dot{width:30px;height:30px;border:1px solid var(--line);border-radius:5px;background:var(--card);font:.75rem var(--mono);color:var(--muted);padding:0}
.dot.done{background:var(--accent-soft);border-color:var(--accent);color:var(--fg)}
.names{display:grid;gap:6px}
.nameRow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}
.nameRow span{font-size:.85rem;color:var(--muted);font-variant-numeric:tabular-nums}
#copy{width:100%;height:120px;font:.8rem var(--mono);color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;margin-top:8px}
@media (max-width:560px){.choices{grid-template-columns:minmax(0,1fr)}kbd{display:none}}
@media (pointer:coarse){kbd{display:none}}
</style>
<div class="top"><div class="wrap">
<h1>AC-7 labels</h1><span id="count"></span><span id="status" role="status"></span>
<div class="bar"><i id="fill"></i></div>
</div></div>
<main class="wrap">
<section id="work">
<p class="where" id="where"></p>
<blockquote id="text"></blockquote>
<details id="citeBox"><summary id="citeSum"></summary><div id="cites"></div></details>
<p class="note" id="tevNote" hidden>Pre-registered: the k=4 vs k=9 episode and the 260714 target swap are claim-scale slips. An entry that reports either as a separate purpose is a sub-line.</p>
<div class="choices" id="choices"></div>
<div class="panel" id="nameBox" hidden>
<label for="name">Name this purpose</label>
<input type="text" id="name" autocomplete="off" placeholder="Use the same name for the same purpose">
<div class="chips" id="chips"></div>
<p class="hint" id="nameHint"></p>
</div>
<div class="panel" id="agBox" hidden>
<h2>Which diagnosed purposes does this entry correspond to?</h2>
<div class="ticks" id="ticks"></div>
<p class="hint">Tick any that apply, including on a sub-line. Leave them blank if none do.</p>
</div>
<div class="nav">
<button class="btn" id="back" type="button">Back <kbd>&larr;</kbd></button>
<button class="btn primary" id="next" type="button">Next <kbd>Enter</kbd></button>
<button class="btn right" id="toOverview" type="button">Overview and names</button>
</div>
</section>
<section id="overview" hidden>
<p id="doneMsg"></p>
<button class="btn primary" id="resume" type="button">Back to labelling</button>
<div id="ovBody"></div>
<h2>Backup copy</h2>
<p class="hint">Your labels save as you go, and Claude reads them from this page. A copy is only needed if saving reports a problem.</p>
<div class="nav"><button class="btn" id="saveCopy" type="button">Save labels.json</button></div>
<textarea id="copy" readonly hidden aria-label="Labels as JSON"></textarea>
</section>
</main>
<script>
const DATA=__DATA__;const AG=__PURPOSES__;
const KEY="ac7-labels-v2";
const entries=DATA.flatMap(r=>r.entries.map((x,i)=>({...x,arm:r.arm,reader:r.reader,rkey:r.key,k:r.arm+"."+r.reader+"."+x.id,pos:i+1,n:r.entries.length})));
const $=id=>document.getElementById(id);
const el=(t,p={},...kids)=>{const e=document.createElement(t);Object.assign(e,p);kids.forEach(c=>e.append(c));return e};
let state={};try{state=JSON.parse(localStorage.getItem(KEY))||{}}catch(e){}
let idx=0,acted=false,db=null,downloads=null,writes=Promise.resolve(),pending=0,failed=false,loaded=false,nameTimer=null;
const CHOICES=[["project","Project-level purpose","A purpose of the whole project"],["sub","Sub-line or unsupported","Part of something larger, or the record does not support it"],["generic","Too vague","Nobody could disagree with it"]];
const isDone=s=>!!(s&&s.choice&&(s.choice!=="project"||(s.identity||"").trim()));
const cur=()=>entries[idx];
const get=k=>state[k]||{};

function setStatus(){const s=$("status");s.className=failed?"bad":"";
if(failed)s.textContent="Last save failed. Check your connection, then change any label to retry.";
else if(!db)s.textContent=loaded?"Saving in this browser only":"";
else if(pending)s.textContent="Saving…";else s.textContent="Saved";}

function persist(k){try{localStorage.setItem(KEY,JSON.stringify(state))}catch(e){}
if(!db)return;const s=state[k];const x=entries.find(e=>e.k===k);
const body={arm:x.arm,reader:x.reader,entry:x.id,choice:s.choice||null,identity:(s.identity||"").trim()||null,maps:[...(s.maps||[])].sort()};
pending++;setStatus();
const ref=db.collection("labels").doc(k);
writes=writes.then(()=>ref.set(body).catch(err=>{if(err&&err.code==="unavailable")return new Promise(r=>setTimeout(r,800+Math.random()*800)).then(()=>ref.set(body));throw err}))
.then(()=>{failed=false},()=>{failed=true}).then(()=>{pending--;setStatus()});}

function chrome(){const n=entries.filter(x=>isDone(state[x.k])).length;
$("count").textContent=n+" of "+entries.length+" labelled";$("fill").style.width=(100*n/entries.length)+"%";setStatus();}

function armNames(arm){const seen=new Map();for(const x of entries){if(x.arm!==arm)continue;const s=get(x.k);const v=(s.identity||"").trim();if(s.choice==="project"&&v)seen.set(v,(seen.get(v)||0)+1);}return seen;}

function nameHint(){const x=cur(),s=get(x.k),v=(s.identity||"").trim(),h=$("nameHint");
const again=v&&entries.some(e=>e.rkey===x.rkey&&e.k!==x.k&&get(e.k).choice==="project"&&(get(e.k).identity||"").trim()===v);
h.className=again?"hint warn":"hint";
h.textContent=again?"This reader already has an entry with this name. A name counts once per reader.":"Pick a name you already used, or type a new one.";}

function chips(){const x=cur(),s=get(x.k),v=(s.identity||"").trim(),box=$("chips");box.replaceChildren();
for(const n of armNames(x.arm).keys()){const b=el("button",{type:"button",className:"chip",textContent:n});b.setAttribute("aria-pressed",String(n===v));
b.onclick=()=>{setName(n);$("name").value=n;chips();};box.append(b);}nameHint();}

const savedName={};
function setName(v){const x=cur();const s=state[x.k]||(state[x.k]={});s.identity=v;savedName[x.k]=v;acted=true;persist(x.k);chrome();}

function choose(v){const x=cur();const s=state[x.k]||(state[x.k]={});s.choice=v;if(v!=="project")s.identity="";acted=true;persist(x.k);render();chrome();
if(v==="project"){if(x.arm!=="colliculix")$("name").focus();return;}
if(v==="generic"||x.arm!=="colliculix")next();}

function toggle(c){const x=cur();const s=state[x.k]||(state[x.k]={});const on=(s.maps||[]).includes(c);s.maps=on?s.maps.filter(m=>m!==c):[...(s.maps||[]),c];
if(!on&&s.choice==="project"&&!(s.identity||"").trim())s.identity=AG[c];
acted=true;persist(x.k);render();chrome();}

function render(){const x=cur(),s=get(x.k);$("work").hidden=false;$("overview").hidden=true;
$("where").replaceChildren(el("b",{textContent:x.arm})," · "+x.reader.replace("reader","Reader ")+" · "+(x.kind==="purpose"?"Purpose":"Open line")+" · entry "+x.pos+" of "+x.n);
$("text").textContent=x.text;
$("citeSum").textContent="Citations ("+x.citations.length+")";
$("cites").replaceChildren(...x.citations.map(c=>el("div",{textContent:typeof c==="string"?c:JSON.stringify(c)})));
$("citeBox").hidden=!x.citations.length;$("tevNote").hidden=x.arm!=="tev_design";
const box=$("choices");box.replaceChildren();CHOICES.forEach(([v,t,d],i)=>{const b=el("button",{type:"button",className:"choice"},el("b",{},t+" ",el("kbd",{textContent:String(i+1)})),el("span",{textContent:d}));b.setAttribute("aria-pressed",String(s.choice===v));b.onclick=()=>choose(v);box.append(b);});
$("nameBox").hidden=s.choice!=="project";
if(document.activeElement!==$("name")){$("name").value=s.identity||"";savedName[x.k]=s.identity||"";}
if(s.choice==="project")chips();
const showAg=x.arm==="colliculix"&&s.choice!=="generic";$("agBox").hidden=!showAg;
if(showAg){const t=$("ticks");t.replaceChildren();for(const [c,label] of Object.entries(AG)){const b=el("button",{type:"button",className:"tick"},el("kbd",{textContent:c.toLowerCase()})," "+c+": "+label);b.setAttribute("aria-pressed",String((s.maps||[]).includes(c)));b.onclick=()=>toggle(c);t.append(b);}}
$("back").disabled=idx===0;}

function go(i){if(document.activeElement===$("name"))$("name").blur();idx=Math.max(0,Math.min(entries.length-1,i));render();window.scrollTo(0,0);}
function next(){const x=cur(),s=get(x.k);
if(s.choice==="project"&&!(s.identity||"").trim()){$("name").focus();$("nameHint").className="hint warn";$("nameHint").textContent="Add a name, or choose a different label.";return;}
if(idx<entries.length-1){let j=idx+1;go(j);}else overview();}

function exportLabels(){const out={};for(const r of DATA){const o=out[r.key]={};for(const x of r.entries){const s=get(r.arm+"."+r.reader+"."+x.id);if(!isDone(s))continue;
o[x.id]={identity:s.choice==="project"?s.identity.trim():null,generic:s.choice==="generic",maps_to:[...(s.maps||[])].sort()};}}return out;}

function overview(){$("work").hidden=true;$("overview").hidden=false;window.scrollTo(0,0);
const left=entries.filter(x=>!isDone(state[x.k])).length;
$("doneMsg").textContent=left?left+" entries still need a label. Select a square to go to one.":"All "+entries.length+" entries are labelled. Check the names below, then tell Claude you are done.";
const body=$("ovBody");body.replaceChildren();
for(const arm of [...new Set(DATA.map(r=>r.arm))]){body.append(el("h2",{textContent:arm}));
for(const r of DATA.filter(d=>d.arm===arm)){body.append(el("h3",{textContent:r.reader.replace("reader","Reader ")}));const m=el("div",{className:"map"});
entries.forEach((x,i)=>{if(x.rkey!==r.key)return;const d=el("button",{type:"button",className:"dot"+(isDone(state[x.k])?" done":""),textContent:String(x.pos),title:(isDone(state[x.k])?"Labelled: ":"Needs a label: ")+x.text.slice(0,80)});d.setAttribute("aria-label","Entry "+x.pos+", "+(isDone(state[x.k])?"labelled":"needs a label"));d.onclick=()=>go(i);m.append(d);});body.append(m);}
const names=armNames(arm);body.append(el("h3",{textContent:"Names used. Edit one to rename it everywhere in this project; give two the same name to merge them."}));const list=el("div",{className:"names"});
for(const [n,c] of names){const i=el("input",{type:"text",value:n,id:"rename-"+arm+"-"+list.children.length});i.setAttribute("aria-label","Rename "+n);
i.onchange=()=>{const v=i.value.trim();if(!v||v===n){i.value=n;return;}for(const x of entries){if(x.arm!==arm)continue;const s=state[x.k];if(s&&s.choice==="project"&&(s.identity||"").trim()===n){s.identity=v;persist(x.k);}}overview();};
list.append(el("div",{className:"nameRow"},i,el("span",{textContent:c+(c===1?" entry":" entries")})));}
if(!names.size)list.append(el("p",{className:"hint",textContent:"No names yet."}));body.append(list);}
chrome();}

document.addEventListener("keydown",e=>{if(e.metaKey||e.ctrlKey||e.altKey||!$("overview").hidden)return;
const tag=e.target.tagName;
if(tag==="INPUT"){if(e.key==="Enter"){e.preventDefault();clearTimeout(nameTimer);const v=e.target.value;if(v!==(get(cur().k).identity||""))setName(v);next();}else if(e.key==="Escape")e.target.blur();return;}
if(e.key==="1"||e.key==="2"||e.key==="3"){e.preventDefault();choose(CHOICES[+e.key-1][0]);return;}
if(e.key==="ArrowLeft"){go(idx-1);return;}
if(e.key==="ArrowRight"){next();return;}
if(e.key==="Enter"&&tag!=="BUTTON"&&tag!=="SUMMARY"){e.preventDefault();next();return;}
const c=e.key.toUpperCase();if(cur().arm==="colliculix"&&get(cur().k).choice!=="generic"&&AG[c])toggle(c);});

$("name").oninput=e=>{const v=e.target.value;const x=cur();const s=state[x.k]||(state[x.k]={});s.identity=v;nameHint();chrome();clearTimeout(nameTimer);nameTimer=setTimeout(()=>{setName(v);chips();},500);};
$("name").onblur=e=>{clearTimeout(nameTimer);const v=e.target.value;if(v!==(savedName[cur().k]||""))setName(v);};
$("back").onclick=()=>go(idx-1);$("next").onclick=next;$("toOverview").onclick=overview;$("resume").onclick=()=>go(idx);
$("saveCopy").onclick=async()=>{const data=JSON.stringify(exportLabels(),null,2);
if(downloads){try{await downloads.save({filename:"labels.json",data});return;}catch(err){if(err&&err.code==="declined")return;}}
const t=$("copy");t.hidden=false;t.value=data;t.select();};

function firstOpen(){const i=entries.findIndex(x=>!isDone(state[x.k]));return i<0?entries.length-1:i;}
idx=firstOpen();render();chrome();

(async()=>{const use=n=>window.claude&&window.claude.use?window.claude.use(n):Promise.resolve(null);
downloads=await use("downloads");db=await use("db");
if(!db){loaded=true;setStatus();return;}
const timer=setTimeout(()=>{$("status").textContent="Saved labels can't be reached yet. New labels will keep trying.";},10000);
db.collection("labels").onSnapshot(snap=>{let touched=false;
for(const d of snap.docs){if(d.metadata.hasPendingWrites)continue;const v=d.data();if(!v)continue;
if(cur().k===d.id&&document.activeElement===$("name"))continue;
state[d.id]={choice:v.choice||undefined,identity:v.identity||"",maps:Array.isArray(v.maps)?[...v.maps]:[]};touched=true;}
if(!snap.metadata.fromCache)clearTimeout(timer);
const first=!loaded;loaded=true;
if(first&&!acted)idx=firstOpen();
if(touched||first){if($("overview").hidden)render();else overview();}
chrome();},err=>{clearTimeout(timer);failed=true;setStatus();});})();
</script>
"""


def load_entries(work: Path) -> list[dict]:
    """Return one record per reading, with its purposes and open lines as labellable entries."""
    data = []
    for arm in ARMS:
        for reader in READERS:
            reading = json.loads((work / "readings" / arm / f"{reader}.json").read_text(encoding="utf-8"))
            entries = [
                {"id": p["id"], "kind": "purpose", "text": p["text"], "citations": p["citations"]}
                for p in reading["purposes"]
            ]
            entries += [
                {"id": f"open:{i}", "kind": "open line", "text": o["text"], "citations": o["citations"]}
                for i, o in enumerate(reading.get("open_lines", []))
            ]
            data.append({"key": f"{arm}/{reader}", "arm": arm, "reader": reader, "entries": entries})
    return data


def build(work: Path, out: Path) -> None:
    """Write the labelling page, escaping the data so a reading's text cannot close the inline script."""
    data = load_entries(work)

    def embed(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")

    out.write_text(PAGE.replace("__DATA__", embed(data)).replace("__PURPOSES__", embed(PURPOSES)), encoding="utf-8")
    logger.info("wrote %s: %d readings, %d entries", out, len(data), sum(len(r["entries"]) for r in data))


def export(work: Path, docs_dir: Path) -> None:
    """Write labels.json from documents saved by the artifact's database; incomplete entries stay out."""
    docs: dict[str, dict] = {}
    for path in docs_dir.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        docs[path.stem] = raw["data"] if isinstance(raw.get("data"), dict) else raw

    labels: dict[str, dict] = {}
    n_expected = n_written = 0
    for reading in load_entries(work):
        out = labels[reading["key"]] = {}
        for entry in reading["entries"]:
            n_expected += 1
            doc = docs.get(f"{reading['arm']}.{reading['reader']}.{entry['id']}")
            if not doc or not doc.get("choice"):
                continue
            identity = (doc.get("identity") or "").strip()
            if doc["choice"] == "project" and not identity:
                continue
            out[entry["id"]] = {
                "identity": identity if doc["choice"] == "project" else None,
                "generic": doc["choice"] == "generic",
                "maps_to": sorted(doc.get("maps") or []),
            }
            n_written += 1

    (work / "labels.json").write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote labels.json: %d of %d entries labelled", n_written, n_expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--export-from", type=Path, help="directory of label documents read from the artifact")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.export_from:
        export(args.work_dir, args.export_from)
    else:
        build(args.work_dir, args.work_dir / "ac7-labels.html")


if __name__ == "__main__":
    main()
