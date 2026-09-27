// A preview is valid only for the exact request, plan and engineer that produced it.
let manualAssignment=null;
const assignmentNode=id=>document.getElementById(id);
function assignmentCurrent(session){
 return !!session&&manualAssignment===session&&session.source===currentRequest&&session.plan===currentPlan&&session.version===requestVersion;
}
function assignmentBusy(){return assignmentNode('shell').classList.contains('loading')}
function assignmentCountDelta(value){return value>0?`+${value}`:String(value)}
function assignmentEligible(session){
 const job=session?.source.jobs.find(item=>item.id===session.jobId);
 return !!job&&!['in_progress','completed','cancelled'].includes(job.status);
}
function assignmentButtons(){
 const session=manualAssignment;
 const disabled=!assignmentCurrent(session)||!assignmentEligible(session)||assignmentBusy();
 assignmentNode('assignmentEngineer').disabled=disabled;
 assignmentNode('previewAssignmentBtn').disabled=disabled||!!session?.controller||!assignmentNode('assignmentEngineer').value;
 assignmentNode('applyAssignmentBtn').disabled=disabled||!session?.preview?.accepted||session?.engineerId!==assignmentNode('assignmentEngineer').value;
 assignmentNode('assignmentActions').hidden=!session?.controller&&!session?.preview&&!session?.hasResult;
 assignmentNode('cancelAssignmentBtn').textContent=session?.controller?'Отменить проверку':'Сбросить проверку';
}
function closeManualAssignment(){
 manualAssignment?.controller?.abort();manualAssignment=null;
 assignmentNode('assignmentPreview').replaceChildren();assignmentNode('assignmentPreview').hidden=true;
 assignmentNode('assignmentStatus').textContent='';
 assignmentButtons();
}
function clearAssignmentPreview(message=''){
 const session=manualAssignment;if(!session)return;
 session.controller?.abort();session.controller=null;session.preview=null;session.engineerId=null;session.hasResult=false;
 assignmentNode('assignmentPreview').replaceChildren();assignmentNode('assignmentPreview').hidden=true;
 assignmentNode('assignmentStatus').textContent=message;
 assignmentNode('assignmentStatus').className='';assignmentButtons();
}
function openManualAssignment(jobId){
 closeManualAssignment();
 const session={jobId,source:currentRequest,plan:currentPlan,version:requestVersion,controller:null,preview:null,engineerId:null,hasResult:false};
 manualAssignment=session;
 const owner=currentPlan.routes.find(route=>route.stops.some(stop=>stop.job_id===jobId))?.engineer_id;
 const select=assignmentNode('assignmentEngineer');
 select.replaceChildren(...currentRequest.engineers.map(engineer=>{
  const label=`${engineer.name} (${engineer.id})${engineer.id===owner?' · текущий исполнитель':''}${engineer.available?'':' · недоступен'}`;
  const option=textNode('option',label);option.value=engineer.id;return option;
 }));
 if(owner)select.value=owner;
 const job=currentRequest.jobs.find(item=>item.id===jobId);
 assignmentNode('assignmentEligibility').textContent=!assignmentEligible(session)
  ?({in_progress:'Работа уже выполняется. Ручное назначение недоступно.',completed:'Заявка завершена. Ручное назначение недоступно.',cancelled:'Заявка отменена. Ручное назначение недоступно.'}[job?.status]||'Заявка отсутствует в исходных данных.')
  :!currentRequest.engineers.length?'В наборе нет инженеров.':'Время планирования останется прежним. В остальных заявках сохранятся исполнители и относительный порядок; время визитов может сдвинуться.';
 assignmentButtons();
}
function assignmentStop(plan,jobId){
 for(const route of plan.routes){const index=route.stops.findIndex(stop=>stop.job_id===jobId);if(index>=0)return {route,stop:route.stops[index],position:index+1}}
 return null;
}
function assignmentTable(caption,rows){
 const wrapper=textNode('div','','assignment-table-wrap'),table=textNode('table','','assignment-table');
 table.append(textNode('caption',caption));
 const head=textNode('thead'),heading=textNode('tr');
 for(const label of ['Показатель','Сейчас','После применения','Изменение']){const cell=textNode('th',label);cell.scope='col';heading.append(cell)}
 head.append(heading);table.append(head);
 const body=textNode('tbody');
 for(const [label,before,after,delta] of rows){
  const row=textNode('tr'),name=textNode('th',label);name.scope='row';row.append(name);
  for(const value of [before,after,delta])row.append(textNode('td',value));body.append(row);
 }
 table.append(body);wrapper.append(table);return wrapper;
}
function renderAssignmentPreview(session,result){
 const container=assignmentNode('assignmentPreview');container.replaceChildren();container.hidden=false;
 if(result.scope)container.append(textNode('p',result.scope,'muted'));
 if(!result.accepted){
  const list=textNode('ul','','assignment-notices');
  for(const blocker of result.blockers||[]){
   const stage=blocker.stage==='source_route'?'Исходный маршрут: ':blocker.stage==='target_route'?'Маршрут выбранного инженера: ':'';
   const job=blocker.job_id&&blocker.job_id!==session.jobId?`Заявка ${blocker.job_id}. `:'';
   list.append(textNode('li',`${stage}${job}${blocker.message}`));
  }
  container.append(list);
 }else{
  const before=assignmentStop(session.plan,session.jobId),after=assignmentStop(result.plan,session.jobId);
  const time=value=>value?dateTimeFormat.format(new Date(value)):'Не назначена';
  const shift=before?(new Date(after.stop.service_start)-new Date(before.stop.service_start))/60000:null;
  container.append(assignmentTable('Заявка и расписание',[
   ['Исполнитель',before?`${before.route.engineer_name} (${before.route.engineer_id})`:'Не назначена',`${after.route.engineer_name} (${after.route.engineer_id})`,before?.route.engineer_id===after.route.engineer_id?'Закрепляется текущий':'Закрепляется выбранный'],
   ['Начало',time(before?.stop.service_start),time(after.stop.service_start),shift===null?'Новое назначение':`${signed(shift)} мин`],
   ['Окончание',time(before?.stop.departure),time(after.stop.departure),''],
  ]));
  const old=session.plan.metrics,next=result.plan.metrics;
  const rows=[
   ['Назначено заявок',old.assigned_jobs,next.assigned_jobs,assignmentCountDelta(next.assigned_jobs-old.assigned_jobs)],
   ['Инженеров',old.used_engineers,next.used_engineers,assignmentCountDelta(next.used_engineers-old.used_engineers)],
   ['Пробег, км',old.total_distance_km.toFixed(2),next.total_distance_km.toFixed(2),signed(next.total_distance_km-old.total_distance_km)],
   ['В пути, мин',old.total_travel_minutes.toFixed(1),next.total_travel_minutes.toFixed(1),signed(next.total_travel_minutes-old.total_travel_minutes)],
   ['Соблюдены SLA',`${old.sla_met_jobs}/${old.sla_jobs}`,`${next.sla_met_jobs}/${next.sla_jobs}`,assignmentCountDelta(next.sla_met_jobs-old.sla_met_jobs)],
  ];
  if(old.labor&&next.labor)rows.push(['Трудозатраты, чел·ч',fmtHours(old.labor.total_hours),fmtHours(next.labor.total_hours),fmtHours(next.labor.total_hours-old.labor.total_hours,true)]);
  container.append(assignmentTable('Последствия для всего плана',rows));
  const changed=(result.plan.diff?.items||[]).filter(item=>item.job_id!==session.jobId&&item.kinds.includes('rescheduled'));
  if(changed.length){
   const details=textNode('details');details.append(textNode('summary',`Изменится расписание других заявок: ${changed.length}`));
   const list=textNode('ul','','assignment-notices');
   for(const item of changed){
    const times=[['прибытие','arrival'],['начало','service_start'],['окончание','departure']]
     .filter(([,key])=>new Date(item.before[key]).getTime()!==new Date(item.after[key]).getTime())
     .map(([label,key])=>`${label} ${time(item.before[key])} → ${time(item.after[key])}`);
    list.append(textNode('li',`${item.job_id} · ${item.title}: ${times.join('; ')}`));
   }
   details.append(list);container.append(details);
  }
  container.append(textNode('p','После применения исполнитель будет закреплён за заявкой. При пересчёте это назначение сохраняется. Проверьте изменения: ручной выбор может ухудшить показатели выбранной цели.','muted'));
 }
 if(result.notices?.length){
  const list=textNode('ul','','assignment-notices');
  for(const notice of result.notices)list.append(textNode('li',notice));
  container.append(list);
 }
}
async function previewManualAssignment(event){
 event.preventDefault();
 const session=manualAssignment;
 if(!assignmentCurrent(session)||!assignmentEligible(session)||assignmentBusy())return;
 clearAssignmentPreview();
 const engineerId=assignmentNode('assignmentEngineer').value;if(!engineerId)return;
 const controller=new AbortController();session.controller=controller;session.engineerId=engineerId;
 assignmentNode('assignmentStatus').textContent='Проверяем назначение и расписание. Показанный план пока не изменён.';
 assignmentButtons();
 try{
  const response=await fetch('/api/v1/assignments/preview',{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify({request:session.source,plan:session.plan,job_id:session.jobId,engineer_id:engineerId})});
  const result=await response.json();
  if(controller.signal.aborted||!assignmentCurrent(session)||session.controller!==controller||assignmentNode('assignmentEngineer').value!==engineerId)return;
  if(!response.ok)throw new Error(typeof apiDetail==='function'?apiDetail(result):typeof result.detail==='string'?result.detail:'Не удалось проверить назначение.');
  if(result.accepted&&(!result.request||!result.plan||!result.event||!assignmentStop(result.plan,session.jobId)))throw new Error('Сервер вернул неполный результат проверки.');
  session.preview=result;session.hasResult=true;
  renderAssignmentPreview(session,result);
  assignmentNode('assignmentStatus').textContent=result.accepted?'Назначение допустимо. Проверьте изменения и нажмите «Применить назначение».':'Назначение недопустимо. Причины указаны ниже; выберите другого инженера.';
  assignmentNode('assignmentStatus').className=result.accepted?'ok':'warn';
 }catch(error){
  if(!controller.signal.aborted&&assignmentCurrent(session)&&session.controller===controller){
   session.hasResult=true;session.preview=null;
   assignmentNode('assignmentStatus').textContent=`Проверка не выполнена: ${error.message}`;assignmentNode('assignmentStatus').className='warn';
  }
 }finally{
  if(assignmentCurrent(session)&&session.controller===controller){session.controller=null;assignmentButtons()}
 }
}
async function applyManualAssignment(){
 const session=manualAssignment;
 if(!assignmentCurrent(session)||!assignmentEligible(session)||assignmentBusy()||!session.preview?.accepted||session.engineerId!==assignmentNode('assignmentEngineer').value){
  clearAssignmentPreview('План или исполнитель изменились. Повторите проверку назначения.');return;
 }
 // Capture the checked result before setLoading closes the dialog and aborts its UI session.
 const result=session.preview,previousDataset=currentDataset;
 const origin=previousDataset&&Object.hasOwn(previousDataset,'origin')?previousDataset.origin:{name:previousDataset?.name||'Демо',request:session.source};
 const version=++requestVersion;
 setLoading(true);
 try{
  currentRequest=result.request;
  currentDataset={name:previousDataset?.name||'Демо',request:result.request,origin,
   dataset_kind:previousDataset?.dataset_kind||(previousDataset?'custom':'demo'),
   catalogs:typeof currentCatalogSnapshot==='function'?currentCatalogSnapshot():null,
   events:[...(previousDataset?.events||[]),result.event],
   eventOutcome:`Заявка ${session.jobId} закреплена за инженером ${result.request.engineers.find(engineer=>engineer.id===session.engineerId)?.name||session.engineerId}. Время планирования сохранено.`};
  currentView='event';currentComparison=null;
  assignmentNode('datasetLabel').textContent=`${currentDataset.name} · ${currentRequest.engineers.length} инженеров / ${currentRequest.jobs.length} заявок`;
  assignmentNode('datasetHint').hidden=false;assignmentNode('comparison').classList.remove('show');assignmentNode('error').hidden=true;
  document.querySelectorAll('.actions button,.policy-bar button').forEach(node=>node.classList.remove('active'));
  renderPlan(result.plan);
  renderChanges(result.plan.diff,{title:'Изменения после ручного назначения',reference:'Показанный план → проверенное ручное назначение. Время планирования осталось прежним.',before:'До назначения',after:'После назначения'});
  refreshEventPanel();showWorkspaceScreen('plan',{focus:true});
  if(typeof persistWorkspace==='function')await persistWorkspace();
 }finally{if(version===requestVersion)setLoading(false)}
}
assignmentNode('assignmentForm').onsubmit=previewManualAssignment;
assignmentNode('assignmentEngineer').onchange=()=>clearAssignmentPreview('Исполнитель выбран. Проверьте назначение перед применением.');
assignmentNode('cancelAssignmentBtn').onclick=()=>clearAssignmentPreview('Проверка отменена. Показанный план не изменён.');
assignmentNode('applyAssignmentBtn').onclick=applyManualAssignment;
