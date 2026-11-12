"""Versioned database migrations (Alembic).

The schema is changed only through the numbered scripts in ``versions/``: each
one says exactly what it adds, and ``alembic_version`` in the database records
which of them have been applied. ``qts db upgrade`` applies the missing ones.
"""
