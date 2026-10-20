"""Read-only, explicitly labelled demo sales; no PMS/payment/platform integration."""
from datetime import date, timedelta

from .domain import fail


SALES_CATEGORIES = ("accommodation", "dining", "gym", "meeting_room")
DEMO_LABEL = {"data_source": "demo", "is_demo": True, "currency": "CNY",
              "display_notice": "演示数据：模拟已结算订单，非实际营业收入；金额单位为人民币分。"}


def business_date(value, field):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        fail("VALIDATION_ERROR", f"{field} 必须为 YYYY-MM-DD")


class Sales:
    def __init__(self, store):
        self.store = store

    @property
    def label(self):
        if self.store.is_mysql:
            return {**DEMO_LABEL, "display_notice": "营业台账，金额单位为人民币分。"}
        return DEMO_LABEL

    @staticmethod
    def _scope(p):
        if p.role == "manager":
            return p.allowed_hotels
        if p.role == "frontdesk":
            return (p.hotel,)
        fail("FORBIDDEN", "仅前台和授权管理身份可查询门店销售")

    @staticmethod
    def _stores(db, hotels):
        marks = ",".join("?" for _ in hotels)
        return db.execute(f"SELECT hotel,name FROM stores WHERE hotel IN ({marks}) ORDER BY hotel", hotels).fetchall()

    def list_stores(self, p):
        hotels = self._scope(p)
        with self.store.connect() as db:
            db.execute("BEGIN")
            stores = []
            for row in self._stores(db, hotels):
                span = db.execute("SELECT MIN(business_date),MAX(business_date) FROM demo_sales_orders WHERE hotel=?",
                                  (row["hotel"],)).fetchone()
                stores.append({**dict(row), "available_from": span[0], "available_to": span[1]})
        return {**self.label, "stores": stores}

    def query(self, p, date_from, date_to, hotel_ids=None, category=None):
        allowed = self._scope(p)
        start, end = business_date(date_from, "date_from"), business_date(date_to, "date_to")
        if not 0 <= (end - start).days < 366:
            fail("VALIDATION_ERROR", "日期区间须正序且不超过366天（含首尾日期）")
        if category is not None and category not in SALES_CATEGORIES:
            fail("VALIDATION_ERROR", "无效的销售分类")
        selected = allowed
        if hotel_ids is not None:
            if (not isinstance(hotel_ids, list) or not 1 <= len(hotel_ids) <= 100
                    or any(not isinstance(h, str) or not h or len(h) > 80 for h in hotel_ids)
                    or len(set(hotel_ids)) != len(hotel_ids)):
                fail("VALIDATION_ERROR", "hotel_ids 必须为1～100个不重复门店编号")
            if not set(hotel_ids).issubset(allowed):
                fail("FORBIDDEN", "查询包含未授权门店")
            selected = hotel_ids
        with self.store.connect() as db:
            # All breakdowns come from one read snapshot and the same filtered rows.
            db.execute("BEGIN")
            stores = self._stores(db, selected)
            if hotel_ids is not None and len(stores) != len(hotel_ids):
                fail("NOT_FOUND", "授权门店尚未配置")
            marks = ",".join("?" for _ in selected)
            args = [*selected, date_from, date_to]
            where = f"hotel IN ({marks}) AND business_date>=? AND business_date<=?"
            if category is not None:
                where += " AND category=?"
                args.append(category)
            rows = db.execute(f"SELECT hotel,business_date,category,SUM(amount_minor) AS sales_amount_minor,"
                              f"COUNT(*) AS order_count FROM demo_sales_orders WHERE {where} "
                              "GROUP BY hotel,business_date,category", args).fetchall()

        def metrics(**labels):
            return {**labels, "sales_amount_minor": 0, "order_count": 0}

        totals = metrics()
        by_store = {r["hotel"]: metrics(**dict(r)) for r in stores}
        by_category = {c: metrics(category=c) for c in ((category,) if category else SALES_CATEGORIES)}
        trend = {str(start + timedelta(days=i)): metrics(date=str(start + timedelta(days=i)))
                 for i in range((end - start).days + 1)}
        for row in rows:
            for bucket in (totals, by_store[row["hotel"]], by_category[row["category"]], trend[row["business_date"]]):
                for key in ("sales_amount_minor", "order_count"):
                    bucket[key] += row[key]
        ranked = sorted(by_store.values(), key=lambda r: (-r["sales_amount_minor"], r["hotel"]))
        return {**self.label, "date_from": date_from, "date_to": date_to,
                "date_basis": "Asia/Shanghai 酒店营业日期，包含首尾日期",
                "category": category, "hotel_ids": sorted(by_store), "totals": totals,
                "by_store": ranked, "by_category": list(by_category.values()), "daily_trend": list(trend.values())}
