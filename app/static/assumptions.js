// Assumptions describe the request that produced the displayed plan. Draft settings
// only become workspace state after loadView has successfully received a new plan.
let assumptionsRenderedRequest;
let assumptionsRenderedPlan;
let assumptionsSaving=false;
const assumptionTransportNames={car:'Автомобиль',walk:'Пешком',bicycle:'Велосипед',public_transport:'Общественный транспорт'};
const assumptionObjectiveNames={
 locked_unassigned:'Сохранить назначения закреплённых заявок',
 urgent_unassigned_on_replan:'Назначить срочные заявки при перепланировании',
 urgent_unassigned:'Назначить больше срочных заявок',
 urgent_start_minutes:'Уменьшить суммарное время до начала срочных работ',
 unassigned:'Назначить больше заявок',
 sla_missed_jobs:'Уменьшить число заявок с нарушенным SLA',
 late_minutes:'Уменьшить суммарное опоздание',
 used_engineers:'Использовать меньше инженеров',
 distance_km:'Сократить километры',
 soft_cost:'Уменьшить дополнительные штрафы',
 weighted_residual_cost:'Учесть дорогу, SLA и изменения прежнего плана',
};

function assumptionNumber(value,maximumFractionDigits=1){
 return Number.isFinite(value)?value.toLocaleString('ru-RU',{maximumFractionDigits}):'нет данных';
}
function assumptionDate(value){
 if(!value)return 'не задано';
 const date=new Date(value);
 return Number.isNaN(date.getTime())?'не задано':dateTimeFormat.format(date);
}
function assumptionCount(value,forms){
 const tens=value%100,ones=value%10;
 const form=tens>=11&&tens<=14?forms[2]:ones===1?forms[0]:ones>=2&&ones<=4?forms[1]:forms[2];
 return `${value.toLocaleString('ru-RU')} ${form}`;
}
function assumptionUtcOffset(minutes){
 if(!Number.isInteger(minutes))return 'не записано';
 const absolute=Math.abs(minutes),sign=minutes>=0?'+':'−';
 return `UTC${sign}${String(Math.floor(absolute/60)).padStart(2,'0')}:${String(absolute%60).padStart(2,'0')}`;
}
function assumptionPanel(title,description){
 const panel=textNode('section','','assumption-panel');
 panel.append(textNode('h2',title));
 if(description)panel.append(textNode('p',description,'assumption-description'));
 return panel;
}
function assumptionFactList(rows){
 const list=textNode('dl','','assumption-facts');
 for(const [label,detail] of rows){
  const row=textNode('div','','assumption-fact');
  row.append(textNode('dt',label),textNode('dd',detail));list.append(row);
 }
 return list;
}
function assumptionTable(headers,rows){
 const wrap=textNode('div','','assumption-table-wrap');
 const table=document.createElement('table');table.className='assumption-table';
 const head=document.createElement('thead'),headRow=document.createElement('tr');
 for(const label of headers){const th=textNode('th',label);th.scope='col';headRow.append(th)}
 head.append(headRow);table.append(head);
 const body=document.createElement('tbody');
 for(const cells of rows){const row=document.createElement('tr');for(const value of cells)row.append(textNode('td',String(value)));body.append(row)}
 table.append(body);wrap.append(table);return wrap;
}
function assumptionDetails(label,...children){
 const details=textNode('details','','assumption-details');
 details.append(textNode('summary',label),...children);return details;
}
function assumptionLazyDetails(label,id,renderContent){
 const details=assumptionDetails(label);details.id=id;
 let rendered=false;
 details.addEventListener('toggle',()=>{
  if(!details.open||rendered)return;
  rendered=true;details.append(renderContent());
 });
 return details;
}

function assumptionProfileSnapshot(request){
 const profiles=new Set(),notes=new Set(),offsets=new Set(),sourceFiles=new Set();
 const locations=new Set(),reviewedLocations=new Set(),rules=new Map();
 let profiledJobs=0;
 for(const job of request.jobs||[]){
  const enrichment=job.metadata?.enrichment;
  const profile=typeof enrichment?.profile==='string'?enrichment.profile.trim():'';
  if(!profile)continue;
  profiledJobs++;profiles.add(profile);
  for(const note of Array.isArray(enrichment.notes)?enrichment.notes:[]){if(typeof note==='string'&&note.trim())notes.add(note.trim())}
  if(Number.isInteger(enrichment.utc_offset_minutes))offsets.add(enrichment.utc_offset_minutes);
  const filename=job.metadata?.source?.filename;
  if(typeof filename==='string'&&filename.trim())sourceFiles.add(filename.trim());
  const location=job.location||{};
  const locationKey=Number.isFinite(location.lat)&&Number.isFinite(location.lon)
   ?`${location.lat},${location.lon}`:typeof location.label==='string'?location.label.trim():'';
  if(locationKey)locations.add(locationKey);
  const sourceAddress=job.metadata?.source?.fields?.['Адрес'];
  const reviewKey=typeof sourceAddress==='string'&&sourceAddress.trim()
   ?sourceAddress.trim():typeof location.label==='string'&&location.label.trim()?location.label.trim():locationKey;
  if(enrichment.location_review&&reviewKey)reviewedLocations.add(reviewKey);
  const rule=enrichment.rule;
  if(!rule||typeof rule!=='object')continue;
  const equipment=Array.isArray(rule.required_equipment)?[...rule.required_equipment].map(String).sort():[];
  const values=[rule.bk_type||'',rule.hd_type||'',rule.skill||'',rule.service_minutes??'',rule.service_norm||'',rule.priority||'',rule.required_transport||'',equipment,rule.window_semantics||'',rule.note||''];
  const key=JSON.stringify(values);
  if(!rules.has(key))rules.set(key,{
   bkType:rule.bk_type||'не записано',hdType:rule.hd_type||'',skill:rule.skill||'',
   serviceMinutes:rule.service_minutes,serviceNorm:rule.service_norm||'',priority:rule.priority||'',
   requiredTransport:rule.required_transport||'',requiredEquipment:equipment,
   windowSemantics:rule.window_semantics||'',note:rule.note||'',jobs:0,
  });
  rules.get(key).jobs++;
 }
 if(!profiledJobs)return null;
 const activeJobs=(request.jobs||[]).filter(job=>!['completed','cancelled'].includes(job.status)).length;
 return {
  profiles:[...profiles],notes:[...notes],offsets:[...offsets].sort((a,b)=>a-b),
  sourceFiles:[...sourceFiles],profiledJobs,totalJobs:(request.jobs||[]).length,activeJobs,
  engineers:(request.engineers||[]).length,
  availableEngineers:(request.engineers||[]).filter(engineer=>engineer.available!==false).length,
  locations:locations.size,reviewedLocations:reviewedLocations.size,
  rules:[...rules.values()].sort((a,b)=>`${a.bkType}\n${a.hdType}`.localeCompare(`${b.bkType}\n${b.hdType}`,'ru')),
 };
}

function renderAssumptionProfile(request){
 const profile=assumptionProfileSnapshot(request);
 if(!profile)return null;
 const details=textNode('details','','assumption-panel assumption-profile');details.id='assumptionDataProfile';
 const summary=textNode('summary','','assumption-profile-summary');summary.id='assumptionDataProfileSummary';
 const heading=textNode('span','','assumption-profile-heading');
 heading.append(
  textNode('strong','Применённый профиль'),
  textNode('span',`${profile.profiles.join(' + ')} · ${assumptionCount(profile.profiledJobs,['заявка','заявки','заявок'])} из ${profile.totalJobs.toLocaleString('ru-RU')} · ${assumptionCount(profile.rules.length,['правило','правила','правил'])} · ${assumptionCount(profile.engineers,['инженер','инженера','инженеров'])}`,'assumption-profile-meta'),
 );
 summary.append(heading);details.append(summary);
 const body=textNode('div','','assumption-profile-body');
 body.append(textNode('p','Здесь показаны фрагменты профиля, которые были применены при импорте текущего набора. Неиспользованные правила и адреса из исходного JSON в запрос расчёта не переносятся.','assumption-description'));
 const profileNames=profile.profiles.length===1?profile.profiles[0]:`Смешанные данные: ${profile.profiles.join('; ')}`;
 const datasetName=typeof currentDataset?.name==='string'&&currentDataset.name.trim()?currentDataset.name.trim():'Текущий набор';
 let source=datasetName;
 if(profile.sourceFiles.length&&!(profile.sourceFiles.length===1&&profile.sourceFiles[0]===datasetName))source+=`; CSV: ${profile.sourceFiles.join(', ')}`;
 else if(!profile.sourceFiles.length)source+='; имя CSV не записано';
 const offset=profile.offsets.length===1?assumptionUtcOffset(profile.offsets[0]):profile.offsets.length?`смешанные: ${profile.offsets.map(assumptionUtcOffset).join(', ')}`:'не записано';
 body.append(assumptionFactList([
  ['Профиль',profileNames],
  ['Источник',`${source}. Смещение времени в профиле: ${offset}.`],
  ['Охват',`Метаданные профиля есть у ${profile.profiledJobs.toLocaleString('ru-RU')} из ${profile.totalJobs.toLocaleString('ru-RU')} заявок. В текущем запросе ${profile.activeJobs.toLocaleString('ru-RU')} активных заявок; ${profile.availableEngineers.toLocaleString('ru-RU')} из ${profile.engineers.toLocaleString('ru-RU')} инженеров не помечены недоступными.`],
  ['Координаты',`${assumptionCount(profile.locations,['точка','точки','точек'])} в применённой части профиля; у ${profile.reviewedLocations.toLocaleString('ru-RU')} адресов есть запись о ручной проверке.`],
 ]));
 if(profile.notes.length){
  body.append(assumptionLazyDetails(`Примечания профиля (${profile.notes.length})`,'assumptionProfileNotes',()=>{
   const list=textNode('ul','','assumption-profile-notes');
   for(const note of profile.notes)list.append(textNode('li',note));
   return list;
  }));
 }
 if(profile.rules.length){
  body.append(assumptionLazyDetails(`Применённые правила работ (${profile.rules.length})`,'assumptionProfileRules',()=>{
   const rows=profile.rules.map(rule=>{
    const duration=Number.isFinite(rule.serviceMinutes)?`${assumptionNumber(rule.serviceMinutes)} мин`:'Длительность не записана';
    const priority=rule.priority==='urgent'?'Срочная':rule.priority==='normal'?'Обычная':rule.priority?`Приоритет: ${String(rule.priority)}`:'Приоритет не записан';
    const windowRule=rule.windowSemantics==='completion'?'выполнить в окне':rule.windowSemantics==='start'?'начать в окне':rule.windowSemantics?`правило окна: ${String(rule.windowSemantics)}`:'правило окна не записано';
    return [
     `${rule.bkType}${rule.hdType?`\n${rule.hdType}`:'\nЛюбой подтип'}`,
     `Навык: ${skillNames[rule.skill]||rule.skill||'не записан'}\nТранспорт: ${assumptionTransportNames[rule.requiredTransport]||rule.requiredTransport||'любой'}\nОборудование: ${rule.requiredEquipment.join(', ')||'не требуется'}`,
     `${duration}${rule.serviceNorm?`\nНорматив: ${rule.serviceNorm}`:''}\n${priority}; ${windowRule}`,
     rule.jobs,
     rule.note||'—',
    ];
   });
   return assumptionTable(['BK / HD','Требования','Время, приоритет и окно','Заявок','Комментарий'],rows);
  }));
 }
 details.append(body);return details;
}
function syncAssumptionsControls(){
 const busy=assumptionsSaving||document.getElementById('shell')?.classList.contains('loading');
 document.querySelectorAll('#assumptionsForm input,#assumptionsForm select,#assumptionsForm button').forEach(control=>control.disabled=!!busy||control.dataset.inactive==='true');
}

async function applyAssumptions(event){
 event.preventDefault();
 const form=event.currentTarget;
 if(assumptionsSaving||!currentRequest||!form.reportValidity())return;
 const freeze=Number(form.elements.freeze_horizon_minutes.value);
 const urgency=form.elements.urgency_policy.value;
 const urgentStart=form.elements.urgent_start_policy.value;
 const status=document.getElementById('assumptionsStatus');
 if(!Number.isSafeInteger(freeze)||freeze<0){
  status.textContent='Горизонт закрепления должен быть целым неотрицательным числом минут.';
  status.className='assumption-status warn';return;
 }
 const sourceRequest=currentRequest;
 const request=structuredClone(sourceRequest);
 request.urgency_policy=urgency;request.urgent_start_policy=urgentStart;request.freeze_horizon_minutes=freeze;
 // In particular, do not replace previous_plan with currentPlan here: that
 // would silently turn an initial plan into a different replanning problem.
 const candidateDataset=currentDataset?structuredClone(currentDataset):{name:'Демо · свои параметры',events:[]};
 if(!Object.hasOwn(candidateDataset,'origin'))candidateDataset.origin={name:currentDataset?.name||'Демо',request:structuredClone(sourceRequest)};
 candidateDataset.request=request;
 assumptionsSaving=true;syncAssumptionsControls();
 status.textContent='Пересчитываем план с выбранными параметрами…';status.className='assumption-status';
 try{
  await loadView('plan',candidateDataset);
  if(currentDataset!==candidateDataset&&currentRequest===sourceRequest){
   status.textContent='Параметры не применены. Текущий план сохранён; причина указана в сообщении об ошибке.';
   status.className='assumption-status warn';
  }
 }finally{assumptionsSaving=false;syncAssumptionsControls()}
}

function renderAssumptionSettings(request){
 const panel=assumptionPanel('Срочные заявки и закрепления','Эти параметры сохраняются вместе с набором данных после успешного пересчёта.');
 const form=document.createElement('form');form.id='assumptionsForm';form.addEventListener('submit',applyAssumptions);
 const fields=textNode('div','','assumption-settings');
 const urgencyLabel=textNode('label','Приоритет срочных заявок');
 const urgency=document.createElement('select');urgency.name='urgency_policy';urgency.id='assumptionUrgency';
 for(const [value,label] of [['urgent_first','Сначала срочные'],['coverage_first','Сначала максимум назначений']]){
  const option=textNode('option',label);option.value=value;urgency.append(option);
 }
 urgency.value=request.urgency_policy||'urgent_first';
 urgency.setAttribute('aria-describedby','assumptionUrgencyHelp');
 urgencyLabel.append(urgency);fields.append(urgencyLabel);
 const urgentStartLabel=textNode('label','Как рано начинать срочные работы');
 const urgentStart=document.createElement('select');urgentStart.name='urgent_start_policy';urgentStart.id='assumptionUrgentStart';
 for(const [value,label] of [['after_primary','После основной цели'],['before_primary','Перед основной целью'],['before_coverage','Раньше, даже ценой обычных заявок']]){
  const option=textNode('option',label);option.value=value;urgentStart.append(option);
 }
 urgentStart.value=request.urgent_start_policy||'after_primary';
 urgentStart.setAttribute('aria-describedby','assumptionUrgentStartHelp');
 urgentStartLabel.append(urgentStart);fields.append(urgentStartLabel);
 const freezeLabel=textNode('label','Закрепить ближайшие визиты, мин');
 const freeze=document.createElement('input');freeze.type='number';freeze.name='freeze_horizon_minutes';freeze.id='assumptionFreeze';
 freeze.min='0';freeze.step='1';freeze.required=true;freeze.value=request.freeze_horizon_minutes??60;
 freeze.setAttribute('aria-describedby','assumptionFreezeHelp');freezeLabel.append(freeze);fields.append(freezeLabel);
 form.append(fields);
 const urgentHelp=textNode('p','Срочная — заявка с приоритетом от 80. При перепланировании «Сначала срочные» ставит их назначение выше общего числа назначений. «Сначала максимум назначений» меняет эти два приоритета местами. Закреплённые заявки остаются первыми.','assumption-description');urgentHelp.id='assumptionUrgencyHelp';
 const urgentStartHelp=textNode('p','Первые два режима сначала применяют выбранный приоритет назначения: при перепланировании «Сначала срочные» уже допускает меньше обычных заявок ради назначения срочных. Среди равных по этим критериям вариантов «После основной цели» ускоряет срочные работы при тех же главных показателях цели. «Перед основной целью» ускоряет их даже ценой дополнительных инженеров, километров или нарушений мягкого SLA. «Раньше, даже ценой обычных заявок» ставит назначение и раннее начало срочных выше общего числа назначений, включая режим «Сначала максимум назначений». Во всех режимах сохраняются обязательные закрепления, начатые работы, строгие окна и смены.','assumption-description');urgentStartHelp.id='assumptionUrgentStartHelp';
 const freezeHelp=textNode('p','Горизонт действует только при наличии предыдущего плана: визиты, начинавшиеся в ближайшие N минут от момента расчёта, остаются у прежнего доступного инженера. Время и порядок могут измениться. 0 отключает только это автоматическое закрепление; явные закрепления и выполняемые работы сохраняются.','assumption-description');freezeHelp.id='assumptionFreezeHelp';
 form.append(urgentHelp,urgentStartHelp,freezeHelp);
 form.append(textNode('p',request.previous_plan?'Сейчас есть предыдущий план: все эти правила участвуют в перепланировании.':'Сейчас строится исходный план: правило раннего начала срочных уже действует. Настройка «Приоритет срочных заявок» и горизонт закрепления начнут действовать при перепланировании.','assumption-note'));
 const actions=textNode('div','','assumption-actions'),submit=textNode('button','Применить и пересчитать','btn primary');submit.type='submit';submit.id='assumptionApplyBtn';
 const status=textNode('span','','assumption-status');status.id='assumptionsStatus';status.setAttribute('role','status');
 actions.append(submit,status);form.append(actions);panel.append(form);return panel;
}

function renderAssumptionObjectives(request,plan){
 const panel=assumptionPanel('Как выбирается лучший план','Показатели проверяются по очереди. Следующий критерий сравнивается только при равенстве всех предыдущих. Положение раннего начала срочных работ зависит от выбранного правила.');
 const diagnostics=plan?.diagnostics||{};
 const order=diagnostics.objective_order;
 if(Array.isArray(order)&&order.length){
  const list=textNode('ol','','assumption-order');
  for(const key of order){
   const item=textNode('li',assumptionObjectiveNames[key]||key);
   if(key==='urgent_unassigned_on_replan'&&!request.previous_plan)item.append(textNode('span','не действует в исходном плане','assumption-tag'));
   list.append(item);
  }
  panel.append(list);
 }else panel.append(textNode('p','В этом сохранённом плане нет порядка критериев. Пересчитайте план, чтобы увидеть фактически использованные правила.','assumption-note'));
 panel.append(assumptionFactList([
  ['Меньше инженеров','Главный показатель цели — число занятых инженеров. Далее сравниваем дорогу и дополнительные штрафы. Место раннего начала срочных задано выбранным правилом и показано в списке выше.'],
  ['Меньше километров','Главный показатель цели — пробег. Далее сравниваем число инженеров и дополнительные штрафы. Место раннего начала срочных задано выбранным правилом и показано в списке выше.'],
  ['Соблюдать SLA','Главные показатели цели — число нарушений SLA, минуты опоздания и число инженеров. Раннее начало срочных может сравниваться перед ними согласно выбранному правилу. Строгие окна и смены соблюдаются при любой цели.'],
 ]));
 if(plan?.solver==='fifo-baseline-v1')panel.append(textNode('p','Показан базовый алгоритм: заявки по порядку и первый допустимый инженер. Он рассчитывает эти критерии для сравнения, но не улучшает план по ним.','assumption-note'));
 if(diagnostics.optimality_proven===false)panel.append(textNode('p','Поиск даёт лучший найденный допустимый вариант. Глобально оптимальное решение не доказано.','assumption-description'));
 return panel;
}

function renderAssumptionRules(request){
 const active=request.jobs.filter(job=>!['completed','cancelled'].includes(job.status));
 const withWindows=active.filter(job=>job.time_windows?.length);
 const completion=withWindows.filter(job=>job.window_semantics==='completion').length;
 const sla=active.filter(job=>job.sla_deadline).length;
 const panel=assumptionPanel('Ограничения и сроки',`В расчёте ${active.length} активных заявок из ${request.jobs.length}. Завершённые и отменённые не назначаются повторно.`);
 panel.append(assumptionFactList([
  ['Начало расчёта',`${assumptionDate(request.planning_time)}. Часовой пояс отображения: ${dateTimeFormat.resolvedOptions().timeZone}.`],
  ['Окно визита',`${withWindows.length} заявок с окнами: ${withWindows.length-completion} требуют начать работу внутри окна, ${completion} — полностью выполнить её внутри одного окна. Несколько окон означают выбор одного подходящего. Без окон ограничением остаётся смена.`],
  ['SLA',`${sla} заявок имеют крайний срок начала работ. SLA = начатые вовремя / все активные заявки со сроком. Неназначенная заявка со сроком также снижает SLA; минуты опоздания считаются только у назначенных.`],
  ['Инженер и смена','Проверяются доступность, все требуемые навыки и оборудование, нужный транспорт и лимит заявок. Работа должна закончиться до конца смены; раннее прибытие приводит к ожиданию начала окна.'],
  ['Начало маршрута','Стартуем не раньше момента расчёта, начала смены и доступности инженера. Используется текущая точка, если она задана, иначе стартовая; выполняемая работа сохраняет свой интервал и задаёт следующую точку отправления.'],
  ['Закрепления','Явно закреплённая заявка остаётся у своего инженера. Автоматическое закрепление ближайших визитов действует только при перепланировании и не отменяет остальных ограничений.'],
 ]));
 return panel;
}

function renderAssumptionTravel(request,plan){
 const panel=assumptionPanel('Дорога и инженеры','Модель берётся из диагностики показанного плана. Скорость — допущение для времени переезда, а не измеренная скорость инженера.');
 const model=plan?.diagnostics?.travel_model;
 const descriptions={
  haversine_by_transport_v1:['Оценка по расстоянию между координатами',`Расстояние по сфере × ${assumptionNumber(plan?.diagnostics?.calculation_settings?.execution?.haversine_road_factor??1.25,3)}; время = расстояние / скорость × 60. Реальная дорожная сеть, пробки и расписания не учитываются.`],
  osm_fixed_speed_v1:['Локальная сеть дорог OpenStreetMap','Расстояние рассчитывается по доступным дорогам для транспорта инженера; время = расстояние / его скорость × 60. Пробки не учитываются. Общественный транспорт оценивается по автомобильным дорогам, без линий, пересадок и расписаний.'],
  osrm_driving:['Автомобильные маршруты OSRM','Расстояние и время возвращает автомобильный маршрутизатор. Индивидуальная фиксированная скорость не используется; данные о текущих пробках в запрос не передаются.'],
 };
 const [title,description]=descriptions[model?.kind]||['Модель дороги неизвестна','В сохранённом плане нет распознанной модели. Пересчитайте план, чтобы увидеть используемые допущения.'];
 panel.append(textNode('h3',title),textNode('p',description,'assumption-description'));
 if(plan?.solver&&plan.solver!=='vroom-local')panel.append(textNode('p','Маршрут заканчивается на последней заявке. Возвращение на базу в этом алгоритме не добавляется к дороге или времени смены.','assumption-note'));
 else if(plan?.solver==='vroom-local')panel.append(textNode('p','Этот алгоритм включает возвращение в конечную точку инженера или на его стартовую базу.','assumption-note'));
 const rows=request.engineers.map(engineer=>{
  const travel=model?.engineers?.[engineer.id];
  const mode=travel?.mode||engineer.travel_mode;
  const speed=travel?(travel.speed_kmh==null?'Время по маршрутизатору':`${assumptionNumber(travel.speed_kmh)} км/ч${engineer.travel_speed_kmh==null?' · по умолчанию':' · задано инженеру'}`):'Нет данных о расчёте';
  const shift=`${assumptionDate(engineer.shift?.start)} — ${assumptionDate(engineer.shift?.end)}`;
  const availability=engineer.available===false?'Недоступен':engineer.available_from?`Доступен с ${assumptionDate(engineer.available_from)}`:'Доступен';
  return [engineer.name||engineer.id,`${shift}\n${availability}`,`${assumptionTransportNames[mode]||mode||'не задано'}\n${speed}`,engineer.max_jobs??'Без лимита',`Навыки: ${(engineer.skills||[]).map(s=>skillNames[s]||s).join(', ')||'не заданы'}\nОборудование: ${(engineer.equipment||[]).join(', ')||'не задано'}`];
 });
 panel.append(assumptionDetails(`Параметры всех инженеров (${rows.length})`,assumptionTable(['Инженер','Смена и доступность','Транспорт и скорость','Максимум заявок','Навыки и оборудование'],rows)));
 panel.append(textNode('p','Навыки и оборудование проверяются отдельно. Оборудование считается доступным на всю смену: количество, расходование, передача и пополнение не моделируются.','assumption-note'));
 return panel;
}

function renderAssumptionDurations(request){
 const panel=assumptionPanel('Откуда берётся время работы','Для каждой заявки используется её длительность на месте. Дорога рассчитывается отдельно; длительность работ сама по себе не уменьшается от смены цели оптимизации.');
 const active=request.jobs.filter(job=>!['completed','cancelled'].includes(job.status));
 const groups=new Map();
 let normCount=0;
 for(const job of active){
  const raw=job.metadata?.enrichment?.service_norm;
  const norm=raw&&typeof raw==='object'?raw:null;
  const duration=job.service_minutes??30;
  if(norm)normCount++;
  const source=norm?`${norm.title||norm.code||'Норматив'}${norm.source?.filename?` · ${norm.source.filename}`:''}`:'Длительность, заданная в заявке';
  let basis='Параметр service_minutes из набора данных';
  if(norm){
   basis=`${assumptionNumber(norm.technical_minutes)} мин работ + ${assumptionNumber(norm.documentation_minutes)} мин документов`;
   if(norm.service_minutes!==duration)basis+=`; длительность в текущей заявке изменена на ${assumptionNumber(duration)} мин`;
   if(norm.travel_policy==='routing_replaces_allowance')basis+=`; нормативные ${assumptionNumber(norm.travel_allowance_minutes)} мин дороги заменены расчётом переезда`;
  }
  const key=JSON.stringify([source,duration,basis]);
  if(!groups.has(key))groups.set(key,{source,duration,basis,count:0});
  groups.get(key).count++;
 }
 const total=active.reduce((sum,job)=>sum+(job.service_minutes??30),0);
 panel.append(assumptionFactList([
  ['Объём активных работ',`${assumptionNumber(total/60,2)} чел·ч на месте по полным длительностям всех активных заявок, включая неназначенные. При перепланировании для выполняемых работ в показателях плана остаётся только неотработанная часть.`],
  ['Источник длительности',`${normCount} заявок содержат сведения о нормативе; ${active.length-normCount} используют длительность из набора без записи о нормативе. Значение модели по умолчанию при отсутствии длительности — 30 минут.`],
 ]));
 panel.append(assumptionDetails(`Длительности и источники (${groups.size} групп)`,assumptionTable(['Источник в наборе','Минут на месте','Заявок','Основание'],[...groups.values()].map(group=>[group.source,assumptionNumber(group.duration),group.count,group.basis]))));
 return panel;
}

function renderAssumptionWeights(request,plan){
 const panel=assumptionPanel('Дополнительные коэффициенты','Это безразмерные настройки алгоритма. Они не являются ставками оплаты, стоимостью часа или денежной выгодой.');
 const local=Array.isArray(plan?.diagnostics?.objective_order);
 const replan=!!request.previous_plan;
 const costDescription=replan
  ?'При перепланировании целей «Меньше инженеров» и «Соблюдать SLA» сумма дополнительных штрафов и взвешенного пробега сравнивается после более важных критериев. При цели «Меньше километров» пробег сравнивается напрямую. Точное место срочности и остальных критериев показано в списке выше.'
  :'В исходном плане назначения, выбранная цель и раннее начало срочных сравниваются отдельными критериями в указанном выше порядке. Дополнительные штрафы решают только оставшиеся равенства.';
 panel.append(textNode('p',local?costDescription:'У этого алгоритма в диагностике нет порядка критериев; роль коэффициентов может отличаться от основного алгоритма вставок.','assumption-note'));
 const definitions=[
  ['sla_violation','За нарушение SLA','За каждую назначенную заявку с опозданием; сверху добавляются минуты опоздания.'],
  ['travel_minutes','За минуту дороги','Умножается на время переездов.'],
  ['distance_km','За километр',local?'Взвешивает пробег в общей дополнительной стоимости при перепланировании; в исходном плане пробег сравнивается напрямую.':'Коэффициент пробега.'],
  ['plan_churn','За смену инженера','При наличии предыдущего плана — за назначение заявки другому инженеру.'],
  ['schedule_shift','За минуту сдвига','При наличии предыдущего плана — за абсолютный сдвиг начала визита раньше или позже.'],
  ['unassigned','За неназначение',local?'Не участвует в выборе основного алгоритма: число неназначенных сравнивается отдельным более важным критерием.':'Роль зависит от используемого алгоритма.'],
  ['overtime','За переработку',local?'Не участвует в выборе основного алгоритма: завершение работ в пределах смены — обязательное ограничение.':'Роль зависит от используемого алгоритма.'],
  ['workload_imbalance','За неравномерную загрузку',local?'Не участвует в выборе основного алгоритма; неравномерность загрузки можно видеть в показателях плана.':'Роль зависит от используемого алгоритма.'],
 ];
 panel.append(assumptionDetails('Значения коэффициентов и область влияния',assumptionTable(['Коэффициент','Значение','Как применяется'],definitions.map(([key,label,meaning])=>[label,assumptionNumber(request.weights?.[key],3),meaning]))));
 return panel;
}

function renderAssumptions(){
 const content=document.getElementById('assumptionsContent');
 if(!content)return;
 if(assumptionsRenderedRequest===currentRequest&&assumptionsRenderedPlan===currentPlan&&content.childNodes.length){syncAssumptionsControls();return}
 assumptionsRenderedRequest=currentRequest;assumptionsRenderedPlan=currentPlan;
 content.replaceChildren();
 if(!currentRequest){
  const panel=assumptionPanel('Нужны исходные данные','Откройте набор данных или рабочий план. У отдельного учебного сценария перепланирования нет доступного для редактирования запроса.');
  content.append(panel);return;
 }
 const request=currentRequest,plan=currentPlan;
 content.append(typeof renderPlannerSettings==='function'?renderPlannerSettings(request,plan):renderAssumptionSettings(request));
 const profile=renderAssumptionProfile(request);
 if(profile)content.append(profile);
 const columns=textNode('div','','assumption-columns');
 columns.append(renderAssumptionObjectives(request,plan),renderAssumptionRules(request));
 content.append(columns,renderAssumptionDurations(request),renderAssumptionTravel(request,plan),renderAssumptionWeights(request,plan));
 syncAssumptionsControls();
}
