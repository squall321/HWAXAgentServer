# RA 초안의 템플릿 id · 판 — 박스 파일이 없으면 종전 글자 그대로, 있으면 스스로 등록한 형식으로(ra_formats)
"""HWAXPortal docs/leftovers-1121 L21-6 ①. 고정하는 것.

  · 파일이 없으면 인자가 종전과 같다 — `template_id="deliberation"` · `template_version=1`.
  · 파일에 맞는 줄이 있으면 그 id · 판으로 올린다. 고치면 재기동 없이 따라간다.
  · 깨진 파일 · 꼴이 틀린 줄 · 모르는 종류는 버리고 기본으로 돌며 까닭을 /health 에 적는다(틀릴 거면 종전 쪽으로).
  · 엔진의 두 저장 자리(심의 보고서 · `/보고서`)가 이 모듈을 거친다 — 글자로 박힌 id 가 남아 있지 않다.
"""
import ast
import json
import os
from pathlib import Path

import ra_formats

_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path, monkeypatch, doc, raw=None):
    f = tmp_path / "ra-formats.local.json"
    f.write_text(raw if raw is not None else json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ra_formats, "PATH", f)
    return f


def test_파일이_없으면_종전_인자_그대로다():
    assert ra_formats.draft_args("deliberation") == {"template_id": "deliberation", "template_version": 1}
    assert ra_formats.draft_args() == {"template_id": "deliberation", "template_version": 1}
    h = ra_formats.health()
    assert h == {"file": False, "error": None,
                 "kinds": {"deliberation": {"template_id": "deliberation", "template_version": 1, "source": "default"}}}


def test_박스_파일의_줄로_바꾼다(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"_doc": "메모 칸은 건너뛴다",
                                   "deliberation": {"template_id": "sys.hwax.deliberation", "template_version": 3}})
    assert ra_formats.draft_args("deliberation") == {"template_id": "sys.hwax.deliberation", "template_version": 3}
    h = ra_formats.health()
    assert h["file"] is True and h["error"] is None
    assert h["kinds"]["deliberation"] == {"template_id": "sys.hwax.deliberation", "template_version": 3, "source": "box"}


def test_포털_자동_파이프라인_줄은_경고_없이_건너뛴다(tmp_path, monkeypatch, capsys):
    # 같은 박스 파일에 포털만 읽는 auto_pipeline 줄이 있어도 「모르는 종류」 경고를 내지 않는다(HWAXPortal docs/auto-pipeline AP-18)
    _write(tmp_path, monkeypatch, {"deliberation": {"template_id": "sys.hwax.deliberation", "template_version": 3},
                                   "auto_pipeline": {"board": "sys-hwax-auto", "token_file": "infra/secrets/x.token", "formats": {}}})
    assert ra_formats.draft_args("deliberation") == {"template_id": "sys.hwax.deliberation", "template_version": 3}
    assert ra_formats.health()["error"] is None
    assert "auto_pipeline" not in ra_formats.health()["kinds"]
    assert "모르는 종류" not in capsys.readouterr().out


def test_파일을_고치면_재기동_없이_따라간다(tmp_path, monkeypatch):
    f = _write(tmp_path, monkeypatch, {"deliberation": {"template_id": "sys.hwax.deliberation", "template_version": 1}})
    assert ra_formats.template()[1] == 1
    f.write_text(json.dumps({"deliberation": {"template_id": "sys.hwax.deliberation", "template_version": 22}}), encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))       # 같은 틱에 쓰여도 바뀐 시각이 달라지게
    assert ra_formats.template() == ("sys.hwax.deliberation", 22)
    f.unlink()
    assert ra_formats.template() == ("deliberation", 1), "파일을 지우면 기본으로 돌아간다"


def test_깨진_파일과_틀린_줄은_버리고_기본으로_돈다(tmp_path, monkeypatch, capsys):
    _write(tmp_path, monkeypatch, None, raw="{ 깨진 JSON")
    assert ra_formats.draft_args() == {"template_id": "deliberation", "template_version": 1}
    assert "못 읽음" in ra_formats.health()["error"]
    for bad in ({"template_id": "sys.hwax.deliberation", "template_version": "1"},       # 판이 글이다
                {"template_id": "sys.hwax.deliberation", "template_version": True},      # bool 은 수가 아니다
                {"template_id": "sys.hwax.deliberation", "template_version": 0},
                {"template_id": "../etc", "template_version": 1},                        # id 꼴이 아니다
                {"template_id": "", "template_version": 1},
                {"template_version": 1}, "글", None, ["목록"]):
        monkeypatch.setattr(ra_formats, "_STATE", {"key": "아직 읽지 않음", "map": {}, "error": None})
        _write(tmp_path, monkeypatch, {"deliberation": bad})
        assert ra_formats.draft_args() == {"template_id": "deliberation", "template_version": 1}, bad
        assert "deliberation" in ra_formats.health()["error"], bad
    monkeypatch.setattr(ra_formats, "_STATE", {"key": "아직 읽지 않음", "map": {}, "error": None})
    _write(tmp_path, monkeypatch, ["목록"])
    assert ra_formats.draft_args() == {"template_id": "deliberation", "template_version": 1}
    monkeypatch.setattr(ra_formats, "_STATE", {"key": "아직 읽지 않음", "map": {}, "error": None})
    _write(tmp_path, monkeypatch, {"모르는종류": {"template_id": "sys.x.y", "template_version": 1},
                                   "deliberation": {"template_id": "sys.hwax.deliberation", "template_version": 2}})
    assert ra_formats.draft_args() == {"template_id": "sys.hwax.deliberation", "template_version": 2}, "맞는 줄은 살린다"
    assert "모르는종류" in ra_formats.health()["error"]
    assert "⚠" in capsys.readouterr().out


def test_엔진의_저장_자리는_글자로_박힌_id_없이_이_모듈을_거친다():
    """create_report_draft 를 부르는 곳이 템플릿 id 를 글자로 들고 있으면 박스 파일을 따라가지 않는 자리가 남는다."""
    src = (_ROOT / "deliberation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    literal, via = [], 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "template_id" in keys:
                literal.append(node.lineno)
            for k, v in zip(node.keys, node.values):
                if k is None and isinstance(v, ast.Call) and ast.unparse(v.func) == "ra_formats.draft_args":
                    via += 1
    assert literal == [], f"template_id 를 글자로 든 dict 가 남았다 — 줄 {literal}"
    assert via == 2, f"ra_formats.draft_args 를 펼치는 저장 자리가 둘이어야 한다(심의 보고서 · /보고서) — {via}"


def test_health_에_칸이_있다():
    import app

    assert app._ra_formats_health()["kinds"]["deliberation"]["source"] == "default"
