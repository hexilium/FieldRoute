// Resource limits belong to one explicit preview, never silently to future events.
let compromiseSource=null;
const compromiseNode=id=>document.getElementById(id);
function compromiseButtons(){
 const busy=compromiseNode('shell').classList.contains('loading');
 if(!busy&&compromiseSource&&compromiseSource.version!==requestVersion){compromiseSource=null;compromiseNode('compromisePanel').hidden=true}
 compromiseNode('openCompromiseBtn').disabled=busy||!currentRequest||!currentPlan||currentView==='incident';
 compromiseNode('closeCompromiseBtn').disabled=busy;
 compromiseNode('compromiseForm').querySelectorAll('input,select,button').forEach(node=>{
  node.disabled=busy||!compromiseSource||compromiseSource.version!==requestVersion;
 });
}
function updateCompromiseLimits(){
 if(!compromiseSource)return;
 const km=Number(compromiseNode('compromiseDistance').value),extra=Number(compromiseNode('compromiseEngineers').value);
 const m=compromiseSource.plan.metrics;
 const limit=m.total_distance_km*(1+km/100);
 const engineers=Math.min(compromiseSource.request.engineers.length,m.used_engineers+extra);
 compromiseNode('compromiseLimits').textContent=Number.isFinite(limit)&&Number.isInteger(extra)
  ?`Ориентир лимитов: до ${numberRu(limit,2)} км и ${engineers} инженеров. Точный предел сервер считает по неокруглённым расстояниям исходных маршрутов.`:'';
}
function openCompromise(variant=null){
 if(!currentRequest||!currentPlan||currentView==='incident')return;
 const useVariant=variant&&currentComparison&&['before','after'].includes(variant);
 const plan=useVariant?currentComparison[variant]:currentPlan;
 const label=useVariant?comparisonLabels(currentComparison)[variant==='before'?0:1]:selectedPlanLabel();
 compromiseSource={request:structuredClone(currentRequest),plan:structuredClone(plan),label,version:requestVersion};
 // A preview card may use a different district rule from the live plan.
 if(plan.diagnostics?.districts?.mode)compromiseSource.request.district_mode=plan.diagnostics.districts.mode;
 // Workspace defaults initialize this separate form; edits in it remain local.
 const defaults=compromiseSource.request.compromise_options;
 if(defaults){
  for(const [field,id] of Object.entries({neighborhood:'compromiseNeighborhood',extra_distance_percent:'compromiseDistance',extra_engineers:'compromiseEngineers',target_sla_percent:'compromiseTarget',attempt_limit:'compromiseAttempts'})){
   if(Object.hasOwn(defaults,field))compromiseNode(id).value=defaults[field]??'';
  }
 }
 compromiseNode('compromiseReference').textContent=`Исходный вариант: ${label}. ${planSummary(plan.metrics)}`;
 compromiseNode('compromiseError').hidden=true;
 compromiseNode('compromisePanel').hidden=false;
 updateCompromiseLimits();compromiseButtons();
 compromiseNode('compromisePanel').scrollIntoView({block:'nearest',behavior:'smooth'});
}
function renderCompromiseReport(plan){
 const d=plan.diagnostics.compromise;
 const section=textNode('section','','compromise-report');section.setAttribute('aria-label','Проверка пределов компромисса');
 section.append(textNode('h3','Результат поиска в пределах ресурсов'));
 const limit=d.limits;
 const line=`Назначено: ${plan.metrics.assigned_jobs}, тот же набор работ. Пробег: ${numberRu(d.selected.distance_km,2)} / ${numberRu(limit.max_distance_km,2)} км. Инженеров: ${plan.metrics.used_engineers} / ${limit.max_used_engineers}.`;
 section.append(textNode('p',line));
 const target=d.target_sla_jobs;
 section.append(textNode('p',!d.sla_jobs?'У заявок нет сроков SLA; поиск не запускался.':target===null
  ?`Своевременно: ${plan.metrics.sla_met_jobs} из ${d.sla_jobs}. Ищем максимум SLA в пределах лимитов; оптимум не доказан.`
  :`Цель: ${target} из ${d.sla_jobs} в SLA. Найдено: ${plan.metrics.sla_met_jobs}. ${d.target_reached?'Цель достигнута.':'Цель не достигнута за этот поиск. Это не доказательство недостижимости.'}`));
 const stops={attempt_limit:'исчерпан бюджет проверок',time_limit:'исчерпан лимит времени',neighborhood_exhausted:'завершён просмотр выбранных перестроек',round_limit:'достигнут лимит проходов',no_sla_jobs:'нет сроков SLA'};
 section.append(textNode('p',`Проверок: ${numberRu(d.schedule_attempts,0)} / ${numberRu(d.options.attempt_limit,0)}. Принятых улучшений: ${d.accepted_moves}. Остановка: ${stops[d.stop_reason]||d.stop_reason}. Исходный полный расчёт использован повторно, его время не входит в новый запуск.`,'muted tiny'));
 const neighborhood=d.neighborhood||'basic';
 section.append(textNode('p',`Перестройки: ${neighborhood==='extended'?'расширенные':'базовые'}. Сравнивайте режимы от одного исходного плана с одинаковыми лимитами.`,'muted tiny'));
 if(d.basic_prefix){
  const b=d.basic_prefix;
  section.append(textNode('p',`Базовый этап: ${numberRu(b.schedule_attempts,0)} проверок, ${b.summary.sla_met_jobs} заявок в SLA. Его целевая оценка ${d.basic_prefix_preserved?'сохранена или улучшена':'требует проверки'}. Эти проверки уже включены в общий бюджет.`,'muted tiny'));
 }
 if(Array.isArray(d.operators)&&d.operators.length){
  const details=textNode('details','');details.append(textNode('summary','На что потрачен бюджет поиска'));
  const table=textNode('table','');table.setAttribute('aria-label','Проверки и улучшения по операторам');
  const heading=textNode('tr','');for(const title of ['Оператор','Проверок','Улучшений'])heading.append(textNode('th',title));
  const head=textNode('thead','');head.append(heading);table.append(head);
  const body=textNode('tbody',''),labels={relocation:'Перенос заявки',swap:'Обмен на прежних позициях',free_swap:'Обмен с выбором позиций',tail_exchange:'Обмен хвостами маршрутов'};
  for(const op of d.operators){const row=textNode('tr','');for(const text of [labels[op.stage]||op.stage,numberRu(op.schedule_attempts,0),numberRu(op.accepted_moves,0)])row.append(textNode('td',text));body.append(row)}
  table.append(body);details.append(table);section.append(details);
 }
 section.append(textNode('p','Лимиты действуют только для этого запуска. Обычный пересчёт и события используют выбранную основную цель.','muted tiny'));
 return section;
}
async function runCompromise(event){
 event.preventDefault();
 if(!compromiseSource||compromiseSource.version!==requestVersion||!compromiseNode('compromiseForm').reportValidity())return;
 const source=compromiseSource;
 const options={neighborhood:compromiseNode('compromiseNeighborhood').value,extra_distance_percent:Number(compromiseNode('compromiseDistance').value),extra_engineers:Number(compromiseNode('compromiseEngineers').value),attempt_limit:Number(compromiseNode('compromiseAttempts').value)};
 const target=compromiseNode('compromiseTarget').value.trim();
 options.target_sla_percent=target===''?null:Number(target);
 const version=++requestVersion;source.version=version;
 setLoading(true);compromiseNode('compromiseError').hidden=true;
 try{
  const data=await fetchPlanning('/api/v1/compromise/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:source.request,reference_plan:source.plan,reference_label:source.label,options})});
  if(version!==requestVersion)return;
  // This is a preview: currentPlan/currentRequest/events and saved workspace stay
  // unchanged until the existing explicit variant-selection action is used.
  currentView='compromise';currentComparison=data;selectedComparisonVariant=null;
  renderComparison(data);renderOverview();
  const labels=comparisonLabels(data);
  renderChanges(data.diff,{title:'Предпросмотр компромисса по заявкам',reference:`${labels[0]} → ${labels[1]}. Текущий план пока не изменён.`,before:labels[0],after:labels[1]});
  compromiseNode('comparison').scrollIntoView({block:'start',behavior:'smooth'});
 }catch(error){
  if(version===requestVersion){compromiseNode('compromiseError').textContent='Текущий план не изменён. '+error.message;compromiseNode('compromiseError').hidden=false}
 }finally{if(version===requestVersion)setLoading(false)}
}
compromiseNode('openCompromiseBtn').onclick=()=>openCompromise();
compromiseNode('closeCompromiseBtn').onclick=()=>{compromiseNode('compromisePanel').hidden=true};
compromiseNode('compromiseForm').onsubmit=runCompromise;
for(const id of ['compromiseDistance','compromiseEngineers'])compromiseNode(id).oninput=updateCompromiseLimits;
