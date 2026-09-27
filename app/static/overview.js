// The overview explains the result; the route screen operates on the selected plan.
let selectedComparisonVariant='after';
const policyNames={staff_first:'Меньше инженеров',distance_first:'Меньше километров',sla_first:'Соблюдать SLA'};
const laborParts=[['service_minutes','Работа','service'],['travel_minutes','Дорога','travel'],['waiting_minutes','Ожидание окон','waiting']];
const numberRu=(value,digits=1,signed=false)=>value.toLocaleString('ru-RU',{maximumFractionDigits:digits,signDisplay:signed?'exceptZero':'auto'});
function minutesText(value,signed=false){
 if(!Number.isFinite(value))return 'Нет данных';
 if(value!==0&&Math.abs(value)<.1)return `${signed?(value<0?'−':'+'):''}< 0,1 мин`;
 return `${numberRu(value,1,signed)} мин`;
}
function deltaText(before,after,unit='',digits=1){
 if(!Number.isFinite(before)||!Number.isFinite(after))return 'Нет данных';
 const value=Number((after-before).toFixed(8));
 if(value!==0&&Math.abs(value)<10**-digits)return `${value<0?'−':'+'}< ${numberRu(10**-digits,digits)}${unit}`;
 return `${numberRu(value,digits,true)}${unit}`;
}
function sameAssignments(comparison){
 const ids=plan=>new Set(plan.routes.flatMap(route=>route.stops.map(stop=>stop.job_id)));
 const before=ids(comparison.before),after=ids(comparison.after);
 return before.size===after.size&&[...before].every(id=>after.has(id));
}
function comparisonLabels(comparison){
 if(Array.isArray(comparison.event?.variant_labels)&&comparison.event.variant_labels.length===2)return comparison.event.variant_labels;
 if(comparison.event.comparison==='optimization_policies')return ['Меньше инженеров','Соблюдать SLA'];
 if(comparison.event.comparison==='fifo_baseline_vs_insertion')return ['Базовый план','Улучшенный план'];
 return ['Полный пересчёт','Стабильный план'];
}
function calculationBadge(plan,index){
 const ms=plan.metrics?.solve_time_ms;
 const suffix=Number.isFinite(ms)?` · ${ms<1000?`${numberRu(ms,0)} мс`:`${numberRu(ms/1000,1)} с`}`:'';
 const shared=plan.diagnostics?.district_search?' · по районам':plan.diagnostics?.execution?.shared_candidate_pool?' · общий поиск':'';
 return `Расчёт ${index===0?'A':'B'}${suffix}${shared}`;
}
function selectedPlanLabel(){
 if(currentComparison&&['before','after'].includes(selectedComparisonVariant))return comparisonLabels(currentComparison)[selectedComparisonVariant==='before'?0:1];
 if(currentPlan?.diagnostics?.plan_label)return currentPlan.diagnostics.plan_label;
 if(currentPlan?.solver==='fifo-baseline-v1'||currentPlan?.solver?.endsWith('/fifo-baseline-v1'))return 'Базовый план';
 return policyNames[currentPlan?.diagnostics?.optimization_policy||currentRequest?.optimization_policy]||'План смены';
}
function metricTable(headings,rows,className=''){
 const wrap=textNode('div','',`metric-table-wrap ${className}`);
 const table=document.createElement('table');table.className='metric-table';
 const head=document.createElement('thead'),header=document.createElement('tr');
 for(const title of headings){const th=textNode('th',title);th.scope='col';header.append(th)}
 head.append(header);table.append(head);
 const body=document.createElement('tbody');
 for(const {label,values,note,total=false} of rows){
  const row=document.createElement('tr');if(total)row.className='total-row';
  const name=textNode('th',label);name.scope='row';if(note)name.append(textNode('small',note,'muted'));
  row.append(name);
  values.forEach(value=>row.append(textNode('td',value)));
  body.append(row);
 }
 table.append(body);wrap.append(table);return wrap;
}
function timeComposition(labor,label,maximum){
 const box=textNode('div','','composition');box.append(textNode('div',label,'composition-label'));
 if(!labor){box.append(textNode('div','Нет данных','muted'));return box}
 const bar=textNode('div','','time-bar');
 // Use a common scale for both plans, so different totals remain visible.
 bar.style.width=`${maximum?Math.max(0,labor.total_minutes/maximum*100):0}%`;
 for(const [key,title,color] of laborParts){
  const part=textNode('span','',`time-part ${color}`);
  part.style.width=`${labor.total_minutes?labor[key]/labor.total_minutes*100:0}%`;
  part.title=`${title}: ${minutesText(labor[key])}`;bar.append(part);
 }
 const track=textNode('div','','time-track');track.append(bar);
 box.append(track,textNode('div',`${fmtHours(labor.total_hours)} чел·ч · ${minutesText(labor.total_minutes)}`,'composition-total'));
 return box;
}
function compositionLegend(){
 const legend=textNode('div','','time-legend');
 for(const [,label,color] of laborParts){const item=textNode('span',label);item.prepend(textNode('i','',color));legend.append(item)}
 return legend;
}
function renderOverview(){
 if(!currentPlan)return;
 const plan=currentPlan,m=plan.metrics,d=plan.diagnostics||{},label=selectedPlanLabel();
 document.getElementById('currentPlanLabel').textContent=label;
 document.getElementById('routePlanSummary').textContent=`${label} · ${planSummary(m)}`;
 const reasoning=document.getElementById('planReasoning');reasoning.replaceChildren(textNode('h2','Как получен план'));
 const steps=textNode('ol','','reasoning-steps');
 const pool=d.candidate_pool?.length;
 const isFifo=plan.solver==='fifo-baseline-v1'||plan.solver?.endsWith('/fifo-baseline-v1');
 const items=[
  ['Исходные данные',`${m.total_jobs} активных заявок, ${currentRequest?.engineers.length??plan.routes.length} инженеров. ${m.labor?.scope==='remaining'?'Рассчитываем остаток после события.':'Рассчитываем план на весь день.'}`],
  ['Выбор маршрутов',`${isFifo?'Базовый алгоритм: заявки по порядку, первый допустимый инженер':d.plan_label||policyNames[d.optimization_policy]||'Назначение работ'}. ${pool?`Сопоставлено кандидатов: ${pool}.`:isFifo?'Цель не оптимизировалась.':'Работы распределены по допустимым маршрутам.'} Навыки, оборудование, окна и доступность ограничивают назначения.`],
  ['Проверка результата',`${d.validation==='passed'?'Независимая проверка ограничений пройдена.':'Статус независимой проверки не указан.'} ${m.unassigned_jobs?`Не удалось назначить: ${m.unassigned_jobs}. Причины доступны в маршрутах.`:'Все активные заявки назначены.'} ${d.search_budget?.stop_reason==='time_limit'?'Поиск завершён по лимиту времени.':'Найденный план не гарантирует математический оптимум.'}`],
 ];
 if(d.districts)items.splice(1,0,['Районы',d.districts.mode==='strict'?'Каждый район рассчитывается отдельно. Назначения вне района инженера запрещены.':'Выезды разрешены, распределение общее.']);
 for(const [title,body] of items){const step=document.createElement('li');step.append(textNode('h3',title),textNode('p',body,'muted'));steps.append(step)}
 reasoning.append(steps);
 if(d.compromise&&typeof renderCompromiseReport==='function')reasoning.append(renderCompromiseReport(plan));
 if(typeof compromiseButtons==='function')compromiseButtons();
 const details=document.getElementById('planDetails'),detailsBody=document.getElementById('planDetailsBody');
 details.hidden=!!currentComparison;detailsBody.replaceChildren();
 const detailSummary=[];
 if(m.labor)detailSummary.push(`${fmtHours(m.labor.total_hours)} чел·ч`);
 if(m.workload)detailSummary.push(`макс. нагрузка ${minutesText(m.workload.max_engineer_minutes)}`);
 document.getElementById('planDetailsSummary').textContent=detailSummary.join(' · ')||'Состав времени и нагрузка инженеров';
 if(!currentComparison){
  const card=textNode('section','','card detail-card');card.append(textNode('h2','Из чего складывается время'));
  card.append(textNode('p','Человеко-часы складываются по всем инженерам. Продолжительность дня зависит от того, как работа распределена между ними.','muted'));
  card.append(compositionLegend(),timeComposition(m.labor,label,m.labor?.total_minutes||0));
  if(m.labor){
   card.append(metricTable(['Составляющая','Человеко-часы','Минуты всех инженеров'],[
    ...laborParts.map(([key,title])=>({label:title,values:[fmtHours(m.labor[key]/60),minutesText(m.labor[key])]})),
    {label:'Всего по визитам',values:[fmtHours(m.labor.total_hours),minutesText(m.labor.total_minutes)],total:true},
   ]));
   card.append(textNode('p',`${laborScope(m.labor)}. Свободное время после последнего визита и инженеры без назначений в сумму не входят. Число инженеров × длительность смены — другой показатель.`,'muted tiny'));
  }
  detailsBody.append(card,renderWorkload(null,plan));
 }
 document.querySelectorAll('[data-comparison-variant]').forEach(button=>{
  const active=button.dataset.comparisonVariant===selectedComparisonVariant;
  button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));
 });
}
function renderLaborComparison(comparison,beforeLabel,afterLabel){
 const before=comparison.before.metrics.labor,after=comparison.after.metrics.labor;
 const section=textNode('section','','labor-comparison');section.setAttribute('aria-label','Плановые трудозатраты');
 section.append(textNode('h3','Время: общий итог и его составляющие'));
 section.append(textNode('p',`Все изменения ниже: «${afterLabel}» минус «${beforeLabel}». Минус — меньше времени, плюс — больше.`,'muted'));
 section.append(compositionLegend());
 const scale=Math.max(before?.total_minutes||0,after?.total_minutes||0);
 section.append(timeComposition(before,beforeLabel,scale),timeComposition(after,afterLabel,scale));
 if(!before||!after){section.append(textNode('p','Нет данных о трудозатратах одного из вариантов. Пересчитайте сравнение.','muted'));return section}
 const delta=Number((after.total_minutes-before.total_minutes).toFixed(4));
 section.append(metricTable(['Составляющая',`${beforeLabel}, чел·ч`,`${afterLabel}, чел·ч`,'Изменение, чел·ч','Изменение, мин'],[
  ...laborParts.map(([key,title])=>({label:title,values:[fmtHours(before[key]/60),fmtHours(after[key]/60),deltaText(before[key]/60,after[key]/60,'',2),minutesText(after[key]-before[key],true)]})),
  {label:'Всего по визитам',values:[fmtHours(before.total_hours),fmtHours(after.total_hours),deltaText(before.total_minutes/60,after.total_minutes/60,'',2),minutesText(delta,true)],total:true},
 ]));
 const componentChange=laborParts.some(([key])=>Math.abs(after[key]-before[key])>=.0001);
 const explanation=delta===0
  ?componentChange?'Итог одинаковый, но структура времени изменилась: уменьшение одних составляющих компенсировано увеличением других. Ниже — изменение нагрузки и сроков.':'Сумма и составляющие времени совпали. Различия могут оставаться в исполнителях, времени визитов и SLA — они показаны ниже.'
  :Math.abs(delta)<.3?'Разница мала и может округляться до 0,00 чел·ч. Точное направление и величина видны в колонке минут.':`Суммарное время ${delta<0?'уменьшилось':'увеличилось'} на ${minutesText(Math.abs(delta))}. Эффект по срокам и нагрузке оценивается отдельно.`;
 section.append(textNode('p',explanation,'comparison-insight'));
 if(!sameAssignments(comparison))section.append(textNode('p','Состав назначенных работ различается, даже если их количество совпало. Уменьшение времени здесь не означает экономию на том же объёме работ.','comparison-caution'));
 if(before.scope!==after.scope)section.append(textNode('p','Области расчёта различаются: весь план и его остаток. Прямое сравнение трудозатрат не отражает экономию.','comparison-caution'));
 section.append(textNode('p',`${laborScope(before)} → ${laborScope(after)}. Работа + дорога + ожидание начала визитов. Это время по плану, а не оплаченная смена; свободное время после последней работы исключено. Округление часов может скрыть небольшие изменения, поэтому рядом указаны минуты.`,'muted tiny'));
 return section;
}
function renderEffect(comparison){
 const b=comparison.before.metrics,a=comparison.after.metrics;
 const section=textNode('section','','effect-section');section.append(textNode('h3','Выигрыш и издержки второго варианта'));
 const positive=[],negative=[];
 const assess=(change,label,unit,lowerBetter=true,digits=1)=>{
  if(!Number.isFinite(change))return;
  change=Number(change.toFixed(4));
  if(change===0)return;
  const text=`${label}: ${unit===' мин'?minutesText(change,true):deltaText(0,change,unit,digits)}`;
  ((change<0)===lowerBetter?positive:negative).push(text);
 };
 assess(a.assigned_jobs-b.assigned_jobs,'Назначено заявок','',false,0);
 assess(a.sla_met_jobs-b.sla_met_jobs,'Заявок в SLA','',false,0);
 assess(a.late_minutes-b.late_minutes,'Опоздание к SLA',' мин');
 assess(a.used_engineers-b.used_engineers,'Задействовано инженеров','',true,0);
 assess(a.total_distance_km-b.total_distance_km,'Пробег',' км');
 if(sameAssignments(comparison)&&a.labor&&b.labor&&a.labor.scope===b.labor.scope)assess(a.labor.total_minutes-b.labor.total_minutes,'Время всех инженеров',' мин');
 if(a.workload&&b.workload)assess(a.workload.max_engineer_minutes-b.workload.max_engineer_minutes,'Нагрузка самого занятого инженера',' мин');
 if(['naive_full_recalculation_vs_stable_replanning','replanning_profiles'].includes(comparison.event.comparison)){
  assess(a.changed_assignments-b.changed_assignments,'Переназначения','',true,0);
  assess(a.schedule_shift_minutes-b.schedule_shift_minutes,'Сумма сдвигов расписания',' мин');
 }
 const columns=textNode('div','','effect-grid');
 for(const [title,items,cls] of [['Что улучшилось',positive,'benefit'],['Что увеличилось или ухудшилось',negative,'cost']]){
  const col=textNode('div','',`effect-box ${cls}`);col.append(textNode('h4',title));
  if(items.length){const list=document.createElement('ul');items.forEach(item=>list.append(textNode('li',item)));col.append(list)}
  else col.append(textNode('p','По этим показателям изменений нет.','muted'));
  columns.append(col);
 }
 section.append(columns,textNode('p','Эффект показан в заявках, времени, километрах и числе инженеров. Денежная прибыль не рассчитана: в данных нет стоимости смены, километра и нарушения SLA.','muted tiny'));
 return section;
}
function renderOutcomeTable(comparison,labels){
 const b=comparison.before.metrics,a=comparison.after.metrics;
 const section=textNode('section','','result-details');section.append(textNode('h3','Объём работ, сроки и ресурсы'));
 const rows=[
  {label:'Назначено заявок',values:[`${b.assigned_jobs} / ${b.total_jobs}`,`${a.assigned_jobs} / ${a.total_jobs}`,deltaText(b.assigned_jobs,a.assigned_jobs,'',0)]},
  {label:'Инженеров с назначениями',values:[b.used_engineers,a.used_engineers,deltaText(b.used_engineers,a.used_engineers,'',0)]},
  {label:'Пробег',values:[`${numberRu(b.total_distance_km,2)} км`,`${numberRu(a.total_distance_km,2)} км`,deltaText(b.total_distance_km,a.total_distance_km,' км',2)]},
  {label:'Заявки в SLA',note:'Начало работ до срока; неназначенные с SLA тоже в знаменателе',values:[b.sla_jobs?`${b.sla_met_jobs} / ${b.sla_jobs} (${fmtSla(b)})`:'Нет сроков',a.sla_jobs?`${a.sla_met_jobs} / ${a.sla_jobs} (${fmtSla(a)})`:'Нет сроков',b.sla_jobs&&a.sla_jobs?deltaText(b.sla_rate*100,a.sla_rate*100,' п.п.'):'Не применимо']},
  {label:'Сумма опозданий к SLA',note:'Только назначенные работы; неназначенные оценивайте по покрытию',values:[minutesText(b.late_minutes),minutesText(a.late_minutes),minutesText(a.late_minutes-b.late_minutes,true)]},
 ];
 if(b.workload&&a.workload){
  rows.push({label:'Последняя работа закончится',note:'По часам, без возврата; это не сумма человеко-часов',values:[b.workload.completion_at?dateTimeFormat.format(new Date(b.workload.completion_at)):'Нет визитов',a.workload.completion_at?dateTimeFormat.format(new Date(a.workload.completion_at)):'Нет визитов',b.workload.completion_at&&a.workload.completion_at?minutesText((new Date(a.workload.completion_at)-new Date(b.workload.completion_at))/60000,true):'Не применимо']});
  rows.push({label:'Самый занятый инженер',note:'Работа + дорога + ожидание; максимум среди назначенных',values:[minutesText(b.workload.max_engineer_minutes),minutesText(a.workload.max_engineer_minutes),minutesText(a.workload.max_engineer_minutes-b.workload.max_engineer_minutes,true)]});
 }
 section.append(metricTable(['Показатель',...labels,'Изменение'],rows));
 const shifts=(comparison.diff?.items||[]).map(item=>item.start_shift_minutes).filter(Number.isFinite);
 const earlier=shifts.filter(value=>value<0),later=shifts.filter(value=>value>0);
 if(earlier.length||later.length){
  const changes=textNode('div','','visit-shifts');
  changes.append(textNode('h4','Время визитов тоже изменилось'));
  changes.append(textNode('p',`${earlier.length} визитов начнутся раньше (сумма сдвигов ${minutesText(-earlier.reduce((sum,value)=>sum+value,0))}); ${later.length} — позже (сумма сдвигов ${minutesText(later.reduce((sum,value)=>sum+value,0))}). Наибольший перенос одного визита: ${minutesText(Math.max(...shifts.map(Math.abs)))}.`));
  changes.append(textNode('p','Это перемещение визитов по расписанию, а не изменение трудозатрат. Конкретные заявки и время до/после — в таблице «Отличия вариантов по заявкам» ниже.','muted tiny'));
  section.append(changes);
 }
 return section;
}
function renderWorkload(before,after,labels=['До','Текущий план']){
 const section=textNode('section','',before?'workload-details':'card detail-card workload-details');
 section.append(textNode('h3',before?'Что изменилось у каждого инженера':'Нагрузка по инженерам'));
 const b=before?.metrics.workload,a=after.metrics.workload;
 if(!a||(before&&!b)){section.append(textNode('p','В этом плане нет детализации по инженерам. Пересчитайте план для её получения.','muted'));return section}
 if(!before){
  section.append(textNode('p',`Среднее среди занятых: ${minutesText(a.average_engineer_minutes)}. Максимум: ${minutesText(a.max_engineer_minutes)}. ${a.completion_at?`Последняя работа: ${dateTimeFormat.format(new Date(a.completion_at))}.`:''}`,'muted'));
  section.append(metricTable(['Инженер','Заявок','Работа, мин','Дорога, мин','Ожидание, мин','Всего, мин'],a.engineers.map(e=>({label:e.engineer_name,note:e.engineer_id,values:[e.assigned_jobs,numberRu(e.service_minutes),numberRu(e.travel_minutes),numberRu(e.waiting_minutes),numberRu(e.total_minutes)]}))));return section;
 }
 const previous=new Map(b.engineers.map(e=>[e.engineer_id,e])),next=new Map(a.engineers.map(e=>[e.engineer_id,e]));
 const ids=[...new Set([...previous.keys(),...next.keys()])];
 const zero={assigned_jobs:0,service_minutes:0,travel_minutes:0,waiting_minutes:0,total_minutes:0};
 const pairs=ids.map(id=>({id,before:previous.get(id)||zero,after:next.get(id)||zero}));
 pairs.sort((x,y)=>Math.abs(y.after.total_minutes-y.before.total_minutes)-Math.abs(x.after.total_minutes-x.before.total_minutes));
 const changed=pairs.filter(x=>Math.abs(x.after.total_minutes-x.before.total_minutes)>=.0001);
 const max=changed.length?Math.abs(changed[0].after.total_minutes-changed[0].before.total_minutes):0;
 section.append(textNode('p',changed.length?`Суммарная нагрузка изменилась у ${changed.length} инженеров. Наибольшее изменение у одного человека: ${minutesText(max)}. Плюс означает больше нагрузки во втором варианте.`:'Суммарная нагрузка каждого инженера совпала. Составляющие и время отдельных визитов всё равно могут различаться.','comparison-insight'));
 section.append(textNode('p','В таблице — минуты работы, дороги и ожидания. Перераспределение между людьми не является экономией времени всей команды.','muted tiny'));
 section.append(metricTable(['Инженер',`${labels[0]}, мин`,`${labels[1]}, мин`,'Δ всего, мин','Δ работа','Δ дорога','Δ ожидание','Заявок до → после'],pairs.map(({id,before:x,after:y})=>({label:y.engineer_name||x.engineer_name||id,note:id,values:[numberRu(x.total_minutes),numberRu(y.total_minutes),deltaText(x.total_minutes,y.total_minutes),deltaText(x.service_minutes,y.service_minutes),deltaText(x.travel_minutes,y.travel_minutes),deltaText(x.waiting_minutes,y.waiting_minutes),`${x.assigned_jobs} → ${y.assigned_jobs}`]})),'engineer-comparison'));
 return section;
}
async function selectComparisonVariant(variant,{routes=false}={}){
 if(!currentComparison||!['before','after'].includes(variant))return;
 selectedComparisonVariant=variant;
 if(currentView==='incident'){
  document.getElementById('variantSelect').value=variant;renderIncident();
 }else{
  const plan=currentComparison[variant];
  if(currentRequest)currentRequest={...currentRequest,optimization_policy:plan.diagnostics.optimization_policy||currentRequest.optimization_policy};
  if(currentRequest&&plan.diagnostics?.districts?.mode)currentRequest={...currentRequest,district_mode:plan.diagnostics.districts.mode};
  if(typeof syncDistrictMode==='function')syncDistrictMode();
  document.getElementById('policySelect').value=currentRequest?.optimization_policy||'staff_first';updatePolicyHint();
  renderPlan(plan);
  const labels=comparisonLabels(currentComparison);
  renderChanges(currentComparison.diff,{title:'Отличия вариантов по заявкам',reference:`${labels[0]} → ${labels[1]}. Текущий вариант: ${labels[variant==='before'?0:1]}.`,before:labels[0],after:labels[1]});
 }
 refreshEventPanel();refreshWorkspace();
 if(routes)showWorkspaceScreen('routes',{focus:true});
 if(typeof persistWorkspace==='function')await persistWorkspace();
}
function renderComparison(comparison){
 const node=document.getElementById('comparison');node.classList.add('show');node.replaceChildren();
 const labels=comparisonLabels(comparison);
 const title=comparison.event.comparison_title||(comparison.event.comparison==='optimization_policies'?'Цена выбора цели':comparison.event.comparison==='fifo_baseline_vs_insertion'?'Эффект оптимизации':'Устойчивость к событиям');
 node.append(textNode('span','Результат сравнения','eyebrow'),textNode('h2',title));
 node.append(textNode('p',`${labels[0]} → ${labels[1]}. Сравнение на одном наборе данных. ${['resource_limited_sla','district_modes'].includes(comparison.event.comparison)?'Результат не применяется автоматически. ':''}Выберите вариант, чтобы использовать его как текущий план.`,'muted'));
 const variants=textNode('div','','compare-grid');
 for(const [index,key] of ['before','after'].entries()){
  const box=textNode('div','','compare-box');box.append(textNode('span',calculationBadge(comparison[key],index),'calculation-badge'),textNode('h3',labels[index]),textNode('p',planSummary(comparison[key].metrics),'muted'));
  const actions=textNode('div','','actions');
  const choose=textNode('button','Выбрать вариант','btn');choose.type='button';choose.dataset.comparisonVariant=key;choose.onclick=()=>selectComparisonVariant(key);
  const map=textNode('button','Маршруты →','text-btn');map.onclick=()=>selectComparisonVariant(key,{routes:true});
  actions.append(choose,map);
  if(currentRequest&&currentView!=='incident'&&typeof openCompromise==='function'){
   const improve=textNode('button','SLA в пределах ресурсов','btn');improve.type='button';improve.onclick=()=>openCompromise(key);actions.append(improve);
  }
  box.append(actions);variants.append(box);
 }
 const variantSelect=document.getElementById('variantSelect'),selected=selectedComparisonVariant;
 variantSelect.replaceChildren(...['before','after'].map((key,index)=>{const option=textNode('option',labels[index]);option.value=key;return option}));variantSelect.value=selected;
 if(comparison.after.diagnostics?.compromise&&typeof renderCompromiseReport==='function')node.append(renderCompromiseReport(comparison.after));
 node.append(variants,renderEffect(comparison),renderLaborComparison(comparison,...labels),renderOutcomeTable(comparison,labels),renderWorkload(comparison.before,comparison.after,labels));
}
