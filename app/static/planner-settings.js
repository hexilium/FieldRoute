// Defaults come from the API. Edits are a draft until a complete plan succeeds.
// This module never changes .env or process-wide settings.
let plannerSettingsCatalog=null;
let plannerSettingsCatalogPromise=null;
let plannerSettingsDirty=false;
const plannerSettingsGroups=[
 ['search','Поиск и вычислительный бюджет',true],
 ['rules','Цели, срочность и районы',true],
 ['routing','Переезды и скорости',false],
 ['weights','Дополнительные коэффициенты',false],
 ['compromise','Начальные значения SLA-компромисса',false],
];
function plannerSettingKey(field){return `${field.section}.${field.name}`}
function plannerSettingId(field){return `setting_${field.section}_${field.name}`}
async function getPlannerSettingsCatalog(){
 if(plannerSettingsCatalog)return plannerSettingsCatalog;
 if(!plannerSettingsCatalogPromise)plannerSettingsCatalogPromise=(async()=>{
  const response=await fetch('/api/v1/settings');
  if(!response.ok)throw new Error('Не удалось получить значения параметров с сервера.');
  const data=await response.json();
  if(data.schema_version!==1||!Array.isArray(data.fields)||!data.factory_defaults||!data.server_defaults)throw new Error('Версия страницы и API настроек не совпадает. Обновите страницу.');
  plannerSettingsCatalog=data;return data;
 })().finally(()=>{plannerSettingsCatalogPromise=null});
 return plannerSettingsCatalogPromise;
}
function plannerValues(request,catalog,plan=null){
 const values=structuredClone(catalog.server_defaults);
 for(const key of Object.keys(values.rules))if(request[key]!==undefined)values.rules[key]=request[key];
 Object.assign(values.execution,plan?.diagnostics?.calculation_settings?.execution||{},request.execution||{});
 Object.assign(values.weights,request.weights||{});
 Object.assign(values.compromise,request.compromise_options||{});
 return values;
}
async function plannerRequestWithSettings(request){
 const catalog=await getPlannerSettingsCatalog();
 // Pin actual runtime defaults into this request so a restart or another
 // server's .env cannot silently change the next event or restored snapshot.
 return {...request,execution:{...catalog.server_defaults.execution,...(request.execution||{})}};
}
function plannerBuildRequest(source,values){
 const request=structuredClone(source);
 Object.assign(request,structuredClone(values.rules));
 request.execution=structuredClone(values.execution);
 request.weights=structuredClone(values.weights);
 request.compromise_options=structuredClone(values.compromise);
 return request;
}
function plannerSetValues(form,values,group=null){
 for(const field of plannerSettingsCatalog.fields){
  if(group!==null&&field.group!==group)continue;
  const control=form.elements.namedItem(plannerSettingKey(field));
  if(control)control.value=values[field.section][field.name]??'';
 }
}
function plannerReadValues(form){
 const values={rules:{},execution:{},weights:{},compromise:{}};
 for(const field of plannerSettingsCatalog.fields){
  const input=form.elements.namedItem(plannerSettingKey(field)),raw=input.value.trim();
  let value=raw;
  if(field.kind!=='select'){
   if(raw===''){
    if(!field.nullable)throw new Error(`${field.title}: заполните значение.`);
    value=null;
   }else{
    value=Number(raw);
    if(!Number.isFinite(value)||(field.kind==='integer'&&!Number.isSafeInteger(value)))throw new Error(`${field.title}: требуется ${field.kind==='integer'?'целое число':'конечное число'}.`);
    if(value<field.min||value>field.max)throw new Error(`${field.title}: допустимо от ${field.min} до ${field.max}.`);
   }
  }else if(!field.choices.some(([key])=>key===value))throw new Error(`${field.title}: неизвестный вариант.`);
  values[field.section][field.name]=value;
 }
 return values;
}
function plannerFieldActive(field,values){
 const runtime=values.execution;
 switch(field.active){
  case 'adaptive':return runtime.search_strategy==='adaptive';
  case 'full':return runtime.search_strategy==='full'&&runtime.search_time_limit_ms===null;
  case 'haversine':return runtime.routing_backend==='haversine';
  case 'fixed_speed':return runtime.routing_backend!=='osrm';
  case 'local_roads':return runtime.routing_backend==='local_roads';
  default:return true;
 }
}
function plannerDraftChanged(form){
 if(!plannerSettingsCatalog||!form._appliedValues)return;
 let values;
 try{values=plannerReadValues(form)}catch(error){
  plannerSettingsDirty=true;
  document.getElementById('plannerDraftSummary').textContent=error.message;
  return;
 }
 let changed=0;
 for(const field of plannerSettingsCatalog.fields){
  const input=form.elements.namedItem(plannerSettingKey(field));
  const differs=values[field.section][field.name]!==form._appliedValues[field.section][field.name];
  input.closest('.planner-setting').classList.toggle('is-changed',differs);
  input.dataset.inactive=String(!plannerFieldActive(field,values));
  input.closest('.planner-setting').classList.toggle('is-inactive',!plannerFieldActive(field,values));
  changed+=differs?1:0;
 }
 plannerSettingsDirty=changed>0;
 document.getElementById('plannerDraftSummary').textContent=changed
  ?`Изменённых параметров: ${changed}. Текущий план и сохранённая копия пока не изменены.`
  :'Показаны применённые параметры текущего плана. Изменений в черновике нет.';
 const warnings=[];
 if(values.execution.search_strategy==='adaptive'){
  warnings.push(`Adaptive: ${values.execution.search_attempt_limit.toLocaleString('ru-RU')} проверок на одну цель. Малый бюджет может оставить заявки неназначенными, даже когда допустимый план существует.`);
  if(values.rules.district_mode==='strict')warnings.push('Этот бюджет делится между районами с инженерами и будущими работами. Неиспользованный остаток другим районам не передаётся.');
 }
 if(values.execution.search_time_limit_ms!==null)warnings.push(`Лимит времени: ${values.execution.search_time_limit_ms} мс. Подготовка дорог и валидация вне этого лимита. 0 не означает отсутствие ограничения.`);
 if(values.execution.routing_backend==='osrm'&&values.execution.search_time_limit_ms!==null)warnings.push('OSRM не поддерживает лимит времени поиска. Очистите это поле перед применением.');
 if(form._appliedValues.rules.district_mode==='strict'&&values.rules.district_mode==='unrestricted')warnings.push('После применения будут разрешены выезды за пределы района. Принадлежность инженеров и заявок не меняется.');
 if(values.rules.service_priority_policy==='numeric')warnings.push('Выбран старый числовой приоритет. Он не гарантирует порядок организаторов: Авария → Подключение → Ремонт / Дозаказ.');
 if(values.rules.emergency_replan_policy==='respect_freeze')warnings.push('Новая авария не сможет снять автоматический горизонт закрепления. Это более консервативно, чем правило организаторов.');
 if(values.execution.routing_backend!==form._appliedValues.execution.routing_backend)warnings.push('Модель переездов изменится только после успешного нового расчёта. Старый план не будет выдан за результат новой модели.');
 document.getElementById('plannerSettingsWarnings').replaceChildren(...warnings.map(text=>textNode('p',text)));
 syncAssumptionsControls();
}
function plannerSettingsMessage(text,error=false){
 const status=document.getElementById('assumptionsStatus');
 if(status){status.textContent=text;status.className=`assumption-status${error?' warn':''}`}
}
async function plannerValidateDocument(values){
 const response=await fetch('/api/v1/settings/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({format:'fieldroute-settings',format_version:1,values})});
 const data=await response.json();
 if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||'Параметры отклонены сервером.'));
 return data;
}
async function applyPlannerSettings(event){
 event.preventDefault();
 const form=event.currentTarget;
 if(assumptionsSaving||!currentRequest||!form.reportValidity())return;
 const source=currentRequest,version=requestVersion;
 assumptionsSaving=true;syncAssumptionsControls();
 plannerSettingsMessage('Проверяем параметры…');
 try{
  const document=await plannerValidateDocument(plannerReadValues(form));
  if(source!==currentRequest||version!==requestVersion)throw new Error('Исходный план изменился. Откройте параметры заново.');
  const request=plannerBuildRequest(source,document.values);
  // Changing preferences does not establish a previous plan, replay history,
  // change district membership or modify jobs, engineers, locks and clocks.
  const dataset=currentDataset?structuredClone(currentDataset):{name:'Демо · свои параметры',events:[]};
  if(!Object.hasOwn(dataset,'origin'))dataset.origin={name:currentDataset?.name||'Демо',request:structuredClone(source)};
  dataset.request=request;
  const selector=globalThis.document.getElementById('policySelect');
  selector.value=request.optimization_policy;updatePolicyHint();
  plannerSettingsMessage('Пересчитываем план. До успешного результата старый план сохранён…');
  const ok=await loadView('plan',dataset);
  if(ok===true||currentDataset===dataset){
   plannerSettingsDirty=false;
   showWorkspaceScreen('assumptions');
   plannerSettingsMessage('Параметры применены. План пересчитан; состояние сохраняется обычным механизмом рабочего набора.');
  }else{
   selector.value=currentRequest.optimization_policy;updatePolicyHint();
   plannerSettingsMessage('Параметры не применены. Старый план сохранён; причина в сообщении об ошибке.',true);
  }
 }catch(error){plannerSettingsMessage(error.message,true)}
 finally{assumptionsSaving=false;syncAssumptionsControls()}
}
function plannerReset(form,group=null,source='factory_defaults'){
 if(group===null&&!window.confirm(source==='factory_defaults'
  ?'Сбросить все поля черновика к значениям приложения? Включая общий режим с выездами и оценку Haversine. Заявки, инженеры, закрепления и текущий план не изменятся до применения.'
  :'Подставить текущие значения сервера в черновик? Изменения начнут действовать только после пересчёта.'))return;
 plannerSetValues(form,plannerSettingsCatalog[source],group);plannerDraftChanged(form);
 plannerSettingsMessage('Значения восстановлены только в черновике. Проверьте их и нажмите «Применить и пересчитать».');
}
function plannerDownload(document){
 const url=URL.createObjectURL(new Blob([JSON.stringify(document,null,2)],{type:'application/json'}));
 const link=globalThis.document.createElement('a');link.href=url;link.download='fieldroute-settings.json';link.click();
 setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function plannerImportFile(file,form){
 if(!file)return;
 if(file.size>100000)throw new Error('Файл параметров должен быть не больше 100 КБ. Это не импорт плана.');
 const source=currentRequest,version=requestVersion;
 const raw=JSON.parse(await file.text());
 if(raw.format!=='fieldroute-settings'||raw.format_version!==1)throw new Error('Нужен файл fieldroute-settings версии 1.');
 // Send the whole document: unknown fields, not only values, must be rejected.
 const response=await fetch('/api/v1/settings/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(raw)});
 const validated=await response.json();
 if(!response.ok)throw new Error(typeof validated.detail==='string'?validated.detail:JSON.stringify(validated.detail));
 if(currentRequest!==source||version!==requestVersion||!form.isConnected)throw new Error('План изменился во время чтения файла. Импорт не применён.');
 plannerSetValues(form,validated.values);plannerDraftChanged(form);
 plannerSettingsMessage('Параметры загружены в черновик. Для изменения плана нужен пересчёт.');
}
function renderPlannerSettings(request,plan){
 const panel=assumptionPanel('Параметры расчёта','Все изменения относятся к текущему набору. Успешно применённые значения сохраняются вместе с планом, переживают перезагрузку и входят в экспорт состояния. Файл .env не меняется.');
 panel.id='plannerSettingsPanel';
 if(!plannerSettingsCatalog){
  const message=textNode('p','Загружаем значения и ограничения параметров…');panel.append(message);
  getPlannerSettingsCatalog().then(()=>{
   if(currentRequest!==request||!panel.isConnected)return;
   assumptionsRenderedRequest=undefined;renderAssumptions();
  }).catch(error=>{
   if(!panel.isConnected)return;
   message.textContent=error.message;
   const retry=textNode('button','Повторить','btn');retry.type='button';
   retry.onclick=()=>{assumptionsRenderedRequest=undefined;renderAssumptions()};panel.append(retry);
  });
  return panel;
 }
 const catalog=plannerSettingsCatalog,applied=plannerValues(request,catalog,plan);
 const form=textNode('form','');form.id='assumptionsForm';form._appliedValues=structuredClone(applied);
 form.addEventListener('submit',applyPlannerSettings);
 form.addEventListener('input',()=>plannerDraftChanged(form));form.addEventListener('change',()=>plannerDraftChanged(form));
 form.addEventListener('invalid',event=>{let parent=event.target.parentElement;while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement}},true);
 const summary=textNode('p','Показаны применённые параметры текущего плана. Изменений в черновике нет.','planner-draft-summary');summary.id='plannerDraftSummary';summary.setAttribute('role','status');form.append(summary);
 const quickApply=textNode('button','Применить и пересчитать','btn primary');quickApply.type='submit';quickApply.id='plannerApplyTop';form.append(quickApply);
 const warnings=textNode('div','','planner-settings-warnings');warnings.id='plannerSettingsWarnings';form.append(warnings);
 const serverSolver=plan?.diagnostics?.calculation_settings?.solver_backend||catalog.server.solver_backend;
 form.append(textNode('p',`Алгоритм сервера: ${serverSolver}. Режимы full/adaptive относятся к insertion; базовый FIFO в сравнении использует собственный алгоритм. Начальные значения из .env можно подставить отдельной кнопкой. Сброс по умолчанию возвращает значения самого приложения.`,'assumption-description'));
 for(const [group,title,open] of plannerSettingsGroups){
  const section=textNode('details','','planner-settings-group');section.open=open;section.id=`plannerGroup_${group}`;
  section.append(textNode('summary',title));
  const grid=textNode('div','','planner-settings-grid');
  for(const field of catalog.fields.filter(item=>item.group===group)){
   const row=textNode('div','','planner-setting'),label=textNode('label',field.title);label.htmlFor=plannerSettingId(field);
   const input=document.createElement(field.kind==='select'?'select':'input');input.id=plannerSettingId(field);input.name=plannerSettingKey(field);
   if(field.kind==='select'){
    for(const [value,label] of field.choices){const option=textNode('option',label);option.value=value;input.append(option)}
   }else{input.type='number';input.min=String(field.min);input.max=String(field.max);input.step=field.kind==='integer'?'1':'any';input.required=!field.nullable;if(field.nullable)input.placeholder='Без ограничения / не задано'}
   input.setAttribute('aria-describedby',`${input.id}_help`);
   const help=textNode('p',field.help,'planner-setting-help');help.id=`${input.id}_help`;
   const value=catalog.factory_defaults[field.section][field.name];
   const display=field.kind==='select'?field.choices.find(([key])=>key===value)?.[1]:(value===null?'пусто':String(value));
   const baseline=textNode('small',`По умолчанию: ${display}.`,'planner-setting-default');
   row.append(label,input,help,baseline);grid.append(row);
  }
  const reset=textNode('button','Восстановить значения раздела','btn');reset.type='button';reset.dataset.resetGroup=group;reset.onclick=()=>plannerReset(form,group);
  section.append(grid,reset);form.append(section);
 }
 const scope=assumptionDetails('Данные и серверные параметры');
 scope.append(textNode('p','Смены, координаты, длительности, окна и SLA отдельных заявок остаются данными набора, не общими настройками. Они не меняются при сбросе этой формы. Справочники и принадлежность к районам редактируются соответствующими инструментами.','assumption-description'));
 scope.append(textNode('p',`Настройки развёртывания здесь не изменяются: ${catalog.server_only.join('; ')}. Это не скрытые коэффициенты оптимизации. Геокодер сервера: ${catalog.server.geocoding_backend}.`,'assumption-description'));form.append(scope);
 const actions=textNode('div','','planner-settings-actions');
 for(const [id,title,action] of [
  ['plannerResetAll','Сбросить всё к умолчаниям',()=>plannerReset(form)],
  ['plannerUseServer','Значения сервера',()=>plannerReset(form,null,'server_defaults')],
  ['plannerDiscard','Отменить изменения',()=>{plannerSetValues(form,applied);plannerDraftChanged(form);plannerSettingsMessage('Черновик возвращён к применённым значениям.')}],
  ['plannerExport','Экспорт параметров',async()=>{try{plannerDownload(await plannerValidateDocument(plannerReadValues(form)))}catch(error){plannerSettingsMessage(error.message,true)}}],
 ]){const button=textNode('button',title,'btn');button.type='button';button.id=id;button.onclick=action;actions.append(button)}
 const label=textNode('label','Импорт параметров','btn file-button'),file=document.createElement('input');file.type='file';file.id='plannerImport';file.accept='.json,application/json';
 file.onchange=async()=>{try{await plannerImportFile(file.files[0],form)}catch(error){plannerSettingsMessage(error.message,true)}finally{file.value=''}};label.append(file);actions.append(label);form.append(actions);
 const footer=textNode('div','','assumption-actions planner-settings-apply'),submit=textNode('button','Применить и пересчитать','btn primary');submit.type='submit';submit.id='assumptionApplyBtn';
 const status=textNode('span','','assumption-status');status.id='assumptionsStatus';status.setAttribute('role','status');footer.append(submit,status);form.append(footer);
 plannerSetValues(form,applied);
 panel.append(form);
 // The form is not in the DOM yet; update state once mounted, not a stale panel.
 queueMicrotask(()=>{if(form.isConnected&&currentRequest===request)plannerDraftChanged(form)});
 return panel;
}
window.addEventListener('beforeunload',event=>{
 if(plannerSettingsDirty&&!assumptionsSaving){event.preventDefault();event.returnValue=''}
});
