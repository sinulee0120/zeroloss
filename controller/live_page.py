# ruff: noqa: E501  (the inline page script below is kept as one readable block)
"""Live monitor page of the results web (02 12 ``/live``; 2026-10-08 GAP-119).

The user: "모니터링이 지금은 너무 빈약해" — the old page was one line per device and a
row of 5-minute icons, reloaded every 5 s. This page asks ``/live.json`` every second
and shows, per receiver: the last second, the open 5-minute box and the last 10 minutes
as cards, 10-minute charts, the 5-minute boxes with their reasons and the last 60
seconds as a table — modelled on the operators' board of the earlier RealTest tool.
What a value means comes from the module's ``live_view`` columns; this page names no
field of its own. Standard library only; the page works from any browser on the LAN.
"""

from __future__ import annotations

import json

from core.ui_schema.design import COLORS, ICONS

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZeroLoss live</title>
<style>
:root{--bg:#fafafa;--fg:#1b1b1b;--muted:#5f6368;--card:#fff;--line:#ddd;--chart:#1565c0;--warnline:#b26a00;
 --green:__GREEN__;--yellow:__YELLOW__;--red:__RED__;--purple:__PURPLE__;--grey:__GREY__}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#121212;--fg:#e8e8e8;--muted:#a0a0a0;--card:#1e1e1e;--line:#333;--chart:#64b5f6;--warnline:#ffb74d}}
:root[data-theme="dark"]{--bg:#121212;--fg:#e8e8e8;--muted:#a0a0a0;--card:#1e1e1e;--line:#333;--chart:#64b5f6;--warnline:#ffb74d}
*{box-sizing:border-box}body{margin:0;padding:16px;font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:17px;margin:20px 0 8px}h3{font-size:14px;margin:12px 0 6px;color:var(--muted);font-weight:600}
.top{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
.badge{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:6px;color:#fff;font-weight:700;font-size:16px}
.meta{color:var(--muted);font-size:13px}.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:6px;max-width:420px}
.bar>i{display:block;height:100%;background:var(--chart)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:6px;padding:8px 10px;min-width:0}
.card .l{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card.bad{border-left-color:var(--red)}.card.side{border-left-color:var(--purple)}.card.judging{border-left-color:var(--muted)}.card.warn{border-left-color:var(--yellow)}.card.ok{border-left-color:var(--green)}
.dev{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:16px}
.why{font-size:13px;color:var(--muted);margin:4px 0 0}
.charts{display:grid;grid-template-columns:1fr;gap:8px;min-width:0}
.chart{border:1px solid var(--line);border-radius:6px;padding:6px 8px;min-width:0;position:relative}.chart svg{width:100%;display:block}
.chart .tip{display:none;position:absolute;top:28px;background:var(--card);border:1px solid var(--line);border-radius:4px;padding:2px 6px;font-size:12px;pointer-events:none;white-space:nowrap}
.split{display:grid;grid-template-columns:1fr;gap:16px;align-items:start}
@media (min-width:1100px){.split{grid-template-columns:minmax(380px,2fr) 3fr}}
.chart .l{font-size:12px;color:var(--muted);display:flex;justify-content:space-between;gap:6px}
.chart .x{font-size:11px;color:var(--muted);display:flex;justify-content:space-between}
.boxes{display:flex;flex-wrap:wrap;gap:2px}.box{width:20px;height:20px;border-radius:3px;color:#fff;font-size:12px;line-height:20px;text-align:center}
.wide{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
td.bad{color:var(--red);font-weight:700}td.side{color:var(--purple);font-weight:700}td.judging{font-weight:700;text-decoration:underline dotted var(--muted)}.boxb{display:inline-block;color:#fff;border-radius:4px;padding:1px 8px;white-space:nowrap}td.warn{color:var(--warnline);font-weight:700}td.txt{text-align:left;white-space:normal}
.stale{color:var(--red);font-weight:700}a{color:var(--chart)}
.filter{margin-top:8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}.filter input[type=text],.filter input:not([type]){min-width:0}
</style></head><body>
<div class="top"><div><h1>ZeroLoss — the test now</h1><div class="meta" id="meta">Connecting…</div><div class="bar"><i id="prog" style="width:0"></i></div></div>
<div class="badge" id="badge" style="background:var(--grey)">…</div></div>
<p id="summary" class="meta"></p>
<details id="howbox" class="meta"><summary>How it is measured</summary><ul id="method"></ul></details>
<div id="senders" class="meta"></div>
<div id="ov"></div>
<div class="filter meta" id="filter">5-minute boxes —
<label>from <input id="ffrom" size="16" placeholder="14:30 or 2026-10-09 14:30"></label>
<label>to <input id="fto" size="16" placeholder="now"></label>
<label><input type="checkbox" id="fbad"> problems only</label>
<button id="fclear">All</button></div>
<div id="devs"></div>
<p class="meta"><a href="/">Reports</a> · updates every second</p>
<script>
var ICON=__ICONS__,COLOR={green:"var(--green)",yellow:"var(--yellow)",red:"var(--red)",purple:"var(--purple)",grey:"var(--grey)"};
var NAME={green:"OK",yellow:"Warning",red:"Problem",purple:"Sender or receiver side",grey:"No data"};
var WORSE=["green","grey","yellow","purple","red"];
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e;}
function hms(s){s=Math.max(0,Math.floor(s||0));var h=Math.floor(s/3600),m=Math.floor(s%3600/60),z=s%60;return h+"h "+(m<10?"0":"")+m+"m "+(z<10?"0":"")+z+"s";}
function num(c,v){if(v===undefined||v===null)return null;if(c.text)return v;var x=Number(v)*(c.scale||1);return isFinite(x)?x:null;}
function fmt(c,v){if(v===null||v===undefined)return "—";if(c.text)return String(v);var d=c.digits||0;return v.toFixed(d)+(c.unit?" "+c.unit:"");}
function isBad(c,v){if(v===null||v===undefined)return false;if(c.text)return c.ok&&c.ok.indexOf(v)<0;return (!!c.bad&&v>0)||(!!c.bad_zero&&v===0);}
/* a warning column (GAP-132): counted, shown amber, does not fail the test */
function isWarn(c,v){return !!c.warn&&v!==null&&v!==undefined&&v>0;}
function cls(c,v){return isBad(c,v)?"bad":(isWarn(c,v)?"warn":"");}
/* 2026-10-31 user ("702 lost라고 빨갛게 lost라고 적으면 라우터가 놓친것도 아닌데 라우터 이슈처럼
   보임 다른 색깔로 바꿔"): red is the router's only. A loss whose seconds all sit in 5-minute
   boxes judged sender or receiver side (purple) shows in that color; until judged, red */
var CELL=300;
function boxAt(cells,e){for(var i=0;i<(cells||[]).length;i++){var b=cells[i],a=b.start_elapsed_s!==undefined&&b.start_elapsed_s!==null?b.start_elapsed_s:b.index*CELL;
 if(e>=a&&e<a+(b.duration_s||CELL))return b.color;}return null;}
/* the class of trouble in these seconds: "bad" (red) when a box of them is red or not judged
   yet, else "side" when one is purple, else "warn" (a yellow box: a warning, not the router) */
function judged(cells,es){var cs=es.map(function(e){return boxAt(cells,e);});
 if(cs.some(function(x){return x==="red"||x==="grey";}))return "bad";
 /* 2026-10-31: the box still running is not judged yet: said so, not red before its time */
 if(!cs.length||cs.some(function(x){return x===null||x===undefined;}))return "judging";
 return cs.indexOf("purple")>=0?"side":"warn";}
function boxCls(color){return color==="purple"?"side":color==="red"||color==="grey"?"bad":"warn";}
function lossCls(c,v,cells,es){var k=cls(c,v);return k!=="bad"?k:(c.receiver?"warn":judged(cells,es));}
var FILL={bad:"var(--red)",side:"var(--purple)",warn:"var(--warnline)",judging:"var(--muted)"};
function lossFill(cells,e){return FILL[judged(cells,[e])];}
/* one second: red / blue only for the network columns, the receiver's own player amber */
function secCls(cols,cells,r,e){if(cols.some(function(c){return !c.receiver&&isBad(c,num(c,r[c.key]));}))return judged(cells,[e]);
 return cols.some(function(c){var v=num(c,r[c.key]);return isBad(c,v)||isWarn(c,v);})?"warn":"";}
function hits(c,recs){return recs.filter(function(x){var y=num(c,x[c.key]);return y!==null&&isBad(c,y);}).map(function(x){return x.e;});}
function boxStart(b){return b.start_elapsed_s!==undefined&&b.start_elapsed_s!==null?b.start_elapsed_s:b.index*CELL;}
function boxVal(b,k){return ((b.values||[]).filter(function(x){return x.key===k;})[0]||{}).value;}
function agg(c,recs,how){var vals=[];recs.forEach(function(r){var v=num(c,r[c.key]);if(v!==null&&!c.text)vals.push(v);});if(!vals.length)return null;
 if(how==="sum")return vals.reduce(function(a,b){return a+b;},0);if(how==="max")return Math.max.apply(null,vals);if(how==="min")return Math.min.apply(null,vals);
 return vals.reduce(function(a,b){return a+b;},0)/vals.length;}
function card(label,c,v){var d=el("div","card"+(isBad(c,v)?" bad":(v!==null&&c.bad?" ok":"")));d.appendChild(el("div","l",label));d.appendChild(el("div","v",fmt(c,v)));d.title=label+": "+fmt(c,v);return d;}
/* GAP-131 (user: "그래프 3개가 너무 허접하다", "우측 빈칸도 많은데"): charts drawn to the pixel
   once the box is on the page — value axis with a grid, clock times under it, the S-23
   line, red ticks for seconds with trouble, and the value of the second under the mouse */
var PENDING=[];
function nice(v){if(v<=0)return 1;var p=Math.pow(10,Math.floor(Math.log10(v))),n=v/p;return (n<=1?1:n<=2?2:n<=5?5:10)*p;}
function hhmmss(e){return T0===null?"t+"+e+" s":new Date(T0+e*1000).toLocaleTimeString();}
function chart(c,recs,lines,bads,cells,tick){var box=el("div","chart"),head=el("div","l");var last=recs.length?num(c,recs[recs.length-1][c.key]):null;
 head.appendChild(el("span",null,c.label+(c.unit?" ("+c.unit+")":"")+" · last 10 min"));head.appendChild(el("b",null,fmt(c,last)));box.appendChild(head);
 var ns="http://www.w3.org/2000/svg",svg=document.createElementNS(ns,"svg");box.appendChild(svg);var tip=el("div","tip");box.appendChild(tip);
 PENDING.push(function(){var W=Math.max(240,svg.clientWidth||box.clientWidth-16),H=170,L=46,R=8,T=8,B=22,PW=W-L-R,PH=H-T-B;
  svg.setAttribute("viewBox","0 0 "+W+" "+H);svg.setAttribute("height",H);
  function add(tag,a){var n=document.createElementNS(ns,tag);for(var k in a)n.setAttribute(k,a[k]);svg.appendChild(n);return n;}
  if(!recs.length){add("text",{x:L,y:T+20,"font-size":12,fill:"var(--muted)"}).textContent="No values yet";return;}
  var e0=recs[0].e,e1=recs[recs.length-1].e,span=Math.max(1,e1-e0),hi=0;
  recs.forEach(function(r){var v=num(c,r[c.key]);if(v!==null&&v>hi)hi=v;});
  var floor=c.key==="jit"?5:(c.bad?4:0);hi=nice(Math.max(hi*1.15,floor));
  var keep=(lines||[]).filter(function(y){return y<=hi*1.6;});if(keep.length)hi=nice(Math.max(hi,Math.max.apply(null,keep)*1.1));
  var off=(lines||[]).filter(function(y){return y>hi;});if(off.length)head.firstChild.textContent+=" · warning line "+off.join(", ")+" "+(c.unit||"")+" is far above";
  function X(e){return L+(e-e0)/span*PW;}function Y(v){return T+PH-v/hi*PH;}
  for(var g=0;g<=4;g++){var gv=hi*g/4,gy=Y(gv);add("line",{x1:L,x2:L+PW,y1:gy,y2:gy,stroke:"var(--line)","stroke-width":1});
   add("text",{x:L-6,y:gy+4,"text-anchor":"end","font-size":11,fill:"var(--muted)"}).textContent=(Math.round(gv*100)/100).toString();}
  for(var k=0;k<=4;k++){var te=e0+span*k/4,tx=X(te);add("line",{x1:tx,x2:tx,y1:T,y2:T+PH,stroke:"var(--line)","stroke-width":1,"stroke-dasharray":"2 3"});
   add("text",{x:tx,y:H-6,"text-anchor":k===0?"start":k===4?"end":"middle","font-size":11,fill:"var(--muted)"}).textContent=hhmmss(Math.round(te));}
  (keep.length?keep:[]).forEach(function(y){if(y<=hi){var ly=Y(y);add("line",{x1:L,x2:L+PW,y1:ly,y2:ly,stroke:"var(--warnline)","stroke-width":1.5,"stroke-dasharray":"6 4"});
   add("text",{x:L+PW-4,y:ly-4,"text-anchor":"end","font-size":11,fill:"var(--warnline)"}).textContent="warning "+y+" "+(c.unit||"");}});
  bads.forEach(function(e){add("rect",{x:X(e)-1,y:T+PH-4,width:2,height:4,fill:FILL[tick[e]]||FILL.bad});});
  if(c.bad){recs.forEach(function(r){var v=num(c,r[c.key]);if(v>0)add("rect",{x:X(r.e)-1.5,y:Y(v),width:3,height:T+PH-Y(v),fill:c.receiver?FILL.warn:lossFill(cells,r.e)});});}
  else{var pts=[];recs.forEach(function(r){var v=num(c,r[c.key]);if(v!==null)pts.push(X(r.e).toFixed(1)+","+Y(v).toFixed(1));});
   add("polyline",{points:pts.join(" "),fill:"none",stroke:"var(--chart)","stroke-width":1.6});}
  add("line",{x1:L,x2:L,y1:T,y2:T+PH,stroke:"var(--muted)","stroke-width":1});add("line",{x1:L,x2:L+PW,y1:T+PH,y2:T+PH,stroke:"var(--muted)","stroke-width":1});
  var cur=add("line",{x1:0,x2:0,y1:T,y2:T+PH,stroke:"var(--fg)","stroke-width":1,opacity:0});
  var hit=add("rect",{x:L,y:T,width:PW,height:PH,fill:"transparent"});
  hit.addEventListener("mousemove",function(ev){var b=svg.getBoundingClientRect(),xx=(ev.clientX-b.left)*W/b.width,te=e0+(xx-L)/PW*span,best=recs[0];
   recs.forEach(function(r){if(Math.abs(r.e-te)<Math.abs(best.e-te))best=r;});var bx=X(best.e);cur.setAttribute("x1",bx);cur.setAttribute("x2",bx);cur.setAttribute("opacity",0.5);
   tip.textContent=hhmmss(best.e)+" (t+"+best.e+" s) · "+fmt(c,num(c,best[c.key]));tip.style.display="block";
   tip.style.left=Math.min(b.width-160,Math.max(0,(ev.clientX-b.left)+12))+"px";});
  hit.addEventListener("mouseleave",function(){cur.setAttribute("opacity",0);tip.style.display="none";});});
 return box;}
/* 2026-10-24 user ("태블릿 두대는 여전히 앱화면에 RSSI가 안나오네", "11ax로 붙었는지 bandwidth는
   80Mhz인지, 5Ghz등 정보가 하나도 없네"): a tablet has no card in the plan; its own newest
   second says the Wi-Fi, in the parts and order the module gives (view.wifi) */
function wifiLine(last,parts){var bits=[];parts.forEach(function(p){var v=last[p.key];
 if(v===undefined||v===null||v==="")return;bits.push([p.label,v,p.unit].filter(function(x){return x!==undefined&&x!==null&&x!=="";}).join(" "));});
 return bits.length?"Receives on: Wi-Fi (its own) · "+bits.join(" · "):"";}
function present(cols,recs){return cols.filter(function(c){return recs.some(function(r){return r[c.key]!==undefined&&r[c.key]!==null;});});}
function trouble(cols,r){return cols.some(function(c){var v=num(c,r[c.key]);return isBad(c,v)||isWarn(c,v);});}
/* GAP-129: the boxes by the date and time on the clock ("14:30", "10-09 14:30",
   "2026-10-09 14:30") and only those with trouble; kept across the 1-second redraws */
var F={from:null,to:null,bad:false},T0=null;
function clock(t){if(!t||T0===null)return null;var m=/^\s*(?:(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+)?(\d{1,2}):(\d{2})\s*$/.exec(t);
 if(!m)return null;var s=new Date(T0),d=new Date(m[1]?+m[1]:s.getFullYear(),m[2]?+m[2]-1:s.getMonth(),m[3]?+m[3]:s.getDate(),+m[4],+m[5]);
 if(!m[2])while(d.getTime()<T0-60000)d.setDate(d.getDate()+1);return Math.max(0,Math.ceil((d.getTime()-T0)/1000));}
function wall(s){return T0===null?"":new Date(T0+s*1000).toLocaleString();}
function fset(){F.from=clock(document.getElementById("ffrom").value);F.to=clock(document.getElementById("fto").value);
 F.bad=document.getElementById("fbad").checked;if(window.LAST)render(window.LAST);}
function lossTotal(row){var t=null;(row.items||[]).forEach(function(i){if(i.label==="Loss")t=i.value;});return t;}
/* 2026-10-23 user ("태블릿 3대면 live view에는 어떻게 다 보여줄거야", "그래프는?"): with two or
   more receivers, all of them first, on one clock: a line each with its values now, the
   charts with one line per receiver, a strip of each one's trouble seconds, and each
   trouble said as "all at once" (router or sender side) or "only this one" (its side) */
var SER=["#2a78d6","#eb6834","#1baf7a","#eda100","#e87ba4","#6250d6"],DASH=["","6 4","2 3","8 3 2 3","1 3","10 2"],HIDE={};
function shifted(r){var s=r.e_shift||0;return (r.records||[]).map(function(x){var y={};for(var k in x)y[k]=x[k];y.e=x.e+s;return y;});}
function swatch(i){var ns="http://www.w3.org/2000/svg",s=document.createElementNS(ns,"svg");s.setAttribute("width",22);s.setAttribute("height",8);
 var l=document.createElementNS(ns,"line");l.setAttribute("x1",1);l.setAttribute("x2",21);l.setAttribute("y1",4);l.setAttribute("y2",4);
 l.setAttribute("stroke",SER[i%SER.length]);l.setAttribute("stroke-width",2);if(DASH[i%DASH.length])l.setAttribute("stroke-dasharray",DASH[i%DASH.length]);s.appendChild(l);return s;}
function multiChart(c,series,lines){var box=el("div","chart"),head=el("div","l");
 head.appendChild(el("span",null,c.label+(c.unit?" ("+c.unit+")":"")+" · last 10 min, one line per receiver"));box.appendChild(head);
 var ns="http://www.w3.org/2000/svg",svg=document.createElementNS(ns,"svg");box.appendChild(svg);var tip=el("div","tip");box.appendChild(tip);
 PENDING.push(function(){var W=Math.max(240,svg.clientWidth||box.clientWidth-16),H=170,L=46,R=8,T=8,B=22,PW=W-L-R,PH=H-T-B;
  svg.setAttribute("viewBox","0 0 "+W+" "+H);svg.setAttribute("height",H);
  function add(tag,a){var n=document.createElementNS(ns,tag);for(var k in a)n.setAttribute(k,a[k]);svg.appendChild(n);return n;}
  var all=[];series.forEach(function(s){all=all.concat(s.recs);});
  if(!all.length){add("text",{x:L,y:T+20,"font-size":12,fill:"var(--muted)"}).textContent="No values yet";return;}
  var es=all.map(function(r){return r.e;}),e0=Math.min.apply(null,es),e1=Math.max.apply(null,es),span=Math.max(1,e1-e0),hi=0;
  all.forEach(function(r){var v=num(c,r[c.key]);if(v!==null&&v>hi)hi=v;});
  hi=nice(Math.max(hi*1.15,c.key==="jit"?5:(c.bad?4:0)));
  var keep=(lines||[]).filter(function(y){return y<=hi*1.6;});if(keep.length)hi=nice(Math.max(hi,Math.max.apply(null,keep)*1.1));
  function X(e){return L+(e-e0)/span*PW;}function Y(v){return T+PH-v/hi*PH;}
  for(var g=0;g<=4;g++){var gv=hi*g/4,gy=Y(gv);add("line",{x1:L,x2:L+PW,y1:gy,y2:gy,stroke:"var(--line)","stroke-width":1});
   add("text",{x:L-6,y:gy+4,"text-anchor":"end","font-size":11,fill:"var(--muted)"}).textContent=(Math.round(gv*100)/100).toString();}
  for(var k=0;k<=4;k++){var te=e0+span*k/4,tx=X(te);
   add("text",{x:tx,y:H-6,"text-anchor":k===0?"start":k===4?"end":"middle","font-size":11,fill:"var(--muted)"}).textContent=hhmmss(Math.round(te));}
  keep.forEach(function(y){if(y<=hi){var ly=Y(y);add("line",{x1:L,x2:L+PW,y1:ly,y2:ly,stroke:"var(--warnline)","stroke-width":1.5,"stroke-dasharray":"6 4"});}});
  series.forEach(function(s,i){if(c.bad){s.recs.forEach(function(r){var v=num(c,r[c.key]);
    if(v>0)add("rect",{x:X(r.e)-2+i*1.5,y:Y(v),width:2,height:T+PH-Y(v),fill:s.color});});return;}
   var pts=[];s.recs.forEach(function(r){var v=num(c,r[c.key]);if(v!==null)pts.push(X(r.e).toFixed(1)+","+Y(v).toFixed(1));});
   var a={points:pts.join(" "),fill:"none",stroke:s.color,"stroke-width":1.6};if(s.dash)a["stroke-dasharray"]=s.dash;add("polyline",a);});
  add("line",{x1:L,x2:L,y1:T,y2:T+PH,stroke:"var(--muted)","stroke-width":1});add("line",{x1:L,x2:L+PW,y1:T+PH,y2:T+PH,stroke:"var(--muted)","stroke-width":1});
  var cur=add("line",{x1:0,x2:0,y1:T,y2:T+PH,stroke:"var(--fg)","stroke-width":1,opacity:0}),hit=add("rect",{x:L,y:T,width:PW,height:PH,fill:"transparent"});
  hit.addEventListener("mousemove",function(ev){var b=svg.getBoundingClientRect(),xx=(ev.clientX-b.left)*W/b.width,te=Math.round(e0+(xx-L)/PW*span);
   cur.setAttribute("x1",X(te));cur.setAttribute("x2",X(te));cur.setAttribute("opacity",0.5);
   tip.textContent=hhmmss(te)+" · "+series.map(function(s){var r=s.recs.filter(function(x){return x.e===te;})[0];return s.name+" "+(r?fmt(c,num(c,r[c.key])):"—");}).join(" · ");
   tip.style.display="block";tip.style.left=Math.min(Math.max(0,b.width-320),Math.max(0,(ev.clientX-b.left)+12))+"px";});
  hit.addEventListener("mouseleave",function(){cur.setAttribute("opacity",0);tip.style.display="none";});});
 return box;}
function strips(series,cols,cellsOf){var box=el("div","chart");box.appendChild(el("div","l","Each second, last 10 min: red = router problem, blue = sender or receiver side, grey = being judged, amber = warning, blank = fine"));
 var ns="http://www.w3.org/2000/svg",svg=document.createElementNS(ns,"svg");box.appendChild(svg);
 PENDING.push(function(){var W=Math.max(240,svg.clientWidth||box.clientWidth-16),RH=16,L=110,R=8,T=4,H=T+series.length*(RH+4)+20,PW=W-L-R;
  svg.setAttribute("viewBox","0 0 "+W+" "+H);svg.setAttribute("height",H);
  function add(tag,a){var n=document.createElementNS(ns,tag);for(var k in a)n.setAttribute(k,a[k]);svg.appendChild(n);return n;}
  var es=[];series.forEach(function(s){s.recs.forEach(function(r){es.push(r.e);});});if(!es.length)return;
  var e0=Math.min.apply(null,es),e1=Math.max.apply(null,es),span=Math.max(1,e1-e0+1);function X(e){return L+(e-e0)/span*PW;}
  series.forEach(function(s,i){var y=T+i*(RH+4);add("text",{x:4,y:y+RH-4,"font-size":12,fill:"var(--fg)"}).textContent=s.name;
   add("rect",{x:L,y:y,width:PW,height:RH,fill:"var(--line)",opacity:0.35});
   s.recs.forEach(function(r){var k=secCls(cols,cellsOf[s.r.device],r,r.e-(s.r.e_shift||0));
    if(k)add("rect",{x:X(r.e),y:y,width:Math.max(1,PW/span),height:RH,fill:FILL[k]});});});
  for(var k=0;k<=4;k++){var te=e0+(span-1)*k/4;add("text",{x:X(te),y:H-4,"text-anchor":k===0?"start":k===4?"end":"middle","font-size":11,fill:"var(--muted)"}).textContent=hhmmss(Math.round(te));}});
 return box;}
function together(d){var eps=[];d.receivers.forEach(function(r){var s=r.e_shift||0;(r.episodes||[]).forEach(function(x){
  eps.push({a:x.start_e+s,b:x.end_e+s,dev:r.device,ch:r.ch,what:x.what||[]});});});
 eps.sort(function(p,q){return p.a-q.a;});var groups=[];
 eps.forEach(function(x){var g=groups[groups.length-1];if(g&&x.a<=g.b+2){g.b=Math.max(g.b,x.b);g.who[x.dev]=x.ch;x.what.forEach(function(w){if(g.what.indexOf(w)<0)g.what.push(w);});}
  else{var w={};w[x.dev]=x.ch;groups.push({a:x.a,b:x.b,who:w,what:x.what.slice()});}});
 var n=d.receivers.length;return groups.map(function(g){var who=Object.keys(g.who),chs=[];who.forEach(function(k){if(chs.indexOf(g.who[k])<0)chs.push(g.who[k]);});
  var onCh=d.receivers.filter(function(r){return chs.indexOf(r.ch)>=0;}).length,side;
  if(who.length===n)side="All "+n+" at once: the router or the sender side";
  else if(who.length>=2&&who.length===onCh)side="All on CH"+chs.join("/")+" at once: the router or the sender side";
  else if(onCh>=2)side="Only "+who.join(", ")+": that receiver's side (its Wi-Fi, place or player)";
  else side="";return {a:g.a,b:g.b,who:who,chs:chs,what:g.what,side:side};});}
function overview(d,allCols,lines){var box=document.getElementById("ov");box.textContent="";
 if((d.receivers||[]).length<2)return;var sec=el("section","dev");sec.appendChild(el("h2",null,"All receivers ("+d.receivers.length+")"));
 var series=d.receivers.map(function(r,i){return {key:r.key,name:r.device+" · CH"+r.ch,recs:shifted(r),color:SER[i%SER.length],dash:DASH[i%DASH.length],r:r};});
 var allRecs=[];series.forEach(function(s){allRecs=allRecs.concat(s.recs);});var cols=present(allCols,allRecs);
 var nowCols=cols.filter(function(c){return c.now;}).slice(0,8);
 var w=el("div","wide"),t=el("table"),hr=el("tr");["","Show","Receiver"].concat(nowCols.map(function(c){return c.label+(c.unit?" ("+c.unit+")":"");})).forEach(function(x){hr.appendChild(el("th",null,x));});t.appendChild(hr);
 series.forEach(function(s,i){var tr=el("tr"),row=s.r.row||{},ic=el("td"),b=el("span","box",ICON[row.color||"grey"]||"");b.style.background=COLOR[row.color||"grey"];ic.appendChild(b);tr.appendChild(ic);
  var sh=el("td"),cb=el("input");cb.type="checkbox";cb.checked=!HIDE[s.key];cb.addEventListener("change",function(){HIDE[s.key]=!cb.checked;render(window.LAST);});sh.appendChild(cb);sh.appendChild(swatch(i));tr.appendChild(sh);
  var nm=el("td","txt",s.name);if(row.why)nm.title=row.why;tr.appendChild(nm);
  var last=s.recs.length?s.recs[s.recs.length-1]:{};
  var own=(d.cells||{})[s.r.device],at=last.e!==undefined?[last.e-(s.r.e_shift||0)]:[];
  nowCols.forEach(function(c){var v=num(c,last[c.key]);tr.appendChild(el("td",lossCls(c,v,own,at)+(c.text?" txt":""),v===null?"":fmt(c,v).replace(" "+c.unit,"")));});t.appendChild(tr);});
 w.appendChild(t);sec.appendChild(w);
 var shown=series.filter(function(s){return !HIDE[s.key];});
 var ch=el("div","charts");cols.forEach(function(c){if(c.chart)ch.appendChild(multiChart(c,shown,lines[c.key]));});ch.appendChild(strips(shown,cols,d.cells||{}));sec.appendChild(ch);
 var tg=together(d).filter(function(g){return (F.from===null||g.b>=F.from)&&(F.to===null||g.a<F.to);});
 sec.appendChild(el("h3",null,"Trouble, all receivers together ("+tg.length+", newest first)"));
 if(!tg.length)sec.appendChild(el("p","why","None so far."));
 else{var ew=el("div","wide"),et=el("table"),eh=el("tr");["Began","Length","Who","What","Likely side"].forEach(function(x){eh.appendChild(el("th",null,x));});et.appendChild(eh);
  tg.slice().reverse().slice(0,100).forEach(function(g){var tr=el("tr");tr.appendChild(el("td",null,wall(g.a)));tr.appendChild(el("td",null,(g.b-g.a+1)+" s"));
   tr.appendChild(el("td","txt",g.who.join(", ")));tr.appendChild(el("td","txt",g.what.join(", ")));tr.appendChild(el("td","txt",g.side));et.appendChild(tr);});
  ew.appendChild(et);sec.appendChild(ew);}
 sec.appendChild(el("p","why","The side is a hint from timing only; colors and the verdict do not change by it."));
 box.appendChild(sec);box.appendChild(el("h2",null,"Each receiver"));}
function render(d){window.LAST=d;PENDING=[];if(d.elapsed_s!==undefined&&d.state==="RUNNING")T0=Date.now()-d.elapsed_s*1000;
 var allCols=(d.view&&d.view.columns)||[],cell=(d.view&&d.view.cell_s)||300,lines=(d.view&&d.view.lines)||{};CELL=cell;
 var meta=document.getElementById("meta"),prog=document.getElementById("prog"),badge=document.getElementById("badge");
 meta.textContent=(d.run_id||"no run")+" · "+d.state+(d.quality?" · "+d.quality:"")+(d.planned_s?" · "+hms(d.elapsed_s)+" of "+hms(d.planned_s):"");
 prog.style.width=d.planned_s?Math.min(100,100*d.elapsed_s/d.planned_s).toFixed(1)+"%":"0";
 var worst="green";(d.receivers||[]).forEach(function(r){var c=(r.row||{}).color||"grey";if(WORSE.indexOf(c)>WORSE.indexOf(worst))worst=c;});
 Object.keys(d.cells||{}).forEach(function(k){(d.cells[k]||[]).forEach(function(c){if(WORSE.indexOf(c.color)>WORSE.indexOf(worst))worst=c.color;});});
 if(d.result&&d.result.verdict){badge.textContent=d.result.verdict;badge.style.background=COLOR[d.result.verdict==="PASS"?"green":d.result.verdict==="FAIL"?"red":"purple"];}
 else if(d.state==="RUNNING"){badge.textContent=ICON[worst]+" "+(worst==="green"?"No trouble so far":"Trouble seen");badge.style.background=COLOR[worst];}
 else{badge.textContent=d.state;badge.style.background=COLOR.grey;}
 var sm=document.getElementById("summary");sm.textContent=(d.view&&d.view.summary)||"";
 document.getElementById("howbox").style.display=((d.view&&d.view.method)||[]).length?"":"none";
 var mt=document.getElementById("method");mt.textContent="";((d.view&&d.view.method)||[]).forEach(function(x){mt.appendChild(el("li",null,x));});
 var s=document.getElementById("senders");s.textContent="";Object.keys(d.senders||{}).forEach(function(n){var row=d.senders[n]||{};
  var t=(row.items||[]).map(function(i){var v=i.value!==undefined&&i.value!==null;return (i.text||i.label||"")+(v?" "+i.value+(i.unit?" "+i.unit:""):"");}).join(" · ");if(t)s.appendChild(el("div",null,n+": "+t));});
 overview(d,allCols,lines);
 var root=document.getElementById("devs");root.textContent="";
 document.getElementById("filter").style.display=(d.receivers||[]).length?"":"none";
 if(!(d.receivers||[]).length){var any=false;
  /* 2026-10-17 user ("live view에는 아무것도 없네"): a test without receivers (throughput) has
     its judged cells only -- one table per PC, one line per cell, as the module named them */
  Object.keys(d.cells||{}).sort().forEach(function(dv){var cs=d.cells[dv]||[];if(!cs.length)return;any=true;
   var sec=el("section","dev");sec.appendChild(el("h2",null,dv));var w=el("div","wide"),tb=el("table"),hr=el("tr");
   var main=function(c){return (c.values||[]).filter(function(v){return !v.detail;});};
   hr.appendChild(el("th",null,""));main(cs[0]).forEach(function(v){hr.appendChild(el("th",null,v.label+(v.unit?" ("+v.unit+")":"")));});hr.appendChild(el("th",null,"Why"));tb.appendChild(hr);
   cs.forEach(function(c){var tr=el("tr"),ic=el("td");
    tr.title=(c.values||[]).filter(function(v){return v.detail&&v.value!==null&&v.value!==undefined;})
     .map(function(v){return v.label+": "+v.value+(v.unit?" "+v.unit:"");}).join("\n");var bx=el("span","boxb",ICON[c.color]||"");bx.style.background=COLOR[c.color]||COLOR.grey;ic.appendChild(bx);tr.appendChild(ic);
    main(c).forEach(function(v){var x=v.value===null||v.value===undefined?"":String(v.value);tr.appendChild(el("td",(v.bad?boxCls(c.color):"")+(typeof v.value==="number"?"":" txt"),x));});
    tr.appendChild(el("td","txt",(c.reasons||[]).join("; ")));tb.appendChild(tr);});
   w.appendChild(tb);sec.appendChild(w);root.appendChild(sec);});
  if(!any)root.appendChild(el("p","meta",Object.keys(d.senders||{}).length&&d.state==="RUNNING"
   ?"Measuring now (the lines above). Each case shows here as one line when it ends."
   :"No receiver data yet. The values appear once the receivers measure."));return;}
 d.receivers.forEach(function(r){var recs=r.records||[],last=recs.length?recs[recs.length-1]:{},row=r.row||{};
  var cols=present(allCols,recs),bads=recs.filter(function(x){return trouble(cols,x);}).map(function(x){return x.e;});
  var dev=el("section","dev"),h=el("h2");var ic=el("span","box");ic.textContent=ICON[row.color||"grey"]||"";ic.style.background=COLOR[row.color||"grey"];
  h.appendChild(ic);h.appendChild(document.createTextNode(" "+r.device+" · CH"+r.ch));dev.appendChild(h);
  var cd=r.card;dev.appendChild(el("p","meta",cd?("Receives on: "+(cd.wireless?"Wi-Fi ":"Wired ")+(cd.nic||"")+" "+cd.ip
   +(cd.link_mbps?" · "+(cd.link_mbps>=1000?(cd.link_mbps/1000)+" Gbps":cd.link_mbps+" Mbps"):"")
   +(cd.wifi&&cd.wifi.ssid?" · "+cd.wifi.ssid:"")+(cd.wifi&&cd.wifi.rssi_dbm!==undefined&&cd.wifi.rssi_dbm!==null?" · "+cd.wifi.rssi_dbm+" dBm":"")
   /* GAP-149: how the frames arrive (the reference's M2U finding); absent = not measured */
   +(r.arrives?" · arrives as "+(r.arrives.form==="unicast"?"unicast to this PC (the router converts multicast: M2U)"
     :r.arrives.form==="multicast"?"real multicast":"mixed multicast and unicast"):""))
   :wifiLine(last,(d.view&&d.view.wifi)||[])||"Receives on: not known yet"));  // wired or Wi-Fi, always shown (GAP-127)
  var cells=(d.cells||{})[r.device]||[],badBoxes=cells.filter(function(c){return c.color!=="green";});
  var said=badBoxes.map(function(c){return (c.reasons||[]).join("; ");});
  if(row.why&&said.indexOf(row.why)<0)dev.appendChild(el("p","why",row.why));  // each reason once
  if(r.age_s!==null&&r.age_s>3)dev.appendChild(el("p","stale","No answer for "+Math.round(r.age_s)+" s — values are old"));
  var idx=last.e!==undefined?Math.floor(last.e/cell):0,open=recs.filter(function(x){return Math.floor(x.e/cell)===idx;});
  var tot=lossTotal(row);
  dev.appendChild(el("h3",null,"Values (t+"+(last.e!==undefined?last.e:"?")+" s · this 5-minute box "+hms(idx*cell)+" to "+hms((idx+1)*cell)+", "+open.length+" s so far)"));
  var vw=el("div","wide"),vt=el("table"),vh=el("tr");["","Now","This 5-min box","Last 10 min","Whole test"].forEach(function(x){vh.appendChild(el("th",null,x));});vt.appendChild(vh);
  cols.forEach(function(c){var tr=el("tr");tr.appendChild(el("td",null,c.label+(c.unit?" ("+c.unit+")":"")));
   function cellOf(v,es){var td=el("td",lossCls(c,v,cells,es)+(c.text?" txt":""),v===null||v===undefined?"":fmt(c,v).replace(" "+c.unit,""));return td;}
   var how=c.cell||(c.total?"sum":null);
   tr.appendChild(cellOf(c.now?num(c,last[c.key]):null,last.e!==undefined?[last.e]:[]));
   tr.appendChild(cellOf(how&&!c.text?agg(c,open,how):null,hits(c,open)));
   tr.appendChild(cellOf(how&&!c.text?agg(c,recs,c.total?"sum":how):null,hits(c,recs)));
   tr.appendChild(cellOf(c.key===((cols.filter(function(x){return x.label==="Lost";})[0]||{}).key)&&tot!==null?Number(tot):null,
    cells.filter(function(b){return boxVal(b,"lost")>0;}).map(boxStart)));
   vt.appendChild(tr);});
  vw.appendChild(vt);var split=el("div","split");split.appendChild(vw);dev.appendChild(split);
  var tick={};recs.forEach(function(x){var k=secCls(cols,cells,x,x.e);if(k)tick[x.e]=k;});
  var ch=el("div","charts");cols.forEach(function(c){if(c.chart)ch.appendChild(chart(c,recs,lines[c.key],bads,cells,tick));});split.appendChild(ch);
  dev.appendChild(el("p","why",bads.length?"Marks under the charts: "+bads.length+" second(s) with trouble in the last 10 minutes (red = router side, blue = sender or receiver side, as its 5-minute box was judged; grey = its box is not judged yet).":"No second with trouble in the last 10 minutes."));
  /* GAP-138 (user: "when was the whole-test loss? older than 10 minutes I can only guess"):
     every stretch with trouble since the start, from the Controller, newest first */
  /* GAP-147: the reference board's whole-run cards (boxes by color, loss bursts, peaks) */
  (function(){var n={green:0,yellow:0,red:0,purple:0,grey:0},jm=null,lp=null,br=[];
   cells.forEach(function(c){n[c.color]=(n[c.color]||0)+1;var v=function(k){return ((c.values||[]).filter(function(x){return x.key===k;})[0]||{}).value;};
    var j=v("jitter_max"),p=v("loss_pct"),b=v("bitrate_avg");if(j!==null&&j!==undefined&&(jm===null||j>jm))jm=j;
    if(p!==null&&p!==undefined&&(lp===null||p>lp))lp=p;if(b!==null&&b!==undefined)br.push(b);});
   var lk=(cols.filter(function(x){return x.label==="Lost";})[0]||{}).key;  // lostKey is set further down
   var eps=r.episodes||[],lostEps=eps.filter(function(e){return (e.values||{})[lk]>0||(e.what||[]).some(function(w){return w.indexOf("no ")===0;});});
   var epCls=judged(cells,lostEps.map(function(e){return e.start_e;}));
   var lpCls=judged(cells,cells.filter(function(b){return boxVal(b,"loss_pct")>0;}).map(boxStart));
   var cw=el("div","cards");function card(l,v,cls){var d=el("div","card"+(cls?" "+cls:""));d.appendChild(el("div","l",l));d.appendChild(el("div","v",v));cw.appendChild(d);}
   card("Problem boxes",String(n.red),n.red?"bad":"");card("Warning boxes",String(n.yellow));card("Sender or receiver side",String(n.purple));
   card("OK boxes",String(n.green));card("Loss bursts",r.episodes===undefined?"—":String(lostEps.length),lostEps.length?epCls:"");
   card("Peak jitter",jm===null?"—":jm+" ms");card("Peak loss",lp===null?"—":lp+" %",lp?lpCls:"");
   card("Avg bitrate",br.length?(Math.round(br.reduce(function(a,b){return a+b;},0)/br.length*100)/100)+" Mbps":"—");
   dev.appendChild(el("h3",null,"This test so far (5-minute boxes; \u2014 = not measured)"));dev.appendChild(cw);})();
  if(r.episodes!==undefined){  // an older Controller sends none: say nothing rather than "None"
  var eps=(r.episodes||[]).filter(function(x){return (F.from===null||x.end_e>=F.from)&&(F.to===null||x.start_e<F.to);});
  var lostKey=(cols.filter(function(x){return x.label==="Lost";})[0]||{}).key;
  dev.appendChild(el("h3",null,"Trouble in this test ("+(r.episodes||[]).length+" since the start"+(eps.length!==(r.episodes||[]).length?", "+eps.length+" in the chosen time":"")+", newest first)"));
  if(!eps.length)dev.appendChild(el("p","why","None so far."));
  else{var ew=el("div","wide"),et=el("table"),eh=el("tr");["Began","Run time","Length","Lost","What","Around then"].forEach(function(x){eh.appendChild(el("th",null,x));});et.appendChild(eh);
   eps.slice().reverse().slice(0,200).forEach(function(x){var tr=el("tr");tr.appendChild(el("td",null,wall(x.start_e)));tr.appendChild(el("td",null,hms(x.start_e)));
    tr.appendChild(el("td",null,(x.end_e-x.start_e+1)+" s"));var lv=lostKey!==undefined?x.values[lostKey]:undefined;
    tr.appendChild(el("td",lv>0?judged(cells,[x.start_e]):"",lv===undefined?"":String(lv)));tr.appendChild(el("td","txt",(x.what||[]).join(", ")));
    tr.appendChild(el("td","txt",(x.evidence||[]).concat(x.notes||[]).join(" · ")));et.appendChild(tr);});
   ew.appendChild(et);dev.appendChild(ew);}}
  if(cells.length){dev.appendChild(el("h3",null,"5-minute boxes"));var bx=el("div","boxes");
   cells.forEach(function(c){var b=el("span","box",ICON[c.color]||"");b.style.background=COLOR[c.color]||COLOR.grey;b.title=hms(c.index*cell)+" "+c.color+((c.reasons||[]).length?": "+c.reasons.join("; "):"");bx.appendChild(b);});dev.appendChild(bx);
   /* GAP-146 (user: "the reference board shows each 5 minutes' max jitter and loss"):
      every box as a row with its values, newest first; From/To and problems only apply */
   var pick=cells.filter(function(c){var a=c.index*cell,b=a+cell;return (F.from===null||b>F.from)&&(F.to===null||a<F.to)&&(!F.bad||c.color!=="green");});
   var SEQ=["reordered","duplicates","late","gaps","cc_errors"];  /* one Seq R/D/L/G/E column, as the reference */
   var vcols=((cells[0]||{}).values||[]).filter(function(v){return SEQ.indexOf(v.key)<0;});
   var hasSeq=((cells[0]||{}).values||[]).some(function(v){return SEQ.indexOf(v.key)>=0;});
   var bw=el("div","wide"),bt=el("table"),bh=el("tr");
   ["Began","Run time","Box"].concat(vcols.map(function(v){return v.label+(v.unit?" ("+v.unit+")":"");})).concat(hasSeq?["Seq R/D/L/G/E"]:[]).concat(["Why"]).forEach(function(x){var th=el("th",null,x);if(x.indexOf("Seq")===0)th.title="reordered / duplicates / late / gaps / TS CC errors";bh.appendChild(th);});bt.appendChild(bh);
   if(!pick.length){var tr0=el("tr");tr0.appendChild(el("td",null,"No box in this choice."));bt.appendChild(tr0);}
   pick.slice().reverse().slice(0,300).forEach(function(c){var t0=c.start_elapsed_s!==undefined&&c.start_elapsed_s!==null?c.start_elapsed_s:c.index*cell;
    var tr=el("tr");tr.appendChild(el("td",null,wall(t0)));tr.appendChild(el("td",null,hms(t0)));
    var bd=el("td");var bs=el("span","boxb",(ICON[c.color]||"")+" "+(NAME[c.color]||c.color));bs.style.background=COLOR[c.color]||COLOR.grey;bd.appendChild(bs);tr.appendChild(bd);
    vcols.forEach(function(vc){var v=((c.values||[]).filter(function(x){return x.key===vc.key;})[0]||{}).value;
     tr.appendChild(el("td",vc.bad&&v>0?boxCls(c.color):"",v===null||v===undefined?"":String(v)));});
    if(hasSeq){var sv=SEQ.map(function(k){var x=((c.values||[]).filter(function(y){return y.key===k;})[0]||{}).value;return x===null||x===undefined?"-":x;});
     tr.appendChild(el("td",sv.some(function(x){return x>0;})?"warn":"",sv.join("/")));}
    tr.appendChild(el("td","txt",(c.reasons||[]).join("; ")||(c.color==="green"?"":"no reason recorded")));bt.appendChild(tr);});
   bw.appendChild(bt);dev.appendChild(bw);}
  function table(title,rows){dev.appendChild(el("h3",null,title));var w=el("div","wide"),t=el("table"),hr=el("tr");hr.appendChild(el("th",null,"t+s"));
  cols.forEach(function(c){hr.appendChild(el("th",null,c.label));});t.appendChild(hr);
  rows.forEach(function(x){var tr=el("tr");tr.appendChild(el("td",null,String(x.e)));cols.forEach(function(c){var v=num(c,x[c.key]);var td=el("td",lossCls(c,v,cells,[x.e])+(c.text?" txt":""),v===null?"":fmt(c,v).replace(" "+c.unit,""));tr.appendChild(td);});t.appendChild(tr);});
  w.appendChild(t);dev.appendChild(w);}
  var recent=recs.slice(-60),cut=recent.length?recent[0].e:Infinity;
  table("Last 60 seconds (newest first)",recent.slice().reverse());
  var older=recs.filter(function(x){return x.e<cut&&trouble(cols,x);});  // never a second twice
  if(older.length)table("Earlier seconds with trouble, last 10 minutes (newest first, up to 30)",older.slice(-30).reverse());
  root.appendChild(dev);});
 PENDING.forEach(function(f){f();});PENDING=[];}
function tick(first){if(document.hidden&&!first){setTimeout(tick,1000);return;}
 fetch("/live.json",{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){render(d);}).catch(function(){var m=document.getElementById("meta");m.textContent="Cannot reach the Controller — trying again";m.className="meta stale";})
 .then(function(){setTimeout(tick,1000);});}
["ffrom","fto"].forEach(function(id){document.getElementById(id).addEventListener("change",fset);});
document.getElementById("fbad").addEventListener("change",fset);
document.getElementById("fclear").addEventListener("click",function(){document.getElementById("ffrom").value="";document.getElementById("fto").value="";document.getElementById("fbad").checked=false;fset();});
tick(true);
document.addEventListener("visibilitychange",function(){if(!document.hidden)fetch("/live.json",{cache:"no-store"}).then(function(r){return r.json();}).then(render).catch(function(){});});
</script></body></html>"""


def page() -> str:
    """The live page; colors and icons are the design values (11-ui 1.7)."""
    out = PAGE.replace("__ICONS__", json.dumps(ICONS, ensure_ascii=False))
    for name in ("green", "yellow", "red", "purple", "grey"):
        out = out.replace(f"__{name.upper()}__", COLORS.get(name, "#616161"))
    return out
