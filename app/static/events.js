const eventNames={urgent_job:'Авария',new_job:'Внеплановая заявка',cancel_job:'Отмена заявки',engineer_delay:'Задержка инженера',engineer_unavailable:'Инженер недоступен',manual_assignment:'Ручное назначение'};
const skillNames={local:'Локальные работы',connection:'Подключения',additional_order:'Дозаказы',emergency:'Аварийные работы',fiber:'Оптика',router:'Маршрутизаторы',voice:'Телефония'};
const transportNames={car:'Автомобиль',walk:'Пешком',bicycle:'Велосипед',public_transport:'Общественный транспорт',metro:'Метро'};
const eventNode=id=>document.getElementById(id);
function localInput(value){
  const date=new Date(value);
  return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);
}
function isoInput(id){return new Date(eventNode(id).value).toISOString()}
function options(id,items){
  const select=eventNode(id),selected=select.value;
  select.replaceChildren(...items.map(([value,label])=>{const node=textNode('option',label);node.value=value;return node}));
  if(items.some(([value])=>value===selected))select.value=selected;
}
function eventButtons(){
  const busy=eventNode('shell').classList.contains('loading');
  const unavailable=!currentRequest||!currentPlan;
  const type=eventNode('eventType').value;
  eventNode('eventForm').querySelectorAll('input,select,button,fieldset').forEach(n=>n.disabled=busy||unavailable);
  eventNode('urgentFields').disabled=busy||unavailable||!['urgent_job','new_job'].includes(type);
  eventNode('cancelJob').disabled=busy||unavailable||type!=='cancel_job';
  eventNode('unavailableEngineer').disabled=busy||unavailable||type!=='engineer_unavailable';
  eventNode('delayEngineer').disabled=busy||unavailable||type!=='engineer_delay';
  eventNode('delayMinutes').disabled=busy||unavailable||type!=='engineer_delay';
  eventNode('urgentPriority').disabled=busy||unavailable||type==='urgent_job'||!['urgent_job','new_job'].includes(type);
  eventNode('applyEventBtn').disabled=busy||unavailable||(type==='cancel_job'&&!eventNode('cancelJob').value)||(type==='engineer_unavailable'&&!eventNode('unavailableEngineer').value)||(type==='engineer_delay'&&!eventNode('delayEngineer').value);
  eventNode('downloadStateBtn').disabled=busy||unavailable;
  eventNode('closeEventBtn').disabled=busy;
  eventNode('resetEventsBtn').disabled=busy||!currentDataset?.origin;
  eventNode('applyEventTemplateBtn').disabled=busy||unavailable||!eventNode('eventTemplateSelect').value;
  window.urgentLocationPicker?.syncButtons();
}
function updateEventFields(){
  const type=eventNode('eventType').value;
  const jobEvent=['urgent_job','new_job'].includes(type);
  eventNode('urgentDistrict').required=jobEvent&&currentRequest?.district_mode==='strict';
  eventNode('urgentFields').hidden=!jobEvent;
  eventNode('cancelFields').hidden=type!=='cancel_job';
  eventNode('unavailableFields').hidden=type!=='engineer_unavailable';
  eventNode('delayFields').hidden=type!=='engineer_delay';
  eventNode('eventJobLegend').textContent=type==='urgent_job'?'Аварийная заявка':'Внеплановая заявка';
  if(type==='urgent_job')eventNode('urgentPriority').value=100;
  const time=new Date(eventNode('eventTime').value).getTime();
  const started=new Set((currentPlan?.routes||[]).flatMap(r=>r.stops.filter(s=>new Date(s.service_start).getTime()<=time).map(s=>s.job_id)));
  options('cancelJob',(currentRequest?.jobs||[]).filter(j=>['pending','assigned'].includes(j.status)&&!started.has(j.id)).map(j=>[j.id,`${j.id} · ${j.title}`]));
  options('unavailableEngineer',(currentRequest?.engineers||[]).filter(e=>e.available).map(e=>[e.id,e.name]));
  options('delayEngineer',(currentRequest?.engineers||[]).filter(e=>e.available).map(e=>[e.id,e.name]));
  eventNode('eventEligibility').textContent=type==='cancel_job'?(eventNode('cancelJob').value?'В списке только заявки, работа по которым ещё не начнётся к указанному времени.':'На это время нет заявок, доступных для отмены. Измените время события.'):jobEvent?'Выберите найденный адрес, точку на карте или введите координаты вручную.':type==='engineer_delay'?(!eventNode('delayEngineer').value?'В плане нет доступных инженеров.':'Задержка временно сдвигает доступность инженера.'):(!eventNode('unavailableEngineer').value?'В плане нет доступных инженеров.':'');
  eventButtons();
}
function syncEventCatalogOptions(){
  const templates=editableCatalogs?.templates||[],templateItems=templates.map(item=>[item.id,`${item.title} · ${item.service_minutes} мин`]);
  for(const id of ['eventTemplateSelect','dataEventTemplate'])options(id,templateItems);
  const skills=editableCatalogs?.skills||[];
  if(skills.length)options('urgentSkill',skills.map(item=>[item.id,item.label]));
}
function refreshEventPanel(){
  window.urgentLocationPicker?.reset();
  eventNode('eventDraftStatus').textContent='';
  const history=currentDataset?.events||[];
  eventNode('eventState').textContent=currentRequest?`Состояние на ${dateTimeFormat.format(new Date(currentRequest.planning_time))} · событий: ${history.length}`:'Открыт сценарий P1. Для своих событий нажмите «Пересчитать план».';
  const zone=dateTimeFormat.resolvedOptions().timeZone;
  eventNode('eventTimeLabel').textContent=`Время события (${zone})`;
  eventNode('eventHistory').replaceChildren(...history.map(event=>{
    const target=event.job?.title||event.job_id||currentRequest?.engineers.find(e=>e.id===event.engineer_id)?.name||event.engineer_id;
    const assigned=event.type==='manual_assignment'?` → ${currentRequest?.engineers.find(e=>e.id===event.engineer_id)?.name||event.engineer_id}`:'';
    const delay=event.type==='engineer_delay'?` · ${event.delay_minutes} мин`:'';
    return textNode('li',`${dateTimeFormat.format(new Date(event.time))} · ${eventNames[event.type]||event.type} · ${target}${delay}${assigned}`);
  }));
  eventNode('resetEventsBtn').hidden=!history.length;
  eventNode('resetEventsBtn').title=currentDataset?.origin?'':'В этом состоянии нет исходного набора для сброса.';
  eventNode('eventOutcome').textContent=history.length&&!currentDataset?.origin
    ?'Исходный набор отсутствует в загруженном файле. Сброс событий недоступен; новые события можно применять.'
    :currentDataset?.eventOutcome||'';
  eventNode('eventError').hidden=true;
  if(currentRequest){
    eventNode('urgencyPolicy').value=currentRequest.urgency_policy||'urgent_first';
    eventNode('urgentStartPolicy').value=currentRequest.urgent_start_policy||'after_primary';
    const windowRules=[...new Set(currentRequest.jobs.map(j=>j.window_semantics||'start'))];
    eventNode('urgentWindowSemantics').value=windowRules.length===1?windowRules[0]:'start';
    const time=new Date(currentRequest.planning_time).getTime()+15*60000;
    eventNode('eventTime').value=localInput(time);
    eventNode('eventTime').min=localInput(currentRequest.planning_time);
    eventNode('urgentStart').value=localInput(time);
    eventNode('urgentEnd').value=localInput(time+120*60000);
    eventNode('urgentSla').value='';
    eventNode('urgentDistrict').value='';
    let number=1;while(currentRequest.jobs.some(j=>j.id===`urgent-${number}`||j.id===`extra-${number}`))number++;
    eventNode('urgentId').value=`urgent-${number}`;
    const skills=editableCatalogs?.skills||[...new Set(currentRequest.engineers.flatMap(e=>e.skills))].sort().map(id=>({id,label:skillNames[id]||id}));
    options('urgentSkill',skills.map(s=>[s.id,s.label]));
    const transports=[...new Set(currentRequest.engineers.flatMap(e=>e.transport_modes))].sort();
    options('urgentTransport',[['','Любой'],...transports.map(t=>[t,transportNames[t]||t])]);
  }
  syncEventCatalogOptions();
  updateEventFields();
}
function applyEventTemplateDraft(template){
  if(!template)return;
  eventNode('eventType').value=template.priority>=80?'urgent_job':'new_job';
  updateEventFields();
  eventNode('urgentTitle').value=template.title;
  eventNode('urgentDuration').value=template.service_minutes;
  eventNode('urgentSkill').value=template.skill;
  eventNode('urgentPriority').value=template.priority;
  eventNode('urgentEquipment').value=(template.equipment||[]).join(', ');
  const prefix=template.priority>=80?'urgent':'extra';let number=1;
  while(currentRequest?.jobs.some(job=>job.id===`${prefix}-${number}`))number++;
  eventNode('urgentId').value=`${prefix}-${number}`;
  eventButtons();
}
function applyEventDraft(draft){
  const value=draft?.event||draft;
  const supported=['urgent_job','new_job','cancel_job','engineer_delay','engineer_unavailable'];
  if(!value||typeof value!=='object'||!supported.includes(value.type))throw new Error('JSON должен содержать поддерживаемый тип события.');
  eventNode('eventType').value=value.type;
  if(value.type==='new_job')eventNode('urgentPriority').value=50;
  if(value.time){const date=new Date(value.time);if(Number.isNaN(date.getTime()))throw new Error('В JSON указано некорректное время события.');eventNode('eventTime').value=localInput(date)}
  updateEventFields();
  if(['urgent_job','new_job'].includes(value.type)){
    const job=value.job;if(!job||typeof job!=='object')throw new Error('Для новой заявки в JSON нужен объект job.');
    if(job.id!=null)eventNode('urgentId').value=job.id;
    if(job.title!=null)eventNode('urgentTitle').value=job.title;
    eventNode('urgentDistrict').value=job.district_id||'';
    if(job.service_minutes!=null)eventNode('urgentDuration').value=job.service_minutes;
    if(job.priority!=null)eventNode('urgentPriority').value=job.priority;
    if(job.required_skills?.[0]!=null)eventNode('urgentSkill').value=job.required_skills[0];
    if(job.required_transport!=null)eventNode('urgentTransport').value=job.required_transport;
    eventNode('urgentEquipment').value=(job.required_equipment||[]).join(', ');
    if(job.window_semantics!=null)eventNode('urgentWindowSemantics').value=job.window_semantics;
    if(job.sla_deadline)eventNode('urgentSla').value=localInput(job.sla_deadline);
    const window=job.time_windows?.[0];if(window){eventNode('urgentStart').value=localInput(window.start);eventNode('urgentEnd').value=localInput(window.end)}
    if(job.location){if(job.location.label!=null)eventNode('urgentAddress').value=job.location.label;if(job.location.lat!=null)eventNode('urgentLat').value=job.location.lat;if(job.location.lon!=null)eventNode('urgentLon').value=job.location.lon}
  }else if(value.type==='cancel_job')eventNode('cancelJob').value=value.job_id||'';
  else if(value.type==='engineer_delay'){eventNode('delayEngineer').value=value.engineer_id||'';eventNode('delayMinutes').value=value.delay_minutes||30}
  else eventNode('unavailableEngineer').value=value.engineer_id||'';
  updateEventFields();
}
function openEventDialog({type='urgent_job',template=null,draft=null}={}){
  if(!currentRequest||!currentPlan)return;
  refreshEventPanel();eventNode('eventType').value=type;
  if(type==='new_job')eventNode('urgentPriority').value=50;
  updateEventFields();
  if(template)applyEventTemplateDraft(template);
  if(draft)applyEventDraft(draft);
  eventNode('eventDetails').showModal();window.urgentLocationPicker?.showMapIfOpen();
}
async function loadEventDraftFile(input,statusId){
  const status=eventNode(statusId);status.textContent='';
  try{
    const file=input.files[0];
    if(!file)return;if(file.size>100_000)throw new Error('JSON-черновик должен быть не больше 100 КБ.');
    const draft=JSON.parse((await file.text()).replace(/^\uFEFF/,''));openEventDialog({draft});
    status.textContent='Черновик загружен. Проверьте поля и примените событие.';
  }catch(error){status.textContent='Черновик не загружен. '+error.message}
  finally{input.value=''}
}
async function submitEvent(event){
  event.preventDefault();
  if(!currentRequest||!currentPlan)return;
  window.urgentLocationPicker?.cancel();
  const version=++requestVersion;
  eventNode('eventError').hidden=true;
  setLoading(true);
  try{
    const type=eventNode('eventType').value;
    const action={type,time:isoInput('eventTime')};
    if(['urgent_job','new_job'].includes(type)){
      if(isoInput('urgentEnd')<=isoInput('urgentStart'))throw new Error('Конец окна должен быть позже начала.');
      action.job={id:eventNode('urgentId').value.trim(),title:eventNode('urgentTitle').value.trim(),
        ...(eventNode('urgentDistrict')?.value.trim()?{district_id:eventNode('urgentDistrict').value.trim()}:{}),
        location:{lat:Number(eventNode('urgentLat').value),lon:Number(eventNode('urgentLon').value),label:eventNode('urgentAddress').value.trim()},
        service_minutes:Number(eventNode('urgentDuration').value),required_skills:[eventNode('urgentSkill').value],
        window_semantics:eventNode('urgentWindowSemantics').value,
        required_transport:eventNode('urgentTransport').value||null,
        required_equipment:eventNode('urgentEquipment').value.split(',').map(s=>s.trim()).filter(Boolean),
        time_windows:[{start:isoInput('urgentStart'),end:isoInput('urgentEnd')}],priority:Number(eventNode('urgentPriority').value),
        sla_deadline:eventNode('urgentSla').value?isoInput('urgentSla'):null};
    }else if(type==='cancel_job')action.job_id=eventNode('cancelJob').value;
    else if(type==='engineer_delay'){
      action.engineer_id=eventNode('delayEngineer').value;action.delay_minutes=Number(eventNode('delayMinutes').value);
    }else action.engineer_id=eventNode('unavailableEngineer').value;
    const request={...currentRequest,urgency_policy:eventNode('urgencyPolicy').value,
      urgent_start_policy:eventNode('urgentStartPolicy').value};
    const result=await fetchPlanning('/api/v1/events/replan/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request,plan:currentPlan,event:action})});
    if(version!==requestVersion)return;
    const previousDataset=currentDataset;
    const origin=previousDataset&&Object.hasOwn(previousDataset,'origin')
      ?previousDataset.origin:{name:previousDataset?.name||'Демо',request:currentRequest};
    currentRequest=result.request;
    currentDataset={name:previousDataset?.name||'Демо',request:result.request,
      origin,
      dataset_kind:previousDataset?.dataset_kind||(previousDataset?'custom':'demo'),
      catalogs:currentCatalogSnapshot(),
      events:[...(previousDataset?.events||[]),result.event],
      eventOutcome:`В этом шаге завершено: ${result.completed_job_ids.length}; выполняется: ${result.in_progress_job_ids.length}; снято автоматических закреплений: ${result.released_freeze_job_ids.length}. ${result.notices.at(-1)}`};
    currentView='event';currentComparison=null;
    eventNode('datasetLabel').textContent=`${currentDataset.name} · ${currentRequest.engineers.length} инженеров / ${currentRequest.jobs.length} заявок`;
    eventNode('datasetHint').hidden=false;
    eventNode('comparison').classList.remove('show');
    eventNode('error').hidden=true;
    document.querySelectorAll('.actions button,.policy-bar button').forEach(n=>n.classList.remove('active'));
    renderPlan(result.plan);
    renderChanges(result.plan.diff,{title:'Изменения после события',reference:'Показанный план до события → новый план. Показатели относятся к оставшимся активным заявкам.',before:'До события',after:'После события'});
    refreshEventPanel();
    eventNode('eventDetails').close();
    showWorkspaceScreen('plan',{focus:true});
    if(typeof persistWorkspace==='function')await persistWorkspace();
  }catch(error){
    if(version===requestVersion){eventNode('eventError').textContent='Событие не применено. '+error.message;eventNode('eventError').hidden=false;}
  }finally{if(version===requestVersion)setLoading(false)}
}
eventNode('eventType').onchange=()=>{if(eventNode('eventType').value==='new_job'&&Number(eventNode('urgentPriority').value)>=80)eventNode('urgentPriority').value=50;updateEventFields()};
eventNode('eventTime').onchange=updateEventFields;
eventNode('eventForm').onsubmit=submitEvent;
eventNode('resetEventsBtn').onclick=async()=>{
  if(!currentDataset?.origin)return;
  const catalogs=currentCatalogSnapshot();
  const origin=currentDataset.origin,isDemo=currentDataset.dataset_kind==='demo';
  if(!isDemo){await loadView('plan',{...origin,catalogs});return}
  await loadView('plan',null);
  setEditableCatalogs(catalogs,currentRequest);
  await persistWorkspace();
};
eventNode('applyEventTemplateBtn').onclick=()=>applyEventTemplateDraft(catalogTemplate(eventNode('eventTemplateSelect').value));
eventNode('dataManualEventBtn').onclick=()=>openEventDialog();
eventNode('dataTemplateEventBtn').onclick=()=>openEventDialog({template:catalogTemplate(eventNode('dataEventTemplate').value)});
eventNode('eventJsonFile').onchange=()=>loadEventDraftFile(eventNode('eventJsonFile'),'eventDraftStatus');
eventNode('dataEventJsonFile').onchange=()=>loadEventDraftFile(eventNode('dataEventJsonFile'),'dataEventStatus');
refreshEventPanel();
