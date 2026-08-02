const TABS = [
  ['gallery','Gallery','▦'], ['live','Live','●'], ['local','Local','★'], ['settings','Settings','⚙'], ['logs','Logs','≡']
];
const S = {tab:'gallery',settings:null,cameras:[],events:[],saved:[],liveCamera:null,hls:null,evt:null};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

async function api(url, options={}) {
  const response = await fetch(url, {headers:{'Content-Type':'application/json',...(options.headers||{})}, ...options});
  if (!response.ok) { let detail=`HTTP ${response.status}`; try { detail=(await response.json()).detail||detail; } catch {} throw new Error(detail); }
  return response.status === 204 ? null : response.json();
}
function toast(message, error=false) { const el=$('toast'); el.textContent=message; el.style.background=error?'#7f1d1d':'#263445'; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600); }
function formatTime(value) { try { return new Intl.DateTimeFormat([], {dateStyle:'medium',timeStyle:'short'}).format(new Date(value)); } catch { return value; } }

function initNav() {
  for (const nav of document.querySelectorAll('.desktop-nav,.mobile-nav')) {
    nav.innerHTML=TABS.map(([id,label,icon])=>`<button class="nav-button" data-tab="${id}"><span class="nav-icon">${icon}</span>${label}</button>`).join('');
    nav.addEventListener('click', e=>{ const b=e.target.closest('[data-tab]'); if(b) showTab(b.dataset.tab); });
  }
}
async function showTab(tab) {
  if(S.tab==='live'&&tab!=='live') await stopLive();
  S.tab=tab;
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('active',el.id===`tab-${tab}`));
  document.querySelectorAll('.nav-button').forEach(el=>el.classList.toggle('active',el.dataset.tab===tab));
  if(tab==='gallery') loadEvents(); if(tab==='local') loadSaved(); if(tab==='settings') loadSettings(); if(tab==='logs') loadLogs();
}

async function loadStatus() {
  try { const data=await api('/api/status'); const el=$('service-status'); el.textContent=data.mqtt.connected?'MQTT online':(data.mqtt.error?'MQTT offline':'MQTT connecting'); el.className='status-pill '+(data.mqtt.connected?'online':'offline'); }
  catch { $('service-status').textContent='Server offline'; $('service-status').className='status-pill offline'; }
}
async function loadCameras() {
  S.cameras=await api('/api/cameras');
  const options=S.cameras.filter(c=>c.enabled).map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  $('live-camera').innerHTML=options||'<option>No enabled cameras</option>';
  $('gallery-camera').innerHTML='<option value="">All cameras</option>'+S.cameras.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
}
async function loadEvents() {
  const params=new URLSearchParams(); const camera=$('gallery-camera').value,status=$('gallery-status').value,q=$('gallery-query').value.trim();
  if(camera)params.set('camera_id',camera); if(status)params.set('status',status); if(q)params.set('q',q);
  try { S.events=(await api('/api/events?'+params)).events; renderEvents(); } catch(e){ toast(e.message,true); }
}
function renderEvents() {
  $('event-count').textContent=`${S.events.length} event${S.events.length===1?'':'s'}`;
  $('gallery-empty').classList.toggle('hidden',S.events.length>0);
  $('gallery-grid').innerHTML=S.events.map(event=>{
    const detections=(event.analysis?.detections||[]).slice(0,4).map(d=>`<span class="badge">${esc(d.label)} ${Number(d.confidence||0).toFixed(0)}</span>`).join('');
    const thumb=event.primary_media_id?`<img class="media-thumb" loading="lazy" src="/api/media/${event.primary_media_id}/thumb" alt="${esc(event.camera_name)} capture">`:'<div class="media-thumb placeholder">No preview</div>';
    return `<article class="media-card" data-event="${event.id}">${thumb}<div class="card-body"><div class="card-title">${esc(event.camera_name)}</div><div class="card-meta"><span>${formatTime(event.triggered_at)}</span><span>${event.trigger_count>1?'×'+event.trigger_count:''}</span></div><div class="badges"><span class="badge ${esc(event.status)}">${esc(event.status)}</span>${detections}</div></div></article>`;
  }).join('');
}
async function openEvent(id) {
  try {
    const event=await api('/api/events/'+id); const detections=(event.analysis?.detections||[]).map(d=>`${esc(d.label)} (${Number(d.confidence||0).toFixed(0)}/10)`).join(', ');
    $('event-detail').innerHTML=`<h2>${esc(event.camera_name)}</h2><p class="muted">${formatTime(event.triggered_at)} · ${esc(event.source)} · ${esc(event.status)}${event.trigger_count>1?' · '+event.trigger_count+' triggers':''}</p>${event.analysis?`<p><strong>${detections||'No detections'}</strong><br>${esc(event.analysis.description||'')}</p>`:''}<div class="detail-media">${event.media.map(media=>detailMedia(media)).join('')}</div><div class="camera-actions"><button class="button danger" data-delete-event="${event.id}">Delete event</button></div>`;
    $('event-dialog').showModal();
  } catch(e){ toast(e.message,true); }
}
function detailMedia(media) {
  const body=media.path?(media.kind==='video'?`<video controls playsinline src="/api/media/${media.id}/file"></video>`:`<img src="/api/media/${media.id}/file" alt="Captured image">`):`<div class="empty">${esc(media.error||'Unavailable')}</div>`;
  return `<div class="detail-item">${body}<div class="detail-actions"><span class="badge">${esc(media.kind)}</span>${media.path?`<button class="button" data-save-media="${media.id}">Save to Local</button>${media.kind==='snapshot'?`<button class="button" data-analyze-media="${media.id}">Analyze</button><button class="button" data-chat-media="${media.id}">Ask…</button>`:''}`:''}</div></div>`;
}

async function loadSaved() {
  try { S.saved=(await api('/api/saved')).items; $('local-empty').classList.toggle('hidden',S.saved.length>0); $('local-grid').innerHTML=S.saved.map(item=>`<article class="media-card"><${item.kind==='video'?'video controls playsinline':'img loading="lazy"'} class="media-thumb" src="/api/saved/${item.id}/${item.kind==='video'?'file':'thumb'}"></${item.kind==='video'?'video':'img'}><div class="card-body"><div class="card-title">${esc(item.camera_name)}</div><div class="card-meta"><span>${formatTime(item.saved_at)}</span><button class="button danger" data-delete-saved="${item.id}">Delete</button></div></div></article>`).join(''); }
  catch(e){toast(e.message,true)}
}

async function startLive() {
  await stopLive(); const cameraId=$('live-camera').value;if(!cameraId)return;S.liveCamera=cameraId;
  try { const info=await api(`/api/cameras/${cameraId}/live/info`); $('live-placeholder').classList.add('hidden'); $('ptz-pad').classList.toggle('hidden',!info.ptz);
    if(info.mjpeg){$('live-mjpeg').src=info.mjpeg_url+($('live-hd').checked?'?hd=true':'');$('live-mjpeg').classList.remove('hidden');}
    else { const result=await api(`/api/cameras/${cameraId}/hls/start?hd=${$('live-hd').checked}`,{method:'POST'}); const video=$('live-video');video.classList.remove('hidden'); if(window.Hls?.isSupported()){S.hls=new Hls({liveSyncDurationCount:2,maxLiveSyncPlaybackRate:1.5});S.hls.loadSource(result.playlist);S.hls.attachMedia(video);}else{video.src=result.playlist;} }
    $('live-message').textContent='Live view active.';
  } catch(e){$('live-message').textContent=e.message;$('live-message').className='message error';toast(e.message,true)}
}
async function stopLive() {
  const id=S.liveCamera; $('live-mjpeg').src=''; $('live-mjpeg').classList.add('hidden'); const video=$('live-video');video.pause();video.removeAttribute('src');video.load();video.classList.add('hidden');if(S.hls){S.hls.destroy();S.hls=null;}if(id){try{await api(`/api/cameras/${id}/hls/stop`,{method:'POST'})}catch{}}S.liveCamera=null;$('ptz-pad').classList.add('hidden');$('live-placeholder').classList.remove('hidden');
}
async function ptz(command,coarse=false){if(!S.liveCamera)return;try{await api(`/api/cameras/${S.liveCamera}/ptz`,{method:'POST',body:JSON.stringify({command,coarse})});}catch(e){toast(e.message,true)}}
async function manualCapture(kind){const id=$('live-camera').value;if(!id)return;try{await api(`/api/cameras/${id}/capture/${kind}`,{method:'POST'});toast(kind==='clip'?'Recording started':'Snapshot started');}catch(e){toast(e.message,true)}}

async function loadSettings() {
  try { S.settings=await api('/api/settings'); S.cameras=S.settings.cameras; fillForm('capture-settings',S.settings.capture); fillForm('retention-settings',{...S.settings.retention,mqtt_host:S.settings.mqtt.host,mqtt_port:S.settings.mqtt.port,mqtt_username:S.settings.mqtt.username}); fillForm('analysis-settings',S.settings.analysis); renderCameraSettings(); }
  catch(e){toast(e.message,true)}
}
function fillForm(id,data){const form=$(id);for(const [key,value] of Object.entries(data)){const input=form.elements.namedItem(key);if(!input)continue;if(input.type==='checkbox')input.checked=Boolean(value);else input.value=value??'';}}
function formData(id){const result={};for(const el of $(id).elements){if(!el.name)continue;let value=el.type==='checkbox'?el.checked:el.value;if(el.type==='number')value=Number(value);result[el.name]=value;}return result;}
function renderCameraSettings(){
  $('camera-settings').innerHTML=S.cameras.map((c,i)=>`<form class="camera-form panel" data-index="${i}"><div class="camera-form-head"><h3>${esc(c.name||'New camera')}</h3><button type="button" class="button danger" data-remove-camera="${i}">Remove</button></div><div class="field-grid">
    ${field('id','Camera ID',c.id)}${field('name','Display name',c.name)}${field('host','Host or IP',c.host)}<label>Adapter<select name="adapter"><option value="thingino" ${c.adapter==='thingino'?'selected':''}>Thingino</option><option value="onvif" ${c.adapter==='onvif'?'selected':''}>ONVIF</option></select></label>
    ${field('onvif_port','ONVIF port',c.onvif_port,'number')}${field('username','Username',c.username)}${field('password','Password','','password',c.has_password?'Configured; leave blank to keep':'')}${field('token','HTTP token','','password',c.has_token?'Configured; leave blank to keep':'')}
    ${field('mqtt_topic','MQTT topic',c.mqtt_topic)}${field('prompt_override','Prompt override',c.prompt_override,'textarea')}
    <label class="switch"><input name="enabled" type="checkbox" ${c.enabled?'checked':''}><span>Enabled</span></label><label class="switch"><input name="needs_credentials" type="checkbox" ${c.needs_credentials?'checked':''}><span>Needs credentials</span></label><label class="switch"><input name="ptz" type="checkbox" ${c.ptz?'checked':''}><span>PTZ</span></label><label class="switch"><input name="sd_redundancy" type="checkbox" ${c.sd_redundancy?'checked':''}><span>SD redundancy</span></label>
    </div><div class="camera-actions"><button type="button" class="button" data-probe-camera="${esc(c.id)}">Probe ONVIF</button><button type="button" class="button" data-sd-camera="${esc(c.id)}">Check SD</button><span class="message" data-camera-result="${esc(c.id)}"></span></div></form>`).join('');
}
function field(name,label,value,type='text',placeholder=''){if(type==='textarea')return`<label class="wide-field">${label}<textarea name="${name}" rows="4">${esc(value)}</textarea></label>`;return`<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}"></label>`;}
function collectCameras(){return [...document.querySelectorAll('.camera-form')].map(form=>{const prior=S.cameras[Number(form.dataset.index)]||{};const result={...prior};for(const el of form.elements){if(!el.name)continue;let value=el.type==='checkbox'?el.checked:el.value;if(el.type==='number')value=Number(value);if((el.name==='password'||el.name==='token')&&!value)continue;result[el.name]=value;}return result;});}
async function saveSettings(){
  const capture=formData('capture-settings'),retentionRaw=formData('retention-settings'),analysis=formData('analysis-settings');
  const mqtt={...S.settings.mqtt,host:retentionRaw.mqtt_host,port:retentionRaw.mqtt_port,username:retentionRaw.mqtt_username};if(retentionRaw.mqtt_password)mqtt.password=retentionRaw.mqtt_password;
  const retention={days:retentionRaw.days,max_gb:retentionRaw.max_gb,interval_minutes:S.settings.retention.interval_minutes};
  try{S.settings=await api('/api/settings',{method:'PUT',body:JSON.stringify({timezone:S.settings.timezone,capture,retention,mqtt,analysis,cameras:collectCameras()})});$('settings-message').textContent='Settings saved and event sources restarted.';$('settings-message').className='message success';await loadCameras();renderCameraSettings();}
  catch(e){$('settings-message').textContent=e.message;$('settings-message').className='message error';}
}
async function discoverCameras(){const out=$('discovery-results');out.textContent='Scanning…';try{const data=await api('/api/cameras/discover',{method:'POST'});out.textContent=data.devices.length?data.devices.map(d=>`${d.host} ${d.scopes.join(' ')}`).join('\n'):'No ONVIF devices answered.';}catch(e){out.textContent=e.message;out.className='message error';}}
function addCamera(){S.cameras.push({id:'new-camera',name:'New camera',host:'',adapter:'onvif',enabled:false,needs_credentials:true,onvif_port:80,username:'',mqtt_topic:'',prompt_override:'',ptz:false,sd_redundancy:false,capture:{}});renderCameraSettings();}
async function probeCamera(id){const out=document.querySelector(`[data-camera-result="${CSS.escape(id)}"]`);out.textContent='Probing…';try{const data=await api(`/api/cameras/${id}/probe`);out.textContent=data.ok?`Services: ${data.services.join(', ')}`:data.error;}catch(e){out.textContent=e.message;}}
async function checkSd(id){const out=document.querySelector(`[data-camera-result="${CSS.escape(id)}"]`);out.textContent='Checking SD…';try{const data=await api(`/api/cameras/${id}/sd`);out.textContent=data.supported?(data.mounted===false?'SD not mounted':'SD reachable'):'Not supported';}catch(e){out.textContent=e.message;}}
async function loadLogs(){try{const entries=(await api('/api/logs')).entries;$('log-list').innerHTML=entries.slice().reverse().map(x=>`<div class="log-entry"><time>${formatTime(x.time)}</time><span class="${esc(x.level)}">${esc(x.level)}</span><span>${esc(x.message)}</span></div>`).join('')||'<div class="empty">No log entries.</div>';}catch(e){toast(e.message,true)}}

function bind() {
  $('refresh-events').onclick=loadEvents; $('gallery-camera').onchange=loadEvents;$('gallery-status').onchange=loadEvents;$('gallery-query').oninput=debounce(loadEvents,350);
  $('gallery-grid').onclick=e=>{const card=e.target.closest('[data-event]');if(card)openEvent(card.dataset.event)};
  $('event-dialog').querySelector('.dialog-close').onclick=()=>$('event-dialog').close(); $('event-dialog').addEventListener('click',async e=>{const save=e.target.closest('[data-save-media]');if(save){try{await api(`/api/media/${save.dataset.saveMedia}/save`,{method:'POST'});toast('Saved to Local');}catch(err){toast(err.message,true)}}const analyze=e.target.closest('[data-analyze-media]');if(analyze){try{await api(`/api/media/${analyze.dataset.analyzeMedia}/analyze`,{method:'POST'});toast('Analysis complete');}catch(err){toast(err.message,true)}}const chat=e.target.closest('[data-chat-media]');if(chat){const promptText=prompt('Ask about this image:');if(promptText){try{const result=await api(`/api/media/${chat.dataset.chatMedia}/chat`,{method:'POST',body:JSON.stringify({prompt:promptText})});alert(result.description||result.raw||'No response');}catch(err){toast(err.message,true)}}}const del=e.target.closest('[data-delete-event]');if(del&&confirm('Delete this event from the server? SD copies are not affected.')){await api('/api/events/'+del.dataset.deleteEvent,{method:'DELETE'});$('event-dialog').close();loadEvents();}});
  $('local-grid').onclick=async e=>{const b=e.target.closest('[data-delete-saved]');if(b&&confirm('Delete this saved item?')){await api('/api/saved/'+b.dataset.deleteSaved,{method:'DELETE'});loadSaved();}};
  $('live-start').onclick=startLive;$('live-stop').onclick=stopLive;$('manual-snapshot').onclick=()=>manualCapture('snapshot');$('manual-clip').onclick=()=>manualCapture('clip');
  let clickTimer;document.querySelectorAll('#ptz-pad button').forEach(b=>{b.onclick=e=>{if(e.detail===1)clickTimer=setTimeout(()=>ptz(b.dataset.command,false),210)};b.ondblclick=()=>{clearTimeout(clickTimer);ptz(b.classList.contains('ptz-center')?'home':b.dataset.command,true)}});
  $('save-settings').onclick=saveSettings;$('discover-cameras').onclick=discoverCameras;$('add-camera').onclick=addCamera;$('test-alert').onclick=async()=>{try{await api('/api/alerts/test',{method:'POST'});toast('Test email sent')}catch(e){toast(e.message,true)}};
  $('camera-settings').onclick=e=>{const remove=e.target.closest('[data-remove-camera]');if(remove){S.cameras.splice(Number(remove.dataset.removeCamera),1);renderCameraSettings();return}const probe=e.target.closest('[data-probe-camera]');if(probe)probeCamera(probe.dataset.probeCamera);const sd=e.target.closest('[data-sd-camera]');if(sd)checkSd(sd.dataset.sdCamera);};
  $('refresh-logs').onclick=loadLogs;
}
function debounce(fn,ms){let t;return()=>{clearTimeout(t);t=setTimeout(fn,ms)}}
function connectEvents(){if(S.evt)S.evt.close();S.evt=new EventSource('/api/updates');S.evt.onmessage=e=>{const data=JSON.parse(e.data);if(data.type.startsWith('event_')||data.type==='capture_progress'||data.type==='analysis_update'){if(S.tab==='gallery')loadEvents();}if(data.type==='saved'&&S.tab==='local')loadSaved();};S.evt.onerror=()=>setTimeout(connectEvents,3000);}

async function init(){initNav();bind();showTab('gallery');await loadCameras();await Promise.all([loadStatus(),loadEvents()]);connectEvents();setInterval(loadStatus,10000);setInterval(()=>{if(S.tab==='logs')loadLogs()},3000);}
document.addEventListener('DOMContentLoaded',init);
