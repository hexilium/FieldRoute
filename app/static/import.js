let importReport = null;
let importPayload = null;
let importVersion = 0;
let importBusy = false;
let addressAuditController = null;
let addressAuditReport = null;
let reviewedProfileDraft = null;
let locationReviewDirty = false;
const locationChoices = new Map();
const appliedLocationChoices = new Map();
let appliedOfficeStartAddress = null;

function importButtons() {
  const planning = document.getElementById('shell').classList.contains('loading');
  document.querySelectorAll('#importPanel input,#inspectCsvBtn,#importExampleBtn,#importNormsExampleBtn,#importRealisticBtn,#importCapabilitiesBtn').forEach(n=>n.disabled=importBusy||planning);
  document.getElementById('applyImportBtn').disabled=importBusy||planning||locationReviewDirty||!importReport?.request;
  for(const id of ['downloadTemplateBtn','downloadReportBtn'])document.getElementById(id).disabled=importBusy||planning||!importReport;
  document.getElementById('downloadRequestBtn').disabled=importBusy||planning||!importReport?.request;
  document.getElementById('inspectAddressesBtn').disabled=importBusy||planning||!!addressAuditController||!importPayload;
  document.getElementById('downloadAddressAuditBtn').disabled=!!addressAuditController||!addressAuditReport;
  document.getElementById('editLocationsBtn').disabled=importBusy||planning||!importReport;
  document.getElementById('applyLocationsBtn').disabled=importBusy||planning||!!addressAuditController||!locationChoices.size;
  document.getElementById('downloadReviewedProfileBtn').disabled=importBusy||locationReviewDirty||!reviewedProfileDraft;
  document.querySelectorAll('#addressAuditRows input,#addressAuditRows select,#officeStartAddress').forEach(n=>n.disabled=importBusy||planning||!!addressAuditController);
  document.getElementById('resetDemoBtn').disabled=importBusy||planning||!currentDataset;
}
function invalidateImport() {
  importVersion++;
  addressAuditController?.abort();addressAuditController=null;addressAuditReport=null;
  reviewedProfileDraft=null;locationReviewDirty=false;locationChoices.clear();
  appliedLocationChoices.clear();appliedOfficeStartAddress=null;
  document.getElementById('locationReviewStatus').textContent='';
  document.getElementById('locationProfileIssues').replaceChildren();
  document.getElementById('locationProfileIssuesPanel').hidden=true;
  document.getElementById('officeStartAddress').replaceChildren(new Option('Сохранить старты инженеров', ''));
  document.getElementById('addressAuditPanel').hidden=true;
  document.getElementById('addressAuditRows').replaceChildren();
  importReport=null;importPayload=null;
  document.getElementById('importResult').hidden=true;
  document.getElementById('importError').hidden=true;
  importButtons();
}
function saveJson(value, filename) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download=filename;a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function importError(error) {
  const node=document.getElementById('importError');node.hidden=false;node.textContent=error.message;
}
function apiDetail(data) {
  if(typeof data.detail==='string')return data.detail;
  if(Array.isArray(data.detail))return data.detail.slice(0,12).map(e=>`${e.loc.slice(1).join('.')}: ${e.msg}`).join('\n');
  return 'Не удалось проверить файл.';
}
function renderImport(report) {
  document.getElementById('importResult').hidden=false;
  const s=report.summary;
  document.getElementById('importSummary').textContent=`${report.filename} · ${report.encoding||'кодировка не определена'} · строк: ${s.total_rows} · заявок: ${s.job_rows} · офисов: ${s.office_rows} · пустых: ${s.blank_rows} · некорректных: ${s.invalid_rows}`;
  const ready=document.getElementById('importReady');
  ready.textContent=report.request?`Набор подготовлен: ${report.request.engineers.length} инженеров, ${report.request.jobs.length} заявок. Можно построить план.`:s.errors?`Ошибок: ${s.errors}. Исправьте данные и повторите проверку.`:'CSV проверен. Для расчёта загрузите заполненный JSON-профиль.';
  ready.className=report.request?'ok':'warn';
  const notes=document.getElementById('importNotes');notes.replaceChildren();
  for(const note of importPayload?.profile?.notes||[])notes.append(textNode('p',note,'muted'));
  const issues=document.getElementById('importIssues');issues.replaceChildren();
  for(const issue of report.issues){
    const item=textNode('li',`${issue.row?`Строка ${issue.row}: `:''}${issue.message}`,issue.severity==='error'?'import-problem':'');
    issues.append(item);
  }
  const history=document.getElementById('crewHistory');history.replaceChildren();
  const crews=report.history?.crews||report.crew_history;
  if(report.history||crews.length){
    history.append(textNode('b',`История бригад${report.history?`: ${report.history.filename}`:''}`));
    history.append(textNode('p',`Найдено бригад: ${crews.length}. Навыки выведены из типов BK/HD; неизвестные типы требуют решения. Исторические назначения не закрепляют новые заявки.`,'muted'));
    if(report.history){
      const s=report.history.summary;
      history.append(textNode('p',`Строк истории: ${s.total_rows}, ошибок: ${s.errors}. Ошибки истории не удаляют строки синтетического набора.`,s.errors?'warn':'muted'));
      for(const issue of report.history.issues.filter(i=>i.severity==='error'||i.code==='duplicate_id'))history.append(textNode('div',`${issue.row?`Строка ${issue.row}: `:''}${issue.message}`,issue.severity==='error'?'import-problem':'warn'));
    }
    for(const crew of crews)history.append(textNode('div',`${crew.name}: ${crew.record_count} записей / ${crew.unique_job_ids} уникальных ID; ${crew.observed_skills.map(s=>skillNames[s]||s).join(', ')||'навык не определён'}${crew.unresolved_rows.length?`; неразобранные строки: ${crew.unresolved_rows.join(', ')}`:''}`));
  }
  const rows=document.getElementById('importRows');rows.replaceChildren();
  const labels={job:'Заявка',office:'Офис',blank:'Пустая',invalid:'Ошибка'};
  for(const item of report.rows){
    const row=document.createElement('tr');
    for(const value of [item.row===item.end_row?item.row:`${item.row}–${item.end_row}`,labels[item.kind],item.source_id||'—',item.address||'—',item.bk_type||'—'])row.append(textNode('td',String(value)));
    rows.append(row);
  }
}
async function runImport(payload, version) {
  const response=await fetch('/api/v1/imports/csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const report=await response.json();
  if(!response.ok)throw new Error(apiDetail(report));
  if(version!==importVersion)return;
  importPayload=payload;importReport=report;renderImport(report);
}
async function fileBase64(file) {
  const bytes=new Uint8Array(await file.arrayBuffer());
  let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
  return btoa(binary);
}
async function inspectAddresses() {
  if(!importPayload||addressAuditController)return;
  const version=importVersion,controller=new AbortController();
  addressAuditController=controller;addressAuditReport=null;
  document.getElementById('addressAuditPanel').hidden=false;
  document.getElementById('addressAuditRows').replaceChildren();
  const status=document.getElementById('addressAuditStatus');
  status.textContent='Ищем адреса заявок и офиса в локальном индексе…';
  importButtons();
  try{
    const {filename,csv_base64}=importPayload;
    const response=await fetch('/api/v1/imports/addresses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename,csv_base64}),signal:controller.signal});
    const result=await response.json();
    if(!response.ok)throw new Error(apiDetail(result));
    if(version!==importVersion||addressAuditController!==controller)return;
    addressAuditReport=result;
    const s=result.summary;
    status.textContent=`Адресов: ${s.addresses}. Один кандидат: ${s.one_candidate}; несколько: ${s.multiple_candidates}; не найдено: ${s.not_found}. ${s.city_omitted?`Для ${s.city_omitted} адресов выполнен дополнительный поиск без слова «Москва»; населённый пункт нужно проверить. `:''}${result.source_summary.errors?`Ошибок исходного CSV: ${result.source_summary.errors}. Некорректные строки не участвуют в поиске. `:''}Даже единственный кандидат требует проверки.`;
    renderLocationReview();
  }catch(error){
    if(version===importVersion&&addressAuditController===controller&&error.name!=='AbortError'){
      status.textContent=error.message+' Можно ввести координаты вручную с указанием источника.';
      renderLocationReview();
    }
  }finally{
    if(addressAuditController===controller){addressAuditController=null;importButtons();}
  }
}
function addressKey(value) {return value.trim().replace(/\s+/g,' ').toLocaleLowerCase('ru');}
function locationReviewItems() {
  if(addressAuditReport)return addressAuditReport.addresses;
  const grouped=new Map();
  for(const row of importReport?.rows||[]){
    if(!['job','office'].includes(row.kind)||!row.address)continue;
    const key=addressKey(row.address);
    if(!grouped.has(key))grouped.set(key,{address:row.address,uses:[],candidates:[]});
    grouped.get(key).uses.push({...row,job_id:row.source_id});
  }
  return [...grouped.values()];
}
function locationChoicesChanged() {
  locationReviewDirty=locationChoices.size>0;
  document.getElementById('locationReviewStatus').textContent=`Выбрано адресов: ${locationChoices.size}. ${locationReviewDirty?'Нажмите «Перенести выбранные координаты», чтобы проверить профиль.':''}`;
  importButtons();
}
function renderLocationReview() {
  document.getElementById('addressAuditPanel').hidden=false;
  const rows=document.getElementById('addressAuditRows');rows.replaceChildren();
  const office=document.getElementById('officeStartAddress'),previousOffice=office.value;
  office.replaceChildren(new Option('Сохранить старты инженеров',''));
  const currentLocations=new Map(Object.entries(reviewedProfileDraft?.locations||importPayload?.profile?.locations||{}).map(([a,l])=>[addressKey(a),l]));
  const labels={one_candidate:'Один кандидат',multiple_candidates:'Несколько кандидатов',not_found:'Не найден'};
  for(const [index,item] of locationReviewItems().entries()){
    const address=item.address,key=addressKey(address),selected=locationChoices.get(key),row=document.createElement('tr');
    row.append(textNode('td',address),textNode('td',item.uses.map(u=>`${u.row===u.end_row?u.row:`${u.row}–${u.end_row}`} · ${u.kind==='office'?'офис':u.job_id}`).join('; ')));
    const status=textNode('td',`${labels[item.status]||'Поиск не выполнен'}${item.city_omitted?' · поиск без города':''}`),existing=currentLocations.get(key);
    if(existing?.lat!=null&&existing?.lon!=null)status.append(textNode('p',`В профиле: ${existing.lat}, ${existing.lon}`));
    row.append(status);
    if(item.uses.some(u=>u.kind==='office'))office.add(new Option(`Все инженеры из офиса: ${address}`,address));
    const cell=document.createElement('td');cell.className='location-review-cell';
    const select=document.createElement('select');select.setAttribute('aria-label',`Координаты: ${address}`);
    select.add(new Option('Выберите вариант',''));
    item.candidates.forEach((c,i)=>select.add(new Option(`${c.label} · ${c.lat}, ${c.lon}`,String(i))));
    select.add(new Option('Ввести координаты вручную','manual'));
    const fields=document.createElement('div');fields.className='location-review-fields';fields.hidden=true;
    const input=(label,type)=>{
      const wrapper=textNode('label',label),n=document.createElement('input');n.type=type;
      n.setAttribute('aria-label',`${label}: ${address}`);wrapper.append(n);fields.append(wrapper);return n;
    };
    const lat=input('Широта','number'),lon=input('Долгота','number'),source=input('Источник координат','text');
    lat.min=-90;lat.max=90;lon.min=-180;lon.max=180;lat.step=lon.step='any';source.maxLength=1000;
    const confirmation=textNode('label',''),check=document.createElement('input');check.type='checkbox';check.id=`locationConfirmed-${index}`;
    confirmation.append(check,document.createTextNode(' Адрес и координаты проверены; перенести в профиль'));
    const error=textNode('p','','warn');error.setAttribute('role','status');
    const choice=()=>{
      if(select.value==='manual'){
        if(!lat.value.trim()||!lon.value.trim()||!lat.checkValidity()||!lon.checkValidity()||!source.value.trim())return null;
        return {address,location:{lat:Number(lat.value),lon:Number(lon.value),label:address},source:source.value.trim(),method:'user_entered_coordinates'};
      }
      const candidate=select.value===''?null:item.candidates[Number(select.value)];
      if(!candidate)return null;
      const fingerprint=addressAuditReport?.index_metadata?.source_sha256;
      return {address,location:{lat:candidate.lat,lon:candidate.lon,label:candidate.label},source:`Локальный индекс OSM: ${candidate.label}${fingerprint?`; SHA-256 OSM: ${fingerprint}`:''}`,method:'user_selected_candidate'};
    };
    const reset=()=>{check.checked=false;locationChoices.delete(key);error.textContent='';locationChoicesChanged();};
    select.onchange=()=>{fields.hidden=select.value!=='manual';reset();};
    for(const n of [lat,lon,source])n.oninput=reset;
    check.onchange=()=>{
      const value=choice();
      if(check.checked&&!value){check.checked=false;error.textContent='Выберите кандидата или заполните широту, долготу и источник.';return;}
      error.textContent='';
      if(check.checked)locationChoices.set(key,value);else locationChoices.delete(key);
      locationChoicesChanged();
    };
    if(selected){
      const candidateIndex=selected.method==='user_selected_candidate'?item.candidates.findIndex(c=>c.lat===selected.location.lat&&c.lon===selected.location.lon&&c.label===selected.location.label):-1;
      select.value=candidateIndex>=0?String(candidateIndex):'manual';fields.hidden=select.value!=='manual';
      lat.value=selected.location.lat;lon.value=selected.location.lon;source.value=selected.source;check.checked=true;
    }
    cell.append(select,fields,confirmation,error);row.append(cell);rows.append(row);
  }
  office.value=previousOffice;
  office.onchange=locationChoicesChanged;
  importButtons();
}
async function applyLocationChoices() {
  if(!importPayload||!locationChoices.size||importBusy)return;
  const version=importVersion;
  importBusy=true;importButtons();
  const status=document.getElementById('locationReviewStatus');status.textContent='Переносим выбранные координаты и проверяем профиль…';
  try{
    const mergedChoices=new Map([...appliedLocationChoices,...locationChoices]);
    const office=document.getElementById('officeStartAddress').value||(!importPayload.profile?appliedOfficeStartAddress:null);
    const payload={...importPayload,source_sha256:importReport.sha256,selections:[...mergedChoices.values()],office_start_address:office};
    const response=await fetch('/api/v1/imports/locations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json();
    if(!response.ok)throw new Error(apiDetail(result));
    if(version!==importVersion)return;
    reviewedProfileDraft=result.profile;locationReviewDirty=false;
    appliedLocationChoices.clear();for(const [key,value] of mergedChoices)appliedLocationChoices.set(key,value);
    locationChoices.clear();appliedOfficeStartAddress=office||appliedOfficeStartAddress;
    document.getElementById('officeStartAddress').value='';
    if(!result.profile_issues.length)importPayload={...importPayload,profile:result.profile};
    importReport=result.report;renderImport(importReport);
    status.textContent=result.profile_issues.length?`Координаты перенесены в черновик. Скачайте профиль и заполните оставшиеся поля (${result.profile_issues.length}).`:importReport.request?'Координаты перенесены, профиль проверен. Можно построить план.':`Координаты перенесены. Исправьте ошибки CSV или недостающие правила и адреса: ${importReport.summary.errors}.`;
    const issues=document.getElementById('locationProfileIssues');issues.replaceChildren();
    for(const issue of result.profile_issues)issues.append(textNode('li',issue));
    document.getElementById('locationProfileIssuesPanel').hidden=!result.profile_issues.length;
    renderLocationReview();
  }catch(error){if(version===importVersion)status.textContent=error.message;}
  finally{importBusy=false;importButtons();}
}
async function inspectImport(example=false, useNorms=false, dataset='training') {
  invalidateImport();
  const version=importVersion;
  importBusy=true;importButtons();
  try {
    let payload;
    if(example){
      const params=new URLSearchParams({dataset,use_organizer_norms:String(useNorms)});
      const response=await fetch(`/api/v1/imports/example?${params}`);
      if(!response.ok)throw new Error('Готовый набор недоступен.');
      payload=await response.json();
      document.getElementById('csvFile').value='';document.getElementById('profileFile').value='';document.getElementById('historyFile').value='';
    }else{
      const file=document.getElementById('csvFile').files[0];
      if(!file)throw new Error('Выберите CSV-файл.');
      if(file.size>1_000_000)throw new Error('CSV должен быть не больше 1 МБ.');
      payload={filename:file.name,csv_base64:await fileBase64(file)};
      const history=document.getElementById('historyFile').files[0];
      if(history){
        if(history.size>1_000_000)throw new Error('Контрольная история должна быть не больше 1 МБ.');
        payload.history={filename:history.name,csv_base64:await fileBase64(history)};
      }
      const profile=document.getElementById('profileFile').files[0];
      if(profile){
        if(profile.size>2_000_000)throw new Error('JSON-профиль должен быть не больше 2 МБ.');
        try{payload.profile=JSON.parse((await profile.text()).replace(/^\uFEFF/,''));}
        catch{throw new Error('JSON-профиль не читается. Проверьте синтаксис JSON и кодировку UTF-8.');}
      }
    }
    await runImport(payload,version);
  }catch(error){if(version===importVersion)importError(error);}
  finally{importBusy=false;importButtons();}
}
document.getElementById('csvFile').onchange=invalidateImport;
document.getElementById('profileFile').onchange=invalidateImport;
document.getElementById('historyFile').onchange=invalidateImport;
document.getElementById('inspectCsvBtn').onclick=()=>inspectImport();
document.getElementById('importExampleBtn').onclick=()=>inspectImport(true);
document.getElementById('importNormsExampleBtn').onclick=()=>inspectImport(true,true);
document.getElementById('importRealisticBtn').onclick=()=>inspectImport(true,false,'realistic');
document.getElementById('importCapabilitiesBtn').onclick=()=>inspectImport(true,false,'capabilities');
document.getElementById('applyImportBtn').onclick=async()=>{
  if(!importReport?.request)return;
  const dataset={name:importReport.filename,request:importReport.request};
  await loadView('plan',dataset);
  if(currentDataset===dataset)document.getElementById('importDetails').open=false;
  importButtons();
};
document.getElementById('resetDemoBtn').onclick=()=>loadView('plan',null);
document.getElementById('downloadTemplateBtn').onclick=()=>saveJson(importReport.profile_template,'import-profile-template.json');
document.getElementById('downloadReportBtn').onclick=()=>saveJson(importReport,'import-report.json');
document.getElementById('downloadRequestBtn').onclick=()=>saveJson(importReport.request,'plan-request.json');
document.getElementById('inspectAddressesBtn').onclick=inspectAddresses;
document.getElementById('downloadAddressAuditBtn').onclick=()=>{if(addressAuditReport)saveJson(addressAuditReport,'address-audit.json')};
document.getElementById('editLocationsBtn').onclick=renderLocationReview;
document.getElementById('applyLocationsBtn').onclick=applyLocationChoices;
document.getElementById('downloadReviewedProfileBtn').onclick=()=>{if(reviewedProfileDraft&&!locationReviewDirty)saveJson(reviewedProfileDraft,'reviewed-import-profile.json')};
importButtons();
