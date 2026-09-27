// One atomic workspace snapshot per origin. A revision prevents silent writes from stale tabs.
class StateConflictError extends Error {}
class StateStore {
  constructor(){this.connection=null}
  open(){
    if(this.connection)return this.connection;
    this.connection=new Promise((resolve,reject)=>{
      const request=indexedDB.open('fieldroute-workspace',1);
      let failed=false;
      const fail=error=>{failed=true;this.connection=null;reject(error)};
      request.onupgradeneeded=()=>request.result.createObjectStore('snapshots');
      request.onerror=()=>fail(request.error);
      request.onblocked=()=>fail(new Error('Хранилище занято другой версией приложения.'));
      request.onsuccess=()=>{
        const db=request.result;
        if(failed){db.close();return}
        db.onversionchange=()=>{db.close();this.connection=null};
        resolve(db);
      };
    });
    return this.connection;
  }
  async read(){
    const db=await this.open();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('snapshots','readonly');
      const request=tx.objectStore('snapshots').get('current');
      tx.oncomplete=()=>resolve(request.result||null);
      tx.onabort=()=>reject(tx.error||new Error('Чтение состояния прервано.'));
    });
  }
  async write(json,expectedRevision){
    const db=await this.open();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('snapshots','readwrite'),store=tx.objectStore('snapshots');
      let error=null;
      const row={revision:crypto.randomUUID(),json};
      const request=store.get('current');
      request.onsuccess=()=>{
        if((request.result?.revision||null)!==expectedRevision){
          error=new StateConflictError('В другой вкладке уже сохранено новое состояние.');
          tx.abort();return;
        }
        store.put(row,'current');
      };
      tx.oncomplete=()=>resolve(row);
      tx.onabort=()=>reject(error||tx.error||new Error('Запись состояния прервана.'));
    });
  }
  async replaceWithBackup(json,expectedRevision,sourceJson){
    const db=await this.open();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('snapshots','readwrite'),store=tx.objectStore('snapshots');
      let error=null;
      const row={revision:crypto.randomUUID(),json};
      const request=store.get('current');
      request.onsuccess=()=>{
        const previous=request.result;
        if((previous?.revision||null)!==expectedRevision){
          error=new StateConflictError('В другой вкладке уже сохранено новое состояние.');
          tx.abort();return;
        }
        const originals=[...(previous?[previous.json]:[])];
        if(sourceJson&&!originals.includes(sourceJson))originals.push(sourceJson);
        for(const original of originals){
          const backup={id:crypto.randomUUID(),created_at:new Date().toISOString(),json:original,reason:'distance_model_recovery'};
          store.put(backup,`backup:${backup.id}`);
        }
        store.put(row,'current');
      };
      // Both the old snapshots and the replacement commit, or neither does.
      tx.oncomplete=()=>resolve(row);
      tx.onabort=()=>reject(error||tx.error||new Error('Не удалось сохранить резервную копию.'));
    });
  }
  async readBackups(){
    const db=await this.open();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('snapshots','readonly');
      const request=tx.objectStore('snapshots').getAll();
      tx.oncomplete=()=>resolve(request.result.filter(row=>row.reason==='distance_model_recovery')
        .sort((a,b)=>b.created_at.localeCompare(a.created_at)));
      tx.onabort=()=>reject(tx.error||new Error('Не удалось прочитать резервные копии.'));
    });
  }
}
