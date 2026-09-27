let explanationController=null;
const decisionNode=id=>document.getElementById(id);
const alternativeLabels={better:'Лучше по выбранной цели',equal:'Та же оценка',worse:'Хуже по выбранной цели',infeasible:'Не подходит',executing:'Работа выполняется'};
function closeJobExplanation(){
 explanationController?.abort();explanationController=null;
 if(typeof closeManualAssignment==='function')closeManualAssignment();
 const dialog=decisionNode('explanationDialog');if(dialog?.open)dialog.close();
}
function explainButton(jobId,label){
 const button=textNode('button',label,'btn explain-action');button.type='button';
 button.onclick=()=>showJobExplanation(jobId);
 return button;
}
function signed(value){return Math.abs(value)<0.005?'≈0':`${value>0?'+':''}${value.toFixed(2)}`}
function renderJobExplanation(result){
 decisionNode('explanationTitle').textContent=`Разбор заявки ${result.job_id}: ${result.title}`;
 const goal={staff_first:'меньше инженеров',distance_first:'меньше километров',sla_first:'соблюдение SLA'}[result.optimization_policy];
 decisionNode('explanationScope').textContent=`Цель сравнения: ${goal}. ${result.scope}`;
 const facts=decisionNode('explanationFacts');
 facts.replaceChildren(...result.summary.map(line=>textNode('li',line)));
 const rows=decisionNode('explanationAlternatives');rows.replaceChildren();
 for(const alternative of result.alternatives){
  const row=textNode('details','','candidate-row');
  const heading=textNode('summary');
  heading.append(textNode('b',`${alternative.engineer_name} (${alternative.engineer_id})`),
   textNode('span',alternativeLabels[alternative.comparison],`candidate-status ${alternative.comparison}`));
  if(alternative.is_current_engineer)heading.append(textNode('span','Показанный исполнитель','badge'));
  const reason=alternative.first_difference?.message||alternative.blockers[0]?.message;
  if(reason)heading.append(textNode('div',reason,'candidate-reason'));
  row.append(heading);
  const body=textNode('div','','candidate-body');
  if(alternative.feasible&&alternative.comparison!=='executing'){
   appendTime(body,'Прибытие: ',alternative.arrival);
   appendTime(body,`Визит №${alternative.position}, начало: `,alternative.service_start);
   appendTime(body,'Окончание: ',alternative.departure);
   body.append(textNode('p',`Переезд от предыдущей точки: ${alternative.leg_distance_km.toFixed(2)} км · ${alternative.leg_travel_minutes.toFixed(1)} мин.`));
   body.append(textNode('p',`Изменение всего плана: ${signed(alternative.plan_distance_delta_km)} км · ${signed(alternative.plan_travel_delta_minutes)} мин в пути · ${alternative.used_engineers_delta>0?'+':''}${alternative.used_engineers_delta} инженеров.`));
  }
  if(alternative.comparison==='executing')body.append(textNode('p','Сохраняется фактический интервал; позиции переноса не проверяются.'));
  if(alternative.positions_tested)body.append(textNode('p',`Проверено позиций: ${alternative.positions_tested}. Для допустимого варианта показана лучшая оценка по выбранной цели.`,'muted'));
  if(alternative.blockers.length){
   const list=textNode('ul');
   for(const blocker of alternative.blockers){
    const prefix=blocker.stage==='source_route'?'После удаления из исходного маршрута: ':'';
    const example=blocker.example_position?` Пример: вставка №${blocker.example_position}.`:'';
    list.append(textNode('li',`${prefix}${blocker.message}${blocker.positions>1?` · блокирует ${blocker.positions} позиций`:''}.${example}`));
   }
   body.append(list);
   if(alternative.positions_tested>1)body.append(textNode('p','Для каждой позиции указано первое обнаруженное препятствие; повторения сгруппированы. Времена относятся к конкретной пробной вставке.','muted'));
  }
  row.append(body);rows.append(row);
 }
 decisionNode('explanationStatus').textContent=`Проверено инженеров: ${result.alternatives.length}. Показанный план не изменён.`;
}
async function showJobExplanation(jobId){
 if(!currentRequest||!currentPlan)return;
 closeJobExplanation();
 const source=currentRequest,plan=currentPlan,version=requestVersion;
 const controller=new AbortController();explanationController=controller;
 decisionNode('explanationTitle').textContent=`Разбор заявки ${jobId}`;
 decisionNode('explanationFacts').replaceChildren();decisionNode('explanationAlternatives').replaceChildren();
 decisionNode('explanationScope').textContent='Проверяем ограничения и пробные вставки в показанный план.';
 decisionNode('explanationStatus').textContent='Расчёт альтернатив…';
 if(typeof openManualAssignment==='function')openManualAssignment(jobId);
 decisionNode('explanationDialog').showModal();
 try{
  const response=await fetch('/api/v1/explanations/job',{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify({request:source,plan,job_id:jobId})});
  const result=await response.json();
  if(controller.signal.aborted||version!==requestVersion||currentPlan!==plan||currentRequest!==source)return;
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось проверить заявку.');
  renderJobExplanation(result);
 }catch(error){if(!controller.signal.aborted)decisionNode('explanationStatus').textContent=`Разбор недоступен: ${error.message}`}
}
decisionNode('closeExplanationBtn').onclick=closeJobExplanation;
decisionNode('explanationDialog').addEventListener('cancel',closeJobExplanation);
