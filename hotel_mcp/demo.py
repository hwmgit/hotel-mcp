"""Generate a new isolated demo instance. Never seed an existing database/config."""
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sales import SALES_CATEGORIES, business_date
from .store import HotelStore
from .domain import Principal


DEMO_STORES = (("demo-central", "中心店（演示）"), ("demo-riverside", "江畔店（演示）"),
               ("demo-airport", "机场店（演示）"))
DEPLOYMENT_STORES = (("demo-central", "中心店"), ("demo-riverside", "江畔店"),
                     ("demo-airport", "机场店"), ("demo-lake", "湖畔店"), ("demo-convention", "会展店"))
DEMO_ALIASES = [
    {"hotel": "demo-central", "alias": "中心店"},
    {"hotel": "demo-central", "alias": "中心门店"},
    {"hotel": "demo-central", "alias": "市区店"},
    {"hotel": "demo-riverside", "alias": "江畔店"},
    {"hotel": "demo-riverside", "alias": "江畔门店"},
    {"hotel": "demo-riverside", "alias": "江盼店", "kind": "asr"},
    {"hotel": "demo-riverside", "alias": "市区店"},
    {"hotel": "demo-airport", "alias": "机场店"},
]


def init_sales_demo(directory, end_date=None, *, stores=DEMO_STORES, days=7):
    end = business_date(end_date, "demo_end_date") if end_date is not None else datetime.now(timezone(timedelta(hours=8))).date()
    if not 1 <= days <= 366 or end.toordinal() < days:
        raise ValueError("demo_end_date must allow seven days of history")
    root = Path(directory)
    # Exclusive directory creation prevents mixing samples into any existing instance.
    root.mkdir(parents=True, exist_ok=False)
    store = HotelStore(root / "hotel.sqlite3")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("INSERT INTO stores(hotel,name) VALUES(?,?)", stores)
        for index, (hotel, _) in enumerate(stores):
            for day in range(days):
                business_day = str(end - timedelta(days=days - 1 - day))
                for category, base in zip(SALES_CATEGORIES, (39800, 8800, 3000, 60000)):
                    # Each row is one synthetic settled order, not a daily aggregate.
                    db.execute("INSERT INTO demo_sales_orders(id,hotel,business_date,category,amount_minor) VALUES(?,?,?,?,?)",
                               (f"demo-{hotel}-{business_day}-{category}", hotel, business_day, category,
                                base + index * 1000 + day * 100))
    store.entities.configure_aliases(DEMO_ALIASES)
    principals = [{"id": "sales-manager", "role": "manager", "allowed_hotels": [h for h, _ in stores]},
                  {"id": "central-manager", "role": "manager", "allowed_hotels": [DEMO_STORES[0][0]]}]
    principals.extend({"id": f"frontdesk-{hotel}", "hotel": hotel, "role": "frontdesk"} for hotel, _ in stores)
    for row in principals:
        row["token"] = secrets.token_urlsafe(32)
    with (root / "config.json").open("x", encoding="utf-8") as stream:
        json.dump({"principals": principals, "rooms": [], "store_aliases": DEMO_ALIASES}, stream, ensure_ascii=False, indent=2)
    return {"directory": str(root.resolve()), "date_from": str(end - timedelta(days=days - 1)), "date_to": str(end),
            "store_count": len(stores), "order_count": len(stores) * days * 4, "is_demo": True}


def init_full_demo(directory, end_date=None, *, stores=DEMO_STORES, days=7):
    summary = init_sales_demo(directory, end_date, stores=stores, days=days)
    root = Path(directory)
    store = HotelStore(root / "hotel.sqlite3")
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    local_now = datetime.now(timezone(timedelta(hours=8)))
    start_day = local_now.date() + timedelta(days=1)
    for hotel, _ in stores:
        rooms = [{"hotel": hotel, "room": str(n)} for n in (101, 102)]
        config["rooms"].extend(rooms)
        store.configure_rooms(rooms)
        desk = Principal(hotel, "frontdesk", principal_id=f"frontdesk-{hotel}")
        guests = []
        for room in rooms:
            stay = store.start_stay(desk, room["room"], str(start_day + timedelta(days=7)), "demo-stay-" + room["room"])["stay"]
            ident = f"room-{hotel}-{room['room']}"
            config["principals"].append({"id": ident, "role": "room", "hotel": hotel, "room": room["room"],
                                         "stay_id": stay["id"], "token": secrets.token_urlsafe(32)})
            guests.append(Principal(hotel, "room", room["room"], stay["id"], ident))
        with store.connect() as db:
            for slug, name, desc, price in [("breakfast", "元气早餐", "全麦面包 · 鸡蛋 · 鲜牛奶", 3800),
                                             ("noodles", "招牌牛肉面", "慢炖牛肉 · 时蔬 · 手工面", 4800),
                                             ("salad", "田园轻食沙拉", "当季蔬菜 · 鸡胸肉 · 油醋汁", 4200),
                                             ("juice", "鲜榨橙汁", "鲜榨果汁，少冰可备注", 2200)]:
                db.execute("INSERT INTO menu_items(id,hotel,name,description,price_minor) VALUES(?,?,?,?,?)",
                           (f"{hotel}-{slug}", hotel, name, desc, price))
            for slug, name, kind, capacity, max_people in [("gym", "活力健身房", "gym", 4, 4),
                                                           ("meeting", "云端会议室", "meeting", 1, 12)]:
                rid = f"{hotel}-{slug}"
                db.execute("INSERT INTO booking_resources(id,hotel,name,kind,capacity,max_people) VALUES(?,?,?,?,?,?)",
                           (rid, hotel, name, kind, capacity, max_people))
                for offset in range(3):
                    day = start_day + timedelta(days=offset)
                    for hour in (9, 14, 18):
                        start = datetime.combine(day, datetime.min.time(), tzinfo=local_now.tzinfo) + timedelta(hours=hour)
                        utc = lambda value: value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                        db.execute("INSERT INTO booking_slots VALUES(?,?,?,?,?,?)",
                                   (f"{rid}-{day}-{hour}", hotel, rid, str(day), utc(start), utc(start + timedelta(hours=1))))
        guest = guests[0]
        store.submit_repair(guest, "demo-repair", "air_conditioning", "空调制冷效果不好", location="卧室")
        store.extensions.submit(guest, str(start_day + timedelta(days=9)), "demo-extension")
        store.services.submit_meal(guest, [{"item_id": f"{hotel}-noodles", "quantity": 1},
                                         {"item_id": f"{hotel}-juice", "quantity": 1}], "demo-meal", "请送到房间")
        store.services.submit_booking(guest, f"{hotel}-gym-{start_day}-9", 2, "demo-gym")
        store.services.submit_booking(guests[1], f"{hotel}-meeting-{start_day}-14", 6, "demo-meeting", "需要投屏")
    with (root / "config.json").open("w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    return {**summary, "booking_date": str(start_day), "admin_path": "/admin/", "request_count": len(stores) * 5}
