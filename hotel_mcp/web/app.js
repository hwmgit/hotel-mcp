"use strict";
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = value => new Intl.NumberFormat("zh-CN", {style:"currency",currency:"CNY"}).format((value || 0) / 100);
const day = offset => {const d = new Date(Date.now() + offset * 86400000); return new Intl.DateTimeFormat("sv-SE", {timeZone:"Asia/Shanghai"}).format(d);};
const when = value => value ? new Date(value).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "—";
const time = value => new Date(value).toLocaleTimeString("zh-CN", {timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",hour12:false});
const kinds = {general:"客房服务",repair:"客房报障",stay_extension:"续房申请",meal:"客房点餐",gym_booking:"健身房预约",meeting_booking:"会议室预约"};
const statuses = {pending:"待确认",accepted:"已受理",completed:"已完成",rejected:"已拒绝",cancelled:"已取消",closed_unresolved:"未解决结案",quoted:"待住客确认",confirmed:"待办理",processing:"办理中",failed:"办理失败"};
const actions = {accept:"确认 / 受理",complete:"确认完成",reject:"拒绝申请",cancel:"取消服务",add_note:"追加处理记录",close_unresolved:"无法解决结案",quote:"核实并报价",confirm:"同意当前报价",begin:"开始办理续房",finish_success:"记录办理成功",finish_failed:"记录办理失败"};
const cats = {accommodation:"住宿",dining:"餐饮",gym:"健身",meeting_room:"会议"};
const state = {auth:null, session:null, view:"overview", stores:[], requests:[], cursor:null, filterType:"", filterStatus:"", menu:[], bookingKind:"gym", bookingDate:day(1), slots:[], selected:null, history:[], salesHotel:"", salesFrom:"", salesTo:"", busy:false};
const retryKeys = new Map();
let toastTimer;
function toast(message) {$("#toast").textContent = message; $("#toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("#toast").hidden = true, 5000);}
const has = tool => state.session?.tools.includes(tool);
const role = () => state.session?.principal.role;
function badge(status, kind) {const color = ["pending","quoted","confirmed"].includes(status) ? "amber" : ["completed","accepted"].includes(status) ? "green" : ["rejected","failed","closed_unresolved"].includes(status) ? "red" : "gray"; const label = status === "accepted" && kind?.includes("booking") ? "已确认" : status === "accepted" && kind === "meal" ? "待配送" : statuses[status]; return `<span class="tag ${color}">${esc(label || status)}</span>`;}
function pageHead(title, subtitle, side = "") {return `<div class="page-head"><div><p class="eyebrow">HOTEL SERVICE DESK</p><h1>${title}</h1><p>${subtitle}</p></div>${side}</div>`;}
const empty = (title, note="新的服务请求会显示在这里。") => `<div class="empty"><strong>${title}</strong>${note}</div>`;
async function api(path, data) {
  const auth = state.auth;
  if (!auth) throw new Error("请先登录");
  const response = await fetch(`/api/${encodeURIComponent(auth.id)}/${path}`, {method:data === undefined ? "GET" : "POST", headers:{Authorization:`Bearer ${auth.token}`, ...(data === undefined ? {} : {"Content-Type":"application/json"})}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  if (auth !== state.auth) throw new Error("身份已切换，请重新操作");
  const value = await response.json();
  if (!response.ok) {const error = new Error(value.message || "服务暂时不可用"); error.code = value.code; throw error;}
  return value;
}
const call = (tool, args={}) => api(`tools/${tool}`, args);
async function write(tool, args) {
  const signature = `${state.auth.id}:${tool}:${JSON.stringify(args)}`;
  if (!retryKeys.has(signature)) retryKeys.set(signature, crypto.randomUUID());
  const result = await call(tool, {...args, request_key:retryKeys.get(signature)});
  retryKeys.delete(signature);
  return result;
}
function nav() {
  const all = [["overview","◫",role()==="room"?"我的服务":"工作概览",has("hotel_query_requests")], ["requests","▤","工单中心",has("hotel_query_requests")], ["meals","♧","餐饮服务",has("hotel_list_menu")], ["bookings","▦","场地预约",has("hotel_list_booking_slots")], ["sales","▥","经营数据",has("hotel_query_sales")], ["resolve","⌕","名称助手",has("hotel_resolve_entities")], ["connections","⇄","AI 接入配置",state.session?.capabilities?.manage_connections]];
  $("#nav").innerHTML = all.filter(x=>x[3]).map(([id,icon,label])=>`<button data-view="${id}" class="${state.view===id?"active":""}"><span class="nav-icon">${icon}</span>${label}</button>`).join("");
  $("#breadcrumb").textContent = all.find(x=>x[0]===state.view)?.[2] || "工作台";
}
function requestRows(rows,compact=false) {return rows.map(r=>`<tr><td><span class="room-number">${esc(r.room)}</span></td><td>${esc(kinds[r.request_type])}</td><td><span class="desc">${esc(r.description)}</span></td><td>${badge(r.status,r.request_type)}</td>${compact?"":`<td>${when(r.updated_at)}</td>`}<td><button class="link" data-detail="${esc(r.id)}">处理 →</button></td></tr>`).join("");}
function requestTable(rows,compact=false) {return rows.length ? `<div class="table-wrap"><table><thead><tr><th>客房</th><th>服务类型</th><th>需求内容</th><th>当前状态</th>${compact?"":"<th>最近更新</th>"}<th>操作</th></tr></thead><tbody>${requestRows(rows,compact)}</tbody></table></div>` : empty("暂时没有工单");}
async function loadRequests(more=false, overview=false) {
  const args = {limit:50};
  if (!overview && state.filterType) args.request_type = state.filterType;
  if (!overview && state.filterStatus) args.statuses = [state.filterStatus];
  if (more && state.cursor) args.cursor = state.cursor;
  const data = await call("hotel_query_requests", args);
  state.requests = more ? [...state.requests, ...data.requests] : data.requests;
  state.cursor = data.next_cursor;
}
async function overview() {
  await loadRequests(false, true);
  const rows = state.requests;
  const pending = rows.filter(r=>["pending","confirmed"].includes(r.status)).length;
  const active = rows.filter(r=>["accepted","processing"].includes(r.status)).length;
  let content = pageHead(role()==="room"?"欢迎，您的客房服务":"让每一份需求，都有回应",role()==="room"?"提交需求，随时查看前台处理进度。":"客房 AI 发起需求 · 前台在此处理 · 结果通过业务事件反馈客房", `<button data-refresh>↻ 刷新工作台</button>`);
  content += `<div class="stats">${[["待前台处理",pending,"待确认与待办理","◷"],["正在服务",active,"已受理或办理中","↗"],["服务已完成",rows.filter(r=>r.status==="completed").length,"当前加载工单范围","✓"],["服务工单",rows.length,"最近最多 50 张工单","▤"]].map(([label,num,help,icon])=>`<div class="stat"><span>${label}</span><i class="stat-icon">${icon}</i><strong>${num.toString().padStart(2,"0")}</strong><small>${help}</small></div>`).join("")}</div>`;
  content += `<div class="split"><div class="panel"><div class="panel-head"><h3>最近服务请求</h3><button class="link" data-view="requests">查看全部 →</button></div>${requestTable(rows.slice(0,6),true)}</div><div class="panel"><div class="panel-head"><h3>服务协同</h3><span class="tag">业务在线</span></div><div class="flow"><div class="flow-step"><span>01</span><div><b>客房 AI 接收需求</b><p>报障、续房、点餐与场地预约。</p></div></div><div class="flow-step"><span>02</span><div><b>前台确认并处理</b><p>核实费用、安排服务，确认预约时占用资源。</p></div></div><div class="flow-step"><span>03</span><div><b>结果反馈客房</b><p>处理记录留存，业务事件交由智能体通知。</p></div></div></div></div></div>`;
  if (role()==="room") {
    const stay = (await call("hotel_get_current_stay")).stay;
    content += `<div class="grid"><section class="service-card"><h3>报修与帮助</h3><form id="repair-form"><label>故障分类<select name="category"><option value="air_conditioning">空调</option><option value="plumbing_electrical">水电</option><option value="network">网络</option><option value="door_lock">门锁</option><option value="facilities">设施</option><option value="other">其他</option></select></label><label>需要我们帮助什么？<textarea name="description" maxlength="300" required placeholder="例如：卧室空调不制冷"></textarea></label><button class="primary">提交报障</button></form></section><section class="service-card"><h3>想再住几天？</h3><p class="muted">当前预计退房：${esc(stay.expected_checkout_date)}</p><form id="extension-form"><label>希望退房日期<input name="target_checkout_date" type="date" min="${esc(stay.expected_checkout_date)}" required></label><label>备注<input name="note" maxlength="300" placeholder="费用由前台人工核实报价"></label><button class="primary">提交续房申请</button></form></section></div>`;
  }
  $("#content").innerHTML = content;
}
async function requests(more=false) {
  await loadRequests(more);
  $("#content").innerHTML = pageHead("工单中心","集中查看客房需求，确认处理安排并记录服务结果。",`<span class="tag">每 15 秒刷新待办</span>`) + `<section class="panel"><form id="request-filter" class="filters"><label>服务类型<select name="type"><option value="">全部服务</option>${Object.entries(kinds).map(([id,label])=>`<option value="${id}" ${state.filterType===id?"selected":""}>${label}</option>`).join("")}</select></label><label>工单状态<select name="status"><option value="">全部状态</option>${Object.entries(statuses).map(([id,label])=>`<option value="${id}" ${state.filterStatus===id?"selected":""}>${label}</option>`).join("")}</select></label><button class="primary">筛选工单</button><button type="button" data-refresh>↻ 刷新</button></form>${requestTable(state.requests)}<div class="pagination">${state.cursor?'<button data-more>加载更多工单</button>':`<span class="fine">已显示 ${state.requests.length} 张工单</span>`}</div></section>`;
}
async function meals() {
  state.menu = (await call("hotel_list_menu")).items;
  const guest = role()==="room";
  $("#content").innerHTML = pageHead("餐饮服务",guest?"选一份喜欢的餐食，等待前台确认后配送。":"查看在售菜单，前往工单中心确认点餐和配送结果。",'<span class="tag amber">线下结算</span>') + `<div class="notice">价格以提交时的菜品快照为准。完成表示送餐服务完成，不代表已收款；销售看板使用独立营业台账。</div><section class="panel"><form id="meal-form"><div class="grid">${state.menu.map((m,i)=>`<div class="menu-card"><div class="menu-art" aria-hidden="true">${["◒","♧","◉","❋"][i%4]}</div><div class="menu-text"><h3>${esc(m.name)}</h3><p>${esc(m.description)}</p><span class="price">${money(m.price_minor)}</span></div>${guest?`<label>数量<input class="qty" data-qty data-item="${esc(m.id)}" type="number" value="0" min="0" max="20" step="1" aria-label="${esc(m.name)}数量"></label>`:""}</div>`).join("") || empty("菜单尚未配置","请由本地管理员准备菜品目录。")}</div>${guest&&state.menu.length?'<div class="meal-order"><label>口味或配送备注<input name="note" maxlength="300" placeholder="如：不要香菜，请送到房间"></label><div><span class="fine">合计 · 线下结算</span><div id="meal-total" class="meal-total">¥0.00</div></div><button class="primary">确认下单</button></div>':`<div class="pagination"><button type="button" data-worktype="meal" class="primary">查看点餐工单 →</button></div>`}</form></section>`;
}
async function bookings() {
  const data = await call("hotel_list_booking_slots", {kind:state.bookingKind,date:state.bookingDate}); state.slots=data.slots;
  const guest=role()==="room";
  $("#content").innerHTML=pageHead("场地预约",guest?"选择时段并申请，前台确认后预约才生效。":"查看场地时段和已占用余量，在工单中心完成预约确认。",'<span class="tag">北京时间 · 固定时段</span>')+`<section class="panel"><form id="slot-filter" class="filters"><label>场地类型<select name="kind"><option value="gym" ${state.bookingKind==="gym"?"selected":""}>健身房</option><option value="meeting" ${state.bookingKind==="meeting"?"selected":""}>会议室</option></select></label><label>预约日期<input type="date" name="date" value="${esc(state.bookingDate)}" required></label><button class="primary">查看时段</button>${!guest?`<button type="button" data-worktype="${state.bookingKind==="gym"?"gym_booking":"meeting_booking"}">处理预约工单 →</button>`:""}</form><div class="notice">${state.bookingKind==="gym"?"健身房按预约人数占用名额。":"会议室确认后整间占用，同一时段只接待一组。"}申请不锁定资源，前台确认时重新校验余量；取消或完成后释放占用。</div><form id="booking-form"><div class="grid">${data.slots.map(s=>`<label class="slot">${guest?`<input type="radio" name="slot_id" value="${esc(s.id)}" required>`:""}<h3>${esc(s.name)}</h3><div class="slot-time">${time(s.starts_at)} — ${time(s.ends_at)}</div><p>最多 ${s.max_people} 人 · ${esc(s.business_date)}</p><span class="tag ${s.remaining?"green":"amber"}">${s.remaining?`剩余 ${s.remaining} ${state.bookingKind==="gym"?"个名额":"间"}`:"已约满"}</span></label>`).join("") || empty("这一天没有可预约时段","请选择其他日期，或联系前台。")}</div>${guest&&data.slots.length?'<div class="meal-order"><label>参加人数<input name="participants" type="number" value="1" min="1" max="100" required></label><label>备注<input name="note" maxlength="300" placeholder="如：需要投屏"></label><button class="primary">提交预约申请</button></div>':""}</form></section>`;
}
async function sales() {
  if (!state.salesFrom) {state.salesFrom=state.stores.find(s=>s.available_from)?.available_from||day(-6);state.salesTo=state.stores.find(s=>s.available_to)?.available_to||day(0);}
  const args={date_from:state.salesFrom,date_to:state.salesTo}; if(state.salesHotel)args.hotel_ids=[state.salesHotel];
  const data=await call("hotel_query_sales",args); const max=Math.max(1,...data.by_store.map(s=>s.sales_amount_minor)); const dailyMax=Math.max(1,...data.daily_trend.map(s=>s.sales_amount_minor));
  $("#content").innerHTML=pageHead("经营数据","从单店到多店，以相同口径了解经营表现。",'<span class="tag amber">门店经营概览</span>')+`<section class="panel"><form id="sales-filter" class="filters"><label>门店<select name="hotel"><option value="">全部授权门店</option>${state.stores.map(s=>`<option value="${esc(s.hotel)}" ${state.salesHotel===s.hotel?"selected":""}>${esc(s.name)}</option>`).join("")}</select></label><label>开始日期<input name="from" type="date" value="${esc(state.salesFrom)}" required></label><label>结束日期<input name="to" type="date" value="${esc(state.salesTo)}" required></label><button class="primary">查询数据</button></form></section><div class="stats"><div class="stat"><span>销售额</span><strong>${money(data.totals.sales_amount_minor)}</strong><small>已结算订单口径</small></div><div class="stat"><span>订单数</span><strong>${data.totals.order_count}</strong><small>含所选日期首尾</small></div><div class="stat"><span>所选门店</span><strong>${data.by_store.length}</strong><small>仅当前身份授权门店</small></div><div class="stat"><span>统计天数</span><strong>${data.daily_trend.length}</strong><small>北京时间营业日</small></div></div><div class="grid"><section class="panel"><div class="panel-head"><h3>每日销售趋势</h3><small>人民币元</small></div><div class="bars">${data.daily_trend.map(d=>`<div class="bar-col" title="${esc(d.date)} ${money(d.sales_amount_minor)}"><div class="bar" style="height:${Math.max(1,d.sales_amount_minor/dailyMax*150)}px" role="img" aria-label="${esc(d.date)} ${money(d.sales_amount_minor)}"></div><small>${esc(d.date.slice(5))}</small></div>`).join("")}</div></section><section class="panel"><div class="panel-head"><h3>门店销售对比</h3><small>销售额降序</small></div><div class="ranking">${data.by_store.map((s,i)=>`<div><div class="rank-head"><span>${i+1}. ${esc(s.name)}</span><b>${money(s.sales_amount_minor)}</b></div><div class="rank-track"><div class="rank-fill" style="width:${s.sales_amount_minor/max*100}%"></div></div></div>`).join("")||empty("暂无门店")}</div></section></div><section class="panel"><h3>业务分类</h3>${data.by_category.map(c=>`<div class="category-row"><span>${cats[c.category]} <small class="muted">${c.order_count} 单</small></span><b>${money(c.sales_amount_minor)}</b></div>`).join("")}</section><p class="fine">本页使用独立营业台账。续房报价、点餐下单和服务完成不会自动计入收入。</p>`;
}
function resolver() {$("#content").innerHTML=pageHead("名称助手","把用户口中的门店，匹配到可查询的业务实体。",'<span class="tag">仅当前授权范围</span>')+`<section class="panel resolver"><h3>想查询哪家门店？</h3><p class="muted">支持门店别名与近似名称，存在歧义时先澄清再查询。</p><form id="resolve-form" class="filters"><label>识别到的门店名称<input name="query" placeholder="试试：中心门店、江盼店、市区店" maxlength="80" required></label><button class="primary">解析名称</button></form><div id="resolution"></div></section>`;}
async function connections() {
  const data=await api("connections");
  const labels={disabled:"未启用",stopped:"已停止",connecting:"连接中",connected:"传输已连接",retrying:"正在重试"};
  $("#content").innerHTML=pageHead("AI 接入配置",role()==="manager"?"配置经营查询智能体，查询当前账号授权的门店销售数据。":"将本店的客房或前台身份接入对应智能体，保存后立即生效。",'<button data-refresh>↻ 刷新连接状态</button>')+`<div class="notice">每个身份使用独立接入点。测试检查 WebSocket 握手与心跳；业务工具和通知请在智能体侧验证。修改配置无需重启服务。</div><div class="connection-grid">${data.connections.map(c=>`<section class="panel connection-card"><div class="panel-head"><div><p class="eyebrow">${c.role==="manager"?"经营查询智能体":c.role==="room"?"客房智能体":"前台智能体"}</p><h3>${c.role==="manager"?"经营销售查询":c.role==="room"?esc(c.room)+" 号客房":"前台服务"}</h3></div><span data-connection-state="${esc(c.id)}" class="tag ${c.state==="connected"?"green":c.state==="retrying"?"amber":"gray"}">${esc(labels[c.state]||c.state)}</span></div><p class="fine">业务身份 · ${esc(c.id)}</p>${c.role==="manager"?`<p class="fine">授权门店（${state.stores.length} 家）：${state.stores.map(s=>esc(s.name)).join("、")}。支持销售额、订单数、趋势和门店对比。</p>`:""}<p class="connection-address">${esc(c.masked_endpoint||"尚未配置接入点")}</p><form data-connection="${esc(c.id)}" data-version="${c.version}"><label>新的 MCP 接入点<input name="endpoint" type="password" maxlength="4096" autocomplete="off" spellcheck="false" placeholder="粘贴平台提供的 wss:// 接入地址"></label><p class="fine">保存后不回显完整地址。留空可测试当前已保存连接。</p><div class="connection-actions"><button type="submit" name="action" value="test">测试连通性</button><button type="submit" name="action" value="save" class="primary">保存并生效</button><button type="submit" name="action" value="disable" ${c.configured?"":"disabled"}>停用连接</button></div><p class="connection-result" role="status" aria-live="polite"></p></form></section>`).join("")}</div>`;
}
async function submitConnection(form, action) {
  const target=form.dataset.connection, value=form.elements.endpoint.value.trim();
  const message=form.querySelector(".connection-result");
  message.textContent=action==="test"?"正在测试，请稍候…":"正在保存并切换连接…";
  try {
    if(action==="test") {const result=await api(`connections/${encodeURIComponent(target)}/test`,value?{endpoint:value}:{});message.textContent=result.message;message.classList.toggle("error",!result.ok);return;}
    if(action==="save"&&!value)throw new Error("请填写新的接入点；如需断开，请点击停用连接。");
    const args={endpoint:action==="disable"?"":value,expected_version:Number(form.dataset.version)};
    const signature=`connection:${state.auth.id}:${target}:${JSON.stringify(args)}`;
    if(!retryKeys.has(signature))retryKeys.set(signature,crypto.randomUUID());
    await api(`connections/${encodeURIComponent(target)}`,{...args,request_key:retryKeys.get(signature)});
    retryKeys.delete(signature);form.reset();toast(action==="disable"?"连接已停用":"配置已保存，正在应用新连接");await connections();
  } catch(error) {message.textContent=error.message;message.classList.add("error");}
}
async function showView(view, silent=false) {
  if (!state.session) return;
  state.view=view; nav();
  if(!silent) $("#content").innerHTML='<div class="loading">正在获取最新业务数据…</div>';
  try {await ({overview, requests, meals, bookings, sales, connections, resolve:resolver}[view]||overview)();$("#updated").textContent=`更新于 ${new Date().toLocaleTimeString("zh-CN")}`;}
  catch(error){if(!silent)$("#content").innerHTML=`<section class="panel">${empty("暂时无法加载",esc(error.message))}<button data-refresh>重试</button></section>`;else toast(error.message);}
}
function available(r) {
  if(r.request_type==="stay_extension"){const order=["confirm","begin","finish_success","finish_failed","quote","add_note","reject","cancel"];return [...(r.extension.available_actions[role()]||[])].sort((a,b)=>order.indexOf(a)-order.indexOf(b));}
  if(role()==="room")return r.status==="pending"?["cancel"]:[];
  if(r.status==="pending")return ["accept","reject","cancel"];
  if(r.status==="accepted")return ["complete","add_note","cancel",...(r.request_type==="repair"?["close_unresolved"]:[])];
  return [];
}
async function openDetail(id) {
  const [data,history]=await Promise.all([call("hotel_get_request",{request_id:id}),call("hotel_list_request_actions",{request_id:id,limit:50})]);
  const r=data.request;state.selected=r;state.history=history.actions;
  $("#detail-title").textContent=`${r.room} 号客房 · ${kinds[r.request_type]}`;
  let extra="";
  if(r.repair){const categories={air_conditioning:"空调",plumbing_electrical:"水电",network:"网络",door_lock:"门锁",facilities:"设施",other:"其他"};extra=`<div class="quote-box"><b>${esc(categories[r.repair.category]||r.repair.category)} · ${r.repair.urgency==="urgent"?"紧急":"普通"}</b><p>故障位置：${esc(r.repair.location||"未填写")}</p></div>`;}
  if(r.meal)extra=`<div class="quote-box">线下结算 · 下单金额<strong>${money(r.meal.total_amount_minor)}</strong>${r.meal.items.map(i=>`<div>${esc(i.name)} × ${i.quantity} <span class="muted">${money(i.unit_price_minor)} / 份</span></div>`).join("")}</div>`;
  if(r.booking)extra=`<div class="quote-box"><b>${esc(r.booking.resource_name)}</b><p>${when(r.booking.starts_at)} — ${time(r.booking.ends_at)} · ${r.booking.participants}人</p>确认时校验余量并占用资源，当前状态：${esc(r.status==="accepted"?"已确认":statuses[r.status])}</div>`;
  if(r.extension){const x=r.extension;extra=`<div class="quote-box">申请从 ${esc(x.original_checkout_date)} 续住至 ${esc(x.requested_checkout_date)}${x.current_quote?`<strong>${money(x.current_quote.total_amount_minor)}</strong><p>${esc(x.current_quote.pricing_note)}</p><span class="muted">报价 ${x.current_quote.revision} · ${x.quote_status==="confirmed"?"已获住客确认":x.quote_status==="expired"?"已过期":x.quote_status==="inactive"?"已结束":`有效至 ${when(x.current_quote.expires_at)}`}</span>`:"<p>等待前台核实房态并报价</p>"}</div>`;}
  const opts=available(r);
  $("#detail-body").innerHTML=`<div class="detail-summary">${badge(r.status,r.request_type)}<span class="muted">${esc(r.description)}</span></div><div class="detail-meta">创建 ${when(r.created_at)} · 更新 ${when(r.updated_at)} <button class="link" data-reload-detail>刷新详情</button></div>${extra}${r.note?`<p>最近说明：${esc(r.note)}</p>`:""}${opts.length?`<form id="action-form" class="action-form"><label>处理操作<select name="action" id="action-select">${opts.map(a=>`<option value="${a}">${actions[a]}</option>`).join("")}</select></label><div id="action-fields" class="action-fields"></div><button class="primary">确认操作</button><p id="action-error" class="error" role="alert"></p></form>`:'<div class="notice">当前没有可执行操作。等待下一步处理，或查看历史记录。</div>'}<div class="history"><h3>处理记录 <span class="fine">最近最多 50 条</span></h3>${history.actions.map(a=>`<div class="history-item"><small>${when(a.created_at)} · ${a.actor_role==="room"?"客房":"前台"}</small><b>${esc(actions[a.action]|| (a.action==="submit"?"提交申请":a.action))}</b><p>${esc(a.note||"已记录")}</p></div>`).join("")||'<p class="muted">暂无处理记录</p>'}</div>`;
  if(opts.length)actionFields(); if(!$("#detail").open)$("#detail").showModal();
}
function actionFields() {
  const action=$("#action-select").value;
  $("#action-fields").innerHTML=(action==="quote"?`<div class="detail-grid"><label>续房总价（元）<input name="price" type="number" min="0" max="999999" step="0.01" required></label><label>有效期（分钟）<input name="minutes" type="number" value="30" min="1" max="1440" required></label></div>${state.selected.extension.current_quote?'<label>改价原因<input name="change_reason" maxlength="500" required></label>':""}`:"")+(action!=="confirm"?`<label>${action==="quote"?"费用说明与条件":"处理说明"}<textarea name="note" maxlength="500" required placeholder="请记录已核实的安排或实际结果"></textarea></label>`:'<div class="notice">请确认当前报价的日期、总价和费用条件。确认后仍需前台办理。</div>');
}
async function submitAction(form) {
  const values=Object.fromEntries(new FormData(form));const r=state.selected;const a=values.action;const args={request_id:r.id,expected_version:r.version};let tool;
  if(a==="quote"){tool="hotel_quote_stay_extension";Object.assign(args,{total_amount_minor:Math.round(Number(values.price)*100),pricing_note:values.note,valid_for_minutes:Number(values.minutes),...(values.change_reason?{change_reason:values.change_reason}:{})});}
  else if(a==="confirm"||a==="begin"||a.startsWith("finish_")){tool=a==="confirm"?"hotel_confirm_stay_extension":a==="begin"?"hotel_begin_stay_extension":"hotel_finish_stay_extension";args.quote_id=r.extension.current_quote.id;if(a!=="confirm")args.note=values.note;if(a.startsWith("finish_"))args.outcome=a==="finish_success"?"success":"failed";}
  else if(a==="cancel"){tool="hotel_cancel_request";args.reason=values.note;}
  else if(a==="reject"&&r.request_type==="stay_extension"){tool="hotel_reject_stay_extension";args.reason=values.note;}
  else{tool="hotel_process_request";args.action=a;args.note=values.note;}
  await write(tool,args); toast("操作已记录，业务状态已更新");await openDetail(r.id);
}
$("#login-form").addEventListener("submit",async event=>{
  event.preventDefault();const form=event.currentTarget;const data=Object.fromEntries(new FormData(form));const btn=form.querySelector("button");btn.disabled=true;$("#login-error").textContent="";
  state.auth={id:data.principal.trim(),token:data.token.trim()};
  try{state.session=await api("session");form.reset();state.stores=has("hotel_list_stores")?(await call("hotel_list_stores")).stores:[];const p=state.session.principal;const store=state.stores.find(s=>s.hotel===p.hotel);$("#identity").textContent=`${store?.name||p.hotel||"授权多门店"} · ${{room:`${p.room}号客房`,frontdesk:"前台",manager:"经营管理"}[p.role]}`;$("#login").hidden=true;$("#workspace").hidden=false;await showView(role()==="manager"?"sales":"overview");}
  catch(error){state.auth=null;state.session=null;$("#login-error").textContent=error.message;}finally{btn.disabled=false;}
});
$("#logout").addEventListener("click",()=>{state.auth=null;state.session=null;state.requests=[];state.stores=[];state.selected=null;state.salesFrom="";state.salesTo="";state.salesHotel="";state.filterType="";state.filterStatus="";retryKeys.clear();$("#detail").close();$("#content").replaceChildren();$("#workspace").hidden=true;$("#login").hidden=false;});
$("#close-detail").addEventListener("click",()=>{$("#detail").close();showView(state.view,true);});
document.addEventListener("change",event=>{if(event.target.id==="action-select")actionFields();});
document.addEventListener("input",event=>{if(event.target.matches("[data-qty]")){const total=[...document.querySelectorAll("[data-qty]")].reduce((sum,input)=>sum+(state.menu.find(m=>m.id===input.dataset.item)?.price_minor||0)*Number(input.value||0),0);$("#meal-total").textContent=money(total);}});
document.addEventListener("click",async event=>{
  const button=event.target.closest("button");if(!button||state.busy)return;
  state.busy=true;
  try{if(button.dataset.view)await showView(button.dataset.view);else if(button.hasAttribute("data-refresh"))await showView(state.view);else if(button.dataset.detail)await openDetail(button.dataset.detail);else if(button.hasAttribute("data-reload-detail"))await openDetail(state.selected.id);else if(button.hasAttribute("data-more"))await requests(true);else if(button.dataset.worktype){state.filterType=button.dataset.worktype;state.filterStatus="";await showView("requests");}else if(button.dataset.choose){state.salesHotel=button.dataset.choose;await showView("sales");}}
  catch(error){toast(error.message);}finally{state.busy=false;}
});
document.addEventListener("submit",async event=>{
  const form=event.target;if(form.id==="login-form")return;event.preventDefault();if(state.busy)return;state.busy=true;const btn=event.submitter;if(btn)btn.disabled=true;
  const values=Object.fromEntries(new FormData(form));
  try{
    if(form.dataset.connection)await submitConnection(form,btn?.value||"save");
    if(form.id==="request-filter"){state.filterType=values.type;state.filterStatus=values.status;await requests();}
    if(form.id==="slot-filter"){state.bookingKind=values.kind;state.bookingDate=values.date;await bookings();}
    if(form.id==="sales-filter"){state.salesHotel=values.hotel;state.salesFrom=values.from;state.salesTo=values.to;await sales();}
    if(form.id==="action-form")await submitAction(form);
    if(form.id==="resolve-form") {const r=await call("hotel_resolve_entities",{query:values.query});$("#resolution").innerHTML=`<div class="notice">${esc(r.clarification_question||"已找到唯一门店，可查看该门店的销售。")}</div>${r.candidates.map(c=>`<div class="candidate"><div><b>${esc(c.name)}</b><small>${r.status==="resolved"?"唯一匹配":"等待您明确选择"}</small></div><button type="button" class="primary" data-choose="${esc(c.entity_id)}">${r.status==="resolved"?"查看销售":"确认查询此店"}</button></div>`).join("")}`;}
    let created;
    if(form.id==="repair-form")created=await write("hotel_submit_repair",values);
    if(form.id==="extension-form")created=await write("hotel_submit_stay_extension",values);
    if(form.id==="meal-form") {const items=[...form.querySelectorAll("[data-qty]")].filter(i=>Number(i.value)>0).map(i=>({item_id:i.dataset.item,quantity:Number(i.value)}));if(!items.length)throw new Error("请先选择菜品和数量");created=await write("hotel_submit_meal_order",{items,note:values.note||""});}
    if(form.id==="booking-form"){if(!values.slot_id)throw new Error("请选择预约时段");created=await write("hotel_submit_booking",{slot_id:values.slot_id,participants:Number(values.participants),note:values.note||""});}
    if(created){toast("申请已提交，等待前台确认");await showView("requests");await openDetail(created.request.id);}
  }catch(error){if(form.id==="action-form"&&$("#action-error"))$("#action-error").textContent=error.message;else toast(error.message);}finally{state.busy=false;if(btn)btn.disabled=false;}
});
setInterval(()=>{if(state.session&&!state.busy&&!$("#detail").open&&["overview","requests"].includes(state.view)&&!document.querySelector("#content form :focus"))showView(state.view,true);},15000);
setInterval(async()=>{
  if(!state.session||state.busy||state.view!=="connections")return;
  try {const data=await api("connections");if(state.view!=="connections")return;
    const labels={disabled:"未启用",stopped:"已停止",connecting:"连接中",connected:"传输已连接",retrying:"正在重试"};
    for(const row of data.connections){const el=[...document.querySelectorAll("[data-connection-state]")].find(e=>e.dataset.connectionState===row.id);if(el){el.textContent=labels[row.state]||row.state;el.className="tag "+(row.state==="connected"?"green":row.state==="retrying"?"amber":"gray");}}
  } catch(_) { /* Explicit refresh presents errors; background polling preserves typed credentials. */ }
},5000);
// Analytics is opt-in, public identifiers only, and never active on local/test previews.
fetch("/admin/config").then(r=>r.json()).then(({analytics:a})=>{const host=location.hostname;if(a?.enabled!==true||a.environment!=="production"||!a.website_id||host!==a.production_domain||location.protocol!=="https:"||/(localhost|^127\.|\.test$|\.local$|preview|staging|^\[?::1\]?$)/i.test(host))return;const url=new URL(a.script_url);if(url.protocol!=="https:")return;const script=document.createElement("script");script.src=url.href;script.defer=true;script.dataset.websiteId=a.website_id;document.head.append(script);}).catch(()=>{});
