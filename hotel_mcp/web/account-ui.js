"use strict";
const accountRoles = {super_admin:"总管理员",store_admin:"门店管理员",frontdesk:"前台",customer:"客户"};
let accountRows = [], accountOffset = 0, accountNext = null, accountQuery = "";

function clearLogin(message="") {
  state.auth=null;state.session=null;state.requests=[];state.stores=[];state.selected=null;
  state.salesFrom="";state.salesTo="";state.salesHotel="";state.filterType="";state.filterStatus="";
  accountRows=[];accountOffset=0;accountQuery="";retryKeys.clear();
  $("#detail").close();$("#account-dialog").close();$("#connection-dialog").close();$("#account-body").replaceChildren();$("#content").replaceChildren();
  $("#workspace").hidden=true;$("#login").hidden=false;$("#boot").hidden=true;
  $("#login-error").textContent=message;
}

function rememberContext() {
  try {sessionStorage.setItem("hotel-view-context",JSON.stringify({id:state.auth.id,hotel:state.auth.hotel,stay:state.auth.stay}));} catch(_) {}
}

function contextHeader() {
  const session=state.session, account=session.account;
  $("#identity").textContent=`${account.display_name} · ${accountRoles[account.role]}`;
  let html="";
  if(!account.must_change_password && account.role==="super_admin") {
    html=`<label>当前门店<select id="scope-select"><option value="">全部门店 · 经营汇总</option>${session.stores.map(s=>`<option value="${esc(s.hotel)}" ${state.auth.hotel===s.hotel?"selected":""}>${esc(s.name)}</option>`).join("")}</select></label>`;
  } else if(!account.must_change_password && account.role==="customer" && session.stays.length) {
    html=`<label>入住记录<select id="scope-select">${session.stays.map(s=>`<option value="${esc(s.id)}" ${session.principal.stay_id===s.id?"selected":""}>${esc(s.room)}号房 · ${esc(s.check_in_at.slice(0,10))} · ${s.status==="active"?"入住中":"已退房（只读）"}</option>`).join("")}</select></label>`;
  } else if(account.hotel) {
    html=`<span>${esc(session.stores.find(s=>s.hotel===account.hotel)?.name||account.hotel)}</span>`;
  }
  $("#context").innerHTML=html;
}

async function loadWorkspace(restore=false) {
  state.session=await api("session");
  state.auth.id=state.session.principal.id;state.auth.csrf=state.session.csrf_token;
  if(restore && !state.session.account.must_change_password) {
    let remembered;
    try {remembered=JSON.parse(sessionStorage.getItem("hotel-view-context")||"null");} catch(_) {}
    if(remembered?.id===state.auth.id) {
      state.auth={...state.auth,hotel:remembered.hotel||"",stay:remembered.stay||""};
      try {state.session=await api("session");}
      catch(error) {if(!state.auth)throw error;state.auth={...state.auth,hotel:"",stay:""};state.session=await api("session");}
    }
  }
  state.auth.stay=state.session.principal.stay_id||"";
  state.stores=has("hotel_list_stores")?(await call("hotel_list_stores")).stores:state.session.stores;
  contextHeader();rememberContext();
  $("#login").hidden=true;$("#boot").hidden=true;$("#workspace").hidden=false;
  await showView(state.session.account.must_change_password?"password":has("hotel_query_requests")?"overview":has("hotel_query_sales")?"sales":"welcome");
}

function accountWelcome() {
  $("#content").innerHTML=pageHead("欢迎来到服务工作台",state.session.account.role==="customer"?"账号已就绪，请联系门店管理员关联您的入住记录。":"请先完成门店资料配置。")+`<section class="panel">${empty("暂无可查看的数据","关联入住后，可在这里提交服务需求；退房后仍能查看本人的历史工单。")}</section>`;
}

function passwordField(name, label, minimum=0) {
  const id=`password-${name}`;
  return `<div><label for="${id}">${label}</label><div class="password-input"><input id="${id}" name="${name}" type="password" autocomplete="${name==="current_password"?"current-password":"new-password"}" required ${minimum?`minlength="${minimum}"`:""} maxlength="128" spellcheck="false"><button type="button" class="password-toggle" data-toggle-password="${id}" data-password-label="${label}" aria-controls="${id}" aria-label="显示${label}" aria-pressed="false">显示</button></div></div>`;
}

function passwordPage() {
  $("#content").innerHTML=pageHead(state.session.account.must_change_password?"设置您的登录密码":"修改密码","密码更新后，所有设备上的旧登录都会失效，请使用新密码重新登录。")+`<section class="panel account-password"><form data-account-form="password">${passwordField("current_password","当前密码")}${passwordField("password","新密码",8)}${passwordField("confirm_password","确认新密码",8)}<p class="fine">使用8～128个字符，建议组合文字、数字和符号。</p><p class="error" role="alert" data-form-error></p><button class="primary">保存新密码</button></form></section>`;
}

async function accountList() {
  const data=await api(`accounts?offset=${accountOffset}&q=${encodeURIComponent(accountQuery)}`);
  accountRows=data.accounts;accountNext=data.next_offset;
  const rows=accountRows.map(a=>`<tr><td><strong>${esc(a.display_name)}</strong><small class="account-login">${esc(a.username)}</small></td><td>${esc(accountRoles[a.role])}</td><td>${a.role==="super_admin"?"全部门店":esc(state.session.stores.find(s=>s.hotel===a.hotel)?.name||a.hotel)}</td><td><span class="tag ${a.enabled?"green":"gray"}">${a.enabled?"启用":"已停用"}</span>${a.must_change_password?'<small class="account-login">首次登录需改密</small>':""}</td><td><button class="link" data-account-edit="${esc(a.id)}">管理 →</button></td></tr>`).join("");
  $("#content").innerHTML=pageHead("账号管理","由管理员创建账号并分配权限；客户通过关联入住记录查看自己的工单。",'<button class="primary" data-account-create>＋ 添加账号</button>')+`<section class="panel"><form class="filters" data-account-form="search"><label>查找账号<input name="query" placeholder="账号或姓名" value="${esc(accountQuery)}" maxlength="80"></label><button>搜索</button></form>${rows?`<div class="table-wrap"><table><thead><tr><th>姓名 / 账号</th><th>角色</th><th>门店范围</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`:empty("没有匹配的账号","可修改搜索条件，或添加新账号。")}<div class="pagination"><button data-account-prev ${accountOffset===0?"disabled":""}>上一页</button><span class="fine">第 ${accountOffset/50+1} 页</span><button data-account-next ${accountNext===null?"disabled":""}>下一页</button></div></section>`;
}

function accountFields(row) {
  const choices=state.session.account.role==="super_admin"?Object.keys(accountRoles):["frontdesk","customer"];
  const selectedRole=row?.role||"frontdesk";
  const selectedHotel=row?.hotel||state.auth.hotel||state.session.account.hotel||state.session.stores[0]?.hotel||"";
  return `<label>姓名<input name="display_name" required maxlength="80" value="${esc(row?.display_name||"")}"></label><div class="grid"><label>角色<select name="role" data-account-role>${choices.map(r=>`<option value="${r}" ${selectedRole===r?"selected":""}>${accountRoles[r]}</option>`).join("")}</select></label><label>所属门店<select name="hotel" ${selectedRole==="super_admin"?"disabled":""}>${state.session.stores.map(s=>`<option value="${esc(s.hotel)}" ${selectedHotel===s.hotel?"selected":""}>${esc(s.name)}</option>`).join("")}</select></label></div>`;
}

async function editAccount(id) {
  const row=accountRows.find(a=>a.id===id);
  if(id&&!row)throw new Error("账号列表已更新，请重试");
  $("#account-title").textContent=row?`管理账号 · ${row.username}`:"添加账号";
  $("#account-body").innerHTML=`<form data-account-form="${row?"update":"create"}" data-id="${esc(row?.id||"")}" data-version="${row?.version||0}">${row?"":'<label>登录账号<input name="username" required minlength="3" maxlength="64" pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}" autocomplete="off" placeholder="3～64位字母、数字、点、横线或下划线"></label>'}${accountFields(row)}${row?`<label>账号状态<select name="enabled"><option value="true" ${row.enabled?"selected":""}>启用</option><option value="false" ${row.enabled?"":"selected"}>停用</option></select></label><p class="fine">修改账号信息后，该账号需重新登录。已关联入住的客户不可变更角色或所属门店。</p>`:'<label>初始密码<input name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label><p class="fine">首次登录必须修改密码。请通过适当方式将账号及初始密码交给使用人。</p>'}<p class="error" role="alert" data-form-error></p><button class="primary">${row?"保存修改":"创建账号"}</button></form>${row?`<section class="account-section"><h3>重置密码</h3><p class="fine">旧登录立即失效，下次登录须修改临时密码。</p><form data-account-form="reset_password" data-id="${esc(row.id)}" data-version="${row.version}"><label>新的临时密码<input name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label><p class="error" role="alert" data-form-error></p><button>确认重置密码</button></form></section>`:""}${row?.role==="customer"?'<section class="account-section" id="stay-binding"><div class="loading">正在加载入住记录…</div></section>':""}`;
  $("#account-dialog").showModal();
  if(row?.role==="customer")await renderStayBinding(row.id);
}

async function renderStayBinding(id, offset=0) {
  const data=await api(`accounts/${encodeURIComponent(id)}/stays?offset=${offset}`);
  $("#stay-binding").innerHTML=`<h3>关联入住</h3><p class="fine">关联后客户可查看该次入住的工单，包括退房后的历史。历史归属不可转给其他客户，请核对入住信息。</p>${data.bound.length?`<ul class="bound-stays">${data.bound.map(s=>`<li>${esc(s.room)}号房 · ${esc(s.check_in_at.slice(0,10))} <span class="tag">${s.status==="active"?"入住中":"已退房"}</span></li>`).join("")}</ul>`:'<p class="muted">尚未关联入住</p>'}${data.available.length?`<form data-account-form="bind_stay" data-id="${esc(id)}" data-version="${data.version}"><label>选择未关联的入住<select name="stay_id" required><option value="">请选择并核对</option>${data.available.map(s=>`<option value="${esc(s.id)}">${esc(s.room)}号房 · ${esc(s.check_in_at)} · ${s.status==="active"?"入住中":"已退房"}</option>`).join("")}</select></label><p class="error" role="alert" data-form-error></p><button>确认关联入住</button></form>`:'<p class="muted">当前没有可关联的入住</p>'}<div class="pagination">${offset?`<button data-stay-page="${Math.max(0,offset-50)}" data-id="${esc(id)}">上一页</button>`:""}${data.next_offset!==null?`<button data-stay-page="${data.next_offset}" data-id="${esc(id)}">更多入住</button>`:""}</div>`;
}

async function accountWrite(path, values) {
  const signature=`accounts:${state.auth.id}:${path}:${JSON.stringify(values)}`;
  if(!retryKeys.has(signature))retryKeys.set(signature,crypto.randomUUID());
  const result=await api(path,{...values,request_key:retryKeys.get(signature)});
  retryKeys.delete(signature);return result;
}

function installAccountUI() {
  document.addEventListener("click",event=>{
    const button=event.target.closest("[data-toggle-password]");if(!button||state.busy)return;
    const input=document.getElementById(button.dataset.togglePassword),visible=input.type==="password";
    input.type=visible?"text":"password";
    button.textContent=visible?"隐藏":"显示";
    button.setAttribute("aria-pressed",String(visible));
    button.setAttribute("aria-label",`${visible?"隐藏":"显示"}${button.dataset.passwordLabel}`);
  });
  $("#login-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget, btn=form.querySelector("button");btn.disabled=true;$("#login-error").textContent="";
    const values=Object.fromEntries(new FormData(form));
    try {
      const response=await fetch("/workspace/login",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-Requested-With":"hotel-admin"},body:JSON.stringify({username:values.username.trim(),password:values.password})});
      const data=await response.json();if(!response.ok)throw new Error(data.message||"暂时无法登录");
      form.reset();state.auth={id:"",hotel:"",stay:"",csrf:""};await loadWorkspace(true);
    } catch(error) {clearLogin(error.message);} finally {btn.disabled=false;}
  });
  $("#logout").addEventListener("click",async()=>{
    if(state.busy)return;state.busy=true;
    try {await api("logout",{});clearLogin();try{sessionStorage.removeItem("hotel-view-context");}catch(_) {}}
    catch(error){toast(error.message);}finally{state.busy=false;}
  });
  $("#close-account").addEventListener("click",()=>$("#account-dialog").close());
  $("#account-dialog").addEventListener("close",()=>$("#account-body").replaceChildren());
  document.addEventListener("change",async event=>{
    if(event.target.matches("[data-account-role]"))event.target.form.elements.hotel.disabled=event.target.value==="super_admin";
    if(event.target.id!=="scope-select")return;
    if(state.busy){contextHeader();return;}
    state.busy=true;const select=event.target;select.disabled=true;
    state.auth={...state.auth,hotel:state.session.account.role==="super_admin"?select.value:state.auth.hotel,stay:state.session.account.role==="customer"?select.value:""};
    state.requests=[];state.selected=null;state.cursor=null;state.salesHotel="";state.salesFrom="";state.salesTo="";state.filterType="";state.filterStatus="";
    accountOffset=0;accountQuery="";retryKeys.clear();$("#detail").close();$("#account-dialog").close();$("#connection-dialog").close();$("#content").innerHTML='<div class="loading">正在切换数据范围…</div>';
    try{await loadWorkspace();}catch(error){clearLogin(error.message);}finally{state.busy=false;}
  });
  document.addEventListener("click",async event=>{
    const button=event.target.closest("button");
    if(!button||state.busy||!button.matches("[data-account-create],[data-account-edit],[data-account-next],[data-account-prev],[data-stay-page]"))return;
    state.busy=true;button.disabled=true;
    try {
      if(button.hasAttribute("data-account-create"))await editAccount();
      if(button.dataset.accountEdit)await editAccount(button.dataset.accountEdit);
      if(button.hasAttribute("data-account-next")){accountOffset=accountNext??accountOffset;await accountList();}
      if(button.hasAttribute("data-account-prev")){accountOffset=Math.max(0,accountOffset-50);await accountList();}
      if(button.hasAttribute("data-stay-page"))await renderStayBinding(button.dataset.id,Number(button.dataset.stayPage));
    }catch(error){toast(error.message);}finally{state.busy=false;button.disabled=false;}
  });
  document.addEventListener("submit",async event=>{
    const form=event.target;if(!form.hasAttribute("data-account-form"))return;
    event.preventDefault();if(state.busy)return;state.busy=true;const button=event.submitter;if(button)button.disabled=true;
    const errorBox=form.querySelector("[data-form-error]");if(errorBox)errorBox.textContent="";
    const values=Object.fromEntries(new FormData(form)), action=form.dataset.accountForm, id=form.dataset.id;
    try {
      if(action==="search"){accountQuery=values.query;accountOffset=0;await accountList();return;}
      if(action==="password") {
        if(values.password!==values.confirm_password)throw new Error("两次新密码不一致");
        delete values.confirm_password;await accountWrite("password",values);form.reset();clearLogin("密码已更新，请重新登录");return;
      }
      if(action==="create"||action==="update")values.hotel=values.role==="super_admin"?"":values.hotel;
      if(action!=="create")values.expected_version=Number(form.dataset.version);
      if(action==="update")values.enabled=values.enabled==="true";
      const path=action==="create"?"accounts":`accounts/${encodeURIComponent(id)}${action==="update"?"":"/"+action}`;
      await accountWrite(path,values);form.reset();
      if(id===state.session.account.id && action!=="bind_stay"){clearLogin("账号已更新，请重新登录");return;}
      $("#account-dialog").close();toast(action==="bind_stay"?"入住已关联，客户可查看该次入住的工单":"账号已保存");await accountList();
    }catch(error){if(errorBox)errorBox.textContent=error.message;else toast(error.message);}finally{state.busy=false;if(button)button.disabled=false;}
  });
  state.auth={id:"",hotel:"",stay:"",csrf:""};
  loadWorkspace(true).catch(error=>clearLogin(error.code==="UNAUTHORIZED"?"":error.message));
}
