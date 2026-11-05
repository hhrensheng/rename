"""Codex adapter.

会话存储在 CODEX_HOME（默认 ~/.codex）的 state_<N>.sqlite。
新版以 threads.name 作为显示名称，旧版仅有 title；写入时同步兼容字段。
正文仍从 threads.rollout_path 指向的 JSONL 读取。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from ..models import Message, Session
from ._sqlite import connect_read, connect_write
from .base import Adapter

_VER_RE = re.compile(r"state_(\d+)\.sqlite$")


def _codex_root() -> Path:
    root = os.environ.get("CODEX_HOME")
    return Path(root).expanduser() if root else Path.home() / ".codex"


def _thread_columns(con: sqlite3.Connection) -> set[str]:
    return {row[1] for row in con.execute("PRAGMA table_info(threads)")}


def _find_state_db() -> Path | None:
    root = _codex_root()
    if not root.is_dir():
        return None
    candidates = sorted(
        root.glob("state_*.sqlite"),
        key=lambda p: int(m.group(1)) if (m := _VER_RE.search(p.name)) else -1,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    legacy = root / "state.sqlite"
    return legacy if legacy.exists() else None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("text")
        ]
        return "\n".join(parts)
    return ""


class CodexAdapter(Adapter):
    name = "codex"
    label = "Codex"

    def available(self) -> bool:
        return _find_state_db() is not None

    def discover(self, since: float) -> list[Session]:
        db = _find_state_db()
        if not db:
            return []
        since_ms = int(since * 1000)
        con = connect_read(db)
        try:
            columns = _thread_columns(con)
            # 新版 name 优先于旧 title，否则会把已改名会话误认为仍使用旧标题。
            title = "COALESCE(NULLIF(trim(name), ''), title)" if "name" in columns else "title"
            # 迁移中的毫秒列允许为空；旧数据库只有秒列，须统一单位后过滤。
            if "updated_at_ms" in columns and "updated_at" in columns:
                updated = "COALESCE(updated_at_ms, updated_at * 1000)"
            elif "updated_at_ms" in columns:
                updated = "updated_at_ms"
            else:
                updated = "updated_at * 1000"
            optional = [c for c in ("cwd", "archived", "first_user_message") if c in columns]
            fields = [
                "id", f"{title} AS title", "rollout_path",
                f"{updated} AS updated_at_ms", *optional,
            ]
            # SQL 结构仅取固定字段与表达式，时间条件始终使用参数绑定。
            cur = con.execute(
                f"SELECT {','.join(fields)} FROM threads "
                f"WHERE {updated} >= ? ORDER BY {updated} DESC",
                (since_ms,),
            )
            cols = [d[0] for d in cur.description]
            out: list[Session] = []
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                if r.get("archived"):  # skip archived threads (truthy flag)
                    continue
                updated = r.get("updated_at_ms")
                if updated is None:
                    continue
                out.append(
                    Session(
                        tool=self.name,
                        id=r["id"],
                        title=r.get("title"),
                        last_active=updated / 1000.0,
                        cwd=r.get("cwd"),
                        meta={
                            "db": str(db),
                            "rollout_path": r.get("rollout_path"),
                            "first_user_message": r.get("first_user_message"),
                        },
                    )
                )
            return out
        finally:
            con.close()

    def read_transcript(self, session: Session) -> list[Message]:
        rollout = session.meta.get("rollout_path")
        msgs: list[Message] = []
        if rollout and Path(rollout).exists():
            try:
                with open(rollout, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"message"' not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") != "response_item":
                            continue
                        payload = obj.get("payload") or {}
                        if payload.get("type") != "message":
                            continue
                        role = payload.get("role")
                        if role not in ("user", "assistant"):
                            continue
                        text = _content_text(payload.get("content"))
                        if text.strip():
                            msgs.append(Message(role=role, text=text))
            except OSError:
                msgs = []
        if not msgs:
            fum = session.meta.get("first_user_message")
            if fum:
                return [Message(role="user", text=fum)]
        return msgs

    def set_title(self, session: Session, title: str) -> None:
        db = session.meta["db"]
        con = connect_write(db)
        try:
            # 同一事务同步新旧显示字段，不修改活跃时间，避免触发重复改名。
            if "name" in _thread_columns(con):
                cur = con.execute(
                    "UPDATE threads SET name = ?, title = ? WHERE id = ?",
                    (title, title, session.id),
                )
            else:
                cur = con.execute(
                    "UPDATE threads SET title = ? WHERE id = ?", (title, session.id)
                )
            if cur.rowcount != 1:
                raise ValueError(f"Codex 会话不存在，未写入标题：{session.id}")
            con.commit()
        finally:
            con.close()
