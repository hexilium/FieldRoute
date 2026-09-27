let planningController=null;
let progressTimer=null;
let progressStarted=0;
let progressSnapshot=null;

function renderPlanningProgress(){
 const state=progressSnapshot;
 const elapsed=Math.floor((performance.now()-progressStarted)/1000);
 const phases={starting:'Запускаем расчёт',routing:'Подготавливаем расстояния',search:'Подбираем маршруты',finalization:'Готовим маршруты и объяснения',validation:'Проверяем ограничения плана'};
 let title=state?(phases[state.phase]||'Рассчитываем план'):'Подготавливаем данные';
 const details=[];
 if(state){
  if(state.plan_number>1)details.push(`Расчёт ${state.plan_number}`);
  if(state.district_id)details.push(`Район ${state.district_number}/${state.district_count}: ${state.district_id}`);
  if(state.phase==='search'){
   const goals={staff_first:'меньше инженеров',distance_first:'меньше километров',sla_first:'соблюдать сроки',fifo:'по порядку поступления'};
   const [goal,step]=(state.stage||'').split(':');
   const steps={construction:'Строим начальные маршруты',initial_finish:'Улучшаем начальные маршруты',refined:'Улучшаем распределение',polished:'Улучшаем отдельные визиты',compound:'Проверяем обмены заявками',consolidated:'Объединяем маршруты',ejection:'Ищем места для пропущенных заявок',repair:'Проверяем альтернативные размещения заявок',segments:'Улучшаем участки маршрутов',urgent:'Ускоряем начало срочных',or_opt:'Переносим группы визитов',cross_exchange:'Обмениваем группы между маршрутами',deep_repair:'Углубляем цепочки назначений',stability_repair:'Сохраняем прежний план',cyclic_exchange:'Переставляем заявки по трём маршрутам',cyclic_reinsertion:'Ищем позиции для трёхмаршрутного цикла'};
   title=steps[step]||title;
   if(goals[goal])details.push(goals[goal]);
   if(goal==='parallel')details.push('Одновременно проверяем разные цели');
   if(state.processed_jobs)details.push(`Разобрано в этом проходе: ${state.processed_jobs}/${state.total_jobs}`);
   if(state.search_strategy==='compromise'){
    title='Улучшаем SLA в пределах ресурсов';
    details.push(`Бюджет: ${(state.budget_attempts||0).toLocaleString('ru-RU')} / ${(state.attempt_limit||0).toLocaleString('ru-RU')}`);
   }else details.push(`Готовых вариантов: ${state.completed_candidates}/${state.total_candidates}`);
   details.push(`Лучшее покрытие: ${state.best_assigned}/${state.total_jobs}`);
   details.push(`Проверок расписаний: ${(state.schedule_attempts||0).toLocaleString('ru-RU')}`);
  }
 }
 document.getElementById('progressTitle').textContent=`${title} · ${elapsed} с`;
 document.getElementById('progressDetails').textContent=details.join(' · ');
}

function resetPlanningProgress(){
 clearInterval(progressTimer);
 planningController?.abort();planningController=null;
 progressSnapshot=null;progressStarted=performance.now();
 document.getElementById('cancelPlanningBtn').hidden=true;
 renderPlanningProgress();
 progressTimer=setInterval(renderPlanningProgress,500);
}

async function fetchPlanning(url,options={}){
 planningController?.abort();
 const controller=new AbortController();planningController=controller;
 const cancel=document.getElementById('cancelPlanningBtn');cancel.hidden=false;cancel.disabled=false;
 // Keep progress and cancellation reachable while the event dialog is modal.
 const busy=document.getElementById('workspaceBusy');
 const home=busy.parentNode,next=busy.nextSibling;
 if(document.getElementById('eventDetails').open)document.getElementById('eventForm').append(busy);
 busy.scrollIntoView({block:'nearest',behavior:'instant'});
 let reader;
 try{
  const response=await fetch(url,{...options,signal:controller.signal});
  if(!response.ok){
   const error=await response.json();
   throw new Error(typeof error.detail==='string'?error.detail:Array.isArray(error.detail)?error.detail.slice(0,5).map(item=>item.msg).join('; '):`Не удалось построить план (${response.status}).`);
  }
  reader=response.body.getReader();
  const decoder=new TextDecoder();let buffer='';
  while(true){
   const {value,done}=await reader.read();
   buffer+=decoder.decode(value,{stream:!done});
   let boundary;
   while((boundary=buffer.indexOf('\n'))>=0){
    const line=buffer.slice(0,boundary);buffer=buffer.slice(boundary+1);
    if(!line.trim())continue;
    const message=JSON.parse(line);
    if(message.type==='progress'){
     if(planningController===controller){progressSnapshot=message;renderPlanningProgress()}
    }else if(message.type==='result')return message.data;
    else if(message.type==='error')throw new Error(typeof message.detail==='string'?message.detail:'Не удалось завершить расчёт.');
   }
   if(done)throw new Error('Соединение прервалось до получения плана. Повторите расчёт.');
  }
 }catch(error){
  if(controller.signal.aborted)throw new Error('Расчёт отменён. Предыдущий план сохранён.');
  throw error;
 }finally{
  if(reader)await reader.cancel().catch(()=>{});
  if(planningController===controller){planningController=null;cancel.hidden=true;home.insertBefore(busy,next)}
 }
}

document.getElementById('cancelPlanningBtn').onclick=()=>{
 planningController?.abort();document.getElementById('cancelPlanningBtn').disabled=true;
};
