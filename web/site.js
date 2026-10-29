// All same-origin mutations use Django CSRF protection. No credentials enter JS storage.
(()=>{
 const nativeFetch=window.fetch.bind(window);
 const ready=nativeFetch('/api/site/session',{credentials:'same-origin'}).then(r=>{if(!r.ok)throw Error('会话初始化失败');return r.json()});
 window.fetch=async(input,options={})=>{
  const url=new URL(typeof input==='string'?input:input.url,location.href);
  if(url.origin!==location.origin)return nativeFetch(input,options);
  await ready;
  const method=(options.method||'GET').toUpperCase();
  if(!['GET','HEAD','OPTIONS'].includes(method)){
   const headers=new Headers(options.headers||{});
   const cookie=document.cookie.split('; ').find(v=>v.startsWith('csrftoken='));
   if(cookie)headers.set('X-CSRFToken',decodeURIComponent(cookie.split('=').slice(1).join('=')));
   options={...options,headers,credentials:'same-origin'};
  }
  const response=await nativeFetch(input,options);
  if(response.status===401&&location.pathname!=='/eschool'&&location.pathname!=='/login')location.replace('/eschool');
  return response;
 };
 document.addEventListener('DOMContentLoaded',()=>{
  const logout=document.createElement('button');logout.textContent='退出网站';logout.type='button';
  logout.style.cssText='float:right;font:inherit;font-size:13px;padding:4px 10px;margin:0 0 12px 12px';
  logout.onclick=async()=>{await fetch('/api/site/logout',{method:'POST'});location.replace('/eschool')};
  (document.querySelector('main')||document.querySelector('header')||document.body).prepend(logout);
  ready.then(s=>{
   if(s.authenticated&&location.pathname==='/eschool'){location.replace('/');return}
   if(s.is_admin){const admin=document.createElement('a');admin.href='/admin/users';admin.textContent='用户管理';admin.style.cssText='float:right;margin:0 12px;color:#2563eb';(document.querySelector('main')||document.body).prepend(admin)}
   if(!s.authenticated)return;
   const manage=document.createElement('details');
   manage.style.cssText='max-width:900px;margin:16px auto;padding:0 22px;font:13px system-ui;color:#596475';
   const title=document.createElement('summary');title.textContent='账号与后台采集';manage.append(title);
   const note=document.createElement('p');note.textContent='退出网站只清除浏览器登录，后台采集继续运行。';manage.append(note);
   const stop=document.createElement('button');stop.textContent='停止采集并清除授权';stop.type='button';
   stop.onclick=async()=>{stop.disabled=true;const r=await fetch('/api/site/disconnect',{method:'POST'});if(r.ok)location.replace('/eschool');else stop.disabled=false};
   manage.append(stop);document.body.append(manage);
  });
 });
})();
