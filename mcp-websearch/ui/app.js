const $=id=>document.getElementById(id);let windowName='24h',loading=false;
const number=value=>Number.isFinite(Number(value))?Number(value):0;
const count=value=>number(value).toLocaleString();
const percent=value=>`${Math.round(number(value)*100)}%`;
const latency=value=>number(value)>0?`${Math.round(number(value))} ms`:'—';
const set=(id,value)=>{$(id).textContent=value};
function showError(message){const box=$('error');box.textContent=message;box.hidden=false;clearTimeout(showError.timer);showError.timer=setTimeout(()=>box.hidden=true,5000)}
function providerHealth(health,stats){
  const provider=(health.providers||[]).find(item=>item.name==='brave')||{};
  const providerStats=(stats.providers||{}).brave||{};
  const circuit=provider.circuit||{};
  set('credential',provider.credential_usable?'Usable':provider.credential_configured?'Configured · unavailable':'Missing');
  set('circuit',circuit.state||'Unknown');set('attempts',count(providerStats.attempts));set('provider-errors',count(providerStats.attempt_errors));
  const badge=$('provider-state');
  if(provider.credential_usable&&circuit.state!=='open'){badge.textContent='Available';badge.className='badge ok'}
  else if(provider.credential_configured){badge.textContent='Attention';badge.className='badge warn'}
  else{badge.textContent='Unavailable';badge.className='badge down'}
}
function render(health,stats){
  const state=$('state'),label=health.ready?(health.status==='degraded'?'Degraded':'Ready'):'Not ready';
  state.querySelector('span').textContent=label;state.className=`state ${health.ready?'ok':'down'}`;
  const searches=stats.searches||{},statuses=searches.statuses||{},fetches=stats.fetches||{},cache=stats.cache||{};
  set('search-total',count(searches.total));
  set('search-outcomes',`${count(statuses.ok)} ok · ${count(statuses.degraded)} degraded · ${count(number(statuses.error)+number(statuses.failed))} failed`);
  set('search-latency',latency((searches.latency_ms||{}).average));
  set('fetch-total',count(fetches.total));set('fetch-success',`${count(fetches.successes)} completed · ${count(fetches.errors)} failed`);
  set('cache-rate',percent((cache.search||{}).hit_rate));set('cache-detail',`${count((cache.search||{}).hits)} hits · ${count((cache.search||{}).misses)} misses`);
  set('fallback',`${percent((stats.fallback||{}).rate)} · ${count((stats.fallback||{}).searches)} searches`);
  set('fetch-rate',percent(fetches.success_rate));set('fetch-cache',percent((cache.fetch||{}).hit_rate));
  set('telemetry',stats.complete?'Complete':stats.available?'Partial':'Unavailable');providerHealth(health,stats);
  set('updated',`Updated ${new Date().toLocaleTimeString()} · ${windowName} window · no query data stored`);
}
async function load(){if(loading)return;loading=true;$('refresh').disabled=true;try{
  const options={cache:'no-store',signal:AbortSignal.timeout(5000)};
  const [healthResult,statsResult]=await Promise.allSettled([fetch('/health',options),fetch(`/stats?window=${windowName}`,options)]);
  if(healthResult.status!=='fulfilled'||!healthResult.value.ok)throw new Error('Broker health unavailable');
  const health=await healthResult.value.json();
  if(statsResult.status==='fulfilled'&&statsResult.value.ok){render(health,await statsResult.value.json())}
  else{render(health,{available:false,searches:{},fetches:{},cache:{},providers:{}});set('telemetry','Unavailable');set('updated',`Updated ${new Date().toLocaleTimeString()} · telemetry unavailable`)}
}catch(error){$('state').className='state down';$('state').querySelector('span').textContent='Unavailable';showError(`Could not load broker status: ${error.message}`)}finally{loading=false;$('refresh').disabled=false}}
$('windows').addEventListener('click',event=>{const button=event.target.closest('[data-window]');if(!button)return;windowName=button.dataset.window;document.querySelectorAll('[data-window]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));load()});
$('refresh').addEventListener('click',load);load();setInterval(load,15000);
