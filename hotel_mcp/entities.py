"""Scoped store-name resolution. Suggestions never execute business operations."""
import unicodedata
from difflib import SequenceMatcher

from .domain import fail, text
from .sales import display_store_name


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


class Entities:
    def __init__(self, store):
        self.store = store

    def configure_aliases(self, entries):
        """Trusted local config: an explicit list atomically replaces the alias catalog."""
        if not isinstance(entries, list) or len(entries) > 2000:
            fail("VALIDATION_ERROR", "store_aliases 必须为最多2000条的列表")
        rows, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"hotel", "alias", "kind"}:
                fail("VALIDATION_ERROR", "别名条目仅支持 hotel/alias/kind")
            hotel = text(entry.get("hotel"), "hotel", 80)
            alias = text(entry.get("alias"), "alias", 80)
            normalized, kind = normalize(alias), entry.get("kind", "alias")
            if not normalized or kind not in ("alias", "asr") or (hotel, normalized) in seen:
                fail("VALIDATION_ERROR", "别名须包含文字或数字，同店规范化别名不能重复；kind 为 alias/asr")
            seen.add((hotel, normalized))
            rows.append((hotel, alias, normalized, kind))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for hotel in {r[0] for r in rows}:
                if not db.execute("SELECT 1 FROM stores WHERE hotel=?", (hotel,)).fetchone():
                    fail("NOT_FOUND", "别名对应的门店尚未配置")
            db.execute("DELETE FROM store_aliases")
            db.executemany("INSERT INTO store_aliases(hotel,alias,normalized_alias,kind) VALUES(?,?,?,?)", rows)

    def resolve(self, p, query, entity_type="store"):
        allowed = self.store.sales._scope(p)
        if entity_type != "store":
            fail("VALIDATION_ERROR", "本期仅支持门店实体 store")
        query = text(query, "query", 80)
        normalized = normalize(query)
        if not normalized:
            fail("VALIDATION_ERROR", "query 须包含门店名称文字或数字")
        with self.store.connect() as db:
            db.execute("BEGIN")
            stores = self.store.sales._stores(db, allowed)
            marks = ",".join("?" for _ in allowed)
            aliases = db.execute(f"SELECT * FROM store_aliases WHERE hotel IN ({marks}) ORDER BY hotel,normalized_alias",
                                 allowed).fetchall()
        terms = {s["hotel"]: [(normalize(s["name"]), "exact_name")] for s in stores}
        for alias in aliases:
            terms[alias["hotel"]].append((alias["normalized_alias"], alias["kind"]))
        matches, approximate = [], []
        priority = {"exact_name": 0, "alias": 1, "asr": 2}
        for s in stores:
            candidate = {"entity_id": s["hotel"], "name": display_store_name(s["name"])}
            # Stable IDs are exact only, never fuzzily corrected.
            if query == s["hotel"]:
                return self._result(query, normalized, [{**candidate, "match_type": "exact_id"}])
            kinds = [kind for value, kind in terms[s["hotel"]] if value == normalized]
            if kinds:
                matches.append({**candidate, "match_type": min(kinds, key=priority.get)})
            elif len(normalized) >= 2:
                # Fuzzy suggestions use names and genuine aliases, not known ASR errors.
                score = max((SequenceMatcher(None, normalized, value, autojunk=False).ratio()
                             for value, kind in terms[s["hotel"]] if kind != "asr"), default=0)
                if score >= .65:
                    approximate.append((score, {**candidate, "match_type": "similar"}))
        if not matches:
            matches = [c for _, c in sorted(approximate, key=lambda pair: (-pair[0], pair[1]["entity_id"]))]
        return self._result(query, normalized, matches)

    @staticmethod
    def _result(query, normalized, matches):
        resolved = len(matches) == 1 and matches[0]["match_type"] in ("exact_id", "exact_name", "alias")
        shown = matches[:5]
        if resolved:
            question = None
        elif not matches:
            question = "在当前可查询的门店中未找到匹配项，请提供完整门店名称。"
        elif len(matches) == 1:
            question = f"您指的是{shown[0]['name']}吗？"
        else:
            question = "您指的是哪家门店：" + "；".join(
                f"{i}. {c['name']}" for i, c in enumerate(shown, 1)) + "？"
            if len(matches) > len(shown):
                question += "还有其他候选，请提供更完整的门店名称。"
        return {"entity_type": "store", "query": query, "normalized_query": normalized,
                "status": "resolved" if resolved else "needs_clarification" if matches else "not_found",
                "resolved_id": shown[0]["entity_id"] if resolved else None,
                "requires_confirmation": not resolved, "candidates": shown, "has_more": len(matches) > len(shown),
                "clarification_question": question,
                "next_step": "query_sales_with_id" if resolved else "ask_user"}
