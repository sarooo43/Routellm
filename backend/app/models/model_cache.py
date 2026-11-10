"""Discovered provider models, persisted so a restart does not re-discover.

The in-memory cache in ``app.providers.model_registry`` is enough for a running
process, but the router restarts (and ``python run.py`` reloads) were re-asking
every provider for its model list on the first request after each start. That
is a live API call on the critical path of a user's prompt.

Storage rules:

* the credential is stored only as a SHA-256 fingerprint — the same rule the
  in-memory cache follows, so this table can never become a key store;
* ``fetched_at`` is a Unix timestamp, so the TTL is comparable without timezone
  arithmetic;
* a row is advisory: if it is missing, stale or unreadable, discovery simply
  runs again.
"""

from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class DiscoveredModelCacheDB(SQLModel, table=True):
    __tablename__ = "discovered_models"
    __table_args__ = (
        UniqueConstraint("provider", "key_fingerprint", name="uq_discovered_models_scope"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)
    #: SHA-256 of the credential, truncated. Never the credential itself.
    key_fingerprint: str = Field(index=True)
    #: JSON array of chat-capable model ids.
    models_json: str = Field(sa_column=Column(Text, nullable=False))
    #: Unix epoch seconds (float), so TTL comparison is trivial.
    fetched_at: float = Field(default=0.0)
