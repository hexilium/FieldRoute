// Explicit dispatch-area labels. No geocoding, guessing or implicit shared pool.
function districtNode(id){return document.getElementById(id)}
function districtRequestForCalculation(source,newDataset=false){
 const mode=newDataset?(source.district_mode||'unrestricted'):(districtNode('districtMode')?.value||source.district_mode||'unrestricted');
 return {...source,district_mode:mode};
}
function syncDistrictMode(){
 if(districtNode('districtModeHint'))districtNode('districtModeHint').textContent='Текущий режим: '+(currentRequest?.district_mode==='strict'?'каждый район отдельно, без выездов.':'выезды разрешены.');
 if(districtNode('districtMode'))districtNode('districtMode').value=currentRequest?.district_mode||'unrestricted';
}
function districtTemplate(request){
 return {version:1,engineer_districts:Object.fromEntries(request.engineers.map(e=>[e.id,e.district_id||null])),
  job_districts:Object.fromEntries(request.jobs.filter(j=>!['completed','cancelled'].includes(j.status)).map(j=>[j.id,j.district_id||null])),
  reference:{engineers:request.engineers.map(e=>({id:e.id,name:e.name})),jobs:request.jobs.filter(j=>!['completed','cancelled'].includes(j.status)).map(j=>({id:j.id,title:j.title,address:j.location?.label||null}))}};
}
function applyDistrictMapping(request,mapping){
 if(!mapping||mapping.version!==1)throw new Error('Нужен файл распределения version: 1.');
 for(const name of Object.keys(mapping))if(!['version','engineer_districts','job_districts','reference'].includes(name))throw new Error(`Неизвестное поле: ${name}`);
 const result=structuredClone(request);
 for(const [field,items] of [['engineer_districts',result.engineers],['job_districts',result.jobs]]){
  const values=mapping[field];
  if(!values||typeof values!=='object'||Array.isArray(values))throw new Error(`Поле ${field} должно быть объектом ID: район.`);
  const ids=new Set(items.map(x=>x.id));
  for(const id of Object.keys(values))if(!ids.has(id))throw new Error(`Неизвестный ID в ${field}: ${id}`);
  for(const item of items){
   if(Object.hasOwn(values,item.id)){
    const value=values[item.id];
    if(value!==null&&(typeof value!=='string'||!value.trim()||value.trim().length>120))throw new Error(`Некорректный район у ${item.id}. Нужна строка длиной 1–120 символов.`);
    item.district_id=value===null?null:value.trim();
   }
   if(!item.district_id&&!['completed','cancelled'].includes(item.status))throw new Error(`Не указан район у ${item.id}. Заполните распределение.`);
  }
 }
 result.district_mode='strict';
 return result;
}
function renderDistrictSummary(){
 const panel=districtNode('districtSummary'),report=currentPlan?.diagnostics?.districts;
 if(!panel)return;
 panel.hidden=!report;
 panel.replaceChildren();
 if(!report)return;
 panel.append(textNode('h2',report.mode==='strict'?'План по отдельным районам':'Районы в общем плане'));
 const scope=report.mode==='strict'?'Инженеры обслуживают заявки только своего района.':'Выезды разрешены. Визитов вне своего района: '+report.cross_district_jobs+'.';
 panel.append(textNode('p',scope+(report.unclassified_assigned_jobs?` Район не определён у ${report.unclassified_assigned_jobs} назначений; они не считаются подтверждёнными выездами.`:''),'muted'));
 panel.append(metricTable(['Район','Назначено / всего','В SLA / со сроком','Свои занятые инженеры','Приехавшие инженеры','Визиты извне','Пробег к заявкам, км'],report.rows.map(row=>({
  label:row.district_id??'Не указан',values:[`${row.assigned_jobs} / ${row.total_jobs}`,row.sla_jobs?`${row.sla_met_jobs} / ${row.sla_jobs}`:'Нет сроков',`${row.used_home_engineers} / ${row.engineers}`,row.visiting_engineer_ids.length,row.inbound_jobs,numberRu(row.total_distance_km,2)]
 }))));
 panel.append(textNode('p','Пробег отнесён к району следующей заявки, без возврата после последней работы. Приехавший инженер может учитываться в нескольких строках, их нельзя складывать как число людей. Запрет выездов ограничивает назначения, но не геометрию проезда по дорогам.','muted tiny'));
 const search=currentPlan.diagnostics.district_search;
 if(search?.parts?.length){
  panel.append(textNode('p',`Независимых расчётов: ${search.parts.length}. Заданные бюджеты поиска разделены между районами, а не умножены на их число.`,'muted tiny'));
  const stopped=search.parts.filter(p=>['attempt_limit','time_limit'].includes(p.diagnostics?.adaptive_search?.stop_reason)||p.diagnostics?.search_budget?.stop_reason==='time_limit');
  if(stopped.length)panel.append(textNode('p',`Лимит поиска исчерпан в ${stopped.length} районах. Неназначенная заявка при остановке поиска не означает доказанную невозможность её выполнить.`,'event warn'));
 }

}
function districtMappingError(error){const node=districtNode('districtMappingError');node.hidden=false;node.textContent=error.message}
function openDistrictSetup(){
 if(!currentRequest)return;
 districtNode('districtMappingError').hidden=true;
 if(currentRequest.previous_plan||currentDataset?.events?.length){
  const node=document.getElementById('error');node.textContent='Распределение по районам задаётся до событий. Откройте исходный набор, чтобы не менять принадлежность уже выполняемых работ и историю.';node.hidden=false;return;
 }
 districtNode('districtMapping').value=JSON.stringify(districtTemplate(currentRequest),null,2);
 districtNode('districtDialog').showModal();
}
async function compareDistrictModes(){
 if(!currentRequest)return;
 const version=++requestVersion;
 const source=structuredClone(currentRequest);
 const policy=districtNode('policySelect').value;source.optimization_policy=policy;
 document.getElementById('error').hidden=true;setLoading(true);
 try{
  const result=await fetchPlanning('/api/v1/districts/compare/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(source)});
  if(version!==requestVersion)return;
  // Comparing a hard assignment constraint must not silently permit crossings.
  // Keep the live plan/request and saved state until explicit variant selection.
  currentComparison=result;currentView='districts';selectedComparisonVariant=null;
  renderComparison(result);renderOverview();
  const labels=comparisonLabels(result);
  renderChanges(result.diff,{title:'Предпросмотр: выезды за пределы района',reference:`${labels[0]} → ${labels[1]}. Текущий план и его режим пока не изменены.`,before:labels[0],after:labels[1]});
  refreshEventPanel();refreshWorkspace();
 }catch(error){if(version===requestVersion){const node=document.getElementById('error');node.textContent='Сравнение не выполнено. '+error.message;node.hidden=false}}
 finally{if(version===requestVersion)setLoading(false)}
}
districtNode('districtSetupBtn').onclick=openDistrictSetup;
districtNode('districtCloseBtn').onclick=()=>districtNode('districtDialog').close();
districtNode('districtCompareBtn').onclick=compareDistrictModes;
districtNode('districtMode').onchange=()=>{
 districtNode('districtModeHint').textContent='Выбор ещё не применён к текущему плану. Нажмите «Рассчитать план»; события используют режим уже выбранного плана.';
};
districtNode('districtDownloadBtn').onclick=()=>{
 const url=URL.createObjectURL(new Blob([districtNode('districtMapping').value],{type:'application/json'}));
 const a=document.createElement('a');a.href=url;a.download='fieldroute-districts.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
districtNode('districtFile').onchange=async event=>{
 try{
  const file=event.target.files[0];if(!file)return;
  if(file.size>3_000_000)throw new Error('Файл распределения слишком большой (более 3 МБ).');
  const text=await file.text();JSON.parse(text);districtNode('districtMapping').value=text;
  districtNode('districtMappingError').hidden=true;
 }catch(error){districtMappingError(error)}finally{event.target.value=''}
};
districtNode('districtApplyBtn').onclick=async()=>{
 try{
  const request=applyDistrictMapping(currentRequest,JSON.parse(districtNode('districtMapping').value));
  const dataset={name:currentDataset?.name||'Набор по районам',request,events:[],catalogs:typeof currentCatalogSnapshot==='function'?currentCatalogSnapshot():null};
  districtNode('districtDialog').close();
  await loadView('plan',dataset);
 }catch(error){districtMappingError(error)}
};

districtNode('districtDemoBtn').onclick=async()=>{
 document.getElementById('error').hidden=true;
 try{
  const response=await fetch('/api/v1/demo/districts/request');
  if(!response.ok)throw new Error('Не удалось открыть учебный набор.');
  const request=await response.json();
  await loadView('plan',{name:'Синтетическая Москва по районам',request,events:[]});
 }catch(error){const node=document.getElementById('error');node.textContent=error.message;node.hidden=false}
};

districtNode('districtSmallDemoBtn').onclick=async()=>{
 document.getElementById('error').hidden=true;
 try{
  const response=await fetch('/api/v1/demo/districts/small');
  if(!response.ok)throw new Error('Не удалось открыть контрольный пример.');
  const request=await response.json();
  await loadView('plan',{name:'Два учебных района: 3 заявки и 2 инженера',request,events:[]});
 }catch(error){const node=document.getElementById('error');node.textContent=error.message;node.hidden=false}
};
