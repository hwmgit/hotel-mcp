def remove_service_tables(db):
    """Remove v7/v6 additions when earlier tests construct old-version fixtures."""
    for table in ("account_audit", "account_operations", "web_sessions", "account_stays", "accounts", "booking_details", "booking_slots", "booking_resources", "meal_order_items", "meal_orders", "menu_items"):
        db.execute(f"DROP TABLE {table}")
    db.execute("DELETE FROM schema_migrations WHERE version>=6")
