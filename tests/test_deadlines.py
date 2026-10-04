"""本轮功能测试: 当前发布版的统一 UTC 评审截止时刻。

覆盖需求:
- 会务方为当前发布版设置一次统一 UTC 评审截止时刻 (双版本: 资料修订号+发布序号);
- 时刻必须是带时区的 UTC ISO 8601 且严格晚于设置时刻;
  naive 时刻/无法解析/过去或现在时刻 -> 422;
- 版本不符 (修订号或发布序号不一致, 过期/超前) -> 409, 拒绝且无任何改动;
  尚未发布 -> 404;
- 同版同值重试幂等 (changed=false, set_at 不变), 同版异值 -> 409 (原时刻不变);
- 设置不推进资料修订号与发布序号;
- 未设置截止时沿用现有行为 (无逾期槽位);
- 各槽位状态: unconfirmed / confirmed_not_submitted / submitted / overdue / recused;
- 到达截止时刻仍未交评语的槽位即逾期 (未确认与已确认未交均算):
  评审人不得再确认或交评语 (409); 已交评语及收据继续有效 (同内容重试仍返原收据);
- 补位预演/发布在同一写事务内释放逾期槽位 (即使此前已确认),
  且本次补位不会把该稿重新分给该逾期评审人 (fixed_reviewer_review_overdue);
  其余确认槽位仍固定, 已交评语随连续保留槽位沿用;
- 旧版截止不约束新发布版: 新发布序号未设置截止时无逾期, 需另行设置;
- 原有确认、评语及补位接口的输入输出、版本与收据语义保持可用。
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMPDIR = tempfile.mkdtemp(prefix="review-deadlines-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
ISO = "%Y-%m-%dT%H:%M:%S.%f%z"


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "review_corrections", "assignment_locks", "paper_guarantee_levels",
            "review_deadlines",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution=None, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, serial, score=4, comment="评语内容"):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def setup(client, reviewers=("R1", "R2", "R3", "R4")):
    """4 名不同机构评审人 + 1 篇论文, 发布。返回 (serial, revision, plan_pair)。"""
    for rid in reviewers:
        add_reviewer(client, rid)
    add_paper(client)
    pub = publish(client)
    rev = client.get("/meta", headers=ORG).json()["revision"]
    return pub["serial"], rev, pub["plan"]["P1"]


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def set_deadline(client, rev, serial, when):
    return client.post(
        "/assignment/review-deadline",
        headers=ORG,
        json={"base_revision": rev, "base_serial": serial, "deadline_at": when},
    )


def force_past_deadline(serial, revision, seconds_ago=1):
    """直接写入一个已过期的截止行 (避免真实等待), 返回其 deadline_at 字符串。"""
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    with db.write_txn() as conn:
        conn.execute(
            "INSERT INTO review_deadlines(serial, revision, deadline_at, set_at)"
            " VALUES (?,?,?,?)",
            (serial, revision, past.isoformat(),
             (past - timedelta(hours=1)).isoformat()),
        )
    return past.isoformat()


def backfill_publish(client):
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"], dry
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------ 设置: 成功 / 幂等 / 拒绝

def test_set_future_deadline_succeeds_and_does_not_bump_versions(client):
    serial, rev, _ = setup(client)
    future = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    r = set_deadline(client, rev, serial, future)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["review_deadline"]["deadline_at"].rstrip("Z").startswith(future.rstrip("Z"))
    assert body["review_deadline"]["serial"] == serial
    assert body["review_deadline"]["revision"] == rev
    assert body["deadline_passed"] is False
    # 设置截止不推进资料修订号, 也不推进发布序号
    meta = client.get("/meta", headers=ORG).json()
    assert meta["revision"] == rev
    assert meta["published"]["serial"] == serial
    assert meta["review_deadline"]["deadline_at"] is not None


def test_same_version_same_value_retry_is_idempotent(client):
    serial, rev, _ = setup(client)
    future = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    first = set_deadline(client, rev, serial, future).json()
    # 同版同值重试: 不产生新变更, set_at 保持首次值
    retry = set_deadline(client, rev, serial, future)
    assert retry.status_code == 200
    j = retry.json()
    assert j["changed"] is False
    assert j["review_deadline"]["set_at"] == first["review_deadline"]["set_at"]
    assert j["review_deadline"]["deadline_at"] == first["review_deadline"]["deadline_at"]


def test_same_version_different_value_rejected(client):
    serial, rev, _ = setup(client)
    t1 = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    t2 = iso(datetime.now(timezone.utc) + timedelta(hours=3))
    set_deadline(client, rev, serial, t1)
    r = set_deadline(client, rev, serial, t2)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["existing_deadline_at"] != t2 or detail["serial"] == serial
    # 原截止时刻不变
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    assert got["review_deadline"]["deadline_at"].rstrip("Z") == t1.rstrip("Z")


def test_wrong_revision_rejected_without_change(client):
    serial, rev, _ = setup(client)
    future = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    r = set_deadline(client, rev + 1, serial, future)  # 超前修订号
    assert r.status_code == 409
    r = set_deadline(client, max(rev - 1, 0), serial, future)  # 过期修订号
    assert r.status_code == 409
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    assert got["review_deadline"] is None


def test_wrong_serial_rejected(client):
    serial, rev, _ = setup(client)
    future = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    r = set_deadline(client, rev, serial + 1, future)
    assert r.status_code == 409
    r = set_deadline(client, rev, 999, future)
    assert r.status_code == 409
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    assert got["review_deadline"] is None


def test_set_deadline_before_publish_404(client):
    for rid in ("R1", "R2", "R3"):
        add_reviewer(client, rid)
    add_paper(client)
    rev = client.get("/meta", headers=ORG).json()["revision"]
    future = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    r = set_deadline(client, rev, 1, future)
    assert r.status_code == 404


def test_past_or_present_time_rejected_422(client):
    serial, rev, _ = setup(client)
    now = datetime.now(timezone.utc)
    for when in (iso(now - timedelta(seconds=1)), iso(now)):
        r = set_deadline(client, rev, serial, when)
        assert r.status_code == 422, when
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    assert got["review_deadline"] is None


def test_naive_or_unparseable_time_rejected_422(client):
    serial, rev, _ = setup(client)
    for when in ("2026-10-10T12:00:00", "not-a-time", "2026-13-40T99:00:00Z", "  "):
        r = set_deadline(client, rev, serial, when)
        assert r.status_code == 422, when


def test_offset_time_accepted_and_normalized_to_utc(client):
    serial, rev, _ = setup(client)
    # 12:00Z == 20:00+08:00
    r = set_deadline(client, rev, serial, "2099-10-10T20:00:00+08:00")
    assert r.status_code == 200, r.text
    stored = r.json()["review_deadline"]["deadline_at"]
    assert "2099-10-10T12:00:00" in stored


# ------------------------------------------------------------ 状态总览

def test_status_overview_before_and_without_deadline(client):
    serial, rev, pair = setup(client)
    # 未设置截止: 沿用现有行为 —— 不存在 overdue
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    assert got["review_deadline"] is None and got["deadline_passed"] is False
    statuses = {s["reviewer_id"]: s["review_status"]
                for s in got["papers"]["P1"]["slots"]}
    assert statuses == {pair[0]: "unconfirmed", pair[1]: "unconfirmed"}
    assert got["summary"]["unconfirmed"] == 2
    assert got["summary"]["overdue"] == 0


def test_status_overview_all_four_states(client):
    serial, rev, pair = setup(client)
    r_confirmed, r_pending = pair[0], pair[1]
    decide(client, r_confirmed, "P1", "confirm")
    # 确认者先交评语 -> submitted; 另一人仍 pending
    submit_review(client, r_confirmed, "P1", serial, 4, "已交评语")
    # 再加一名评审人通过回避路径不易构造 (当前方案仅 2 槽位); 改为补第三篇?
    # 这里直接验证 3 态; recused 态在另一个用例覆盖。
    force_past_deadline(serial, rev)
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    assert got["deadline_passed"] is True
    statuses = {s["reviewer_id"]: s["review_status"]
                for s in got["papers"]["P1"]["slots"]}
    assert statuses[r_confirmed] == "submitted"
    assert statuses[r_pending] == "overdue"
    summary = got["summary"]
    assert summary["submitted"] == 1
    assert summary["overdue"] == 1
    assert summary["unconfirmed"] == 0


def test_confirmed_not_submitted_becomes_overdue(client):
    serial, rev, pair = setup(client)
    decide(client, pair[0], "P1", "confirm")  # 已确认但未交
    force_past_deadline(serial, rev)
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    statuses = {s["reviewer_id"]: s["review_status"]
                for s in got["papers"]["P1"]["slots"]}
    assert statuses[pair[0]] == "overdue"          # 已确认未交 -> 逾期
    assert statuses[pair[1]] == "overdue"          # 未确认 -> 逾期
    # /assignment 与 /papers/{id}/reviews 同样可见 overdue
    assignment = client.get("/assignment", headers=ORG).json()
    astatus = {s["reviewer_id"]: s["review_status"]
               for s in assignment["papers"]["P1"]["slots"]}
    assert astatus[pair[0]] == "overdue"
    pr = client.get("/papers/P1/reviews", headers=ORG).json()
    assert pr["progress"]["overdue"] == 2
    assert pr["deadline_passed"] is True


def test_recused_slot_is_not_overdue(client):
    serial, rev, pair = setup(client)
    decide(client, pair[0], "P1", "recuse", reason="存在利益关系")
    force_past_deadline(serial, rev)
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    statuses = {s["reviewer_id"]: s["review_status"]
                for s in got["papers"]["P1"]["slots"]}
    assert statuses[pair[0]] == "recused"
    assert statuses[pair[1]] == "overdue"
    assert got["summary"]["recused"] == 1
    assert got["summary"]["overdue"] == 1


# ------------------------------------------------------------ 截止后: 确认 / 评语被拒

def test_overdue_pending_slot_cannot_confirm(client):
    serial, rev, pair = setup(client)
    force_past_deadline(serial, rev)
    # 未确认槽位逾期 -> 不能确认
    r = decide(client, pair[0], "P1", "confirm")
    assert r.status_code == 409
    assert "逾期" in r.json()["detail"]["message"]


def test_overdue_confirmed_slot_cannot_submit_review(client):
    serial, rev, pair = setup(client)
    decide(client, pair[0], "P1", "confirm")
    force_past_deadline(serial, rev)
    r = submit_review(client, pair[0], "P1", serial, 4, "迟交的评语")
    assert r.status_code == 409
    assert "逾期" in r.json()["detail"]["message"]


def test_submitted_review_stays_valid_after_deadline_and_retry_returns_receipt(client):
    serial, rev, pair = setup(client)
    decide(client, pair[0], "P1", "confirm")
    first = submit_review(client, pair[0], "P1", serial, 4, "按时评语").json()
    force_past_deadline(serial, rev)
    # 已交评语: 同内容重试仍原样返回原收据 (收据语义保持)
    retry = submit_review(client, pair[0], "P1", serial, 4, "按时评语")
    assert retry.status_code == 200
    assert retry.json()["receipt"] == first["receipt"]
    assert retry.json()["changed"] is False
    # 异内容仍按冲突 409 (而不是逾期 409)
    conflict = submit_review(client, pair[0], "P1", serial, 5, "不同评语")
    assert conflict.status_code == 409
    # 评审人仍能看到已交评语
    mine = client.get("/reviewer/reviews", headers=reviewer_headers(pair[0])).json()
    assert len(mine["reviews"]) == 1 and mine["reviews"][0]["receipt"] == first["receipt"]


def test_before_deadline_confirmation_and_submission_still_work(client):
    serial, rev, pair = setup(client)
    future = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    set_deadline(client, rev, serial, future)
    r = decide(client, pair[0], "P1", "confirm")
    assert r.status_code == 200
    r = submit_review(client, pair[0], "P1", serial, 4, "截止前交")
    assert r.status_code == 200


def test_reviewer_assignment_view_carries_deadline_and_status(client):
    serial, rev, pair = setup(client)
    future = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    set_deadline(client, rev, serial, future)
    decide(client, pair[0], "P1", "confirm")
    view = client.get("/reviewer/assignments", headers=reviewer_headers(pair[0])).json()
    assert view["review_deadline"] is not None
    assert view["review_statuses"]["P1"] == "confirmed_not_submitted"
    assert view["assignments"][0]["review_status"] == "confirmed_not_submitted"
    force_past_deadline(serial, rev)
    view = client.get("/reviewer/assignments", headers=reviewer_headers(pair[1])).json()
    assert view["deadline_passed"] is True
    assert view["review_statuses"]["P1"] == "overdue"


# ------------------------------------------------------------ 补位: 释放逾期槽位且不重分

def test_backfill_releases_overdue_confirmed_slot_and_does_not_reassign(client):
    serial, rev, pair = setup(client)
    r_overdue, r_other = pair[0], pair[1]
    decide(client, r_overdue, "P1", "confirm")   # 已确认但截止后仍未交
    force_past_deadline(serial, rev)

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["deadline_passed"] is True
    released = {(x["paper_id"], x["reviewer_id"]) for x in dry["released_overdue_slots"]}
    assert ("P1", r_overdue) in released
    # 逾期评审人不得重获该稿
    assert r_overdue not in dry["plan"]["P1"], dry["plan"]
    exc = dry["papers"]["P1"]["excluded"]
    codes = {e["reviewer_id"]: e["reason"] for e in exc}
    assert codes[r_overdue] == "fixed_reviewer_review_overdue"
    # 未确认逾期槽位同样释放 (pending)
    assert ("P1", r_other) in released

    pub = backfill_publish(client)
    assert r_overdue not in pub["plan"]["P1"], pub["plan"]
    assert pub["serial"] == serial + 1
    assert pub["released_overdue_slots"] == 2
    # 逾期评审人在新方案中已无该稿 -> 取稿列表不含 P1
    mine = client.get("/reviewer/assignments", headers=reviewer_headers(r_overdue)).json()
    assert "P1" not in [a["paper_id"] for a in mine["assignments"]]


def test_backfill_keeps_other_confirmation_and_carries_review(client):
    serial, rev, pair = setup(client)
    r_overdue, r_good = pair[0], pair[1]
    decide(client, r_good, "P1", "confirm")
    submit_review(client, r_good, "P1", serial, 5, "按时交评语")
    decide(client, r_overdue, "P1", "confirm")  # 该人已确认但逾期未交
    force_past_deadline(serial, rev)

    pub = backfill_publish(client)
    # 良好评审人仍固定在该稿, 评语沿用 (carried_reviews=1)
    assert r_good in pub["plan"]["P1"]
    assert pub["carried_confirmations"] == 1
    assert pub["carried_reviews"] == 1
    # 逾期者不在方案中
    assert r_overdue not in pub["plan"]["P1"]
    # 沿用评语在新序号可查且收据不变
    pr = client.get("/papers/P1/reviews", headers=ORG).json()
    good_slot = next(s for s in pr["slots"] if s["reviewer_id"] == r_good)
    assert good_slot["submitted"] is True
    assert good_slot["review"]["comment"] == "按时交评语"


def test_backfill_without_deadline_keeps_existing_confirmation_rules(client):
    serial, rev, pair = setup(client)
    r_fixed = pair[0]
    decide(client, r_fixed, "P1", "confirm")
    # 未设置截止: 补位沿用既有规则 (已确认槽位固定), 无逾期释放
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["review_deadline"] is None
    assert dry["released_overdue_slots"] == []
    assert r_fixed in dry["plan"]["P1"]


# ------------------------------------------------------------ 旧版截止不约束新发布版

def test_old_serial_deadline_does_not_constrain_new_serial(client):
    serial, rev, pair = setup(client)
    decide(client, pair[0], "P1", "confirm")
    force_past_deadline(serial, rev)
    # 普通发布推进到新序号
    rev2 = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub2 = client.post("/assignment/publish", headers=ORG,
                       json={"base_revision": rev2}).json()
    new_serial = pub2["serial"]
    assert new_serial == serial + 1
    # 新序号未设置截止 -> 无逾期, 可正常确认/交评语
    got = client.get("/assignment/review-deadline", headers=ORG).json()
    assert got["review_deadline"] is None
    assert got["deadline_passed"] is False
    # 旧截止行仍保留在历史中供追溯
    hist_serials = [h["serial"] for h in got["deadline_history"]]
    assert serial in hist_serials and new_serial not in hist_serials
    # 新序号上确认/交评语不受旧截止约束
    new_pair = pub2["plan"]["P1"]
    r = decide(client, new_pair[0], "P1", "confirm")
    assert r.status_code == 200
    r = submit_review(client, new_pair[0], "P1", new_serial, 4, "新版评语")
    assert r.status_code == 200


def test_new_serial_can_set_its_own_deadline(client):
    serial, rev, pair = setup(client)
    force_past_deadline(serial, rev)
    rev2 = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub2 = client.post("/assignment/publish", headers=ORG,
                       json={"base_revision": rev2}).json()
    future = iso(datetime.now(timezone.utc) + timedelta(hours=1))
    r = set_deadline(client, rev2, pub2["serial"], future)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True


# ------------------------------------------------------------ 拒绝不改变方案与历史

def test_rejected_set_does_not_change_plan_or_history(client):
    serial, rev, pair = setup(client)
    # 版本不符拒绝
    r = set_deadline(client, rev, serial + 5, iso(datetime.now(timezone.utc) + timedelta(hours=1)))
    assert r.status_code == 409
    with db.read_txn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM review_deadlines").fetchone()["c"] == 0
        p = conn.execute("SELECT serial FROM published WHERE id=1").fetchone()
        assert p["serial"] == serial
    # 异值拒绝也不新增历史行
    set_deadline(client, rev, serial, iso(datetime.now(timezone.utc) + timedelta(hours=2)))
    set_deadline(client, rev, serial, iso(datetime.now(timezone.utc) + timedelta(hours=3)))
    with db.read_txn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM review_deadlines").fetchone()["c"] == 1
