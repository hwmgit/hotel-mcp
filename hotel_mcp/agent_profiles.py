"""Business capability profiles for independently named AI agents.

Prompts live in the AI platform; these profiles enforce hotel-side permissions.
Add new profiles only when the corresponding business tools/workflow exist.
"""

REQUEST_TOOLS = frozenset({"hotel_list_requests", "hotel_query_requests", "hotel_get_request",
                           "hotel_list_request_actions", "hotel_process_request", "hotel_cancel_request"})
AGENT_TYPES = {
    "room": {"label": "客房助手", "role": "room", "request_types": (), "tools": None,
             "description": "绑定具体入住，提交客房需求并接收处理结果"},
    "frontdesk": {"label": "前台服务", "role": "frontdesk", "request_types": (), "tools": None,
                  "description": "本店服务工单、入住与续房协同，不含经营查询"},
    "dining": {"label": "餐饮服务", "role": "frontdesk", "request_types": ("meal",),
               "tools": REQUEST_TOOLS | {"hotel_list_menu"}, "description": "仅本店菜单、点餐工单与配送结果处理"},
    "repair": {"label": "报修服务", "role": "frontdesk", "request_types": ("repair",),
               "tools": REQUEST_TOOLS, "description": "仅本店报修工单、受理、进展与结案"},
    "booking": {"label": "场地预约", "role": "frontdesk", "request_types": ("gym_booking", "meeting_booking"),
                "tools": REQUEST_TOOLS | {"hotel_list_booking_slots"}, "description": "仅本店健身和会议预约、时段查询与确认"},
    "sales": {"label": "经营查询", "role": "manager", "request_types": (), "tools": None,
              "description": "仅当前门店经营查询，不开放客房工单处理"},
}


def public_types():
    return [{"id": key, "label": row["label"], "description": row["description"], "requires_stay": key == "room"}
            for key, row in AGENT_TYPES.items()]
