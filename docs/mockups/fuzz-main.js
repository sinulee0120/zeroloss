// UI monkey regression for main-screen.html: random clicks + rapid bursts + waits, checked against invariants
// from docs/11-ui.md 1.10 (control state table) and 1.12 (no duplicate work). Usage: await __g(seed, steps)
window.__errs=[];window.addEventListener('error',e=>window.__errs.push(e.message));
(()=>{const _si=window.setInterval,_ci=window.clearInterval;window.__live=new Set();window.setInterval=(f,t)=>{const id=_si(f,t);window.__live.add(id);return id;};window.clearInterval=id=>{window.__live.delete(id);_ci(id);};})();
window.__g=async function(seed,steps){let x=seed;const rnd=()=>((x=(x*1103515245+12345)&0x7fffffff)/0x7fffffff);const q=s=>document.querySelector(s),Q=s=>[...document.querySelectorAll(s)],bad=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));const click=sel=>{const e=q(sel);if(e&&!e.disabled)e.click();};const burst=(sel,k)=>{for(let i=0;i<k;i++)click(sel);};
const setSel=(id,v)=>{const e=q('#'+id);if(!e||e.disabled)return;e.value=v;e.dispatchEvent(new Event('change',{bubbles:true}));};
const acts=[['main',()=>click('#bMain')],['BURST main x5',()=>burst('#bMain',5)],['BURST main x2',()=>burst('#bMain',2)],['mStop',()=>click('#mStop')],['BURST mStop x3',()=>burst('#mStop',3)],['mCancel',()=>click('#mCancel')],['mOk',()=>click('#mOk')],
['rLater',()=>click('#rLater')],['BURST rShow x3',()=>burst('#rShow',3)],['fault',()=>click('#dmFault')],['BURST fault x3',()=>burst('#dmFault',3)],['pre',()=>click('#dmPre')],['fixRetry',()=>click('#fixRetry')],['fixSkip',()=>click('#fixSkip')],
['fReset',()=>click('#fReset')],['BURST fReset x4',()=>burst('#fReset',4)],['fShut',()=>click('#fShut')],['fShow',()=>click('#fShow')],['actSim',()=>click('#actSim')],
['typePC',()=>setSel('sType','pc')],['typeTab',()=>setSel('sType','tablet')],['rep',()=>click('#cRep')],['BURST adv x3',()=>burst('#cAdv',3)],
['wanStatic',()=>setSel('aWan','Static IP')],['wanDhcp',()=>setSel('aWan','Built-in DHCP')],['aReset',()=>click('#aReset')],
['setup',()=>click('#bSetup')],['BURST setup x3',()=>burst('#bSetup',3)],['suDone',()=>click('#suDone')],['BURST suAdd x5',()=>{const i=q('#suIp');if(i){i.value=rnd()<.5?'10.0.0.9':'10.0.0.8';burst('#suAdd',5);}}],
['expand',()=>{const b=Q('[data-open]');if(b.length)b[Math.floor(rnd()*b.length)].click();}],['snap',()=>{const b=Q('[data-snap]');if(b.length)b[0].click();}],
['chan',()=>{const s=Q('select[data-ch]');if(s.length&&!s[0].disabled){s[0].value='CH3';s[0].dispatchEvent(new Event('change',{bubbles:true}));}}],
['BURST prob x3',()=>burst('#bProb',3)],['closeSide',()=>click('#closeSide')],['follow',()=>click('#bFollow')],['BURST last x3',()=>burst('#bLast',3)],
['demoPhase',()=>{const b=Q('#demoState button');b[Math.floor(rnd()*b.length)].click();}],['wait',async()=>{await sleep(750);}],['wait long',async()=>{await sleep(1500);}]];
for(let n=0;n<steps;n++){const a=acts[Math.floor(rnd()*acts.length)];try{await a[1]();}catch(e){bad.push([n,a[0],'exception '+e.message]);}await sleep(0);
 const txt=q('#bMainTxt').textContent,blocked=q('#bMain').getAttribute('aria-disabled')==='true',running=txt==='Stop',starting=txt==='Starting…',stopping=txt==='Stopping…';
 const phase=(Q('#demoState button').find(b=>b.getAttribute('aria-pressed')==='true')||{}).textContent;
 const expect={Idle:'Start',Starting:'Starting…',Running:'Stop',Stopping:'Stopping…',Fault:'Start'}[phase];
 if(expect&&expect!==txt)bad.push([n,a[0],`phase ${phase} but button "${txt}"`]);
 if(running&&blocked)bad.push([n,a[0],'Stop is blocked']);
 if((starting||stopping)&&!blocked)bad.push([n,a[0],'Starting/Stopping not blocked']);
 if((phase==='Idle')===q('#sType').disabled)bad.push([n,a[0],`settings lock wrong in ${phase}`]);
 if(Q('.scrim').length>1)bad.push([n,a[0],'more than one modal']);
 if(q('.setup')&&phase!=='Idle')bad.push([n,a[0],'device setup open while not idle']);
 if(window.__live.size>1)bad.push([n,a[0],'more than one running timer']);
 if((phase==='Idle'||phase==='Fault')&&window.__live.size>0)bad.push([n,a[0],'timer still running in '+phase]);
 const ips=Q('.setup .nodes tbody tr td:first-child > .sub').map(e=>e.textContent.split('·').pop().trim());if(new Set(ips).size!==ips.length)bad.push([n,a[0],'duplicate device rows']);
 const type=q('#sType').value,rows=Q('#devList .dev .name').map(e=>e.textContent);
 if(type==='tablet'&&(rows.includes('Receiver PC')||!rows.includes('Tablet-1')))bad.push([n,a[0],'tablet rows wrong']);
 if(type==='pc'&&(!rows.includes('Receiver PC')||rows.includes('Tablet-1')))bad.push([n,a[0],'pc rows wrong']);
 if(!q('#prog').hidden!==(running||stopping))bad.push([n,a[0],'progress visibility wrong']);
 if(!q('#ipBox').hidden&&q('#aWan').value!=='Static IP')bad.push([n,a[0],'IP box for DHCP']);}
Q('.toast').forEach(t=>t.remove());const kinds={};bad.forEach(b=>{const k=b[2];kinds[k]=(kinds[k]||0)+1;});return {seed,steps,fails:bad.length,kinds,first:bad.slice(0,3),errors:window.__errs.slice(0,5)};};
