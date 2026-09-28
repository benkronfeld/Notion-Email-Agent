"""Repositories — the only code that speaks SQL.

Each function takes an `AsyncSession` first and uses SQLAlchemy 2.0 style (`select`,
`session.execute`). `reminders.py` is owned by another workstream.
"""
