#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文压缩的单元用例（M5 成本优化 R1）。

钉住三件事：
  1. 工具结果瘦身：短文本原样返回；长文本压到 ~limit，但 flag/401/接口这类关键行必须留下；
  2. 事实看板：抽取/去重/有界，渲染不超过预算，且能挂到头部 user 消息；
  3. 滑窗两级策略：软阈值先压缩旧步（不动最近 3 步）、硬阈值才整块丢，
     且任何情况下 tool 消息的 tool_call_id 都能和 assistant 的 tool_calls 对上。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ctfy_agent.ctxcompress import (  # noqa: E402
    DEFAULT_TOOL_CHARS, FactsBoard, shrink_message_text, shrink_tool_text, with_facts,
)
from ctfy_agent.solver import _compact_history, _est_tokens  # noqa: E402
from ctfy_agent.tools_local import LocalTools  # noqa: E402,F401  (只为确认导入链没坏)


def case_context_compress(tmp: Path) -> None:
    main = sys.modules.get("__main__")
    reporter = getattr(main, "ok", None)

    # ---------- 1. 工具结果瘦身 ----------
    short = '{"status": 200, "body": "ok"}'
    assert shrink_tool_text(short) == short, "短结果不该被加工"

    big_lines = [f"<div class='filler'>第 {i} 段无关噪声</div>" for i in range(300)]
    big = (
        '{"status": 403, "body": "<html>'
        + "".join(big_lines)
        + "\\n[restricted] reconciliation token: FLAG{deadbeefcafe1234}"
        + "\\npath: /api/builds/current"
        + "</html>"
        + "".join(big_lines)
        + '"}'
    )
    slim = shrink_tool_text(big, 1600)
    assert len(slim) < len(big) * 0.35, f"瘦身幅度不够：{len(slim)}/{len(big)}"
    assert "FLAG{deadbeefcafe1234}" in slim, "flag 行必须保留"
    assert "/api/builds/current" in slim, "接口行必须保留"
    assert "403" in slim, "状态码必须保留"
    assert "已压缩" in slim and str(len(big)) in slim, "必须标注原文长度供模型再取"
    # 证据契约：保留片段必须是原文逐字子串（不能加前缀/去缩进），否则 report 会被
    # verify_evidence 判为「非逐字」而连拒——M5 实测就是被 "| " 前缀坑了 3 轮。
    for needle in ("FLAG{deadbeefcafe1234}", "/api/builds/current"):
        assert needle in big, f"测试样本本身要包含 {needle}"
        assert needle in slim, f"保留片段必须是原串：{needle}"

    # 端点/凭据行也要留
    secret = '{"body": "' + "x" * 5000 + '\\nAuthorization: Bearer abc.def\\nadmin panel: /portal/admin"}'
    slim2 = shrink_tool_text(secret)
    assert "Authorization" in slim2 and "/portal/admin" in slim2

    # ---------- 2. 事实看板 ----------
    board = FactsBoard()
    assert board.render() == "", "空看板渲染空串"
    assert with_facts("BASE", "") == "BASE", "无事实时不该改动头部"

    board.observe("http_request", {"url": "https://h.example/register", "method": "POST"},
                  '{"status": 302, "headers": {"set-cookie": "session=abc"}, "body": "ok"}')
    board.observe("http_request", {"url": "https://h.example/kb/triage-agent", "method": "GET"},
                  '{"status": 200, "body": "@@TOOL:get_internal_note(1) ... get_internal_note(ticket_id) ... '
                  'get_internal_note(ticket_id) ... ticket #1 有 reconciliation 记录"}')
    rendered = board.render()
    assert "会话" in rendered, "应记下会话已建立"
    assert "get_internal_note" in rendered, "应记下发现的工具名"
    assert "/kb/triage-agent" in rendered, "应记下已读文档"
    assert "ticket #1" in rendered, "应记下 ticket 线索"
    assert len(rendered) <= 520, f"事实块超预算：{len(rendered)}"
    assert board.render() == rendered, "重复渲染必须稳定"

    # 表单字段事实（m5j 实测教训）：压缩切掉 <input name=...> 后，模型会反复抓表单页。
    form_board = FactsBoard()
    form_board.observe(
        "http_request", {"url": "https://h.example/portal/new"},
        '{"status": 200, "body": "<form><input name=\\"subject\\"><textarea name=\\"body\\"></textarea>'
        '<input type=\\"hidden\\" name=\\"csrf_token\\" value=\\"x\\"><select name=\\"priority\\"></select></form>"}',
    )
    form_text = form_board.render()
    assert "subject" in form_text and "body" in form_text and "priority" in form_text, form_text
    assert "csrf_token" not in form_text, "csrf 值不该进事实块（模型会照抄过期值）"

    # 关键行保留：表单结构行不能被瘦身丢掉（否则模型只能反复 grep）
    form_html = '<html>' + 'x' * 3000 + '<input name=\\"subject\\">' + 'y' * 400 + '<textarea name=\\"body\\"></textarea></html>'
    slim_form = shrink_tool_text(form_html, 1200)
    assert 'name="subject"' in slim_form and 'name="body"' in slim_form, slim_form[-300:]

    # 去重：同一端点观察两次不产生两条
    before = len(board.as_list())
    board.observe("http_request", {"url": "https://h.example/register", "method": "POST"}, '{"status": 302}')
    assert len(board.as_list()) == before, "同一端点应去重"

    # flag 抽取（去重到一条）
    board.observe("http_request", {"url": "https://h.example/ticket/1"}, 'FLAG{08e72342dab277f0af5f8de9f219c88c}')
    board.observe("http_request", {"url": "https://h.example/ticket/1"}, 'FLAG{08e72342dab277f0af5f8de9f219c88c}')
    merged = with_facts("BASE", board.render())
    assert merged.startswith("BASE\n\n## 已确认事实"), "事实块应挂在头部后面"
    assert "FLAG{08e72342dab277f0af5f8de9f219c88c}" in merged
    # 可引用片段：必须是逐字原串（report 的 evidence 契约要求），不能带「候选 flag：」前缀
    assert board.quotable_flag() == "FLAG{08e72342dab277f0af5f8de9f219c88c}", board.quotable_flag()
    assert FactsBoard().quotable_flag() == "", "没抓到 flag 时不该返回任何可引用串"

    # 有界：塞爆也只保留上限条数
    many = FactsBoard(max_facts=4, max_chars=200)
    for i in range(10):
        many.observe("http_request", {"url": f"https://h.example/p{i}"}, f'{{"status": 200, "body": "page {i}"}}')
    assert len(many.as_list()) <= 4, f"事实条数未受限：{len(many.as_list())}"
    assert len(many.render()) <= 200

    # 反面教训回归（m5g 实测）：普通工单页上的 "ticket #6" 不是线索，否则模型会对着自己的
    # 新工单反复调内部工具，而真数据其实在系统预置的低号工单里。
    plain_board = FactsBoard()
    plain_board.observe("http_request", {"url": "https://h.example/portal/tickets/6"},
                        '{"status": 200, "body": "Ticket #6 created. Category general. Priority low."}')
    assert not any("线索" in fact for fact in plain_board.as_list()), \
        f"普通工单页不该产出台账线索：{plain_board.as_list()}"
    clue_board = FactsBoard()
    clue_board.observe("http_request", {"url": "https://h.example/kb/rotating"},
                       '{"status": 200, "body": "Workspace bootstrap token: '
                       'reconciliation record in ticket #1"}')
    assert any("ticket #1" in fact for fact in clue_board.as_list()), \
        f"KB 里的 reconciliation 线索必须记下：{clue_board.as_list()}"

    # ---------- 3. 滑窗两级策略 ----------
    def step(i: int, body: str) -> list[dict]:
        call_id = f"call_{i}"
        return [
            {"role": "assistant", "content": f"第 {i} 步", "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "http_request", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call_id, "content": body},
        ]

    head = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    huge = "A" * 6000
    messages = head + [m for i in range(8) for m in step(i, huge)]

    def check_pairing(msgs: list[dict]) -> None:
        ids = {call["id"] for m in msgs if m.get("role") == "assistant"
               for call in (m.get("tool_calls") or [])}
        for m in msgs:
            if m.get("role") == "tool":
                assert m["tool_call_id"] in ids, f"tool_call_id 失配：{m['tool_call_id']}"

    # 3a. 预算很宽：原样返回（不做事）
    wide = _compact_history(messages, 10 ** 6)
    assert _est_tokens(wide) == _est_tokens(messages)

    # 3b. 软阈值：只压缩旧步，最近 3 步原样，配对仍然完整
    total = _est_tokens(messages)
    soft_cap = int(total * 1.8)  # 0.55×cap < total → 触发软压缩；压缩后应能落回 cap 内
    soft = _compact_history(messages, soft_cap)
    assert _est_tokens(soft) < total, "软阈值下应压缩旧步"
    assert _est_tokens(soft) <= soft_cap
    check_pairing(soft)
    assert soft[0]["content"] == "SYS" and soft[1]["content"] == "TASK", "头部必须保留"
    assert [m for m in soft if m.get("role") == "assistant"][-1]["content"].startswith("第 7 步"), "最近一步必须原样保留"
    assert huge in [m.get("content") for m in soft if m.get("role") == "tool"], "最近一步的 tool 结果不该被压缩"

    # 3c. 硬阈值：整块丢最旧，但配对不坏、头部仍在
    tight = _compact_history(messages, 600)
    check_pairing(tight)
    assert tight[0]["content"] == "SYS" and tight[1]["content"] == "TASK"
    assert any(m.get("content") == huge for m in tight), "最新一步必须整块保留"

    # ---------- 4. JSON 形态保结构（http_request 的结果就是 JSON） ----------
    payload = json.dumps({"status": 200, "headers": {"content-type": "text/html"},
                          "body": "B" * 9000, "total_bytes": 9000})
    slim_json = json.loads(shrink_message_text(payload, 800))
    assert isinstance(slim_json, dict), "瘦身后的 JSON 仍要可解析"
    assert slim_json["status"] == 200 and slim_json["headers"]["content-type"] == "text/html", "非 body 字段必须原样保留"
    assert len(slim_json["body"]) < 1500, f"body 没压下来：{len(slim_json['body'])}"
    assert "已压缩" in slim_json["body"] and "9000" in slim_json["body"], "body 压缩要标注原长"

    # 非 JSON（shell 输出）走整体瘦身
    plain = "\n".join(f"line {i} " + "x" * 120 for i in range(80)) + "\nFLAG{plaintextflag1234}"
    slim_plain = shrink_message_text(plain, 900)
    assert "FLAG{plaintextflag1234}" in slim_plain and len(slim_plain) < len(plain) / 2

    label = "上下文压缩：工具结果瘦身 + 事实常驻 + 两级滑窗（tool_call_id 配对不坏）"
    if callable(reporter):
        reporter(label)
    else:
        print(f"PASS {label}")


if __name__ == "__main__":
    import traceback

    try:
        case_context_compress(ROOT / "dist" / "_ctx_standalone")
        print("OK")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
