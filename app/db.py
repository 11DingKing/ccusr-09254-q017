"""服务端业务模块。"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL

# 结算签发关键区置位的线程局部标志：为 True 时，该线程的 SQLite 事务以
# BEGIN IMMEDIATE 启动，其他普通事务仍使用 deferred BEGIN。
_immediate_flag = threading.local()


def set_immediate(enabled: bool) -> None:
    _immediate_flag.enabled = enabled


def is_immediate() -> bool:
    return getattr(_immediate_flag, "enabled", False)


@event.listens_for(Engine, "connect")
def _sqlite_disable_pysqlite_autobegin(dbapi_connection, connection_record) -> None:
    """关闭 pysqlite 隐式 BEGIN，交由 SQLAlchemy 的 begin 钩子统一控制。"""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        dbapi_connection.isolation_level = None


@event.listens_for(Engine, "begin")
def _sqlite_choose_begin(conn) -> None:
    """为 SQLite 显式开启事务。

    默认使用 deferred ``BEGIN``（读不持写锁）；当线程处于结算签发关键区
    （``is_immediate()``）时改用 ``BEGIN IMMEDIATE`` —— 事务一开始即取保留锁，
    使两个并发签发批次串行化，后到者在最新数据上执行，唯一约束必然拦截重复
    结算分录，避免 deferred 事务下各自快照通过约束后双双提交。
    """
    if conn.engine.url.get_backend_name() == "sqlite":
        if is_immediate():
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.exec_driver_sql("BEGIN")


engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """执行确定性的业务处理。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
