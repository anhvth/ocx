"""Read-only browser landing page for the local router.

A self-contained (no external assets) mission-control dashboard: layered aurora +
grain background, live-updating usage/credits, animated bars, copy + refresh + reset
actions. All dynamic strings are HTML-escaped; no token or PII is ever rendered.
"""

from __future__ import annotations

import html
import time
from typing import Any

USAGE_URL = "https://chatgpt.com/codex/settings/usage"


def _context_label(value: Any) -> str:
    try:
        context = int(value)
    except (TypeError, ValueError):
        return "—"
    if context <= 0:
        return "—"
    if context >= 1_000_000 and context % 1_000_000 == 0:
        return f"{context // 1_000_000}M"
    if context >= 1_000:
        return f"{context // 1_000}K" if context % 1_000 == 0 else f"{context / 1_000:.1f}K"
    return str(context)


def _reset_label(reset_at: Any, seconds: int) -> str:
    if not reset_at:
        return ""
    try:
        t = time.localtime(float(reset_at))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return time.strftime("%I:%M %p" if seconds <= 18000 else "%b %d", t).lstrip("0")


def _esc(x: Any) -> str:
    return html.escape(str(x), quote=True)


def _usage_head() -> str:
    return ('<div class="usage-head"><h2 id="usage-title">Remaining usage</h2>'
            f'<a class="btn" href="{USAGE_URL}" target="_blank" rel="noopener noreferrer">'
            '<span>Reset / manage usage</span>'
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M7 17L17 7M7 7h10v10"/></svg></a></div>')


def _usage_inner(usage: dict | None) -> str:
    if not usage:
        return '<p class="note">Codex usage unavailable (not signed in or endpoint unreachable).</p>'
    plan = _esc((usage.get("plan") or "unknown").capitalize())
    out = [f'<div class="usage-source">Usage source: Your {plan} plan</div>']
    for w in usage.get("windows", []):
        left = int(w.get("left", 0))
        reset = _reset_label(w.get("reset_at"), int(w.get("seconds", 0)))
        tone = "ok" if left > 40 else ("warn" if left > 15 else "low")
        out.append(
            '<div class="usage-row"><div class="usage-top"><span>' + _esc(w.get("label", "Limit"))
            + f'</span><span class="pct">{left}% left</span></div>'
            + f'<div class="bar {tone}"><span style="width:{left}%"></span></div>'
            + (f'<div class="usage-reset">Resets {_esc(reset)}</div>' if reset else "") + "</div>")
    if usage.get("credits"):
        out.append(f'<div class="usage-kv"><span>Credits</span><strong>{_esc(usage["credits"])}</strong></div>')
    if usage.get("resets"):
        out.append(f'<div class="usage-kv"><span>Full reset</span><strong>{int(usage["resets"])} remaining</strong></div>')
    return "".join(out)


def _usage_section(usage: dict | None) -> str:
    return ('<section class="card usage reveal" aria-labelledby="usage-title">' + _usage_head()
            + f'<div id="usage-body">{_usage_inner(usage)}</div>'
            + '<div class="usage-foot">Auto-refreshes every 30s · <span id="updated-ago">just now</span></div></section>')


def _models_section(groups: list[dict]) -> str:
    cards = []
    for g in groups:
        provider = _esc(g.get("provider", "Unknown"))
        models = g.get("models", [])
        rows = []
        for m in models:
            rows.append(
                "<tr>"
                f'<td><code>{_esc(m.get("slug", ""))}</code></td>'
                f'<td class="num">{_context_label(m.get("context", 0))}</td>'
                f'<td><span class="pill {"yes" if m.get("images") else "no"}">{"✓" if m.get("images") else "—"}</span></td>'
                f'<td><span class="pill {"yes" if m.get("reasoning") else "no"}">{"✓" if m.get("reasoning") else "—"}</span></td>'
                "</tr>")
        if not rows:
            rows.append('<tr><td class="empty" colspan="4">No models available.</td></tr>')
        cards.append(
            '<section class="card provider reveal"><header class="provider-head">'
            f'<h3>{provider}</h3><span class="badge">{len(models)}</span></header>'
            '<div class="table-scroll"><table><caption>Models served by ' + provider + '</caption>'
            '<thead><tr><th scope="col">Model</th><th scope="col">Context</th>'
            '<th scope="col">Images</th><th scope="col">Reasoning</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')
    return "".join(cards)


def _message_rows(messages: list[dict]) -> str:
    rows = []
    for m in messages:
        counts = "".join(f'<td class="num">{m[key]:,}</td>' if m.get(key) is not None
                         else '<td class="num" title="Not reported by upstream">—</td>'
                         for key in ("input_tokens", "cached_tokens", "output_tokens"))
        stamp = time.strftime("%H:%M:%S", time.localtime(m["at"]))
        response_id = m.get("response_id") or "—"
        rows.append(f'<tr><td>{stamp}</td><td><code>{_esc(m["model"])}</code></td>'
                    f'<td><code title="{_esc(response_id)}">#{m["sequence"]} · {_esc(response_id)}</code></td>'
                    f'{counts}<td>{_esc(m["http_status"])} · {_esc(m["status"])}</td></tr>')
    return "".join(rows) or '<tr><td class="empty" colspan="7">No responses recorded since router startup.</td></tr>'


_CSS = """
:root{
  color-scheme:dark;
  --bg0:#070B14; --bg1:#0B1220; --surface:#101A2C; --surface2:#16233A; --line:#22334E;
  --text:#EAF2FB; --muted:#8FA3BC; --green:#22C55E; --blue:#3B82F6; --amber:#F59E0B;
  --magenta:#D946EF; --cyan:#22D3EE; --red:#EF4444;
  --green-dim:rgba(34,197,94,.14); --blue-dim:rgba(59,130,246,.16);
  --mono:ui-monospace,'JetBrains Mono','SF Mono',Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
}
*{box-sizing:border-box}
html{background:var(--bg0);scrollbar-color:var(--line) var(--bg0);scrollbar-width:thin}
body{margin:0;min-width:0;color:var(--text);font-family:var(--sans);background:linear-gradient(160deg,var(--bg0),var(--bg1) 55%,var(--bg0));min-height:100vh;overflow-x:hidden}
body::before{content:"";position:fixed;inset:-20%;z-index:-2;pointer-events:none;
  background:radial-gradient(42% 34% at 18% 12%,rgba(34,197,94,.16),transparent 70%),
             radial-gradient(40% 32% at 82% 8%,rgba(59,130,246,.15),transparent 70%),
             radial-gradient(36% 30% at 60% 90%,rgba(217,70,239,.10),transparent 70%);
  animation:drift 26s ease-in-out infinite alternate}
body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.05;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E")}
@keyframes drift{from{transform:translate3d(-2%,-1%,0) rotate(-1deg)}to{transform:translate3d(2%,2%,0) rotate(1deg)}}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:var(--bg0)}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:99px;border:2px solid var(--bg0)}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}
code{font-family:var(--mono)}
button{font:inherit;color:inherit}
a{color:var(--cyan)}
:focus-visible{outline:3px solid var(--green);outline-offset:3px;border-radius:6px}
.site-header{position:sticky;top:0;z-index:20;border-bottom:1px solid rgba(34,51,78,.7);background:rgba(7,11,20,.72);backdrop-filter:blur(16px)}
.header-inner{width:min(1180px,calc(100% - 32px));margin:0 auto;min-height:68px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{display:flex;align-items:center;gap:11px;min-width:0;font-weight:800;letter-spacing:-.02em}
.logo{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--green-dim),var(--blue-dim));border:1px solid var(--line);color:var(--green)}
.brand-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.version{color:var(--muted);font:500 .78rem var(--mono);border:1px solid var(--line);border-radius:99px;padding:3px 9px;background:rgba(16,26,44,.6)}
.head-right{display:flex;align-items:center;gap:12px}
.status{display:inline-flex;align-items:center;gap:8px;color:#BBF7D0;font-size:.86rem;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px var(--green-dim)}
.dot.pulse{animation:pulse 1s ease}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.5)}100%{box-shadow:0 0 0 12px rgba(34,197,94,0)}}
.icon-btn{display:grid;place-items:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--line);background:var(--surface);cursor:pointer;transition:border-color .15s,background .15s}
.icon-btn:hover{border-color:var(--green);background:var(--green-dim)}
.icon-btn.spinning svg{animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
main{width:min(1180px,calc(100% - 32px));margin:0 auto;padding:52px 0 72px}
.hero{margin-bottom:34px;max-width:820px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;color:var(--green);font:600 .74rem var(--mono);letter-spacing:.18em;text-transform:uppercase;margin-bottom:14px}
.eyebrow::before{content:"";width:22px;height:1px;background:var(--green)}
h1{margin:0 0 14px;font-size:clamp(2.1rem,5vw,3.4rem);line-height:1.04;letter-spacing:-.045em;font-weight:850}
h1 .grad{background:linear-gradient(90deg,var(--green),var(--cyan) 55%,var(--blue));-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{margin:0;color:var(--muted);font-size:clamp(1rem,2vw,1.13rem);line-height:1.65}
.hero .update-status{min-height:1.65em;color:var(--amber)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0 8px}
.stat{border:1px solid var(--line);border-radius:14px;background:linear-gradient(180deg,var(--surface),rgba(16,26,44,.6));padding:14px 16px;position:relative;overflow:hidden}
.stat::after{content:"";position:absolute;inset:0 0 auto 0;height:2px;background:linear-gradient(90deg,var(--green),var(--blue),var(--magenta))}
.stat .k{color:var(--muted);font-size:.74rem;letter-spacing:.08em;text-transform:uppercase}
.stat .v{margin-top:6px;font-size:1.35rem;font-weight:800;letter-spacing:-.02em;font-family:var(--mono)}
.layout{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:16px;align-items:start;margin-top:26px}
.col{display:grid;gap:16px;min-width:0}
.col.side{position:sticky;top:84px}
.card{border:1px solid var(--line);border-radius:16px;background:linear-gradient(180deg,rgba(16,26,44,.92),rgba(11,18,32,.92));box-shadow:0 18px 44px rgba(0,0,0,.28)}
.reveal{animation:up .5s ease both}
@keyframes up{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.provider-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 20px;background:rgba(22,35,58,.6);border-bottom:1px solid var(--line)}
h3{margin:0;font-size:.98rem;letter-spacing:-.01em}
.badge{font:600 .74rem var(--mono);color:var(--cyan);background:var(--blue-dim);border:1px solid rgba(59,130,246,.35);border-radius:99px;padding:2px 9px}
.table-scroll{overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--line) var(--surface)}
table{width:100%;min-width:520px;border-collapse:collapse;text-align:left}
caption{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
th,td{padding:12px 20px;border-top:1px solid var(--line);font-size:.9rem}
th{color:var(--muted);background:rgba(7,11,20,.3);font-size:.7rem;letter-spacing:.08em;text-transform:uppercase}
td:first-child{width:56%}
td code{color:var(--text);overflow-wrap:anywhere;font-size:.84rem}
td.num{font-family:var(--mono);color:var(--cyan)}
tbody tr{transition:background .15s}
tbody tr:hover{background:rgba(34,197,94,.05)}
.empty{color:var(--muted);text-align:center;padding:22px}
.pill{display:inline-grid;place-items:center;min-width:30px;height:24px;border-radius:99px;border:1px solid var(--line);color:var(--muted);background:rgba(7,11,20,.3);font-size:.78rem}
.pill.yes{color:#BBF7D0;border-color:rgba(34,197,94,.4);background:var(--green-dim)}
.message-usage{margin-top:26px;overflow:hidden}
.message-usage table{min-width:940px}
.message-usage td:first-child{width:auto;white-space:nowrap;font-family:var(--mono)}
.message-usage td code{display:block;max-width:26ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.message-usage td.num{white-space:nowrap;font-variant-numeric:tabular-nums;text-align:right}
.message-usage .note{padding:0 20px 16px}
.usage,.panel{padding:20px}
.usage-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
h2{margin:0;font-size:1.12rem;letter-spacing:-.02em}
.btn{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border-radius:10px;border:1px solid rgba(34,197,94,.4);background:var(--green-dim);color:#BBF7D0;font-size:.83rem;font-weight:650;text-decoration:none;cursor:pointer;white-space:nowrap;transition:background .15s,border-color .15s,transform .06s}
.btn:hover{background:rgba(34,197,94,.28);border-color:var(--green)}
.btn:active{transform:translateY(1px)}
.usage-source{color:var(--muted);font-size:.88rem;margin-bottom:16px}
.usage-source strong{color:var(--text)}
.usage-row{margin-bottom:16px}
.usage-top{display:flex;justify-content:space-between;gap:12px;font-size:.9rem;margin-bottom:8px}
.pct{font-family:var(--mono);color:var(--text)}
.bar{height:8px;border-radius:99px;background:rgba(7,11,20,.7);border:1px solid var(--line);overflow:hidden}
.bar span{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--blue),var(--cyan));transition:width .8s cubic-bezier(.22,1,.36,1)}
.bar.warn span{background:linear-gradient(90deg,var(--amber),#FBBF24)}
.bar.low span{background:linear-gradient(90deg,var(--red),#F87171)}
.usage-reset{color:var(--muted);font-size:.8rem;margin-top:6px}
.usage-kv{display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--line);font-size:.9rem}
.usage-kv strong{font-family:var(--mono);color:var(--amber)}
.usage-foot{margin-top:12px;color:var(--muted);font-size:.76rem}
.panel h2{margin-bottom:14px}
.command{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.command:first-of-type{border-top:0;padding-top:0}
.command code{min-width:0;flex:1;color:#D8E5F6;font-size:.8rem;overflow-wrap:anywhere}
.copy{flex:0 0 auto;min-height:32px;padding:6px 11px;border:1px solid var(--line);border-radius:8px;background:var(--surface2);cursor:pointer;font-size:.78rem;font-weight:650;transition:border-color .15s,background .15s}
.copy:hover{border-color:var(--green);background:var(--green-dim)}
.endpoint{display:block;padding:11px;border:1px solid var(--line);border-radius:9px;color:#D8E5F6;background:rgba(7,11,20,.4);font-size:.82rem;overflow-wrap:anywhere}
.note{margin:12px 0 0;color:var(--muted);font-size:.87rem;line-height:1.55}
footer{width:min(1180px,calc(100% - 32px));margin:0 auto;padding:0 0 40px;color:var(--muted);font-size:.78rem;display:flex;gap:8px;flex-wrap:wrap}
@media (max-width:960px){.layout{grid-template-columns:1fr}.col.side{position:static}.stats{grid-template-columns:repeat(2,1fr)}}
@media (max-width:640px){.header-inner,main,footer{width:calc(100% - 24px)}.version{display:none}main{padding-top:36px}th,td{padding:11px 14px}.usage,.panel{padding:16px}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
"""

_JS = """
(function(){
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function fallback(text){var a=document.createElement('textarea');a.value=text;a.setAttribute('readonly','');a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();try{document.execCommand('copy');}finally{document.body.removeChild(a);}}
  document.querySelectorAll('.copy').forEach(function(b){b.addEventListener('click',function(){var t=b.getAttribute('data-copy')||'';var p=(navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(t):Promise.reject();p.catch(function(){fallback(t);}).finally(function(){var o=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=o;},1400);});});});
  if(!reduce){document.querySelectorAll('.bar > span').forEach(function(s){var w=s.style.width;s.style.width='0%';requestAnimationFrame(function(){requestAnimationFrame(function(){s.style.width=w;});});});}
  var ts=Date.now();
  function ago(){var e=document.getElementById('updated-ago');if(!e)return;var s=Math.max(0,Math.round((Date.now()-ts)/1000));e.textContent=s<5?'just now':s+'s ago';}
  setInterval(ago,1000);ago();
  function esc(x){return String(x).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function resetLabel(at,secs){if(!at)return'';var d=new Date(at*1000);if(secs<=18000){var h=d.getHours(),m=d.getMinutes(),ap=h>=12?'PM':'AM';h=h%12||12;return h+':' +(m<10?'0':'')+m+' '+ap;}var mo=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];return mo[d.getMonth()]+' '+d.getDate();}
  function usageInner(u){if(!u)return'<p class="note">Codex usage unavailable (not signed in or endpoint unreachable).</p>';var plan=(u.plan||'unknown');plan=plan.charAt(0).toUpperCase()+plan.slice(1);var out='<div class="usage-source">Usage source: Your '+esc(plan)+' plan</div>';(u.windows||[]).forEach(function(w){var left=w.left|0;var r=resetLabel(w.reset_at,w.seconds|0);var tone=left>40?'ok':(left>15?'warn':'low');out+='<div class="usage-row"><div class="usage-top"><span>'+esc(w.label||'Limit')+'</span><span class="pct">'+left+'% left</span></div><div class="bar '+tone+'"><span style="width:'+left+'%"></span></div>'+(r?'<div class="usage-reset">Resets '+esc(r)+'</div>':'')+'</div>';});if(u.credits)out+='<div class="usage-kv"><span>Credits</span><strong>'+esc(u.credits)+'</strong></div>';if(u.resets)out+='<div class="usage-kv"><span>Full reset</span><strong>'+(u.resets|0)+' remaining</strong></div>';return out;}
  function set(id,v){var e=document.getElementById(id);if(e&&v!=null)e.textContent=v;}
  function messageRows(ms){if(!ms.length)return'<tr><td class="empty" colspan="7">No responses recorded since router startup.</td></tr>';return ms.map(function(m){var id=m.response_id||'—';var out='<tr><td>'+esc(new Date(m.at*1000).toLocaleTimeString())+'</td><td><code>'+esc(m.model)+'</code></td><td><code title="'+esc(id)+'">#'+esc(m.sequence)+' · '+esc(id)+'</code></td>';['input_tokens','cached_tokens','output_tokens'].forEach(function(k){out+=m[k]==null?'<td class="num" title="Not reported by upstream">—</td>':'<td class="num">'+esc(m[k].toLocaleString())+'</td>';});return out+'<td>'+esc(m.http_status)+' · '+esc(m.status)+'</td></tr>';}).join('');}
  function apply(d){
    var notice=document.getElementById('update-status');
    var text=d.update_available?'OCX '+d.update_available+' is available. Run ocx update.':'';
    if(notice&&notice.textContent!==text)notice.textContent=text;
    ts=Date.now();ago();if(d.messages){var mb=document.getElementById('message-rows');if(mb)mb.innerHTML=messageRows(d.messages);}if(d.groups){var n=0;d.groups.forEach(function(g){n+=(g.models||[]).length;});set('stat-models',n);set('stat-providers',d.groups.length);}if(d.usage){set('stat-plan',(d.usage.plan||'—').toUpperCase());set('stat-credits',(d.usage.credits||'—').replace(' credits',''));var ub=document.getElementById('usage-body');if(ub)ub.innerHTML=usageInner(d.usage);}var dot=document.getElementById('status-dot');if(dot&&!reduce){dot.classList.remove('pulse');void dot.offsetWidth;dot.classList.add('pulse');}}
  function refresh(){var b=document.getElementById('refresh');if(b)b.classList.add('spinning');fetch('/api/overview',{headers:{'Accept':'application/json'}}).then(function(r){if(!r.ok)throw 0;return r.json();}).then(apply).catch(function(){}).finally(function(){if(b)setTimeout(function(){b.classList.remove('spinning');},400);});}
  var rb=document.getElementById('refresh');if(rb)rb.addEventListener('click',refresh);
  setInterval(refresh,30000);
})();
"""


def render_landing(version: str, base_url: str, groups: list[dict], usage: dict | None = None,
                   messages: list[dict] | None = None, *, update_available: str | None = None) -> str:
    update_text = f"OCX {update_available} is available. Run ocx update." if update_available else ""
    total = sum(len(g.get("models", [])) for g in groups)
    plan = (usage or {}).get("plan") or "—"
    credits = ((usage or {}).get("credits") or "—").replace(" credits", "")
    models_html = _models_section(groups)
    usage_html = _usage_section(usage)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ocx · model control plane</title>
<style>{_CSS}</style>
</head>
<body>
<header class="site-header"><div class="header-inner">
  <div class="brand"><span class="logo" aria-hidden="true"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="5" cy="12" r="2.4"/><circle cx="19" cy="5" r="2.4"/><circle cx="19" cy="19" r="2.4"/><path d="M7.2 10.8 16.8 6.2M7.2 13.2 16.8 17.8"/></svg></span>
    <span class="brand-name">ocx</span><span class="version">v{_esc(version)}</span></div>
  <div class="head-right">
    <span class="status"><span class="dot" id="status-dot" aria-hidden="true"></span>Running</span>
    <button class="icon-btn" id="refresh" type="button" aria-label="Refresh usage and model stats" title="Refresh">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>
    </button>
  </div>
</div></header>
<main>
  <section class="hero reveal">
    <span class="eyebrow">Local model router</span>
    <h1>One control plane for <span class="grad">every coding client.</span></h1>
    <p>A single, centralized model list for Codex, Claude and Pi — routed locally, with live usage and credits at a glance.</p>
    <p class="update-status" id="update-status" role="status">{_esc(update_text)}</p>
    <div class="stats" role="list">
      <div class="stat" role="listitem"><div class="k">Models</div><div class="v" id="stat-models">{total}</div></div>
      <div class="stat" role="listitem"><div class="k">Providers</div><div class="v" id="stat-providers">{len(groups)}</div></div>
      <div class="stat" role="listitem"><div class="k">Plan</div><div class="v" id="stat-plan">{_esc(plan.upper())}</div></div>
      <div class="stat" role="listitem"><div class="k">Credits</div><div class="v" id="stat-credits">{_esc(credits)}</div></div>
    </div>
  </section>
  <section class="card message-usage reveal" aria-labelledby="messages-title">
    <header class="provider-head"><h2 id="messages-title">Token usage per response</h2><span class="badge">Latest 100</span></header>
    <div class="table-scroll" tabindex="0" role="region" aria-label="Per-response token usage, scroll horizontally">
      <table><caption>Token counts reported by upstream for each model response, newest first</caption>
        <thead><tr><th scope="col">Time</th><th scope="col">Model</th><th scope="col">Response</th>
          <th scope="col">Input (total)</th><th scope="col">Cached input</th><th scope="col">Output</th><th scope="col">Status</th></tr></thead>
        <tbody id="message-rows">{_message_rows(messages or [])}</tbody>
      </table>
    </div>
    <p class="note">Cached input is included in total input. — means not reported, not zero. Each row is one API response; a chat turn may use several. Kept in memory since startup; no message content stored. Auto-refreshes every 30s.</p>
  </section>
  <div class="layout">
    <div class="col" aria-label="Models">{models_html}</div>
    <div class="col side">
      {usage_html}
      <section class="card panel reveal" aria-label="Connection"><h2>Connection</h2>
        <code class="endpoint">{_esc(base_url)}</code>
        <div class="command"><code>copy endpoint</code><button class="copy" type="button" aria-label="Copy API endpoint" data-copy="{_esc(base_url)}">Copy</button></div>
        <p class="note">API requests require the router token. This page is read-only and local-only.</p>
      </section>
      <section class="card panel reveal" aria-label="Quick commands"><h2>Quick commands</h2>
        <div class="command"><code>pi --list-models ocx</code><button class="copy" type="button" aria-label="Copy pi list models command" data-copy="pi --list-models ocx">Copy</button></div>
        <div class="command"><code>ocx status</code><button class="copy" type="button" aria-label="Copy status command" data-copy="ocx status">Copy</button></div>
        <div class="command"><code>ocx start</code><button class="copy" type="button" aria-label="Copy start command" data-copy="ocx start">Copy</button></div>
      </section>
    </div>
  </div>
</main>
<footer><span>ocx</span>·<span>read-only dashboard</span>·<span>loopback only</span>·<span>no credentials rendered</span></footer>
<script>{_JS}</script>
</body>
</html>"""
