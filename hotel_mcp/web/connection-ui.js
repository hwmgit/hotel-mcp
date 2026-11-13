"use strict";
const connectionLabels={disabled:"已禁用",stopped:"等待连接",connecting:"连接中",connected:"传输已连接",retrying:"正在重试"};
let connectionRows=[],agentTypes=[],agentStays=[],agentStayNext=null;
function connectionScope(row){return row.role==="room"?`${row.room} 号客房`:row.role==="manager"?row.allowed_hotels.map(h=>state.session.stores.find(s=>s.hotel===h)?.name||h).join("、"):"当前门店";}
async function connections(){
  const data=await api("connections");connectionRows=data.connections;
  const count=connectionRows.length,enabled=connectionRows.filter(c=>c.enabled).length;
  $("#content").innerHTML=pageHead("AI 接入配置","按业务能力管理智能体，在智控台配置对应提示词。",`<div class="connection-toolbar"><button data-refresh>↻ 刷新状态</button><button class="primary" data-add-connection>＋ 新增智能体</button></div>`)+
    `<div class="connection-summary"><span><strong>${count}</strong> 个智能体</span><span><i class="connection-dot"></i> ${enabled} 个已启用</span><span>${count-enabled} 个已禁用</span></div><div class="notice">禁用保留接入地址，重新启用即可连接。删除仅移除接入配置，保留账号与历史工单。测试检查握手与心跳。</div>`+
    (count?`<section class="panel agent-list"><div class="table-wrap"><table class="agent-table"><thead><tr><th>名称</th><th>智能体类型</th><th>服务范围</th><th>MCP 接入点</th><th>状态</th><th>操作</th></tr></thead><tbody>${connectionRows.map(c=>`<tr data-agent-row="${esc(c.id)}"><td><strong>${esc(c.name)}</strong></td><td>${esc(c.type_label)}</td><td>${esc(connectionScope(c))}</td><td><span class="agent-address">${esc(c.masked_endpoint)}</span></td><td><span data-connection-state="${esc(c.id)}" class="tag ${c.state==="connected"?"green":c.state==="retrying"?"amber":"gray"}">${esc(connectionLabels[c.state]||c.state)}</span></td><td><div class="agent-row-actions">${[["edit","编辑"],["test","测试"],[c.enabled?"disable":"enable",c.enabled?"禁用":"启用"],["delete","删除"]].map(([action,label])=>`<button class="link ${action==="delete"?"danger":""}" data-agent-action="${action}" data-id="${esc(c.id)}">${label}</button>`).join("")}</div></td></tr>`).join("")}</tbody></table></div></section>`:
    `<section class="panel connection-empty">${empty("尚未添加智能体","添加名称、智能体类型与平台提供的 MCP 接入点。")}<button class="primary" data-add-connection>＋ 新增智能体</button></section>`);
}
async function connectionWrite(path,values){
  const signature=`connection:${state.auth.id}:${state.auth.hotel}:${path}:${JSON.stringify(values)}`;
  if(!retryKeys.has(signature))retryKeys.set(signature,crypto.randomUUID());
  const result=await api(path,{...values,request_key:retryKeys.get(signature)});retryKeys.delete(signature);return result;
}
function stayOptions(){return agentStays.map(s=>`<option value="${esc(s.id)}">${esc(s.room)} 号房 · ${esc(s.check_in_at)}</option>`).join("");}
function updateAgentType(){
  const form=$("#connection-create-form"),type=agentTypes.find(t=>t.id===form?.elements.agent_type?.value);if(!type)return;
  form.querySelector("[data-agent-description]").textContent=type.description;
  form.querySelector("[data-agent-stay]").hidden=!type.requires_stay;form.elements.stay_id.required=type.requires_stay;
}
async function openConnectionEdit(id=""){
  const row=connectionRows.find(c=>c.id===id),data=await api("connections/options");
  agentTypes=data.agent_types;agentStays=data.stays;agentStayNext=data.next_offset;
  const dynamic=!row||row.dynamic;
  $("#connection-dialog-title").textContent=row?"编辑智能体":"新增智能体";
  $("#connection-create-body").innerHTML=`<form id="connection-create-form" data-id="${esc(id)}" data-version="${row?.version||0}" data-dynamic="${dynamic}"><label>名称<input name="name" maxlength="80" required value="${esc(row?.name||"")}" placeholder="例如：餐饮服务助手"></label>${dynamic?`<label>智能体类型<select name="agent_type">${agentTypes.map(t=>`<option value="${esc(t.id)}" ${t.id===(row?.agent_type||"frontdesk")?"selected":""}>${esc(t.label)}</option>`).join("")}</select></label><p class="fine" data-agent-description></p><div data-agent-stay><label>关联入住<select name="stay_id"><option value="">请选择当前入住</option>${stayOptions()}</select></label><button type="button" class="link" data-more-agent-stays ${agentStayNext===null?"hidden":""}>加载更多入住</button></div>`:`<p class="fine">${esc(row.type_label)} · ${esc(connectionScope(row))}</p>`}<label>MCP 接入点<input name="endpoint" type="password" autocomplete="off" spellcheck="false" maxlength="4096" ${row?"":"required"} placeholder="${row?"留空保留当前地址":"粘贴平台提供的 wss:// 接入地址"}"></label>${row?'<p class="fine">保存后按当前启用状态生效。</p>':'<label class="connection-toggle"><input name="enabled" type="checkbox" checked>创建后立即启用</label>'}<p class="connection-result error" role="alert"></p><button class="primary">${row?"保存修改":"创建智能体"}</button></form>`;
  if(dynamic){if(row?.stay_id)$("#connection-create-form").elements.stay_id.value=row.stay_id;updateAgentType();}
  $("#connection-dialog").showModal();
}
async function createConnection(form){
  const message=form.querySelector(".connection-result");message.textContent="";
  try{
    const values={name:form.elements.name.value.trim(),endpoint:form.elements.endpoint.value.trim()};
    if(form.dataset.dynamic==="true")Object.assign(values,{agent_type:form.elements.agent_type.value,stay_id:form.elements.agent_type.value==="room"?form.elements.stay_id.value:""});
    const id=form.dataset.id;
    if(id)values.expected_version=Number(form.dataset.version);else values.enabled=form.elements.enabled.checked;
    await connectionWrite(id?`connections/${encodeURIComponent(id)}/manage/update`:"connections",values);
    form.reset();$("#connection-dialog").close();toast(id?"智能体配置已保存":"智能体已创建");await connections();
  }catch(error){message.textContent=error.message;}
}
function installConnectionUI(){
  $("#close-connection").addEventListener("click",()=>{if(!state.busy)$("#connection-dialog").close();});
  $("#connection-dialog").addEventListener("cancel",event=>{if(state.busy)event.preventDefault();});
  $("#connection-dialog").addEventListener("close",()=>$("#connection-create-body").replaceChildren());
  document.addEventListener("change",event=>{if(event.target.form?.id==="connection-create-form"&&event.target.name==="agent_type")updateAgentType();});
  document.addEventListener("click",async event=>{
    const button=event.target.closest("[data-add-connection],[data-agent-action],[data-more-agent-stays]");if(!button||state.busy)return;
    state.busy=true;button.disabled=true;
    try{
      if(button.hasAttribute("data-add-connection")){await openConnectionEdit();return;}
      if(button.hasAttribute("data-more-agent-stays")){
        const data=await api(`connections/options?offset=${agentStayNext}`);agentStays.push(...data.stays);agentStayNext=data.next_offset;
        const select=$("#connection-create-form").elements.stay_id,value=select.value;
        select.innerHTML='<option value="">请选择当前入住</option>'+stayOptions();select.value=value;button.hidden=agentStayNext===null;return;
      }
      const id=button.dataset.id,action=button.dataset.agentAction,row=connectionRows.find(c=>c.id===id);
      if(action==="edit"){await openConnectionEdit(id);return;}
      if(action==="test"){const result=await api(`connections/${encodeURIComponent(id)}/test`,{});toast(result.message);return;}
      if(action==="delete"&&!confirm(`确定删除“${row.name}”？连接将断开并移除地址，账号与历史工单会保留。`))return;
      await connectionWrite(`connections/${encodeURIComponent(id)}/manage/${action}`,{expected_version:row.version});
      toast({enable:"智能体已启用，正在连接",disable:"智能体已禁用，地址已保留",delete:"智能体接入已删除"}[action]);await connections();
    }catch(error){toast(error.message);}finally{state.busy=false;button.disabled=false;}
  });
}
