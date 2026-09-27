let workspaceScreen='plan';
const workspaceCopy={
 plan:['План и эффект','Как получен план, что даёт оптимизация и чем отличаются варианты.'],
 routes:['Маршруты и расписание','Выберите инженера или заявку на карте. Добавьте событие, чтобы пересчитать оставшуюся работу.'],
 data:['Данные для планирования','Откройте готовый пример, загрузите заявки или продолжите сохранённый план.'],
 assumptions:['Допущения и параметры','Настройки поиска, бюджеты, цели, переезды и восстановление значений по умолчанию.'],
};
function showWorkspaceScreen(screen,{focus=false}={}){
 if(screen==='analysis')screen='plan';
 if(!Object.hasOwn(workspaceCopy,screen))screen='plan';
 workspaceScreen=screen;
 for(const [name,id] of Object.entries({plan:'overviewScreen',routes:'planScreen',data:'dataScreen',assumptions:'assumptionsScreen'}))document.getElementById(id).hidden=screen!==name;
 document.querySelectorAll('.workspace-nav [data-screen]').forEach(button=>{
  if(button.dataset.screen===screen)button.setAttribute('aria-current','page');
  else button.removeAttribute('aria-current');
 });
 document.getElementById('openEventBtn').hidden=screen!=='routes';
 document.querySelector('.workspace-source [data-screen]').hidden=screen==='data';
 refreshWorkspace();
 if(screen==='routes')requestAnimationFrame(()=>routeMap.map.invalidateSize());
 if(focus){const heading=document.getElementById('workspaceTitle');heading.tabIndex=-1;heading.focus({preventScroll:true});window.scrollTo({top:0,behavior:'instant'})}
}
function refreshWorkspace(){
 const [title,hint]=workspaceCopy[workspaceScreen];
 document.getElementById('workspaceTitle').textContent=title;
 document.getElementById('workspaceHint').textContent=hint;
 document.getElementById('openEventBtn').disabled=!currentRequest||!currentPlan||document.getElementById('shell').classList.contains('loading');
 document.getElementById('incidentHint').hidden=!currentDataset;
 document.getElementById('eventCount').textContent=`(${currentDataset?.events?.length||0})`;
 if(typeof renderOverview==='function')renderOverview();
 if(workspaceScreen==='assumptions'&&typeof renderAssumptions==='function')renderAssumptions();
}
function closeEventForm(){document.getElementById('eventDetails').close()}
document.querySelectorAll('[data-screen]').forEach(button=>button.addEventListener('click',()=>showWorkspaceScreen(button.dataset.screen,{focus:true})));
document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();showWorkspaceScreen('plan',{focus:true})});
document.getElementById('openEventBtn').onclick=()=>{
 openEventDialog();
};
document.getElementById('closeEventBtn').onclick=closeEventForm;
document.getElementById('cancelEventBtn').onclick=closeEventForm;
document.getElementById('eventDetails').addEventListener('cancel',event=>{
 if(document.getElementById('shell').classList.contains('loading'))event.preventDefault();
});
// Native validation must reveal invalid fields even when advanced settings are collapsed.
document.getElementById('eventForm').addEventListener('invalid',event=>{
 let parent=event.target.parentElement;
 while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement}
},true);
showWorkspaceScreen('plan');
