const stateStore=new StateStore();
const maxStateBytes=10*1024*1024;
let stateRevision=null;
let persistencePaused=false;
let persistenceQueue=Promise.resolve();
let pendingRecovery=null;
const stateNode=id=>document.getElementById(id);
const distanceModelNames={haversine_estimate:'Оценка по прямой с коэффициентом',osm_fixed_speed:'Дороги OpenStreetMap и фиксированная скорость',osrm_road:'Автомобильные маршруты OSRM'};

function stateButtons(){
  const busy=stateNode('shell').classList.contains('loading');
  for(const id of ['stateFile','restoreSavedBtn','recalculateSavedBtn','downloadBlockedStateBtn','keepCurrentPlanBtn'])stateNode(id).disabled=busy;
  stateNode('keepCurrentPlanBtn').hidden=!pendingRecovery||!currentRequest||!currentPlan;
  stateNode('stateRecovery').hidden=!pendingRecovery;
  // There is no active plan while an incompatible saved snapshot awaits recovery.
  for(const id of ['compareBtn','policiesBtn','baselineBtn'])stateNode(id).disabled=busy||!currentPlan;
  stateNode('incidentBtn').disabled=busy||!currentPlan||!!currentDataset;
  stateNode('resetDemoBtn').disabled=busy||(!currentDataset&&!!currentPlan);
}
function stateStatus(text,warning=false){
  const node=stateNode('persistenceStatus');node.textContent=text;node.className=warning?'warn':'muted';
}
function stateError(error){
  const node=stateNode('stateError');node.textContent=error.message;node.hidden=false;
}
function workspaceDocument(){
  if(!currentRequest||!currentPlan)return null;
  const name=currentDataset?.name||'Демо';
  const datasetKind=currentDataset?.dataset_kind||(currentDataset?'custom':'demo');
  const origin=currentDataset&&Object.hasOwn(currentDataset,'origin')
    ?currentDataset.origin:{name,request:currentRequest};
  return {format:'fieldroute-state',format_version:1,name,saved_at:new Date().toISOString(),
    request:currentRequest,plan:currentPlan,events:currentDataset?.events||[],origin,
    dataset_kind:datasetKind,catalogs:currentCatalogSnapshot()};
}
function persistWorkspace(){
  const document=workspaceDocument();
  const save=()=>persistWorkspaceDocument(document);
  // Rapid variant switches share a revision chain rather than looking like
  // conflicting writes from separate tabs. Capture each selected plan now.
  persistenceQueue=persistenceQueue.then(save,save);
  return persistenceQueue;
}
async function persistWorkspaceDocument(document){
  if(persistencePaused)return;
  if(!document){
    stateStatus('Сценарий сравнения P1 не сохраняется. Последний рабочий план остаётся в браузере.');return;
  }
  const json=JSON.stringify(document);
  try{
    if(new Blob([json]).size>maxStateBytes)throw new Error('Состояние превышает лимит 10 МиБ.');
    const row=await stateStore.write(json,stateRevision);
    stateRevision=row.revision;
    stateStatus(`Сохранено в этом браузере в ${timeFormat.format(new Date(document.saved_at))}. После обновления страницы план восстановится.`);
  }catch(error){
    persistencePaused=true;
    stateStatus(error instanceof StateConflictError
      ?'Другая вкладка сохранила новый план. Автосохранение приостановлено. Скачайте свой план или нажмите «Восстановить сохранённое».'
      :'Автосохранение недоступно. Показанный план можно скачать; предыдущая сохранённая копия не заменена.',true);
  }
}
class WorkspaceValidationError extends Error {
  constructor(result){super(apiDetail(result));this.code=result.code;this.savedModel=result.saved_distance_model;this.currentModel=result.current_distance_model}
}
async function validateWorkspace(json){
  if(new Blob([json]).size>maxStateBytes)throw new Error('Состояние должно быть не больше 10 МиБ.');
  const response=await fetch('/api/v1/states/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:json});
  const result=await response.json();
  if(!response.ok)throw new WorkspaceValidationError(result);
  return result;
}
function offerStateRecovery(json,row,error){
  if(error.code!=='distance_model_mismatch')return false;
  const document=JSON.parse(json.replace(/^\uFEFF/,''));
  pendingRecovery={json,expectedRevision:row?.revision??stateRevision,name:document.name||'Сохранённый сценарий'};
  if(row)stateRevision=row.revision;
  persistencePaused=true;
  stateNode('stateError').hidden=true;
  stateNode('stateRecoverySummary').textContent=`«${pendingRecovery.name}»: ${document.request.jobs.length} заявок, ${document.request.engineers.length} инженеров. ${row?'Автосохранение':'Открытый файл'} использует другую модель расстояний.`;
  stateNode('stateRecoveryModels').textContent=`В копии: ${distanceModelNames[error.savedModel]||error.savedModel}. Сейчас на сервере: ${distanceModelNames[error.currentModel]||error.currentModel}.`;
  stateNode('stateRecoveryStatus').textContent='';
  stateStatus('Исходная копия сохранена. Выберите пересчёт или скачайте её; автосохранение пока приостановлено.',true);
  if(!currentPlan){
    stateNode('datasetLabel').textContent=`Ожидает восстановления: ${pendingRecovery.name}`;
    showWorkspaceScreen('data');
  }
  stateButtons();return true;
}
function clearStateRecovery(){pendingRecovery=null;stateNode('stateRecovery').hidden=true;stateNode('stateRecoveryStatus').textContent=''}
function downloadSnapshot(json,filename){
  // Download the original bytes, not the unrelated plan currently on screen.
  const url=URL.createObjectURL(new Blob([json],{type:'application/json;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download=filename;link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function refreshStateBackups(){
  try{
    const backups=await stateStore.readBackups();
    stateNode('stateBackups').hidden=!backups.length;
    stateNode('stateBackupCount').textContent=`(${backups.length})`;
    stateNode('stateBackupList').replaceChildren(...backups.map(backup=>{
      let name='Сохранённый план';
      try{name=JSON.parse(backup.json.replace(/^\uFEFF/,'')).name||name}catch{}
      const item=document.createElement('div');
      const download=textNode('button','Скачать копию','btn');download.type='button';
      download.onclick=()=>downloadSnapshot(backup.json,`fieldroute-backup-${backup.id}.json`);
      item.append(textNode('span',`${name} · резервная копия от ${dateTimeFormat.format(new Date(backup.created_at))}`),download);return item;
    }));
  }catch{
    // A backup-list read error must not change a successfully restored plan.
    stateNode('stateBackups').hidden=false;
    stateNode('stateBackupList').textContent='Не удалось прочитать список резервных копий. Повторите восстановление или обновите страницу.';
  }
}
function displayWorkspace(document,{recalculated=false}={}){
  currentRequest=document.request;
  const pristineDemo=document.dataset_kind==='demo'&&!document.events.length;
  currentDataset=pristineDemo?null:{name:document.name,request:document.request,events:document.events,origin:document.origin,catalogs:document.catalogs,dataset_kind:document.dataset_kind||'custom'};
  setEditableCatalogs(document.catalogs||null,document.request);
  currentView='plan';currentComparison=null;
  stateNode('policySelect').value=document.request.optimization_policy;updatePolicyHint();
  if(typeof syncDistrictMode==='function')syncDistrictMode();
  stateNode('datasetLabel').textContent=`${document.name} · ${document.request.engineers.length} инженеров / ${document.request.jobs.length} заявок`;
  stateNode('datasetHint').hidden=pristineDemo;
  stateNode('comparison').classList.remove('show');
  stateNode('error').hidden=true;
  stateNode('stateError').hidden=true;
  for(const id of ['baselineBtn','compareBtn','incidentBtn','policiesBtn'])stateNode(id).classList.toggle('active',id==='baselineBtn');
  renderPlan(document.plan);
  renderChanges(document.plan.diff,{title:recalculated?'Изменения после пересчёта модели':'Изменения сохранённого плана',reference:recalculated?'Предыдущий план → план с текущей моделью расстояний. История событий сохранена.':'Предыдущий план → сохранённый план. Расписание восстановлено без нового расчёта.',before:'Предыдущий план',after:'Сохранённый план'});
  refreshEventPanel();
  stateNode('eventDetails').close();
  showWorkspaceScreen('plan',{focus:true});
}
async function recoverWorkspace({keepCurrent=false}={}){
  if(!pendingRecovery||stateNode('shell').classList.contains('loading'))return;
  const recovery=pendingRecovery,displayed=keepCurrent?workspaceDocument():null;
  if(keepCurrent&&!displayed)return;
  const version=++requestVersion;
  setLoading(true);
  stateNode('stateRecoveryStatus').textContent=keepCurrent?'Проверяем показанный план…':'Пересчитываем исходные данные сохранённого плана…';
  try{
    await persistenceQueue;
    // Detect a newer tab's save before doing a potentially expensive calculation;
    // the same revision is also checked atomically at commit time.
    const stored=await stateStore.read();
    if((stored?.revision||null)!==recovery.expectedRevision)throw new StateConflictError();
    const result=keepCurrent?await validateWorkspace(JSON.stringify(displayed))
      :await fetchPlanning('/api/v1/states/recalculate/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:recovery.json});
    if(version!==requestVersion)return;
    const json=JSON.stringify(result);
    if(new Blob([json]).size>maxStateBytes)throw new Error('Пересчитанный план превышает лимит 10 МиБ.');
    const row=await stateStore.replaceWithBackup(json,recovery.expectedRevision,recovery.json);
    stateRevision=row.revision;persistencePaused=false;
    clearStateRecovery();
    displayWorkspace(result,{recalculated:!keepCurrent});
    stateStatus(`${keepCurrent?'Показанный план сохранён':'Сохранённый план пересчитан по текущей модели расстояний'}. Исходная копия доступна в «Данные → Резервные копии». Автосохранение включено.`);
    await refreshStateBackups();
  }catch(error){
    if(version!==requestVersion)return;
    stateNode('stateRecoveryStatus').textContent=error instanceof StateConflictError
      ?'Другая вкладка сохранила новый план. Ничего не заменено. Нажмите «Восстановить сохранённое» в разделе «Данные», чтобы прочитать актуальную копию.'
      :`${error.message} Исходная копия не заменена; можно повторить попытку или скачать её.`;
  }finally{if(version===requestVersion)setLoading(false)}
}
async function loadWorkspace(file=null){
  const version=++requestVersion;
  stateNode('stateError').hidden=true;
  setLoading(true);
  let json,row;
  try{
    await persistenceQueue;
    if(file){
      if(file.size>maxStateBytes)throw new Error('Файл состояния должен быть не больше 10 МиБ.');
      try{json=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(await file.arrayBuffer())}
      catch{throw new Error('Файл состояния должен быть JSON в кодировке UTF-8.')}
    }else{
      row=await stateStore.read();
      if(!row)throw new Error('В этом браузере ещё нет сохранённого плана.');
      json=row.json;
    }
    const result=await validateWorkspace(json);
    if(version!==requestVersion)return;
    // A compatible import can also resolve a pending recovery. Preserve the
    // browser snapshot and the blocked source before enabling autosave again.
    if(pendingRecovery){
      const normalized=JSON.stringify(result);
      if(new Blob([normalized]).size>maxStateBytes)throw new Error('Состояние превышает лимит 10 МиБ.');
      const saved=await stateStore.replaceWithBackup(normalized,row?.revision??stateRevision,pendingRecovery.json);
      stateRevision=saved.revision;
      if(row)row=saved;
    }
    clearStateRecovery();displayWorkspace(result);
    if(row){
      stateRevision=row.revision;persistencePaused=false;
      stateStatus(`Восстановлена копия от ${dateTimeFormat.format(new Date(result.saved_at))}. Автосохранение включено.`);
    }else{
      persistencePaused=false;await persistWorkspace();
    }
    await refreshStateBackups();
  }catch(error){
    if(version===requestVersion&&!offerStateRecovery(json,row,error))stateError(new Error('Состояние не загружено. '+error.message));
  }finally{
    stateNode('stateFile').value='';
    if(version===requestVersion)setLoading(false);
  }
}
async function initializeWorkspace(){
  setLoading(true);
  let restored=false,blocked=false,json,row;
  try{
    row=await stateStore.read();
    if(row){
      stateRevision=row.revision;json=row.json;
      const result=await validateWorkspace(json);
      displayWorkspace(result);restored=true;
      stateStatus(`Восстановлена копия от ${dateTimeFormat.format(new Date(result.saved_at))}. Автосохранение включено.`);
    }
  }catch(error){
    blocked=offerStateRecovery(json,row,error);
    if(!blocked){
      persistencePaused=true;
      stateStatus('Сохранённая копия не заменена. Автосохранение приостановлено; можно повторить восстановление или открыть JSON.',true);
      stateError(new Error('Не удалось восстановить состояние. '+error.message));
    }
  }
  try{if(!restored&&!blocked)await loadView('plan')}
  finally{setLoading(false);await refreshStateBackups()}
}
stateNode('stateFile').onchange=()=>{const file=stateNode('stateFile').files[0];if(file)loadWorkspace(file)};
stateNode('restoreSavedBtn').onclick=()=>loadWorkspace();
stateNode('downloadStateBtn').onclick=()=>{const data=workspaceDocument();if(data)saveJson(data,'fieldroute-state.json')};
stateNode('downloadBlockedStateBtn').onclick=()=>{if(pendingRecovery)downloadSnapshot(pendingRecovery.json,'fieldroute-original-state.json')};
stateNode('recalculateSavedBtn').onclick=()=>recoverWorkspace();
stateNode('keepCurrentPlanBtn').onclick=()=>recoverWorkspace({keepCurrent:true});
initializeWorkspace();
