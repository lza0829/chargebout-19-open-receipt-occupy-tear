from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

# 架构口径（全局唯一，禁止混用）：
# 「可告警仍开充」——非空闲桩不做硬闸，仍允许落会话推进占用，
# 但非空闲开充事实必须在回执、会话册、审计流水可核且不可静默抹掉；
# 总览必须暴露异常占用，不得假装桩空闲可办。
SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  abnormal_open INTEGER NOT NULL DEFAULT 0,
  open_reason TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
-- 开充审计流水：每一次开充尝试一行，只追加不修改；
-- 结果=通过 的行随开充主事务同刷，结果=拒绝/异常 的行由路由独立短事务补登，
-- 任何读路径不得把失败尝试算进充电中。
CREATE TABLE IF NOT EXISTS open_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  business_day TEXT NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER,
  slip_no TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL,
  abnormal INTEGER NOT NULL DEFAULT 0,
  snapshot_pile_status TEXT NOT NULL DEFAULT '',
  snapshot_gun_status TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  session_id INTEGER
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 写锁竞争时排队等待，而不是立刻抛 database is locked
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_immediate():
    """写侧专用事务：立即取 RESERVED 写锁，把并发开充在入口处串行化。

    同库同时只能有一个 IMMEDIATE 事务持写锁，第二个在 BEGIN 处等待/超时，
    保证「并发双开不得双破空闲」：谁先持锁谁原子地把空闲写成占用，
    后来者重放同一谓词时看到的已是占用，按非空闲异常开充记账。
    """
    conn = connect()
    # 锁等待窗口：并发抢同一桩时让后来者排队而不是立刻报 database is locked
    conn.execute(f"PRAGMA busy_timeout={5000}")
    try:
        conn.isolation_level = None  # 手动事务，避免驱动在 BEGIN 后再插 BEGIN
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 既有库的在线迁移：CREATE TABLE IF NOT EXISTS 不会补列，这里幂等补异常列。
_MIGRATIONS = (
    ("sessions", "abnormal_open", "ALTER TABLE sessions ADD COLUMN abnormal_open INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "open_reason", "ALTER TABLE sessions ADD COLUMN open_reason TEXT NOT NULL DEFAULT ''"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for _table, column, ddl in _MIGRATIONS:
        if column not in cols:
            conn.execute(ddl)


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
