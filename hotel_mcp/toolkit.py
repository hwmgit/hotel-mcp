"""MCP schemas and dispatch; business rules remain in HotelStore."""
from mcp import types

from .domain import ACTIONS, CATEGORIES, STATUSES, REQUEST_TYPES
from .stay_extensions import MAX_AMOUNT_MINOR
from .sales import SALES_CATEGORIES


def string(maximum=80, optional=False):
    return {"type": "string", "minLength": 0 if optional else 1, "maxLength": maximum}


def tool(name, description, properties=None, required=None):
    return types.Tool(name=name, description=description, inputSchema={"type": "object",
                      "properties": properties or {}, "required": required or [], "additionalProperties": False})


PAGE = {"cursor": string(2048), "limit": {"type": "integer", "minimum": 1, "maximum": 50}}
WRITE = {"request_key": string(), "expected_version": {"type": "integer", "minimum": 1}}
REQUEST = {"request_id": string()}
QUOTE_WRITE = {**REQUEST, **WRITE, "quote_id": string()}
QUOTE_REQUIRED = ["request_id", "quote_id", "expected_version", "request_key"]


def tools_for(principal):
    sales_tools = [
        tool("hotel_resolve_entities", "在授权门店内解析提取出的门店名称片段；query 不是整句指令。resolved 才可直接使用 resolved_id 查询销售；needs_clarification/not_found 必须先向用户澄清，禁止默认选第一项或省略 hotel_ids 改查全部。本期仅支持 store。向用户使用门店名称，内部ID仅用于工具调用，不播报或从ID推断数据来源。",
             {"query": string(), "entity_type": {"type": "string", "enum": ["store"]}}, ["query"]),
        tool("hotel_list_stores", "列出有权查看的门店及销售数据日期范围。门店切换通过查询时指定 hotel_ids 完成。"),
        tool("hotel_query_sales", "查询销售额和订单数，按门店、分类、每日汇总并比较门店。金额单位为人民币分；数据口径为独立营业台账，不与服务工单自动结算。日期含首尾，最多366天。",
             {"date_from": string(10), "date_to": string(10),
              "hotel_ids": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True, "items": string()},
              "category": {"type": "string", "enum": list(SALES_CATEGORIES)}}, ["date_from", "date_to"]),
    ]
    if principal.role == "manager":
        return sales_tools
    query = {**PAGE, "request_type": {"type": "string", "enum": list(REQUEST_TYPES)},
             "statuses": {"type": "array", "minItems": 1, "uniqueItems": True,
                          "items": {"type": "string", "enum": list(STATUSES)}},
             "created_from": string(), "created_to": string()}
    if principal.role == "frontdesk":
        query.update({"room": string(), "stay_id": string()})
    items = [
        tool("hotel_list_menu", "查看本酒店可点菜品及人民币分价格；餐费线下结算。"),
        tool("hotel_list_booking_slots", "查询本酒店未来可预约固定时段及当前余量；提交不占用，前台确认时重新检查。",
             {"kind": {"type": "string", "enum": ["gym", "meeting"]}, "date": string(10)}, ["kind", "date"]),
        tool("hotel_list_requests", "兼容查询；仅返回当前授权范围工单，按编号分页。", {"after": string(optional=True)}),
        tool("hotel_query_requests", "按创建顺序倒序查询授权工单；分页复用相同筛选条件。", query),
        tool("hotel_get_request", "查询授权工单的最新详情和版本。", REQUEST, ["request_id"]),
        tool("hotel_list_request_actions", "查询工单处理历史，最新记录在前。", {**REQUEST, **PAGE}, ["request_id"]),
        tool("hotel_list_stay_extension_quotes", "查询有权访问的续房报价历史，最新报价在前。", {**REQUEST, **PAGE}, ["request_id"]),
        tool("hotel_cancel_request", "确认取消意图后执行。报障、点餐和预约的客房仅能取消待确认申请，已受理须由前台取消；续房前台还可取消已确认但未办理的申请。",
             {**REQUEST, **WRITE, "reason": string(500)}, ["request_id", "request_key", "expected_version", "reason"]),
    ]
    if principal.role == "room":
        items.extend([
            tool("hotel_submit_meal_order", "向住客说明菜品、数量、总价及线下结算，确认后提交；前台确认后安排送餐。",
                 {"items": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "object",
                            "properties": {"item_id": string(), "quantity": {"type": "integer", "minimum": 1, "maximum": 20}},
                            "required": ["item_id", "quantity"], "additionalProperties": False}},
                  "request_key": string(), "note": string(300, True)}, ["items", "request_key"]),
            tool("hotel_submit_booking", "确认场地、日期时段和人数后提交预约；申请不占资源，须等待前台确认。",
                 {"slot_id": string(), "participants": {"type": "integer", "minimum": 1, "maximum": 100},
                  "request_key": string(), "note": string(300, True)}, ["slot_id", "participants", "request_key"]),
            tool("hotel_get_current_stay", "查询当前凭据绑定的有效入住。"),
            tool("hotel_submit_stay_extension", "确认住客续房意图后申请目标退房日期；房态和价格由前台人工核实，不代表已办理。",
                 {"target_checkout_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                  "request_key": string(), "note": string(300, True)}, ["target_checkout_date", "request_key"]),
            tool("hotel_confirm_stay_extension", "向住客说明当前报价的日期、晚数、总价和费用条件，获得明确同意后确认该 quote_id；确认后仍待前台办理。",
                 QUOTE_WRITE, QUOTE_REQUIRED),
            tool("hotel_submit_request", "提交一般请求；同次请求及重试复用 request_key，房号由身份确定。",
                 {"request_key": string(), "description": string(300)}, ["request_key", "description"]),
            tool("hotel_submit_repair", "确认住客报障后提交；重试复用 request_key，禁止指定房间或入住身份。",
                 {"request_key": string(), "category": {"type": "string", "enum": list(CATEGORIES)},
                  "description": string(300), "location": string(80, True), "related_request_id": string(),
                  "urgency": {"type": "string", "enum": ["normal", "urgent"]}}, ["request_key", "category", "description"]),
        ])
    else:
        for action in ("accept", "complete"):
            items.append(tool("hotel_" + action + "_request", "仅工作人员明确确认后处理一般工单；报障须用 hotel_process_request。",
                              {**REQUEST, "note": string(160, True)}, ["request_id"]))
        items.extend([
            tool("hotel_start_stay", "仅工作人员确认入住后创建批次；不会生成客房凭据。",
                 {"room": string(), "expected_checkout_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                  "request_key": string()}, ["room", "expected_checkout_date", "request_key"]),
            tool("hotel_end_stay", "仅工作人员明确确认退房后执行。报障/一般及已受理点餐保留；未办理续房、待确认点餐和未履行预约自动取消，办理中续房须先核实结果。",
                 {"stay_id": string(), **WRITE, "reason": string(500)},
                 ["stay_id", "request_key", "expected_version", "reason"]),
            tool("hotel_process_request", "仅工作人员明确确认后处理工单。续房仅支持 add_note，其余操作必须使用续房专属工具；通知不能自动执行操作。",
                 {**REQUEST, **WRITE, "action": {"type": "string", "enum": list(ACTIONS)}, "note": string(500)},
                 ["request_id", "action", "note", "expected_version", "request_key"]),
            tool("hotel_quote_stay_extension", "前台人工核实同房续住和费用后报价。金额为人民币整数分，默认30分钟有效；零元需在费用说明中解释。修订须说明原因并重新让住客确认。",
                 {**REQUEST, **WRITE, "total_amount_minor": {"type": "integer", "minimum": 0, "maximum": MAX_AMOUNT_MINOR},
                  "pricing_note": string(500), "valid_for_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                  "change_reason": string(500)}, ["request_id", "total_amount_minor", "pricing_note", "expected_version", "request_key"]),
            tool("hotel_begin_stay_extension", "住客确认后，前台再次核实房态并准备办理时调用；只进入办理中，不代表完成。",
                 {**QUOTE_WRITE, "note": string(500)}, [*QUOTE_REQUIRED, "note"]),
            tool("hotel_finish_stay_extension", "仅前台已核实人工办理结果时记录 success/failed；成功才延长入住。不确定结果时保持办理中并追加说明，不可猜测。",
                 {**QUOTE_WRITE, "outcome": {"type": "string", "enum": ["success", "failed"]},
                  "note": string(500), "result_reference": string(160, True)}, [*QUOTE_REQUIRED, "outcome", "note"]),
            tool("hotel_reject_stay_extension", "前台核实后拒绝尚未确认的续房申请，说明原因。",
                 {**REQUEST, **WRITE, "reason": string(500)}, ["request_id", "reason", "expected_version", "request_key"]),
        ])
    return items + sales_tools if principal.role == "frontdesk" else items


READ_TOOLS = {"hotel_list_requests", "hotel_query_requests", "hotel_get_request", "hotel_list_request_actions", "hotel_get_current_stay",
              "hotel_list_stay_extension_quotes", "hotel_list_stores", "hotel_query_sales", "hotel_resolve_entities",
              "hotel_list_menu", "hotel_list_booking_slots"}


def dispatch(store, principal, name, arguments):
    methods = {"hotel_list_requests": store.list_requests, "hotel_query_requests": store.query_requests,
               "hotel_get_request": store.get_request, "hotel_list_request_actions": store.list_actions,
               "hotel_get_current_stay": store.current_stay, "hotel_start_stay": store.start_stay,
               "hotel_end_stay": store.end_stay, "hotel_submit_request": store.submit,
               "hotel_submit_repair": store.submit_repair, "hotel_process_request": store.process_request,
               "hotel_cancel_request": store.cancel_request,
               "hotel_submit_stay_extension": store.extensions.submit, "hotel_quote_stay_extension": store.extensions.quote,
               "hotel_confirm_stay_extension": store.extensions.confirm, "hotel_begin_stay_extension": store.extensions.begin,
               "hotel_finish_stay_extension": store.extensions.finish, "hotel_reject_stay_extension": store.extensions.reject,
               "hotel_list_stay_extension_quotes": store.extensions.list_quotes,
               "hotel_list_stores": store.sales.list_stores, "hotel_query_sales": store.sales.query,
               "hotel_resolve_entities": store.entities.resolve}
    methods.update({"hotel_list_menu": store.services.menu, "hotel_list_booking_slots": store.services.slots,
                    "hotel_submit_meal_order": store.services.submit_meal, "hotel_submit_booking": store.services.submit_booking})
    if name in ("hotel_accept_request", "hotel_complete_request"):
        return store.transition(principal, status="accepted" if name == "hotel_accept_request" else "completed", **arguments)
    return methods[name](principal, **arguments)
