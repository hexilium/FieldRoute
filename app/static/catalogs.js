// Workspace-local editable catalogs. They seed new event drafts; an existing plan is untouched.
let editableCatalogs=null;
let catalogDefaults=null;
const catalogSkillLabels={local:'Локальные работы',connection:'Подключения',additional_order:'Дозаказы',emergency:'Аварийные работы',fiber:'Оптика',router:'Маршрутизаторы',voice:'Телефония'};
const cloneCatalogs=value=>JSON.parse(JSON.stringify(value));
const catalogIdPattern=/^[\p{L}\p{N}_.-]+$/u;
function safeCatalogIds(values){
 // PlanRequest intentionally accepts opaque capability strings, while editable
 // catalogs have stricter UI/state limits. Keep an automatically derived
 // catalog valid so an otherwise valid large plan can always be restored.
 return [...new Set(values.map(value=>String(value)).filter(value=>value.length<=100&&catalogIdPattern.test(value)))].sort().slice(0,200);
}
function catalogsFromRequest(request){
 const skillValues=(request?.engineers||[]).flatMap(item=>item.skills||[]);
 for(const job of request?.jobs||[])skillValues.push(...(job.required_skills||[]));
 const skillIds=safeCatalogIds(skillValues);if(!skillIds.length)skillIds.push('local');
 const equipmentValues=(request?.engineers||[]).flatMap(item=>item.equipment||[]);
 for(const job of request?.jobs||[])equipmentValues.push(...(job.required_equipment||[]));
 const equipmentIds=safeCatalogIds(equipmentValues);
 const skills=skillIds.map(id=>({id,label:catalogSkillLabels[id]||id}));
 const equipment=equipmentIds.map(id=>({id,label:id}));
 const templates=[
  {id:'urgent',title:'Срочная заявка',service_minutes:45,skill:skillIds.includes('emergency')?'emergency':skills[0].id,priority:100,equipment:[]},
  {id:'unplanned',title:'Внеплановая заявка',service_minutes:30,skill:skills[0].id,priority:50,equipment:[]},
 ];
 const knownTemplates={local:{id:'local_repair',title:'Локальный ремонт',service_minutes:30},connection:{id:'connection',title:'Подключение',service_minutes:70},additional_order:{id:'additional_order',title:'Дозаказ оборудования',service_minutes:20}};
 for(const [skill,template] of Object.entries(knownTemplates))if(skillIds.includes(skill))templates.push({...template,skill,priority:50,equipment:[]});
 return {skills,equipment,templates};
}
function normalizeCatalogs(value,request){
 const fallback=catalogsFromRequest(request);
 if(!value||!Array.isArray(value.skills)||!Array.isArray(value.equipment)||!Array.isArray(value.templates))return fallback;
 return {
  skills:value.skills.map(item=>({id:String(item.id||'').trim(),label:String(item.label||item.id||'').trim()})),
  equipment:value.equipment.map(item=>({id:String(item.id||'').trim(),label:String(item.label||item.id||'').trim()})),
  templates:value.templates.map(item=>({id:String(item.id||'').trim(),title:String(item.title||'').trim(),service_minutes:Number(item.service_minutes),skill:String(item.skill||''),priority:Number(item.priority),equipment:[...(item.equipment||[])].map(String)})),
 };
}
function setEditableCatalogs(value,request=currentRequest){
 catalogDefaults=catalogsFromRequest(request);
 editableCatalogs=normalizeCatalogs(value,request);
 renderCatalogEditor();
 syncCatalogConsumers();
}
function currentCatalogSnapshot(){return editableCatalogs?cloneCatalogs(editableCatalogs):null}
function catalogTemplate(id){return editableCatalogs?.templates.find(item=>item.id===id)||null}
function labeledControl(label,control,className=''){
 const wrapper=textNode('label',label,className);wrapper.append(control);return wrapper;
}
function catalogInput(field,value,type='text'){
 const input=document.createElement('input');input.type=type;input.value=value??'';input.dataset.field=field;
 if(['id','skill'].includes(field))input.maxLength=100;
 if(['label','title'].includes(field))input.maxLength=300;
 return input;
}
function removeCatalogRow(button){button.closest('[data-catalog-row]')?.remove()}
function namedCatalogRow(kind,item={id:'',label:''}){
 const row=textNode('div','','catalog-row');row.dataset.catalogRow=kind;
 const id=catalogInput('id',item.id),label=catalogInput('label',item.label);
 id.autocomplete=label.autocomplete='off';
 if(kind==='skill'){
  id.dataset.previousId=item.id||'';
  id.oninput=()=>{
   const previous=id.dataset.previousId,current=id.value.trim();
   for(const field of document.querySelectorAll('[data-catalog-row="template"] [data-field="skill"]'))if(field.value===previous)field.value=current;
   id.dataset.previousId=current;refreshCatalogSkillSuggestions();
  };
  label.oninput=refreshCatalogSkillSuggestions;
 }
 const remove=textNode('button','Удалить','catalog-remove');remove.type='button';remove.onclick=()=>removeCatalogRow(remove);
 row.append(labeledControl('Код',id),labeledControl('Название',label),remove);return row;
}
function templateCatalogRow(item={id:'',title:'',service_minutes:30,skill:'',priority:50,equipment:[]}){
 const row=textNode('div','','template-row');row.dataset.catalogRow='template';
 const id=catalogInput('id',item.id),title=catalogInput('title',item.title),duration=catalogInput('service_minutes',item.service_minutes,'number'),priority=catalogInput('priority',item.priority,'number'),equipment=catalogInput('equipment',(item.equipment||[]).join(', '));
 duration.min='1';duration.max='1440';priority.min='0';priority.max='100';
 const skill=catalogInput('skill',item.skill||editableCatalogs?.skills[0]?.id||'');skill.setAttribute('list','catalogSkillIds');
 const remove=textNode('button','Удалить','catalog-remove');remove.type='button';remove.onclick=()=>removeCatalogRow(remove);
 row.append(labeledControl('Код',id),labeledControl('Название работы',title,'template-title'),labeledControl('Минут',duration),labeledControl('Навык',skill),labeledControl('Приоритет',priority),labeledControl('Оборудование',equipment),remove);return row;
}
function refreshCatalogSkillSuggestions(){
 const suggestions=document.getElementById('catalogSkillIds');
 const rows=[...document.querySelectorAll('[data-catalog-row="skill"]')];
 suggestions.replaceChildren(...rows.map(row=>{
  const option=document.createElement('option');option.value=row.querySelector('[data-field="id"]').value.trim();
  option.label=row.querySelector('[data-field="label"]').value.trim();return option;
 }).filter(option=>option.value));
}
function renderCatalogEditor(){
 if(!editableCatalogs)return;
 const skills=document.getElementById('skillCatalogRows'),equipment=document.getElementById('equipmentCatalogRows'),templates=document.getElementById('templateCatalogRows');
 skills.replaceChildren(...editableCatalogs.skills.map(item=>namedCatalogRow('skill',item)));
 equipment.replaceChildren(...editableCatalogs.equipment.map(item=>namedCatalogRow('equipment',item)));
 templates.replaceChildren(...editableCatalogs.templates.map(templateCatalogRow));
 refreshCatalogSkillSuggestions();
 document.getElementById('skillCatalogCount').textContent=`(${editableCatalogs.skills.length})`;
 document.getElementById('equipmentCatalogCount').textContent=`(${editableCatalogs.equipment.length})`;
 document.getElementById('templateCatalogCount').textContent=`(${editableCatalogs.templates.length})`;
}
function collectNamedCatalog(kind){
 return [...document.querySelectorAll(`[data-catalog-row="${kind}"]`)].map(row=>({id:row.querySelector('[data-field="id"]').value.trim(),label:row.querySelector('[data-field="label"]').value.trim()}));
}
function collectTemplates(){
 return [...document.querySelectorAll('[data-catalog-row="template"]')].map(row=>({
  id:row.querySelector('[data-field="id"]').value.trim(),title:row.querySelector('[data-field="title"]').value.trim(),
  service_minutes:Number(row.querySelector('[data-field="service_minutes"]').value),skill:row.querySelector('[data-field="skill"]').value,
  priority:Number(row.querySelector('[data-field="priority"]').value),equipment:[...new Set(row.querySelector('[data-field="equipment"]').value.split(',').map(value=>value.trim()).filter(Boolean))],
 }));
}
function validateCatalogItems(items,label){
 if(items.length>200)throw new Error(`${label}: можно сохранить не больше 200 записей.`);
 if(items.some(item=>!item.id||!item.label))throw new Error(`${label}: заполните код и название.`);
 if(items.some(item=>item.id.length>100||item.label.length>300))throw new Error(`${label}: код — до 100 символов, название — до 300.`);
 if(items.some(item=>!catalogIdPattern.test(item.id)))throw new Error(`${label}: код может содержать буквы, цифры, точку, дефис и подчёркивание.`);
 if(new Set(items.map(item=>item.id)).size!==items.length)throw new Error(`${label}: коды не должны повторяться.`);
}
function validateCatalogDraft(skills,equipment,templates){
 validateCatalogItems(skills,'Навыки');validateCatalogItems(equipment,'Оборудование');
 if(!skills.length)throw new Error('Добавьте хотя бы один навык.');
 if(templates.length>200)throw new Error('Шаблоны: можно сохранить не больше 200 записей.');
 if(templates.some(item=>!item.id||!item.title))throw new Error('Шаблоны: заполните код и название.');
 if(templates.some(item=>item.id.length>100||item.title.length>300||item.skill.length>100))throw new Error('Шаблоны: код и навык — до 100 символов, название — до 300.');
 if(new Set(templates.map(item=>item.id)).size!==templates.length)throw new Error('Шаблоны: коды не должны повторяться.');
 if(templates.some(item=>!catalogIdPattern.test(item.id)))throw new Error('Шаблоны: проверьте формат кода.');
 const skillIds=new Set(skills.map(item=>item.id)),equipmentIds=new Set(equipment.map(item=>item.id));
 if(templates.some(item=>!skillIds.has(item.skill)))throw new Error('Шаблоны: выберите существующий навык.');
 if(templates.some(item=>!Number.isInteger(item.service_minutes)||item.service_minutes<1||item.service_minutes>1440))throw new Error('Шаблоны: длительность должна быть от 1 до 1440 минут.');
 if(templates.some(item=>!Number.isInteger(item.priority)||item.priority<0||item.priority>100))throw new Error('Шаблоны: приоритет должен быть от 0 до 100.');
 if(templates.some(item=>item.equipment.length>100))throw new Error('Шаблоны: не больше 100 позиций оборудования в одном шаблоне.');
 const unknown=templates.flatMap(item=>item.equipment).find(id=>!equipmentIds.has(id));
 if(unknown)throw new Error(`Сначала добавьте «${unknown}» в справочник оборудования.`);
}
function syncCatalogConsumers(){
 if(typeof skillNames!=='undefined'&&editableCatalogs)for(const item of editableCatalogs.skills)skillNames[item.id]=item.label;
 if(typeof syncEventCatalogOptions==='function')syncEventCatalogOptions();
 if(typeof refreshCatalogTools==='function')refreshCatalogTools();
}
async function saveCatalogEditor(){
 const status=document.getElementById('catalogStatus');status.className='muted';
 try{
  const skills=collectNamedCatalog('skill'),equipment=collectNamedCatalog('equipment'),templates=collectTemplates();
  validateCatalogDraft(skills,equipment,templates);
  editableCatalogs={skills,equipment,templates};
  if(currentDataset)currentDataset.catalogs=currentCatalogSnapshot();
  renderCatalogEditor();syncCatalogConsumers();
  status.textContent='Справочники сохранены. Они доступны в форме нового события.';
  if(currentPlan&&typeof persistWorkspace==='function')await persistWorkspace();
 }catch(error){status.textContent=error.message;status.className='warn'}
}
async function resetCatalogEditor(){
 editableCatalogs=cloneCatalogs(catalogDefaults||catalogsFromRequest(currentRequest));
 if(currentDataset)currentDataset.catalogs=currentCatalogSnapshot();
 renderCatalogEditor();syncCatalogConsumers();
 const status=document.getElementById('catalogStatus');status.className='muted';status.textContent='Восстановлены значения из текущего набора данных.';
 if(currentPlan&&typeof persistWorkspace==='function')await persistWorkspace();
}
function refreshCatalogTools(){
 const busy=document.getElementById('shell').classList.contains('loading'),unavailable=!currentRequest||!currentPlan;
 for(const id of ['saveCatalogsBtn','resetCatalogsBtn','addSkillBtn','addEquipmentBtn','addTemplateBtn'])document.getElementById(id).disabled=busy||unavailable;
 for(const id of ['dataManualEventBtn','dataTemplateEventBtn','dataEventTemplate','dataEventJsonFile'])document.getElementById(id).disabled=busy||unavailable;
 document.getElementById('dataTemplateEventBtn').disabled=busy||unavailable||!document.getElementById('dataEventTemplate').value;
}
document.getElementById('addSkillBtn').onclick=()=>{document.getElementById('skillCatalogRows').append(namedCatalogRow('skill'));refreshCatalogSkillSuggestions()};
document.getElementById('addEquipmentBtn').onclick=()=>document.getElementById('equipmentCatalogRows').append(namedCatalogRow('equipment'));
document.getElementById('addTemplateBtn').onclick=()=>document.getElementById('templateCatalogRows').append(templateCatalogRow());
document.getElementById('saveCatalogsBtn').onclick=saveCatalogEditor;
document.getElementById('resetCatalogsBtn').onclick=resetCatalogEditor;
