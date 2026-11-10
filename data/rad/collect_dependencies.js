async page => page.evaluate(refs => {
  const c = window.__radCapture;
  if (!c || c.stop) throw Error('Capture not running');
  if (window.__radDependencyCapture && !window.__radDependencyCapture.finished) throw Error('Already running');
  const d = window.__radDependencyCapture = {done:0,total:refs.length,errors:[],finished:false};
  d.worker = (async () => {
    for (const ref of refs) {
      if (c.stop) break;
      const wait = Math.max(0, c.nextRequestAt - Date.now());
      c.nextRequestAt = Math.max(Date.now(), c.nextRequestAt) + 2000;
      await new Promise(resolve => setTimeout(resolve, wait));
      if (c.stop) break;
      try {
        const response = await fetch('/api/airspace/v1/az/' + encodeURIComponent(ref.aerodromeSetId)
          + '?' + new URLSearchParams({backendId:ref.backendId,branchId:'production'}),
          {headers:{accept:'application/json','content-type':'application/json'}});
        if (!response.ok) throw Error('HTTP '+response.status);
        const entity = await response.json();
        if (!entity.ref || entity.ref.backendId !== ref.backendId || entity.ref.uuid !== ref.uuid
            || !Array.isArray(entity.states)) throw Error('Invalid dependency identity');
        window.__radDependencies['az:'+ref.backendId] = entity;
        d.done++;
      } catch(error) {d.errors.push({ref,error:String(error)});c.stop=true;break;}
    }
    d.finished=true;
  })();
  return {pending:d.total};
}, [{"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFMM", "uuid": "102be7ae-3491-4ef2-9dcb-7f1aa86eafb1", "aerodromeSetId": "LFMM"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "EB-6", "uuid": "3cb3387f-2a7b-48e2-b4fd-fde92618df0c", "aerodromeSetId": "EB-6"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFRBTMA:2", "uuid": "76c6ec9a-d076-43cf-a1d4-d2e3ee853211", "aerodromeSetId": "LFRBTMA"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFBB-1", "uuid": "d555aefc-3d67-411a-a586-1bcea38aa04c", "aerodromeSetId": "LFBB-1"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFP", "uuid": "09dffaa3-2cca-4fe1-bb04-98f566957b05", "aerodromeSetId": "LFP"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LIM-MEP", "uuid": "73109666-2f2d-4bcd-9ce0-468ea304e38e", "aerodromeSetId": "LIM-MEP"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "EDLLFIR:1", "uuid": "b896560a-60e1-4692-b5ea-250b56e198cd", "aerodromeSetId": "EDLLFIR"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFBB11", "uuid": "a9a7086e-152b-43e3-9be0-139c6cd7fac4", "aerodromeSetId": "LFBB11"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "TALIM2", "uuid": "12f1bf4f-2db3-4dd3-809a-5848b48a344d", "aerodromeSetId": "TALIM2"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "TALIR1", "uuid": "50b8263a-82c7-4dcd-9c1a-8c0124609c75", "aerodromeSetId": "TALIR1"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFO", "uuid": "33219dc5-5060-4c09-a2c5-91bba48f48bb", "aerodromeSetId": "LFO"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LS-GG", "uuid": "b45fb2bb-3960-4933-8166-e7b889e5223c", "aerodromeSetId": "LS-GG"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFL3:1", "uuid": "4beeb0ed-18a7-44c6-b244-6e130e6913e4", "aerodromeSetId": "LFL3"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LFMMFIR3", "uuid": "46d8ce36-668d-43ba-ae6c-1ca73c25e4da", "aerodromeSetId": "LFMMFIR3"}, {"@class": "eurocontrol.nconect.airspace.em.az.AerodromeSetRef", "backendId": "LS5", "uuid": "8cd8eb84-5adb-4d38-ba82-e34d766f7b4c", "aerodromeSetId": "LS5"}])
