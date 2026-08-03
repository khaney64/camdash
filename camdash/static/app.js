const TABS = [
  ['gallery','Gallery','▦'], ['live','Live','●'], ['local','Local','★'], ['settings','Settings','⚙'], ['logs','Logs','≡']
];
const S = {tab:'gallery',settings:null,cameras:[],events:[],saved:[],liveCamera:null,livePtz:false,hls:null,evt:null,chatMedia:null};
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
  await updateRtspUrl();
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
    const detections=event.analysis?.detections||[];
    const thumb=event.primary_media_id?`<img class="media-thumb" loading="lazy" src="/api/media/${event.primary_media_id}/thumb" alt="${esc(event.camera_name)} capture">`:'<div class="media-thumb placeholder">No preview</div>';
    return `<article class="media-card ${analysisClass(detections)}" data-event="${event.id}">${thumb}${detectionOverlay(detections)}<div class="card-body"><div class="card-title">${esc(event.camera_name)}</div><div class="card-meta"><span>${formatTime(event.triggered_at)}</span><span>${event.trigger_count>1?'×'+event.trigger_count:''}</span></div><div class="badges"><span class="badge ${esc(event.status)}">${esc(event.status)}</span></div></div></article>`;
  }).join('');
}
const DETECTION_LABELS={raccoon:'🦝 raccoon',bear:'🐻 bear',coyote:'🐺 coyote',person:'🚶 person',human:'🚶 person',legs:'🚶 person',fox:'🦊 fox',deer:'🦌 deer',cat:'🐱 cat',dog:'🐶 dog',squirrel:'🐿 squirrel',rabbit:'🐇 rabbit',bird:'🐦 bird',skunk:'🦨 skunk',turkey:'🦃 turkey',opossum:'🐾 opossum',possum:'🐾 possum',cupcake:'🧁 Cupcake',sox:'🧦 Sox'};
function detectionParts(detections){
  const parts=[],seen=new Set();
  for(const d of detections||[]){
    const label=String(d.label||'').trim().toLowerCase();
    if(label&&!seen.has(label)){parts.push({text:DETECTION_LABELS[label]||label,title:d.reasoning||`${label} confidence ${d.confidence??'unknown'}`});seen.add(label);}
    const name=String(d.name||'').trim(),key=name.toLowerCase();
    if(name&&key!==label&&!seen.has(key)){parts.push({text:DETECTION_LABELS[key]||`🏷️ ${name}`,title:d.reasoning||`Identified as ${name}`});seen.add(key);}
  }
  return parts;
}
function detectionOverlay(detections){const parts=detectionParts(detections);return parts.length?`<div class="analysis-overlay">${parts.slice(0,6).map(p=>`<span title="${esc(p.title)}">${esc(p.text)}</span>`).join('<span class="analysis-separator">·</span>')}</div>`:'';}
function analysisClass(detections){const labels=new Set((detections||[]).map(d=>String(d.label||'').toLowerCase()));if(['raccoon','bear','coyote','skunk'].some(x=>labels.has(x)))return'analysis-wild';if(['person','human','legs'].some(x=>labels.has(x)))return'analysis-person';if(['cat','dog'].some(x=>labels.has(x)))return'analysis-pet';return labels.size?'analysis-animal':'';}
async function openEvent(id) {
  try {
    const event=await api('/api/events/'+id); const detections=detectionParts(event.analysis?.detections||[]).map(p=>`<span class="detail-detection">${esc(p.text)}</span>`).join('<span class="analysis-separator">·</span>');
    $('event-detail').dataset.eventId=event.id;
    $('event-detail').innerHTML=`<h2>${esc(event.camera_name)}</h2><p class="muted">${formatTime(event.triggered_at)} · ${esc(event.source)} · ${esc(event.status)}${event.trigger_count>1?' · '+event.trigger_count+' triggers':''}</p>${event.analysis?`<div class="detail-analysis"><strong>${detections||'No detections'}</strong>${event.analysis.description?`<div>${esc(event.analysis.description)}</div>`:''}</div>`:''}<div class="detail-media">${event.media.map(media=>detailMedia(media)).join('')}</div><div class="camera-actions"><button class="icon-action icon-delete" data-delete-event="${event.id}" title="Delete event" aria-label="Delete event">🗑</button></div>`;
    if(!$('event-dialog').open)$('event-dialog').showModal();
  } catch(e){ toast(e.message,true); }
}
function detailMedia(media) {
  const body=media.path?(media.kind==='video'?`<video controls playsinline src="/api/media/${media.id}/file"></video>`:`<img src="/api/media/${media.id}/file" alt="Captured image">`):`<div class="empty">${esc(media.error||'Unavailable')}</div>`;
  const overlay=media.kind==='snapshot'?detectionOverlay(media.analysis?.detections||[]):'';
  const chatButton=S.settings?.analysis?.chat_enabled?`<button class="icon-action icon-chat" data-chat-media="${media.id}" title="Chat about image" aria-label="Chat about image">💬</button>`:'';
  return `<div class="detail-item ${analysisClass(media.analysis?.detections||[])}"><div class="detail-media-frame">${body}${overlay}</div><div class="detail-actions"><span class="badge">${esc(media.kind)}</span>${media.path?`${media.kind==='snapshot'?`${chatButton}<button class="icon-action icon-analyze" data-analyze-media="${media.id}" title="Re-analyze" aria-label="Re-analyze">🔬</button>`:''}<button class="icon-action icon-save" data-save-media="${media.id}" title="Save to Local" aria-label="Save to Local">💾</button>`:''}</div></div>`;
}

async function loadSaved() {
  try { S.saved=(await api('/api/saved')).items; $('local-empty').classList.toggle('hidden',S.saved.length>0); $('local-grid').innerHTML=S.saved.map(item=>`<article class="media-card ${analysisClass(item.analysis?.detections||[])}"><${item.kind==='video'?'video controls playsinline':'img loading="lazy"'} class="media-thumb" src="/api/saved/${item.id}/${item.kind==='video'?'file':'thumb'}"></${item.kind==='video'?'video':'img'}>${detectionOverlay(item.analysis?.detections||[])}<div class="card-body"><div class="card-title">${esc(item.camera_name)}</div><div class="card-meta"><span>${formatTime(item.saved_at)}</span><button class="icon-action icon-delete" data-delete-saved="${item.id}" title="Delete saved item" aria-label="Delete saved item">🗑</button></div></div></article>`).join(''); }
  catch(e){toast(e.message,true)}
}

async function updateRtspUrl() {
  const cameraId=$('live-camera').value,input=$('live-rtsp');
  if(!cameraId){input.value='';return;}
  try{const info=await api(`/api/cameras/${cameraId}/live/info?hd=${$('live-hd').checked}`);input.value=info.rtsp_url||'';}
  catch(e){input.value='';toast(e.message,true)}
}
async function copyRtspUrl() {
  const input=$('live-rtsp');if(!input.value)return;
  try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(input.value);else{input.focus();input.select();if(!document.execCommand('copy'))throw new Error('Copy failed');}toast('RTSP URL copied');}
  catch{toast('Could not copy automatically; select the URL and copy it manually.',true)}
}
async function liveSelectionChanged(){if(S.liveCamera)await stopLive();await updateRtspUrl();}
async function startLive() {
  await stopLive(); const cameraId=$('live-camera').value;if(!cameraId)return;
  try { const info=await api(`/api/cameras/${cameraId}/live/info?hd=${$('live-hd').checked}`); S.liveCamera=cameraId;S.livePtz=info.ptz;$('live-rtsp').value=info.rtsp_url||''; $('live-placeholder').classList.add('hidden'); $('ptz-pad').classList.add('hidden');
    if(info.mjpeg){$('live-mjpeg').src=info.mjpeg_url+($('live-hd').checked?'?hd=true':'');$('live-mjpeg').classList.remove('hidden');}
    else { const result=await api(`/api/cameras/${cameraId}/hls/start?hd=${$('live-hd').checked}`,{method:'POST'}); const video=$('live-video');video.classList.remove('hidden'); if(window.Hls?.isSupported()){S.hls=new Hls({liveSyncDurationCount:2,maxLiveSyncPlaybackRate:1.5});S.hls.loadSource(result.playlist);S.hls.attachMedia(video);}else{video.src=result.playlist;} }
    $('live-start').textContent='Stop live';$('live-start').classList.remove('primary');$('live-message').textContent='Live view active. Click the preview for direction controls.';$('live-message').className='message';
  } catch(e){S.liveCamera=null;S.livePtz=false;$('live-start').textContent='Start live';$('live-start').classList.add('primary');$('live-message').textContent=e.message;$('live-message').className='message error';toast(e.message,true)}
}
async function stopLive() {
  const id=S.liveCamera; $('live-mjpeg').src=''; $('live-mjpeg').classList.add('hidden'); const video=$('live-video');video.pause();video.removeAttribute('src');video.load();video.classList.add('hidden');if(S.hls){S.hls.destroy();S.hls=null;}if(id){try{await api(`/api/cameras/${id}/hls/stop`,{method:'POST'})}catch{}}S.liveCamera=null;S.livePtz=false;$('ptz-pad').classList.add('hidden');$('live-placeholder').classList.remove('hidden');$('live-start').textContent='Start live';$('live-start').classList.add('primary');
}
async function toggleLive(){if(S.liveCamera)await stopLive();else await startLive();}
async function ptz(command,coarse=false){if(!S.liveCamera)return;try{await api(`/api/cameras/${S.liveCamera}/ptz`,{method:'POST',body:JSON.stringify({command,coarse})});}catch(e){toast(e.message,true)}}
async function manualCapture(kind){const id=$('live-camera').value;if(!id)return;try{await api(`/api/cameras/${id}/capture/${kind}`,{method:'POST'});toast(kind==='clip'?'Recording started':'Snapshot started');}catch(e){toast(e.message,true)}}

async function loadSettings() {
  try { S.settings=await api('/api/settings'); S.cameras=S.settings.cameras; fillForm('capture-settings',S.settings.capture); fillForm('retention-settings',{...S.settings.retention,mqtt_host:S.settings.mqtt.host,mqtt_port:S.settings.mqtt.port,mqtt_username:S.settings.mqtt.username}); fillForm('analysis-settings',S.settings.analysis); renderAnalysisSettings(); renderAlertSettings(); renderCameraSettings(); }
  catch(e){toast(e.message,true)}
}
function fillForm(id,data){const form=$(id);for(const [key,value] of Object.entries(data)){const input=form.elements.namedItem(key);if(!input)continue;if(input.type==='checkbox')input.checked=Boolean(value);else input.value=value??'';}}
function formData(id){const result={};for(const el of $(id).elements){if(!el.name)continue;let value=el.type==='checkbox'?el.checked:el.value;if(el.type==='number'||el.type==='range')value=Number(value);result[el.name]=value;}return result;}
function renderAnalysisSettings(){
  const analysis=S.settings.analysis,anthropic=analysis.backend==='anthropic';
  document.querySelectorAll('.analysis-local-field').forEach(el=>el.classList.toggle('hidden',anthropic));document.querySelectorAll('.analysis-anthropic-field').forEach(el=>el.classList.toggle('hidden',!anthropic));
  $('anthropic-key-status').textContent=analysis.anthropic_key_set?'✓ API key configured':'✗ API key not configured on server';
  $('analysis-temperature-value').textContent=Number(analysis.temperature??0.1).toFixed(2);
}
function renderAlertSettings(){
  const analysis=S.settings.analysis;fillForm('alert-settings',analysis);
  const status=$('alert-email-status');status.textContent=analysis.alert_email_configured?`Configured: ${analysis.alert_email}`:'Not configured on server';status.classList.toggle('success',analysis.alert_email_configured);
  $('test-alert').disabled=!analysis.alert_email_configured;
  const enabled=analysis.alert_rules_enabled||{};
  $('alert-rule-toggles').innerHTML=(analysis.alert_rules||[]).map(name=>{const checked=enabled[name]!==false;const person=name.toLowerCase()==='person';return `<div class="alert-rule${person?' alert-rule-person':''}"><label class="switch"><input type="checkbox" data-alert-rule="${esc(name)}" ${checked?'checked':''}><span>${esc(name)} alerts</span></label>${person?'<label class="switch nested"><input name="remove_person_only_images" type="checkbox"><span>Remove new Person only images</span></label>':''}</div>`}).join('')||'<p class="muted">No alert rules configured. Add rules to ~/.camdash/alerts.yaml.</p>';
  const remove=$('alert-settings').elements.namedItem('remove_person_only_images');if(remove)remove.checked=Boolean(analysis.remove_person_only_images);
  const person=[...document.querySelectorAll('[data-alert-rule]')].find(input=>input.dataset.alertRule.toLowerCase()==='person');
  if(person&&remove){person.onchange=()=>{if(person.checked)remove.checked=false};remove.onchange=()=>{if(remove.checked)person.checked=false};}
}
function collectAlertSettings(){
  const values=formData('alert-settings'),rules={...(S.settings.analysis.alert_rules_enabled||{})};
  document.querySelectorAll('[data-alert-rule]').forEach(input=>{rules[input.dataset.alertRule]=input.checked});
  return {...values,alert_rules_enabled:rules};
}
function renderCameraSettings(){
  $('camera-settings').innerHTML=S.cameras.map((c,i)=>`<form class="camera-form panel" data-index="${i}"><div class="camera-form-head"><h3>${esc(c.name||'New camera')}</h3><button type="button" class="button danger" data-remove-camera="${i}">Remove</button></div><div class="field-grid">
    ${field('id','Camera ID',c.id)}${field('name','Display name',c.name)}${field('host','Host or IP',c.host)}<label>Adapter<select name="adapter"><option value="thingino" ${c.adapter==='thingino'?'selected':''}>Thingino</option><option value="onvif" ${c.adapter==='onvif'?'selected':''}>ONVIF</option></select></label>
    ${field('onvif_port','ONVIF port',c.onvif_port,'number')}${field('username','Username',c.username)}${field('password','Password','','password',c.has_password?'Configured; leave blank to keep':'')}${field('token','HTTP token','','password',c.has_token?'Configured; leave blank to keep':'')}
    ${field('mqtt_topic','MQTT topic',c.mqtt_topic)}<label>Recording stream<select name="record_stream"><option value="main" ${c.record_stream!=='sub'?'selected':''}>Main (ch0)</option><option value="sub" ${c.record_stream==='sub'?'selected':''}>Sub (ch1)</option></select></label>${field('prompt_override','Prompt override',c.prompt_override,'textarea')}
    <label class="switch"><input name="enabled" type="checkbox" ${c.enabled?'checked':''}><span>Enabled</span></label><label class="switch"><input name="needs_credentials" type="checkbox" ${c.needs_credentials?'checked':''}><span>Needs credentials</span></label><label class="switch"><input name="ptz" type="checkbox" ${c.ptz?'checked':''}><span>PTZ</span></label><label class="switch"><input name="sd_redundancy" type="checkbox" ${c.sd_redundancy?'checked':''}><span>SD redundancy</span></label>
    </div><div class="camera-actions"><button type="button" class="button" data-probe-camera="${esc(c.id)}">Probe ONVIF</button><button type="button" class="button" data-sd-camera="${esc(c.id)}">Check SD</button><span class="message" data-camera-result="${esc(c.id)}"></span></div></form>`).join('');
}
function field(name,label,value,type='text',placeholder=''){if(type==='textarea')return`<label class="wide-field">${label}<textarea name="${name}" rows="4">${esc(value)}</textarea></label>`;return`<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}"></label>`;}
function collectCameras(){return [...document.querySelectorAll('.camera-form')].map(form=>{const prior=S.cameras[Number(form.dataset.index)]||{};const result={...prior};for(const el of form.elements){if(!el.name)continue;let value=el.type==='checkbox'?el.checked:el.value;if(el.type==='number')value=Number(value);if((el.name==='password'||el.name==='token')&&!value)continue;result[el.name]=value;}return result;});}
async function saveSettings(){
  const capture=formData('capture-settings'),retentionRaw=formData('retention-settings'),analysis={...formData('analysis-settings'),...collectAlertSettings()};
  const mqtt={...S.settings.mqtt,host:retentionRaw.mqtt_host,port:retentionRaw.mqtt_port,username:retentionRaw.mqtt_username};if(retentionRaw.mqtt_password)mqtt.password=retentionRaw.mqtt_password;
  const retention={days:retentionRaw.days,max_gb:retentionRaw.max_gb,interval_minutes:S.settings.retention.interval_minutes};
  try{S.settings=await api('/api/settings',{method:'PUT',body:JSON.stringify({timezone:S.settings.timezone,capture,retention,mqtt,analysis,cameras:collectCameras()})});$('settings-message').textContent='Settings saved and event sources restarted.';$('settings-message').className='message success';$('analysis-settings-message').textContent='Analysis settings saved';$('alert-settings-message').textContent='Alert settings saved';await loadCameras();renderAnalysisSettings();renderAlertSettings();renderCameraSettings();const eventId=$('event-detail').dataset.eventId;if(eventId&&$('event-dialog').open)await openEvent(eventId);}
  catch(e){$('settings-message').textContent=e.message;$('settings-message').className='message error';}
}
async function discoverCameras(){const out=$('discovery-results');out.textContent='Scanning…';try{const data=await api('/api/cameras/discover',{method:'POST'});out.textContent=data.devices.length?data.devices.map(d=>`${d.host} ${d.scopes.join(' ')}`).join('\n'):'No ONVIF devices answered.';}catch(e){out.textContent=e.message;out.className='message error';}}
function addCamera(){S.cameras.push({id:'new-camera',name:'New camera',host:'',adapter:'onvif',enabled:false,needs_credentials:true,onvif_port:80,username:'',mqtt_topic:'',record_stream:'main',prompt_override:'',ptz:false,sd_redundancy:false,capture:{}});renderCameraSettings();}
async function probeCamera(id){const out=document.querySelector(`[data-camera-result="${CSS.escape(id)}"]`);out.textContent='Probing…';try{const data=await api(`/api/cameras/${id}/probe`);out.textContent=data.ok?`Services: ${data.services.join(', ')}`:data.error;}catch(e){out.textContent=e.message;}}
async function checkSd(id){const out=document.querySelector(`[data-camera-result="${CSS.escape(id)}"]`);out.textContent='Checking SD…';try{const data=await api(`/api/cameras/${id}/sd`);out.textContent=data.supported?(data.mounted===false?'SD not mounted':'SD reachable'):'Not supported';}catch(e){out.textContent=e.message;}}
async function loadLogs(){try{const entries=(await api('/api/logs')).entries;$('log-list').innerHTML=entries.slice().reverse().map(x=>`<div class="log-entry"><time>${formatTime(x.time)}</time><span class="${esc(x.level)}">${esc(x.level)}</span><span>${esc(x.message)}</span></div>`).join('')||'<div class="empty">No log entries.</div>';}catch(e){toast(e.message,true)}}

async function openChatDialog(mediaId){
  S.chatMedia=mediaId;$('chat-prompt-input').value='';$('chat-status').textContent='Loading prompt…';$('chat-response').textContent='';$('chat-response').classList.add('hidden');if(!$('chat-dialog').open)$('chat-dialog').showModal();
  try{const data=await api(`/api/media/${mediaId}/chat`);if(S.chatMedia!==mediaId)return;$('chat-prompt-input').value=data.prompt||'';$('chat-status').textContent='';$('chat-prompt-input').focus();}
  catch(e){$('chat-status').textContent=e.message;}
}
function closeChatDialog(){if($('chat-dialog').open)$('chat-dialog').close();S.chatMedia=null;}
async function submitChat(){
  const promptText=$('chat-prompt-input').value.trim();if(!promptText||!S.chatMedia)return;
  const submit=$('chat-submit-btn'),response=$('chat-response');submit.disabled=true;$('chat-status').textContent='Thinking…';response.classList.add('hidden');
  try{const result=await api(`/api/media/${S.chatMedia}/chat`,{method:'POST',body:JSON.stringify({prompt:promptText})});$('chat-status').textContent='';
    if(result.detections?.length){response.innerHTML=result.detections.map(det=>{const name=det.name?` — ${esc(det.name)}`:'';const confidence=det.confidence!=null?` [${esc(det.confidence)}]`:'';const reasoning=det.reasoning?`<div class="chat-reasoning">${esc(det.reasoning)}</div>`:'';return `<div class="chat-detection"><strong>${esc(String(det.label||'').toLowerCase())}${name}</strong>${confidence}${reasoning}</div>`}).join('');}
    else response.textContent=result.description||result.raw||'(no response)';response.classList.remove('hidden');
  }catch(e){$('chat-status').textContent='';response.textContent=`Error: ${e.message}`;response.classList.remove('hidden');}
  finally{submit.disabled=false;}
}

function bind() {
  $('refresh-events').onclick=loadEvents; $('gallery-camera').onchange=loadEvents;$('gallery-status').onchange=loadEvents;$('gallery-query').oninput=debounce(loadEvents,350);
  $('gallery-grid').onclick=e=>{const card=e.target.closest('[data-event]');if(card)openEvent(card.dataset.event)};
  $('event-dialog').querySelector('.dialog-close').onclick=()=>$('event-dialog').close();
  $('event-dialog').addEventListener('click',async e=>{
    const save=e.target.closest('[data-save-media]');if(save){try{await api(`/api/media/${save.dataset.saveMedia}/save`,{method:'POST'});toast('Saved to Local');}catch(err){toast(err.message,true)}}
    const analyze=e.target.closest('[data-analyze-media]');if(analyze){try{await api(`/api/media/${analyze.dataset.analyzeMedia}/analyze`,{method:'POST'});toast('Analysis complete');const eventId=$('event-detail').dataset.eventId;if(eventId)await openEvent(eventId);}catch(err){toast(err.message,true)}}
    const chat=e.target.closest('[data-chat-media]');if(chat)openChatDialog(chat.dataset.chatMedia);
    const del=e.target.closest('[data-delete-event]');if(del&&confirm('Delete this event from the server? SD copies are not affected.')){await api('/api/events/'+del.dataset.deleteEvent,{method:'DELETE'});$('event-dialog').close();loadEvents();}
  });
  $('local-grid').onclick=async e=>{const b=e.target.closest('[data-delete-saved]');if(b&&confirm('Delete this saved item?')){await api('/api/saved/'+b.dataset.deleteSaved,{method:'DELETE'});loadSaved();}};
  $('live-start').onclick=toggleLive;$('manual-snapshot').onclick=()=>manualCapture('snapshot');$('manual-clip').onclick=()=>manualCapture('clip');$('copy-rtsp').onclick=copyRtspUrl;$('live-camera').onchange=liveSelectionChanged;$('live-hd').onchange=liveSelectionChanged;
  $('live-stage').onclick=e=>{if(!S.liveCamera||!S.livePtz||!e.target.closest('.live-media'))return;$('ptz-pad').classList.toggle('hidden');};
  document.addEventListener('click',e=>{if(!e.target.closest('#ptz-pad')&&!e.target.closest('.live-media'))$('ptz-pad').classList.add('hidden');});
  let clickTimer;document.querySelectorAll('#ptz-pad button').forEach(b=>{b.onclick=e=>{if(e.detail===1)clickTimer=setTimeout(()=>ptz(b.dataset.command,false),210)};b.ondblclick=()=>{clearTimeout(clickTimer);ptz(b.classList.contains('ptz-center')?'home':b.dataset.command,true)}});
  $('save-settings').onclick=saveSettings;$('save-analysis').onclick=saveSettings;$('save-alerts').onclick=saveSettings;$('discover-cameras').onclick=discoverCameras;$('add-camera').onclick=addCamera;$('test-alert').onclick=async()=>{const status=$('test-alert-status');status.textContent='Sending…';try{await api('/api/alerts/test',{method:'POST'});status.textContent='Test email sent';toast('Test email sent')}catch(e){status.textContent=e.message;toast(e.message,true)}};
  $('analysis-settings').elements.namedItem('backend').onchange=()=>{S.settings.analysis.backend=$('analysis-settings').elements.namedItem('backend').value;renderAnalysisSettings()};$('analysis-settings').elements.namedItem('temperature').oninput=e=>$('analysis-temperature-value').textContent=Number(e.target.value).toFixed(2);
  $('camera-settings').onclick=e=>{const remove=e.target.closest('[data-remove-camera]');if(remove){S.cameras.splice(Number(remove.dataset.removeCamera),1);renderCameraSettings();return}const probe=e.target.closest('[data-probe-camera]');if(probe)probeCamera(probe.dataset.probeCamera);const sd=e.target.closest('[data-sd-camera]');if(sd)checkSd(sd.dataset.sdCamera);};
  $('refresh-logs').onclick=loadLogs;
  $('chat-dialog').onclick=e=>{if(e.target===$('chat-dialog'))closeChatDialog()};$('chat-dialog').querySelector('.chat-close-btn').onclick=closeChatDialog;$('chat-submit-btn').onclick=submitChat;$('chat-prompt-input').onkeydown=e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter')submitChat()};
  $('chat-dialog').addEventListener('close',()=>{S.chatMedia=null});
}
function debounce(fn,ms){let t;return()=>{clearTimeout(t);t=setTimeout(fn,ms)}}
function connectEvents(){if(S.evt)S.evt.close();S.evt=new EventSource('/api/updates');S.evt.onmessage=e=>{const data=JSON.parse(e.data);if(data.type.startsWith('event_')||data.type==='capture_progress'||data.type==='analysis_update'){if(S.tab==='gallery')loadEvents();}if(data.type==='saved'&&S.tab==='local')loadSaved();};S.evt.onerror=()=>setTimeout(connectEvents,3000);}

async function init(){initNav();bind();showTab('gallery');await loadSettings();await Promise.all([loadStatus(),loadEvents()]);connectEvents();setInterval(loadStatus,10000);setInterval(()=>{if(S.tab==='logs')loadLogs()},3000);}
document.addEventListener('DOMContentLoaded',init);
