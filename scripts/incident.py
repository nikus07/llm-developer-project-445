"""Разбор инцидента по журналам: python scripts/incident.py [trace_id]

Без аргумента берёт последнее исполнение (executed) и показывает цепочку:
prepare (ход с карточкой) → карточка (pending_id в ключе идемпотентности) →
исполнение с before/after. Дальше — полная хронология хода по trace_id.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit  # noqa: E402
from core import db  # noqa: E402


def show(trace_id: str) -> None:
    print(f"── ход {trace_id}")
    for e in audit.trace(trace_id):
        ts = e["ts"].strftime("%H:%M:%S.%f")[:-3]
        if e["kind"] == "audit":
            print(f"  {ts} audit {e['outcome']:<22} {e['what']} before={e['before']} after={e['after']} key={e['idempotency_key']}")
        elif e["kind"] == "llm":
            print(f"  {ts} llm   {e['agent']:<10} {e['model']} токены {e['input_tokens']}→{e['output_tokens']} {e['latency_ms']} мс")
        else:
            print(f"  {ts} tool  {e['tool']:<24} {e['outcome']} {e['latency_ms']} мс args={e['args']}")


def main() -> None:
    trace_id = sys.argv[1] if len(sys.argv) > 1 else None
    row = db.fetchone(
        "select trace_id, idempotency_key from audit_log where outcome = 'executed'"
        + (" and trace_id = %s" if trace_id else "") + " order by id desc limit 1",
        (trace_id,) if trace_id else (),
    )
    if row is None:
        print("исполнений в журнале нет" if not trace_id else f"по {trace_id} исполнения нет; хронология:")
        if trace_id:
            show(trace_id)
        return
    pending_id = (row["idempotency_key"] or "").split(":")[-1]
    prepared = db.fetchone(
        "select trace_id from tool_calls where outcome = 'card_created' and args->>'pending_id' = %s",
        (pending_id,),
    )
    if prepared:
        print(f"prepare → карточка {pending_id}")
        show(prepared["trace_id"])
    print(f"исполнение карточки {pending_id}")
    show(row["trace_id"])
    dups = db.fetchall(
        "select trace_id from audit_log where outcome = 'duplicate_suppressed' and idempotency_key = %s",
        (row["idempotency_key"],),
    )
    for d in dups:
        print("повтор «да»:")
        show(d["trace_id"])


if __name__ == "__main__":
    main()
