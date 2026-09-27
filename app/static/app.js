const policyHints={distance_first:'Сокращаем пробег с учётом выбранных правил срочности. Сроки могут сдвинуться.',staff_first:'Сокращаем число инженеров с учётом выбранных правил срочности. Сроки могут сдвинуться.',sla_first:'Стараемся начать работы вовремя с учётом выбранных правил срочности. Может понадобиться больше инженеров или километров.'};
let currentView='plan';
let currentDataset=null;
let currentRequest=null;
let currentPlan=null;
let requestVersion=0;
let currentComparison=null;
let activeDiff=null;
const changeLabels={added:'Новая заявка',gained_assignment:'Получила назначение',lost_assignment:'Потеря назначения',reassigned:'Другой инженер',reordered:'Другой порядок',rescheduled:'Другое время',completed:'Завершена',cancelled:'Отменена',removed:'Отсутствует в новом плане',reason_changed:'Другая причина неназначения'};
const snapshotLabels={unassigned:'Не назначена',completed:'Завершена',cancelled:'Отменена',absent:'Нет в плане'};
const dateTimeFormat=new Intl.DateTimeFormat('ru-RU',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
const timeFormat=new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit'});
function textNode(tag,text,className=''){const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node}
function appendTime(parent,label,value){
 if(!value)return;
 const row=textNode('div',label,'muted');
 const time=textNode('time',dateTimeFormat.format(new Date(value)));time.dateTime=value;time.title=value;row.append(time);parent.append(row);
}
function snapshotCell(snapshot){
 const cell=document.createElement('td');
 if(snapshot.state==='assigned'){
  cell.append(textNode('b',snapshot.engineer_name||snapshot.engineer_id));
  cell.append(textNode('div',`${snapshot.engineer_id} · визит №${snapshot.position}`,'muted'));
  appendTime(cell,'Прибытие: ',snapshot.arrival);
  appendTime(cell,'Начало: ',snapshot.service_start);
  appendTime(cell,'Окончание: ',snapshot.departure);
 }else{
  cell.append(textNode('b',snapshotLabels[snapshot.state]));
  if(snapshot.state==='unassigned')cell.append(textNode('div',snapshot.explanation||snapshot.reason_codes.join(', '),'muted'));
 }
 return cell;
}
function renderChangeRows(){
 const rows=document.getElementById('changeRows');rows.replaceChildren();
 if(!activeDiff)return;
 const filter=document.getElementById('changeFilter').value;
 const items=activeDiff.items.filter(item=>filter==='all'||item.kinds.includes(filter));
 for(const item of items){
  const row=document.createElement('tr');row.dataset.jobId=item.job_id;
  if(item.kinds.includes('lost_assignment'))row.className='loss-row';
  const job=document.createElement('td');job.append(textNode('b',item.title),textNode('div',item.job_id,'muted'));
  const details=document.createElement('td');
  for(const kind of item.kinds)details.append(textNode('span',changeLabels[kind],'change-kind'));
  const shift=item.start_shift_minutes;
  if(shift)details.append(textNode('div',`Начало ${shift>0?'позже':'раньше'} на ${Math.abs(shift).toLocaleString('ru-RU')} мин`,'muted'));
  row.append(job,snapshotCell(item.before),snapshotCell(item.after),details);rows.append(row);
 }
 document.getElementById('changeTableWrap').hidden=items.length===0;
 const empty=document.getElementById('changeEmpty');empty.hidden=items.length!==0;
 empty.textContent=activeDiff.items.length?'Нет изменений выбранного типа.':'Назначения, относительный порядок, время и статусы заявок не изменились.';
}
function renderChanges(diff,{title,reference,before,after,incident=false}={}){
 activeDiff=diff;
 const changes=document.getElementById('changes');changes.hidden=!diff;
 if(!diff)return;
 document.getElementById('changesTitle').textContent=title;
 document.getElementById('changesReference').textContent=reference;
 document.getElementById('changeBefore').textContent=before;
 document.getElementById('changeAfter').textContent=after;
 document.getElementById('variantControl').hidden=!incident;
 const s=diff.summary;
 const summaryCounts=[`Изменено: ${s.changed_jobs}`];
 if(s.lost_assignments)summaryCounts.push(`Потеряли назначение: ${s.lost_assignments}`);
 if(s.gained_assignments)summaryCounts.push(`Получили назначение: ${s.gained_assignments}`);
 if(s.reassigned_jobs)summaryCounts.push(`Другой инженер: ${s.reassigned_jobs}`);
 document.getElementById('changesSummaryCounts').textContent=summaryCounts.join(' · ');
 changes.open=s.lost_assignments>0;
 const chips=[['Изменились',s.changed_jobs],['Без изменений',s.unchanged_jobs],['Потеря назначения',s.lost_assignments,'loss'],['Получили назначение',s.gained_assignments],['Другой инженер',s.reassigned_jobs],['Другой порядок',s.reordered_jobs],['Другое время',s.rescheduled_jobs],['Новые',s.added_jobs],['Завершены',s.completed_jobs],['Отменены',s.cancelled_jobs],['Отсутствуют',s.removed_jobs],['Другая причина',s.reason_changed_jobs]];
 document.getElementById('changeCounts').replaceChildren(...chips.filter(([,count],i)=>count||i<3).map(([label,count,type])=>textNode('span',`${label}: ${count}`,`change-chip ${count&&type?type:''}`)));
 document.getElementById('changesNote').textContent=`Одна заявка может иметь несколько изменений. Порядок — взаимная перестановка визитов у того же инженера; сдвиг номеров при добавлении или завершении работ не считается перестановкой. Время в таблице: ${dateTimeFormat.resolvedOptions().timeZone}.`;
 const select=document.getElementById('changeFilter'),selected=select.value||'all';
 select.replaceChildren(...[['all','Все изменения'],...Object.entries(changeLabels)].map(([value,label])=>{
  const count=value==='all'?diff.items.length:diff.items.filter(item=>item.kinds.includes(value)).length;
  const option=textNode('option',`${label} (${count})`);option.value=value;return option;
 }));
 select.value=selected;renderChangeRows();
}
function renderIncident(){
 const variant=document.getElementById('variantSelect').value;
 selectedComparisonVariant=variant;
 const labels=comparisonLabels(currentComparison),label=labels[variant==='before'?0:1];
 const plan=currentComparison[variant];
 renderPlan(plan,currentComparison.event,label);
 renderChanges(plan.diff,{title:'Изменения после события',reference:`Утренний план → ${label.toLowerCase()}. Маршруты и показатели соответствуют выбранному варианту.`,before:'Утренний план',after:label,incident:true});
}
function updatePolicyHint(){document.getElementById('policyHint').textContent=policyHints[document.getElementById('policySelect').value]}
function planSummary(m){return `${m.assigned_jobs}/${m.total_jobs} заявок · ${m.used_engineers} инженеров · ${m.total_distance_km.toFixed(1)} км · SLA ${fmtSla(m)}`}

function fmtSla(metrics){return metrics.sla_jobs?fmtPct(metrics.sla_rate):'нет сроков'}
function fmtPct(v){return `${(v*100).toFixed(v===1?0:1)}%`}
function fmtHours(value,signed=false){return value.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2,signDisplay:signed?'exceptZero':'auto'})}
function laborScope(labor){return labor.scope==='remaining'?'Остаток от момента перепланирования':'Весь план'}
function renderKpis(metrics){
 const node=document.getElementById('kpis');
 const hasChanges=!!(metrics.changed_assignments||metrics.frozen_jobs);
 node.classList.toggle('has-changes',hasChanges);
 const items=[
  ['Назначено',`${metrics.assigned_jobs}/${metrics.total_jobs}`],
  ['В срок (SLA)',fmtSla(metrics),metrics.sla_rate>=.99?'ok':'warn'],
  ['Инженеров',metrics.used_engineers],
  ['Общий пробег',`${metrics.total_distance_km.toFixed(1)} км`],
  ['Трудозатраты',metrics.labor?fmtHours(metrics.labor.total_hours):'Нет данных','',true],
 ];
 if(hasChanges)items.push(['Переназначения',metrics.changed_assignments],['Закреплено',metrics.frozen_jobs]);
 node.replaceChildren(...items.map(([label,value,className='',isLabor=false])=>{
  const card=textNode('div','','card kpi');
  const amount=textNode('b',value,className);
  if(isLabor){
   card.classList.add('kpi-labor');
   if(metrics.labor){
    amount.append(textNode('span',' чел·ч','kpi-unit'));
    card.title=`${laborScope(metrics.labor)}. Сумма времени всех инженеров: работа, дорога и ожидание начала визитов.`;
   }else card.title='В сохранённом плане нет данных о трудозатратах. Пересчитайте план, чтобы их получить.';
  }
  card.append(textNode('span',label,'muted'),amount);
  if(isLabor&&metrics.labor?.scope==='remaining')card.append(textNode('span','Оставшееся время','muted kpi-note'));
  return card;
 }));
}
function renderPlan(p, eventInfo=null, variantLabel='Стабильный план'){
 if(typeof closeJobExplanation==='function')closeJobExplanation();
 currentPlan=p;
 const searchStatus=document.getElementById('searchStatus');
 searchStatus.hidden=p.diagnostics?.search_budget?.stop_reason!=='time_limit';
 searchStatus.textContent=searchStatus.hidden?'':'Поиск остановлен по лимиту времени. Показан лучший найденный допустимый план. Дальнейший поиск может улучшить назначения и маршруты.';
 renderKpis(p.metrics);
 renderRoutes(p);
 routeMap.setPlan(p);
 filterRoutes(routeMap.selectedEngineer);
 const event=document.getElementById('event');event.replaceChildren();
 if(typeof renderOverview==='function')renderOverview();
 if(typeof renderDistrictSummary==='function')renderDistrictSummary();
 if(eventInfo){
  const time=eventInfo.time?new Date(eventInfo.time).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}):'—';
  const detail=eventInfo.delay_minutes?`P1 авария + задержка инженера на ${eventInfo.delay_minutes} мин`:'внеплановое событие';
  event.append(textNode('div',`Событие в ${time}: ${detail}. Показан вариант «${variantLabel}».`,'event'));
 }
}
function filterRoutes(id){
 document.querySelectorAll('#routes .route').forEach(node=>{node.hidden=!!id&&node.dataset.engineerId!==id});
}
function highlightJob(id){
 let target=[...document.querySelectorAll('.stop[data-job-id]')].find(node=>node.dataset.jobId===id);
 if(!target)return;
 const route=target.closest('.route');
 if(route?.hidden)routeMap.selectEngineer(null);
 document.querySelectorAll('.stop.selected').forEach(node=>node.classList.remove('selected'));
 target.classList.add('selected');
 const details=target.querySelector('.explain');
 if(details){details.hidden=false;target.querySelector('button').setAttribute('aria-expanded','true')}
 // Scroll only the schedule panel; keep the map itself in view.
 const panel=target.closest('.side');
 if(panel&&panel.scrollHeight>panel.clientHeight)panel.scrollTop+=target.getBoundingClientRect().top-panel.getBoundingClientRect().top-20;
}
function renderRoutes(plan){
 const jobs=new Map((plan.map_data?.jobs||[]).map(job=>[job.job_id,job]));
 const routes=document.getElementById('routes');routes.replaceChildren();
 for(const route of plan.routes){
  const section=textNode('section','','route');section.dataset.engineerId=route.engineer_id;
  const color=routeMap.color(route.engineer_id);
  const heading=textNode('button',route.engineer_name,'route-heading');heading.style.color=color;
  heading.onclick=()=>routeMap.selectEngineer(route.engineer_id);
  section.append(heading,textNode('div',`${route.stops.length} заявок · ${route.total_distance_km.toFixed(1)} км · ${Math.round(route.total_travel_minutes)} мин в пути`,'muted'));
  const travel=plan.diagnostics?.travel_model?.engineers?.[route.engineer_id];
  if(travel){
   const label={car:'Автомобиль',walk:'Пешком',bicycle:'Велосипед',public_transport:'Общественный транспорт'}[travel.mode]||travel.mode;
   section.append(textNode('div',`${label}${travel.speed_kmh==null?' · время по OSRM':` · допущение ${travel.speed_kmh} км/ч`}`,'muted'));
   if(travel.mode==='public_transport' && plan.diagnostics?.travel_model?.kind==='osm_fixed_speed_v1')
    section.append(textNode('div','Условная оценка по автомобильным дорогам, без линий и расписаний транспорта.','muted'));
  }
  if(!route.stops.length)section.append(textNode('p','Нет назначенных заявок.','muted'));
  for(const [index,stop] of route.stops.entries()){
   const card=textNode('div','',`stop ${stop.frozen?'frozen':''}`);card.dataset.jobId=stop.job_id;card.style.borderLeftColor=color;
   const button=textNode('button','','stop-button');button.setAttribute('aria-expanded','false');
   button.append(textNode('b',`${index+1}. ${timeFormat.format(new Date(stop.service_start))}–${timeFormat.format(new Date(stop.departure))} `),textNode('span',stop.title));
   if(stop.frozen)button.append(textNode('span','зафиксировано','badge'));
   if(jobs.get(stop.job_id)?.priority>=80)button.append(textNode('span','срочная','badge p1'));
   const explanation=textNode('div','','explain');explanation.hidden=true;
   for(const line of stop.explanation||[])explanation.append(textNode('div',`• ${line}`));
   if(!explanation.childNodes.length)explanation.textContent='Нет пояснений';
   if(currentRequest)explanation.append(explainButton(stop.job_id,'Проверить назначение'));
   button.onclick=()=>{
    explanation.hidden=!explanation.hidden;button.setAttribute('aria-expanded',String(!explanation.hidden));
    routeMap.focusJob(stop.job_id);
    document.querySelectorAll('.stop.selected').forEach(node=>node.classList.remove('selected'));card.classList.add('selected');
   };
   card.append(button,textNode('div',stop.location.label||stop.job_id,'muted'),explanation);section.append(card);
  }
  routes.append(section);
 }
 const unassigned=document.getElementById('unassigned');unassigned.replaceChildren();
 if(plan.unassigned.length){
  unassigned.append(textNode('h4',`Не назначено: ${plan.unassigned.length}`,'warn'));
  for(const job of plan.unassigned){
   const data=jobs.get(job.job_id);
   const card=textNode('div','','stop unassigned-stop');card.dataset.jobId=job.job_id;
   const button=textNode('button',data?.title||job.job_id,'stop-button');button.onclick=()=>{routeMap.focusJob(job.job_id);highlightJob(job.job_id)};
   card.append(button,textNode('div',data?.location.label||job.job_id,'muted'),textNode('div',job.explanation,'explain'));
   if(currentRequest)card.append(explainButton(job.job_id,'Проверить ограничения'));
   unassigned.append(card);
  }
 }else unassigned.append(textNode('p','Все активные заявки распределены','ok'));
}

function comparisonPair(view){
 const prefix=view==='comparison'?'optimization':view==='policies'?'policies':'stability';
 return [document.getElementById(`${prefix}Before`).value,document.getElementById(`${prefix}After`).value];
}
function comparisonPairIssue(view,before,after){
 if(before===after)return 'Выберите два разных варианта.';
 if(view==='comparison'&&(before!=='baseline'||!['staff_first','distance_first','sla_first'].includes(after)))
  return 'Сравните базовый FIFO с одной из целей оптимизации.';
 if(view==='policies'&&(!['staff_first','distance_first'].includes(before)||after!=='sla_first'))
  return 'Сравните цель по ресурсам или пробегу с целью «Соблюдать SLA».';
 return '';
}
function updateComparisonControls(){
 for(const [view,button] of [['comparison','compareBtn'],['policies','policiesBtn'],['incident','incidentBtn']]){
  const [before,after]=comparisonPair(view),node=document.getElementById(button);
  const issue=comparisonPairIssue(view,before,after);
  node.disabled=document.getElementById('shell').classList.contains('loading')||!currentPlan||!!issue||(view==='incident'&&!!currentDataset);
  node.title=issue||(view==='incident'&&currentDataset?'Сценарий устойчивости доступен в небольшом демо.':'');
 }
}
async function loadView(view,dataset=currentDataset){
 if(view==='event')view='plan';
 if(dataset&&view==='incident')return;
 if(view!=='plan'){
  const [before,after]=comparisonPair(view),issue=comparisonPairIssue(view,before,after);
  if(issue){const node=document.getElementById('error');node.textContent='Сравнение не запущено. '+issue;node.hidden=false;updateComparisonControls();return}
 }
 const version=++requestVersion;
 const previousDataset=currentDataset;
 const policy=document.getElementById('policySelect').value;
 document.getElementById('error').hidden=true;
 setLoading(true);
 try{
  let source=null;
  if(view!=='incident'){
   if(dataset)source=dataset.request;
   else{
    const response=await fetch('/api/v1/demo/request');
    if(!response.ok)throw new Error('Исходный демо-набор недоступен.');
    source=await response.json();
   }
   source={...source,optimization_policy:policy};
   if(typeof districtRequestForCalculation==='function')source=districtRequestForCalculation(source,dataset!==previousDataset);
   if(typeof plannerRequestWithSettings==='function')source=await plannerRequestWithSettings(source);

  }
  let data;
  if(view==='plan')data=await fetchPlanning('/api/v1/plan/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(source)});
  else if(view==='incident'){
   const [before_variant,after_variant]=comparisonPair(view),params=new URLSearchParams({optimization_policy:policy,before_variant,after_variant});
   data=await fetchPlanning(`/api/v1/demo/replanning/stream?${params}`);
  }else{
   const [before_variant,after_variant]=comparisonPair(view);
   data=await fetchPlanning('/api/v1/variants/compare/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:source,before_variant,after_variant})});
  }
  if(version!==requestVersion)return;
  currentView=view;
  currentDataset=dataset;
  // The incident comparison is a self-contained demo result. It has no matching
  // PlanRequest in the response, so it must not replace the persisted workspace.
  currentRequest=source;
  if(dataset&&view==='plan')dataset.request=source;
  if(view!=='plan'&&view!=='incident')currentRequest={...currentRequest,optimization_policy:data.after.diagnostics?.optimization_policy||currentRequest.optimization_policy};
  if(view==='plan'&&(dataset!==previousDataset||!editableCatalogs))setEditableCatalogs(dataset?.catalogs||null,currentRequest);
  document.getElementById('datasetLabel').textContent=dataset?`${dataset.name} · ${dataset.request.engineers.length} инженеров / ${dataset.request.jobs.length} заявок`:'Демо · 3 инженера / 8 заявок';
  document.getElementById('datasetHint').hidden=!dataset;
  currentComparison=view==='plan'?null:data;
  selectedComparisonVariant='after';
  document.getElementById('changeFilter').value='all';
  if(view==='plan'){
   document.getElementById('comparison').classList.remove('show');renderPlan(data);
   renderChanges(data.diff,{title:'Изменения после события',reference:'План до последнего события → текущий план.',before:'До события',after:'Текущий план'});
  }else{
   if(view==='comparison')data.event.comparison_title='Эффект оптимизации';
   if(view==='policies')data.event.comparison_title='Цена соблюдения сроков';
   renderComparison(data);
   if(view==='incident'){
    document.getElementById('variantSelect').value='after';renderIncident();
   }else{
    renderPlan(data.after);
    const labels=comparisonLabels(data);
    renderChanges(data.diff,{title:'Отличия вариантов по заявкам',reference:`${labels[0]} → ${labels[1]}. На карте показан вариант «${labels[1]}».`,before:labels[0],after:labels[1]});
   }
  }
  if(view!=='plan'&&view!=='incident'){document.getElementById('policySelect').value=currentRequest.optimization_policy;updatePolicyHint()}
  for(const [id,name] of Object.entries({baselineBtn:'plan',compareBtn:'comparison',incidentBtn:'incident',policiesBtn:'policies'}))document.getElementById(id).classList.toggle('active',name===view);
  if(typeof refreshEventPanel==='function')refreshEventPanel();
  if(typeof persistWorkspace==='function')await persistWorkspace();
  if(typeof showWorkspaceScreen==='function')showWorkspaceScreen('plan',{focus:true});
  if(typeof syncDistrictMode==='function')syncDistrictMode();
  return true;
 }catch(error){
  if(version===requestVersion){if(currentRequest){document.getElementById('policySelect').value=currentRequest.optimization_policy;updatePolicyHint()}const node=document.getElementById('error');node.textContent='План не обновлён. '+error.message;node.hidden=false;}
  if(typeof syncDistrictMode==='function')syncDistrictMode();
  return false;
 }finally{if(version===requestVersion)setLoading(false)}
}
function setLoading(value){
 if(value)resetPlanningProgress();else clearInterval(progressTimer);
 if(value&&typeof closeJobExplanation==='function')closeJobExplanation();
 document.getElementById('shell').classList.toggle('loading',value);
 document.getElementById('shell').setAttribute('aria-busy',String(value));
 document.getElementById('workspaceBusy').hidden=!value;
 document.querySelectorAll('.actions button,.policy-bar button,.policy-bar select,.change-controls select,.map-toolbar button,.map-toolbar select,.map-toolbar input,.variant-pair select:not([data-fixed])').forEach(control=>control.disabled=value);
 document.getElementById('incidentBtn').disabled=value||!!currentDataset;
 document.getElementById('incidentBtn').title=currentDataset?'Откройте небольшое демо в разделе «Данные».':'';
 if(typeof refreshWorkspace==='function')refreshWorkspace();
 if(typeof importButtons==='function')importButtons();
 if(typeof eventButtons==='function')eventButtons();
 if(typeof stateButtons==='function')stateButtons();
 if(typeof compromiseButtons==='function')compromiseButtons();
 if(typeof refreshCatalogTools==='function')refreshCatalogTools();
 updateComparisonControls();
}
document.getElementById('changeFilter').onchange=renderChangeRows;
document.getElementById('variantSelect').onchange=()=>selectComparisonVariant(document.getElementById('variantSelect').value);
document.getElementById('compareBtn').onclick=()=>loadView('comparison');
document.getElementById('baselineBtn').onclick=()=>loadView('plan');
document.getElementById('incidentBtn').onclick=()=>loadView('incident');
document.getElementById('policiesBtn').onclick=()=>loadView('policies');
document.getElementById('policySelect').onchange=updatePolicyHint;
for(const id of ['optimizationBefore','optimizationAfter','policiesBefore','policiesAfter','stabilityBefore','stabilityAfter'])document.getElementById(id).onchange=updateComparisonControls;
const routeMap=new RouteMap({onEngineer:filterRoutes,onJob:highlightJob});
updatePolicyHint();
