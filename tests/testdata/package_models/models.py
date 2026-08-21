from typing import List

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Notebook(Base):
    __tablename__ = "notebook"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    notes: Mapped[List["Note"]] = relationship(back_populates="notebook")


class Note(Base):
    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(200))
    notebook_id: Mapped[int] = mapped_column(ForeignKey("notebook.id"))
    notebook: Mapped["Notebook"] = relationship(back_populates="notes")
