from datetime import datetime
from sqlalchemy.orm import DeclarativeBase,Mapped,mapped_column
from uuid import UUID
from sqlalchemy import Uuid, Text, Enum,DateTime,func
from devflow_ai.enums import TaskStatus
from sqlalchemy.dialects.postgresql import JSONB


class Base(DeclarativeBase):
    pass

class Task(Base):
    __tablename__ = "tasks"
    id : Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True,server_default = func.gen_random_uuid())
    title : Mapped[str] = mapped_column(Text, nullable=False)
    description : Mapped[str] = mapped_column(Text, nullable=False)
    repo : Mapped[str] = mapped_column(Text, nullable=False)
    status : Mapped[TaskStatus] = mapped_column(Enum(TaskStatus,name ="task_status"),nullable=False,server_default= TaskStatus.PENDING.value)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_snapshot : Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    created_at : Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False,server_default=func.now())
    updated_at : Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False,server_default=func.now(),onupdate=func.now())
    